"""Planning and dispatch: probe, choose, encode, swap.

One worker. The encoder service runs a single encode at a time by default and
refuses at its own limit, so a second dispatcher here would only generate
`queue_full` responses.

The stage machine:

    settling -> pending -> queued -> encoding -> swapping -> done
                   |          |          |
                   |          |          +-> failed / blocked / cancelled
                   +-> skipped
"""

import logging
import os
import queue as queue_mod
import sqlite3
import threading
import time

from app.encoder.client import EncoderRejected, EncoderUnreachable, is_retryable
from app.encoder.events import EventBroadcaster, job_to_payload
from app.encoder.probe import ProbeError, probe
from app.encoder.rules import SKIP, RuleError, evaluate
from app.encoder.store import TERMINAL_STAGES, EncoderStore, Job, StoredPreset
from app.encoder.swap import SwapError, purge_original, swap_in

logger = logging.getLogger(__name__)

_REQUEUE_SECONDS = 30.0

# How long the worker blocks on an empty queue before re-checking `_stopping`.
# Only affects shutdown latency when idle -- a job pushed onto the queue
# wakes `queue.Queue.get()` immediately regardless of this value.
_WORKER_POLL_SECONDS = 0.5

# How long `_await_remote` keeps polling an unreachable encoder before giving
# up and moving the job to `blocked`. There is exactly one worker, so an
# encoder that never comes back would otherwise hold every other job hostage
# forever with no signal the UI could act on.
_UNREACHABLE_BLOCK_SECONDS = 1800.0
_PROBE_RETRY_ATTEMPTS = 3
_PROBE_RETRY_DELAY_SECONDS = 1.0

# Codes/statuses meaning "the remote has no memory of this job at all", the
# only condition under which a reattach may fall back to a fresh submit.
_REMOTE_JOB_GONE_CODES = frozenset({"source_not_found_on_encoder", "job_not_found"})


class _PlanningCancelled(Exception):
    """Internal signal used to stop a bulk plan without recording a failure."""


class EncodeQueue:
    def __init__(
        self,
        store: EncoderStore,
        client,
        broadcaster: EventBroadcaster,
        *,
        mode: str = "review",
        original_ttl: int = 604800,
        holding_dir: str = "/data/encoder/originals",
        poll_interval: float = 2.0,
    ) -> None:
        self._store = store
        self._client = client
        self._events = broadcaster
        self._mode = mode
        self._original_ttl = original_ttl
        self._holding_dir = holding_dir
        self._poll_interval = poll_interval
        self._queue: queue_mod.Queue = queue_mod.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._timers: set[threading.Timer] = set()
        self._timers_lock = threading.Lock()
        self._job_locks: dict[str, threading.RLock] = {}
        self._job_locks_lock = threading.Lock()

    def _job_lock(self, job_id: str) -> threading.RLock:
        """Return the lock that serializes cancellation and file publication."""
        with self._job_locks_lock:
            return self._job_locks.setdefault(job_id, threading.RLock())

    @property
    def events(self) -> EventBroadcaster:
        """The encoder broadcaster shared by queue and bulk reprocess events."""
        return self._events

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        # Checking `is_alive()` too, not just `is not None`, matters after a
        # `stop()` that timed out: the thread handle survives that (it is
        # only nulled once the join confirms the thread has exited), so a
        # bare None-check here would make every start() after a timed-out
        # stop() a permanent no-op even once that old thread has since died
        # on its own.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._worker, name="encoder-dispatch", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()

        # Join the worker before touching `_timers`. While it is still
        # running, `_run` can create a new retry timer at any moment (e.g.
        # `_handle_dispatch_error` mid-flight); draining the set first would
        # race that and could miss a timer created just after the drain.
        # Once the join below returns, the worker cannot schedule anything
        # new, so `_timers` is stable.
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            # Only release the handle once the thread has actually exited.
            # Nulling it unconditionally would let a later start() spawn a
            # second worker while this one is still finishing up, breaking
            # the one-worker guarantee.
            if not self._thread.is_alive():
                self._thread = None

        # Pending retry/reattach timers must not fire after shutdown -- one
        # left running would requeue a job onto a dispatcher no longer
        # reading it (or, in tests, after the store has been closed).
        with self._timers_lock:
            timers = list(self._timers)
            self._timers.clear()
        for timer in timers:
            timer.cancel()
        for timer in timers:
            # cancel() only asks a not-yet-fired timer to stand down; the
            # thread still needs a moment to wake from its wait and exit.
            # Join it so a caller who checks "is anything still running?"
            # right after stop() returns sees a truthful answer.
            timer.join(timeout=1.0)

    def recover(self) -> None:
        """Requeue whatever a restart interrupted.

        ``reset_active_for_recovery`` already flips every resumable job to
        ``queued`` in the store, but the ``Job`` objects it returns still
        carry the stage each job was in *before* that reset -- its SELECT
        runs before its UPDATE, so the row is captured pre-flip. That
        pre-reset stage is what decides how a job must be resumed:

        - ``swapping``: never re-dispatched. ``swap_in`` is not idempotent
          and nothing in the store can say whether the publish had already
          landed when the restart hit, so guessing risks silently
          re-encoding an already-published file and swapping that in over
          the real result. This goes to a human instead. ``blocked`` is
          already excluded from recovery, which is exactly the property
          this needs too.
        - everything else (``queued``, ``encoding``): requeued normally.
          An ``encoding`` job keeps its ``remote_job_id`` in the store (the
          reset only touches `stage`), so ``_run`` will reattach to the
          in-flight remote job instead of submitting a second one.
        """
        for job in self._store.reset_active_for_recovery():
            if job.stage == "swapping":
                self._store.set_stage(
                    job.id,
                    "blocked",
                    error=(
                        "The service restarted while publishing this encode. "
                        "The file's state on disk is unknown -- confirm it "
                        "manually before retrying this job."
                    ),
                    error_code="swap_interrupted",
                )
                self._publish(job.id)
                continue

            logger.info("Resuming interrupted encode job %s", job.id)
            self.enqueue(job.id)

    def enqueue(self, job_id: str) -> None:
        self._store.set_stage(job_id, "queued")
        self._publish(job_id)
        self._queue.put(job_id)

    def enqueue_if_stage(self, job_id: str, allowed_stages: set[str]) -> bool:
        """Atomically enqueue only while an approval-stage claim is still valid."""
        if not self._store.transition_stage(job_id, allowed_stages, "queued"):
            return False
        self._publish(job_id)
        self._queue.put(job_id)
        return True

    def cancel(self, job_id: str) -> bool:
        with self._job_lock(job_id):
            job = self._store.get_job(job_id)
            if job is None or job.stage == "swapping" or job.stage in TERMINAL_STAGES:
                return False
            if job.remote_job_id:
                try:
                    self._client.cancel(job.remote_job_id)
                except (EncoderRejected, EncoderUnreachable):
                    logger.warning("Could not cancel remote job %s", job.remote_job_id)
            self._store.set_stage(job_id, "cancelled")
            self._publish(job_id)
            return True

    # ---- planning --------------------------------------------------------

    def _probe_once(self, path: str) -> dict:
        """Probe a single media file with ffprobe. Forwarding helper used for test mocking."""
        return probe(path)

    def _probe_with_retry(
        self, path: str, cancel_event: threading.Event | None = None
    ) -> dict:
        """Retry short-lived share permission/read races before failing."""
        for attempt in range(_PROBE_RETRY_ATTEMPTS):
            if cancel_event is not None and cancel_event.is_set():
                raise _PlanningCancelled
            try:
                facts = self._probe_once(path)
                if cancel_event is not None and cancel_event.is_set():
                    raise _PlanningCancelled
                return facts
            except ProbeError as exc:
                message = str(exc).lower()
                transient = any(
                    marker in message
                    for marker in (
                        "permission denied",
                        "resource temporarily unavailable",
                        "input/output error",
                        "temporarily",
                    )
                )
                if not transient or attempt == _PROBE_RETRY_ATTEMPTS - 1:
                    raise
                delay = _PROBE_RETRY_DELAY_SECONDS * (attempt + 1)
                logger.warning(
                    "Transient ffprobe failure for %s; retrying in %.1fs (%d/%d): %s",
                    path,
                    delay,
                    attempt + 1,
                    _PROBE_RETRY_ATTEMPTS - 1,
                    exc,
                )
                if cancel_event is not None:
                    if cancel_event.wait(delay):
                        raise _PlanningCancelled from exc
                else:
                    time.sleep(delay)
        raise AssertionError("unreachable")

    def plan_new(
        self,
        source_path: str,
        size: int,
        mtime_ns: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Create a job for *source_path* and run planning synchronously.

        Returns the resulting stage (e.g., 'pending', 'queued', 'skipped',
        'failed'). If the target rule evaluated to 'skip', the job is still
        created with stage='skipped' to record that the file was processed.
        """
        job = self._store.create_job(source_path, size, mtime_ns)
        self._publish(job.id)
        try:
            return self.plan(job.id, cancel_event=cancel_event)
        except Exception:
            logger.exception("Initial plan failed for %s", source_path)
            self._fail(
                job.id, "Internal planning error; see server logs", "plan_failed"
            )
            return "failed"

    def reprocess_path(
        self,
        source_path: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Immediately reconsider a path, bypassing watcher deduplication.

        Precondition: `source_path` must be an absolute path that has already been
        authorized and validated by the caller (e.g. via `_resolve_probe_path` or
        `resolve_authorized_path`).
        """
        if cancel_event is not None and cancel_event.is_set():
            return {
                "job_id": None,
                "path": source_path,
                "stage": "cancelled",
                "created": False,
            }
        active = self._store.active_job_for_source(source_path)
        if active is not None:
            return {
                "job_id": active.id,
                "path": active.source_path,
                "stage": active.stage,
                "created": False,
            }

        try:
            stat = os.stat(source_path)
        except OSError as exc:
            logger.warning(
                "Could not stat path for reprocess: %s (%s)", source_path, exc
            )
            return {
                "job_id": None,
                "path": source_path,
                "stage": "failed",
                "created": False,
            }
        self._store.forget_seen(source_path)
        try:
            stage = self.plan_new(
                source_path,
                stat.st_size,
                stat.st_mtime_ns,
                cancel_event=cancel_event,
            )
        except sqlite3.IntegrityError:
            # Another caller won the unique active-source race between the
            # lookup above and create_job(). Return that job as the idempotent
            # result of this request.
            active = self._store.active_job_for_source(source_path)
            if active is not None:
                return {
                    "job_id": active.id,
                    "path": active.source_path,
                    "stage": active.stage,
                    "created": False,
                }
            logger.warning(
                "IntegrityError on reprocess for %s, but active job already transitioned to terminal",
                source_path,
            )
            return {
                "job_id": None,
                "path": source_path,
                "stage": "failed",
                "created": False,
            }

        job = self._store.newest_job_for_source(source_path)
        if job is None:
            raise RuntimeError("planned job disappeared")
        job_id, path, stage = job.id, job.source_path, job.stage
        return {"job_id": job_id, "path": path, "stage": stage, "created": True}

    def plan(self, job_id: str, *, cancel_event: threading.Event | None = None) -> str:
        """Probe the file, pick a target, and record the decision.

        Returns the resulting stage. In `review` mode this stops at `pending`
        so a human confirms; in `auto` it enqueues. That split is the safety
        property of the feature, which is why `review` is the default.
        """
        job = self._store.get_job(job_id)
        if job is None:
            return "failed"

        try:
            facts = self._probe_with_retry(job.source_path, cancel_event)
        except _PlanningCancelled:
            return self._cancel_planning(job_id)
        except ProbeError as exc:
            self._fail(job_id, str(exc), "probe_failed")
            return "failed"

        rules = self._store.list_rules()
        fallback = self._store.get_setting("fallback_target", SKIP)
        try:
            match = evaluate(facts, rules, fallback)
        except RuleError as exc:
            self._fail(job_id, str(exc), "invalid_rule")
            return "failed"

        if cancel_event is not None and cancel_event.is_set():
            return self._cancel_planning(job_id)

        self._store.set_plan(
            job_id,
            preset_name=None if match.target == SKIP else match.target,
            rule_id=match.rule_id,
            facts=facts,
            original_size=facts.get("size") or 0,
        )

        if cancel_event is not None and cancel_event.is_set():
            return self._cancel_planning(job_id)

        if match.target == SKIP:
            self._store.set_stage(job_id, "skipped")
            self._publish(job_id)
            return "skipped"

        if not any(p.name == match.target for p in self._store.list_presets()):
            # A rule pointing at a deleted preset. Caught here rather than at
            # the encoder, which would report it as a preset_not_found failure
            # after the job had already been dispatched.
            self._fail(
                job_id,
                f"Rule selected preset {match.target!r}, which no longer exists",
                "preset_missing",
            )
            return "failed"

        if self._mode == "auto":
            if cancel_event is not None and cancel_event.is_set():
                return self._cancel_planning(job_id)
            self.enqueue(job_id)
            return "queued"

        self._store.set_stage(job_id, "pending")
        self._publish(job_id)
        return "pending"

    def _cancel_planning(self, job_id: str) -> str:
        self._store.set_stage(job_id, "cancelled")
        self._publish(job_id)
        return "cancelled"

    # ---- retention -------------------------------------------------------

    def run_retentions(self) -> int:
        """Purge originals whose TTL has elapsed. Safe to call repeatedly."""
        purged = 0
        for job in self._store.due_retentions(time.time()):
            kept = job.original_kept_path
            if not kept:
                continue
            if purge_original(kept):
                purged += 1
                self._store.clear_retention(job.id)
            elif not os.path.exists(kept):
                # purge_original() returns False for both "already gone" and
                # "a real error occurred" -- os.path.exists disambiguates.
                # Confirmed-absent is safe to clear too (there is nothing
                # left to retry); anything else (a permission error, a
                # transient I/O failure) must keep the retention record so a
                # later run retries, rather than losing the only pointer to a
                # file that is, in fact, still sitting there.
                self._store.clear_retention(job.id)
        return purged

    # ---- worker ----------------------------------------------------------

    def _worker(self) -> None:
        while not self._stopping.is_set():
            try:
                job_id = self._queue.get(timeout=_WORKER_POLL_SECONDS)
            except queue_mod.Empty:
                continue
            try:
                self._run(job_id)
            except Exception:
                logger.exception("Encode job %s crashed the dispatcher", job_id)
                self._fail(job_id, "Internal error; see server logs", "internal")

    def _lookup_preset(
        self, job_id: str, preset_name: str | None
    ) -> StoredPreset | None:
        if preset_name is None:
            self._fail(job_id, "No preset selected for job", "preset_missing")
            return None
        preset = next(
            (p for p in self._store.list_presets() if p.name == preset_name), None
        )
        if preset is None:
            self._fail(
                job_id, f"Preset {preset_name!r} no longer exists", "preset_missing"
            )
            return None
        return preset

    def _reevaluate_before_dispatch(
        self, job_id: str, job: Job, stat: os.stat_result
    ) -> StoredPreset | None:
        """Re-evaluate an active job if its source was modified on disk while queued."""
        seen_fp = self._store.get_seen(job.source_path)
        if seen_fp is None or (stat.st_size, stat.st_mtime_ns) == seen_fp:
            return self._lookup_preset(job_id, job.preset_name)

        logger.warning(
            "Source file %s modified on disk while queued; re-evaluating before encode",
            job.source_path,
        )
        try:
            facts = self._probe_with_retry(job.source_path)
        except (ProbeError, OSError) as exc:
            self._fail(job_id, str(exc), "probe_failed")
            return None

        rules = self._store.list_rules()
        fallback = self._store.get_setting("fallback_target", SKIP)
        try:
            match = evaluate(facts, rules, fallback)
        except RuleError as exc:
            self._fail(job_id, str(exc), "invalid_rule")
            return None

        if match.target == SKIP:
            logger.info(
                "Modified source file %s now matches SKIP; skipping job %s",
                job.source_path,
                job_id,
            )
            with self._job_lock(job_id):
                current = self._store.get_job(job_id)
                if current is None or current.stage == "cancelled":
                    return None
                self._store.set_plan(
                    job_id,
                    preset_name=None,
                    rule_id=match.rule_id,
                    facts=facts,
                    original_size=facts.get("size") or 0,
                )
                self._store.mark_seen(job.source_path, stat.st_size, stat.st_mtime_ns)
                self._store.set_stage(job_id, "skipped")
                self._publish(job_id)
            return None

        target_preset = next(
            (p for p in self._store.list_presets() if p.name == match.target),
            None,
        )
        if target_preset is None:
            self._fail(
                job_id,
                f"Rule selected preset {match.target!r}, which no longer exists",
                "preset_missing",
            )
            return None

        with self._job_lock(job_id):
            current = self._store.get_job(job_id)
            if current is None or current.stage == "cancelled":
                return None
            self._store.set_plan(
                job_id,
                preset_name=match.target,
                rule_id=match.rule_id,
                facts=facts,
                original_size=facts.get("size") or 0,
            )
            self._store.mark_seen(job.source_path, stat.st_size, stat.st_mtime_ns)
            self._publish(job_id)
        return target_preset

    def _run(self, job_id: str) -> None:
        job = self._store.get_job(job_id)
        if job is None or job.stage == "cancelled":
            return

        if job.remote_job_id:
            # A job that already has a remote id got here via recover()
            # resuming an interrupted `encoding` job. Reattach rather than
            # resubmit -- submitting again would start a second remote
            # encode of the same source while the first may still be
            # running.
            preset = self._lookup_preset(job_id, job.preset_name)
            if preset is None:
                return
            remote_id = self._reattach(job_id, job, preset)
            if remote_id is None:
                return
        else:
            try:
                stat = os.stat(job.source_path)
            except OSError as exc:
                self._fail(
                    job_id,
                    f"Source file no longer exists on disk: {exc}",
                    "source_missing",
                )
                return

            preset = self._reevaluate_before_dispatch(job_id, job, stat)
            if preset is None:
                return

            with self._job_lock(job_id):
                current = self._store.get_job(job_id)
                if current is None or current.stage == "cancelled":
                    return

                try:
                    remote_id = self._client.submit(
                        job.source_path, preset.body, preset.name
                    )
                except (EncoderRejected, EncoderUnreachable) as exc:
                    self._handle_dispatch_error(job_id, exc)
                    return
                self._store.set_remote_job(job_id, remote_id)
                self._store.set_stage(job_id, "encoding")
                self._publish(job_id)

        result = self._await_remote(job_id, remote_id)
        if result is None:
            return

        # A cancel can land in the window between the remote reporting
        # `completed` and the swap actually running. `_await_remote` only
        # checks the stage before each poll, so check once more here,
        # immediately before anything touches the file.
        current = self._store.get_job(job_id)
        if current is None or current.stage == "cancelled":
            return

        self._publish_result(job_id, current.source_path, result)

    def _reattach(self, job_id: str, job: Job, preset) -> str | None:
        """Rejoin a job that already has a remote id instead of resubmitting.

        Polls the existing remote id first. Only falls back to a fresh
        submit if the remote reports it has no memory of that job at all
        (a 404, or a ``source_not_found_on_encoder``-class rejection) --
        anything else means the original encode might still be running or
        might already be done, and either way a second submit would be
        wrong.
        """
        remote_id = job.remote_job_id
        assert remote_id is not None
        try:
            self._client.poll(remote_id)
        except EncoderUnreachable:
            # Can't tell yet. Stay queued and try again shortly rather than
            # guess at a resubmit.
            self._schedule_requeue(job_id, self._poll_interval)
            return None
        except EncoderRejected as exc:
            gone = exc.status == 404 or exc.code in _REMOTE_JOB_GONE_CODES
            if not gone:
                self._fail(job_id, exc.reason, exc.code)
                return None
            with self._job_lock(job_id):
                current = self._store.get_job(job_id)
                if current is None or current.stage == "cancelled":
                    return None
                try:
                    remote_id = self._client.submit(
                        job.source_path, preset.body, preset.name
                    )
                except (EncoderRejected, EncoderUnreachable) as submit_exc:
                    self._handle_dispatch_error(job_id, submit_exc)
                    return None
                self._store.set_remote_job(job_id, remote_id)
                self._store.set_stage(job_id, "encoding")
                self._publish(job_id)
                return remote_id

        with self._job_lock(job_id):
            current = self._store.get_job(job_id)
            if current is None or current.stage == "cancelled":
                return None
            self._store.set_remote_job(job_id, remote_id)
            self._store.set_stage(job_id, "encoding")
            self._publish(job_id)
            return remote_id

    def _await_remote(self, job_id: str, remote_id: str) -> dict | None:
        """Poll until the remote job is terminal. Returns its final body."""
        unreachable_since: float | None = None
        while not self._stopping.is_set():
            job = self._store.get_job(job_id)
            if job is None or job.stage == "cancelled":
                return None
            try:
                body = self._client.poll(remote_id)
            except EncoderUnreachable:
                with self._job_lock(job_id):
                    current = self._store.get_job(job_id)
                    if current is not None and current.stage == "encoding":
                        self._store.set_progress(job_id, current.progress)
                        self._publish(job_id)
                # Transient: the encode is still running on the other side --
                # but only up to a point. There is exactly one worker, so an
                # encoder container that never comes back would otherwise
                # poll forever and hold every other job hostage with no
                # signal the UI could act on. Once unreachable for too long,
                # give up and hand the job to a human instead.
                now = time.monotonic()
                if unreachable_since is None:
                    unreachable_since = now
                elif now - unreachable_since >= _UNREACHABLE_BLOCK_SECONDS:
                    with self._job_lock(job_id):
                        current = self._store.get_job(job_id)
                        if current is None or current.stage == "cancelled":
                            return None
                        self._store.set_stage(
                            job_id,
                            "blocked",
                            error=(
                                "The encoder has been unreachable for over "
                                f"{int(_UNREACHABLE_BLOCK_SECONDS // 60)} minutes. "
                                "Check the encoder service, then approve this "
                                "job to resume polling it."
                            ),
                            error_code="encoder_unreachable",
                        )
                        self._publish(job_id)
                    return None
                time.sleep(self._poll_interval)
                continue
            except EncoderRejected as exc:
                self._fail(job_id, exc.reason, exc.code)
                return None

            unreachable_since = None
            progress = body.get("progress")
            if isinstance(progress, (int, float)):
                with self._job_lock(job_id):
                    current = self._store.get_job(job_id)
                    if current is not None and current.stage != "cancelled":
                        self._store.set_progress(
                            job_id,
                            float(progress),
                            eta_seconds=(
                                body.get("eta_seconds")
                                if body.get("status") == "running"
                                else None
                            ),
                        )
                        self._publish(job_id)

            status = body.get("status")
            if status == "completed":
                return body
            if status in {"failed", "cancelled"}:
                self._fail(
                    job_id, body.get("error") or f"Encode {status}", "encode_failed"
                )
                return None
            time.sleep(self._poll_interval)
        return None

    def _publish_result(self, job_id: str, source_path: str, body: dict) -> None:
        with self._job_lock(job_id):
            current = self._store.get_job(job_id)
            if current is None or current.stage == "cancelled":
                return
            output_path = body.get("output_path")
            if not output_path:
                self._fail(
                    job_id,
                    "Encoder reported success without an output path",
                    "no_output",
                )
                return

            self._store.set_stage(job_id, "swapping")
            self._publish(job_id)
            try:
                result = swap_in(
                    source_path,
                    output_path,
                    original_ttl=self._original_ttl,
                    holding_dir=self._holding_dir,
                )
            except SwapError as exc:
                # The source is intact by construction *unless* the original
                # had already been moved into holding and the restore-back
                # also failed. In that case exc.kept_path names where it
                # survives, so do not claim the source was untouched.
                if exc.kept_path is None:
                    self._fail(
                        job_id,
                        f"{exc} (the original was left untouched)",
                        "swap_failed",
                    )
                else:
                    self._fail(
                        job_id,
                        f"{exc} The original was preserved at {exc.kept_path}.",
                        "swap_failed",
                    )
                return

            self._store.set_result(job_id, result.final_path, result.encoded_size)
            # Re-fingerprint what we just published so the watcher does not
            # treat our own output as a new arrival on the next scan.
            try:
                published = os.stat(result.final_path)
                self._store.mark_seen(
                    result.final_path, published.st_size, published.st_mtime_ns
                )
            except OSError:
                logger.warning(
                    "Could not fingerprint the published file %s",
                    result.final_path,
                    exc_info=True,
                )
            if result.kept_path:
                self._store.set_retention(
                    job_id, result.kept_path, time.time() + self._original_ttl
                )
            self._store.set_progress(job_id, 100.0)
            self._store.set_stage(job_id, "done")
            self._publish(job_id)

    def _handle_dispatch_error(self, job_id: str, exc: Exception) -> None:
        with self._job_lock(job_id):
            current = self._store.get_job(job_id)
            if current is None or current.stage == "cancelled":
                return

            if isinstance(exc, EncoderRejected) and exc.code == "encoder_unavailable":
                # Never a silent CPU fallback: the user chooses. Blocked jobs are
                # excluded from restart recovery for the same reason.
                self._store.set_stage(
                    job_id, "blocked", error=exc.reason, error_code=exc.code
                )
                self._publish(job_id)
                return

            if is_retryable(exc):
                # `None` means the response carried no Retry-After and we fall
                # back to our own default; `0` is a legitimate value from the
                # server (retry immediately) and must not be treated as absent.
                # Floored to 1s regardless: a server-supplied `retry_after=0`
                # must not produce an uncapped hot loop of immediate resubmits.
                retry_after = getattr(exc, "retry_after", None)
                delay = retry_after if retry_after is not None else _REQUEUE_SECONDS
                delay = max(float(delay), 1.0)
                logger.info(
                    "Encoder busy or unreachable; requeueing %s in %ss", job_id, delay
                )
                self._store.set_stage(job_id, "queued")
                self._publish(job_id)
                self._schedule_requeue(job_id, delay)
                return

            code = getattr(exc, "code", "dispatch_failed")
            self._fail(job_id, str(exc), code)

    def _schedule_requeue(self, job_id: str, delay: float) -> None:
        """Put *job_id* back on the dispatch queue after *delay* seconds.

        The timer is tracked on the instance so :meth:`stop` can cancel it.
        Without that, a shutdown mid-retry leaves a live timer that outlives
        the queue -- in tests it fires after the store has been closed; in
        production it requeues onto a dispatcher no longer reading anything.
        """
        holder: list[threading.Timer] = []

        def _fire() -> None:
            with self._timers_lock:
                if holder:
                    self._timers.discard(holder[0])
            self._queue.put(job_id)

        timer = threading.Timer(max(delay, 0.0), _fire)
        timer.daemon = True
        timer.name = "encoder-requeue"
        holder.append(timer)
        with self._timers_lock:
            self._timers.add(timer)
        timer.start()

    def _fail(self, job_id: str, error: str, code: str) -> None:
        with self._job_lock(job_id):
            current = self._store.get_job(job_id)
            if current is None or current.stage == "cancelled":
                return
            self._store.set_stage(job_id, "failed", error=error, error_code=code)
            self._publish(job_id)

    def _publish(self, job_id: str) -> None:
        job = self._store.get_job(job_id)
        if job is not None:
            self._events.publish(job_to_payload(job))
