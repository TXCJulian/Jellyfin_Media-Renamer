import threading
import time

import pytest

from app.encoder.client import EncoderRejected, EncoderUnreachable
from app.encoder.events import EventBroadcaster
from app.encoder.presets import NamedPreset
from app.encoder.queue import EncodeQueue
from app.encoder.rules import Condition, Rule
from app.encoder.store import EncoderStore


class FakeClient:
    """Stands in for the encoder service. Deterministic, no HTTP."""

    def __init__(self):
        self.submitted = []
        self.polls = []
        self.cancels = []
        self.submit_error = None
        self.submit_attempts = 0
        self.terminal = {"status": "completed", "progress": 100.0,
                         "output_path": None, "encoder_used": "x264"}

    def submit(self, source_path, preset_body, preset_name):
        self.submit_attempts += 1
        if self.submit_error:
            raise self.submit_error
        self.submitted.append((source_path, preset_name))
        return "remote-1"

    def poll(self, remote_job_id):
        self.polls.append(remote_job_id)
        return self.terminal

    def cancel(self, remote_job_id):
        self.cancels.append(remote_job_id)
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = EncoderStore(str(tmp_path / "e.db"))
    store.replace_presets([
        NamedPreset("NVENC", "nvenc_h265", "medium", "av_mkv",
                    {"PresetName": "NVENC", "VideoEncoder": "nvenc_h265"})
    ])
    store.replace_rules([Rule("r1", [Condition("height", ">=", 720)], "NVENC")])

    movies = tmp_path / "Movies"
    movies.mkdir()
    source = movies / "Film.mkv"
    source.write_bytes(b"O" * 4096)

    client = FakeClient()

    import app.encoder.queue as queue_mod
    monkeypatch.setattr(queue_mod, "probe",
                        lambda _p: {"height": 1080, "size": 4096, "video_codec": "h264"})

    yield store, client, movies, source, queue_mod
    store.close()


def _queue(store, client, mode="auto", ttl=0, holding="/tmp/hold"):
    return EncodeQueue(store, client, EventBroadcaster(), mode=mode,
                       original_ttl=ttl, holding_dir=holding, poll_interval=0.01)


def test_poll_publishes_eta_and_clears_it_on_disconnect_and_old_server(env):
    store, client, movies, source, _ = env
    q = _queue(store, client)
    job = store.create_job(str(source))
    store.set_stage(job.id, "encoding")
    received = q.events.subscribe()
    responses = iter([
        {"status": "running", "progress": 25, "eta_seconds": 120},
        EncoderUnreachable("offline"),
        {"status": "running", "progress": 30, "eta_seconds": 90},
        {"status": "running", "progress": 35},
        {"status": "completed", "progress": 100},
    ])

    def poll(_id):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    client.poll = poll
    q._await_remote(job.id, "remote-1")
    payloads = [received.get_nowait() for _ in range(received.qsize())]
    assert [payload["eta_seconds"] for payload in payloads] == [120, None, 90, None, None]
    assert [payload["progress"] for payload in payloads] == [25, 25, 30, 35, 100]


def test_plan_records_the_matched_rule_and_preset(env):
    store, client, movies, source, _ = env
    q = _queue(store, client, mode="review")
    job = store.create_job(str(source))
    q.plan(job.id)
    fetched = store.get_job(job.id)
    assert fetched.preset_name == "NVENC"
    assert fetched.rule_id == "r1"
    assert fetched.facts["height"] == 1080


def test_review_mode_stops_at_pending(env):
    """The safety property: review must not dispatch without a human."""
    store, client, movies, source, _ = env
    q = _queue(store, client, mode="review")
    job = store.create_job(str(source))
    assert q.plan(job.id) == "pending"
    assert store.get_job(job.id).stage == "pending"
    assert client.submitted == []


def test_auto_mode_queues_immediately(env):
    store, client, movies, source, _ = env
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    assert q.plan(job.id) == "queued"
    assert store.get_job(job.id).stage == "queued"


def test_reprocess_path_forgets_seen_and_replans_existing_file(env):
    store, client, movies, source, _ = env
    q = _queue(store, client, mode="review")
    store.mark_seen(str(source), source.stat().st_size, source.stat().st_mtime_ns)
    result = q.reprocess_path(str(source))
    assert result["created"] is True
    assert result["stage"] == "pending"


def test_reprocess_path_returns_existing_active_job(env):
    store, client, movies, source, _ = env
    q = _queue(store, client, mode="review")
    q.plan_new(str(source), source.stat().st_size, source.stat().st_mtime_ns)
    result = q.reprocess_path(str(source))
    assert result["created"] is False
    assert result["job_id"] == store.active_job_for_source(str(source)).id


def test_reprocess_path_returns_new_terminal_job_after_same_second_history(env):
    store, client, movies, source, _ = env
    old = store.create_job(str(source))
    store.set_stage(old.id, "done")
    store.replace_rules([Rule("skip", [], "skip")])
    q = _queue(store, client, mode="review")
    result = q.reprocess_path(str(source))
    newest = store.newest_job_for_source(str(source))
    assert result == {
        "job_id": newest.id,
        "path": str(source),
        "stage": "skipped",
        "created": True,
    }
    assert newest.id != old.id


def test_a_skip_target_ends_the_job_without_dispatching(env):
    store, client, movies, source, _ = env
    store.replace_rules([Rule("r1", [Condition("height", ">=", 720)], "skip")])
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    assert q.plan(job.id) == "skipped"
    assert client.submitted == []


def test_an_unknown_preset_target_fails_the_job_rather_than_dispatching(env):
    """A rule pointing at a deleted preset must not reach the encoder."""
    store, client, movies, source, _ = env
    store.replace_rules([Rule("r1", [], "Ghost")])
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    assert q.plan(job.id) == "failed"
    assert "Ghost" in store.get_job(job.id).error


def test_a_full_run_encodes_swaps_and_reports_sizes(env):
    store, client, movies, source, _ = env
    encoded = movies / ".hbenc-remote-1.mkv"
    encoded.write_bytes(b"E" * 1024)
    client.terminal = {"status": "completed", "progress": 100.0,
                       "output_path": str(encoded), "encoder_used": "nvenc_h265"}
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "done")
    finally:
        q.stop()
    fetched = store.get_job(job.id)
    assert fetched.original_size == 4096
    assert fetched.encoded_size == 1024
    assert source.read_bytes() == b"E" * 1024


def test_a_failed_remote_encode_leaves_the_source_intact(env):
    store, client, movies, source, _ = env
    client.terminal = {"status": "failed", "error": "HandBrake exploded"}
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "failed")
    finally:
        q.stop()
    assert source.read_bytes() == b"O" * 4096
    assert "HandBrake exploded" in store.get_job(job.id).error


def test_encoder_unavailable_blocks_rather_than_fails(env):
    """The spec is explicit: never a silent CPU fallback. The user chooses."""
    store, client, movies, source, _ = env
    client.submit_error = EncoderRejected("encoder_unavailable", "no nvenc", 409)
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "blocked")
    finally:
        q.stop()
    assert store.get_job(job.id).error_code == "encoder_unavailable"


def test_a_transient_rejection_requeues_and_retries(env):
    """A retry_after=0 rejection must fire a real second submit, not just
    leave the stage looking plausible -- `plan()` already sets `queued`
    before the worker ever runs, so asserting the stage alone can't tell a
    working retry from a dispatcher that never dispatched."""
    store, client, movies, source, _ = env
    client.submit_error = EncoderRejected("queue_full", "busy", 503, retry_after=0)
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: client.submit_attempts >= 2)
    finally:
        q.stop()
    assert store.get_job(job.id).stage != "failed"
    assert not _timer_alive("encoder-requeue")


def test_an_unreachable_encoder_retries_until_reachable(env, monkeypatch):
    """Jobs queue in the renamer rather than being lost, as the downloader
    already does -- and must actually retry the submit, not merely sit at
    `queued` because nothing ran yet."""
    store, client, movies, source, queue_mod = env
    # EncoderUnreachable carries no retry_after, so the dispatcher falls
    # back to its own default. Shrink that default so the retry is
    # observable within the test's deadline instead of waiting out the real
    # 30s production value.
    monkeypatch.setattr(queue_mod, "_REQUEUE_SECONDS", 0.01)
    client.submit_error = EncoderUnreachable("refused")
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: client.submit_attempts >= 2)
    finally:
        q.stop()
    assert store.get_job(job.id).stage != "failed"
    assert not _timer_alive("encoder-requeue")


def test_stop_cancels_a_pending_retry_timer(env):
    """A shutdown mid-retry must not leave a live timer that requeues onto
    a dispatcher no longer reading anything (and, in tests, fires after the
    store has closed)."""
    store, client, movies, source, _ = env
    client.submit_error = EncoderRejected("queue_full", "busy", 503, retry_after=5)
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: client.submit_attempts >= 1)
    finally:
        q.stop()
    assert not _timer_alive("encoder-requeue")


def test_a_non_retryable_rejection_fails_the_job(env):
    store, client, movies, source, _ = env
    client.submit_error = EncoderRejected(
        "invalid_video_preset", "bad speed preset", 400,
        detail={"valid_presets": ["speed", "balanced"]})
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "failed")
    finally:
        q.stop()
    assert store.get_job(job.id).error_code == "invalid_video_preset"


def test_recover_requeues_interrupted_jobs(env):
    store, client, movies, source, _ = env
    job = store.create_job(str(source))
    store.set_stage(job.id, "encoding")
    q = _queue(store, client, mode="auto")
    q.recover()
    assert store.get_job(job.id).stage == "queued"


def test_recover_reattaches_an_interrupted_encode_instead_of_resubmitting(env):
    """A restart must not start a second remote encode of the same source.
    An `encoding` job keeps its remote_job_id across recover(); the worker
    must poll that id rather than calling submit() again."""
    store, client, movies, source, _ = env
    job = store.create_job(str(source))
    store.set_plan(job.id, preset_name="NVENC", rule_id="r1", facts={},
                   original_size=4096)
    store.set_remote_job(job.id, "remote-old")
    store.set_stage(job.id, "encoding")

    encoded = movies / ".hbenc-remote-old.mkv"
    encoded.write_bytes(b"E" * 10)
    client.terminal = {"status": "completed", "progress": 100.0,
                       "output_path": str(encoded), "encoder_used": "x264"}

    q = _queue(store, client, mode="auto")
    q.recover()
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "done")
    finally:
        q.stop()
    assert client.submitted == []
    assert all(remote_id == "remote-old" for remote_id in client.polls)


def test_recover_blocks_an_interrupted_swap_rather_than_resubmitting(env):
    """swap_in is not idempotent and nothing in the store says whether the
    publish had already landed -- recovery must never guess here. It goes to
    `blocked` for a human instead."""
    store, client, movies, source, _ = env
    job = store.create_job(str(source))
    store.set_remote_job(job.id, "remote-old")
    store.set_stage(job.id, "swapping")

    q = _queue(store, client, mode="auto")
    q.recover()

    fetched = store.get_job(job.id)
    assert fetched.stage == "blocked"
    assert fetched.error_code == "swap_interrupted"
    assert client.submitted == []
    assert client.polls == []


def test_a_cancel_racing_a_completing_encode_does_not_touch_the_source(env):
    """`_await_remote` only checks for a cancel before each poll. Once a
    poll has already returned `completed`, nothing re-checked the stage
    before this fix -- a cancel landing in that exact window still swapped
    the file in. Simulate the race deterministically: cancel from inside
    the fake client's poll(), synchronously, right as it hands back the
    terminal body."""
    store, client, movies, source, _ = env
    encoded = movies / ".hbenc-remote-1.mkv"
    encoded.write_bytes(b"E" * 1024)
    client.terminal = {"status": "completed", "progress": 100.0,
                       "output_path": str(encoded), "encoder_used": "x264"}

    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)

    original_poll = client.poll

    def racing_poll(remote_job_id):
        q.cancel(job.id)
        return original_poll(remote_job_id)

    client.poll = racing_poll

    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "cancelled")
    finally:
        q.stop()

    fetched = store.get_job(job.id)
    assert fetched.output_path is None
    assert source.read_bytes() == b"O" * 4096


def test_a_cancel_racing_dispatch_does_not_rearm_job(env):
    """If cancel() runs while a job is being reevaluated before dispatch,
    _run() must not submit it to the remote encoder or overwrite 'cancelled'
    with 'encoding'."""
    store, client, movies, source, _ = env
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)

    original_reevaluate = q._reevaluate_before_dispatch

    def racing_reevaluate(*args, **kwargs):
        res = original_reevaluate(*args, **kwargs)
        q.cancel(job.id)
        return res

    q._reevaluate_before_dispatch = racing_reevaluate

    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "cancelled")
    finally:
        q.stop()

    fetched = store.get_job(job.id)
    assert fetched.stage == "cancelled"
    assert client.submitted == []



def test_stop_then_start_dispatches_normally(env):
    """A stop()/start() cycle must not poison the dispatcher: a stale `None`
    sentinel left in the queue (the old shutdown mechanism) would make the
    next worker exit immediately on its first get(), silently accepting
    enqueues forever after without ever dispatching them."""
    store, client, movies, source, _ = env
    q = _queue(store, client, mode="auto")
    q.start()
    q.stop()

    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: client.submitted != [])
    finally:
        q.stop()
    assert client.submitted == [(str(source), "NVENC")]
    assert not any(
        t.name == "encoder-dispatch" and t.is_alive() for t in threading.enumerate()
    )


def test_run_retentions_purges_elapsed_originals(env, tmp_path):
    store, client, movies, source, _ = env
    kept = tmp_path / "hold" / "old.mkv"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")
    job = store.create_job("/media3/other.mkv")
    store.set_retention(job.id, str(kept), expires_at=time.time() - 1)
    q = _queue(store, client)
    q.run_retentions()
    assert not kept.exists()
    assert store.get_job(job.id).original_kept_path is None


def test_run_retentions_keeps_the_record_when_the_purge_fails_for_a_real_reason(env, tmp_path, monkeypatch):
    """`purge_original` returns False for both "already gone" and "a real
    error occurred" -- clearing the retention record on both would lose the
    only pointer to a file that, in the real-error case, is still sitting
    there. Only a confirmed-absent file (checked separately) is safe to
    clear alongside a False return."""
    store, client, movies, source, queue_mod = env
    kept = tmp_path / "hold" / "still-here.mkv"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")
    job = store.create_job("/media3/other.mkv")
    store.set_retention(job.id, str(kept), expires_at=time.time() - 1)

    monkeypatch.setattr(queue_mod, "purge_original", lambda _p: False)
    q = _queue(store, client)
    purged = q.run_retentions()

    assert purged == 0
    assert kept.exists()
    assert store.get_job(job.id).original_kept_path == str(kept)


def test_run_retentions_clears_the_record_for_a_confirmed_absent_file(env, tmp_path, monkeypatch):
    """A file that's already gone (a race with a manual purge, e.g.) must
    still have its retention record cleared even though `purge_original`
    reports False for it -- there is nothing left to retry."""
    store, client, movies, source, queue_mod = env
    kept = tmp_path / "hold" / "already-gone.mkv"
    job = store.create_job("/media3/other.mkv")
    store.set_retention(job.id, str(kept), expires_at=time.time() - 1)

    monkeypatch.setattr(queue_mod, "purge_original", lambda _p: False)
    q = _queue(store, client)
    q.run_retentions()

    assert store.get_job(job.id).original_kept_path is None


def test_swap_failure_with_original_untouched_says_so(env):
    """SwapError with no kept_path: the original genuinely never moved."""
    store, client, movies, source, _ = env
    client.terminal = {"status": "completed", "progress": 100.0,
                       "output_path": str(movies / "missing.mkv"),
                       "encoder_used": "nvenc_h265"}
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "failed")
    finally:
        q.stop()
    fetched = store.get_job(job.id)
    assert "the original was left untouched" in fetched.error
    assert source.read_bytes() == b"O" * 4096


def test_swap_failure_with_kept_path_names_it(env, monkeypatch):
    """SwapError with kept_path set: the original moved and must be named."""
    store, client, movies, source, queue_mod = env
    kept = str(movies / "kept-original.mkv")

    from app.encoder.swap import SwapError

    def fake_swap_in(*args, **kwargs):
        raise SwapError(
            f"Could not publish encoded to final: boom. The original could "
            f"not be restored from holding; it survives at {kept}.",
            kept_path=kept,
        )

    monkeypatch.setattr(queue_mod, "swap_in", fake_swap_in)

    client.terminal = {"status": "completed", "progress": 100.0,
                       "output_path": str(movies / "out.mkv"),
                       "encoder_used": "nvenc_h265"}
    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "failed")
    finally:
        q.stop()
    fetched = store.get_job(job.id)
    assert "the original was left untouched" not in fetched.error
    assert kept in fetched.error
    assert "preserved" in fetched.error.lower()


def test_an_unreachable_encoder_eventually_blocks_instead_of_polling_forever(env, monkeypatch):
    """There is exactly one worker; an encoder that never comes back must not
    hold every other job hostage forever with no signal the UI could act on.
    Shrinks the block threshold so this is observable in-test rather than
    waiting out the real 30-minute default."""
    store, client, movies, source, queue_mod = env
    monkeypatch.setattr(queue_mod, "_UNREACHABLE_BLOCK_SECONDS", 0.05)

    original_poll = client.poll
    poll_calls = {"n": 0}

    def always_unreachable(remote_job_id):
        poll_calls["n"] += 1
        raise EncoderUnreachable("refused")

    client.poll = always_unreachable

    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "blocked", timeout=5.0)
    finally:
        q.stop()

    fetched = store.get_job(job.id)
    assert fetched.error_code == "encoder_unreachable"
    assert poll_calls["n"] >= 2  # actually retried, not blocked on the first poll


def test_a_successful_poll_resets_the_unreachable_counter(env, monkeypatch):
    """A brief outage followed by recovery must not count toward the block
    threshold -- only *consecutive* unreachable polls should."""
    store, client, movies, source, queue_mod = env
    monkeypatch.setattr(queue_mod, "_UNREACHABLE_BLOCK_SECONDS", 0.05)

    calls = {"n": 0}

    def flaky_then_fine(remote_job_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise EncoderUnreachable("refused")
        return {"status": "completed", "progress": 100.0,
               "output_path": str(movies / ".hbenc-remote-1.mkv"),
               "encoder_used": "x264"}

    (movies / ".hbenc-remote-1.mkv").write_bytes(b"E" * 10)
    client.poll = flaky_then_fine

    q = _queue(store, client, mode="auto")
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: store.get_job(job.id).stage == "done", timeout=5.0)
    finally:
        q.stop()

    assert store.get_job(job.id).stage == "done"


def test_start_after_a_stale_thread_handle_restarts_the_worker(env):
    """After a `stop()` that times out, the thread handle survives (it is
    only nulled once the join confirms the thread has exited) -- a bare
    `is not None` check in `start()` would then make every later start() a
    permanent no-op, even once that old thread has since died on its own."""
    store, client, movies, source, _ = env
    q = _queue(store, client, mode="auto")
    q.start()
    q.stop()
    assert q._thread is None  # a clean stop() already nulls it

    # Simulate the timed-out-stop scenario directly: a thread object that has
    # since finished, but is still referenced by `_thread`.
    stale = threading.Thread(target=lambda: None)
    stale.start()
    stale.join()
    q._thread = stale

    q.start()
    try:
        assert q._thread is not None
        assert q._thread is not stale
        assert q._thread.is_alive()
    finally:
        q.stop()


def test_a_zero_retry_after_is_floored_rather_than_hot_looping(env, monkeypatch):
    """A server-supplied `retry_after=0` must not schedule an immediate
    (effectively zero-delay) requeue -- floored to at least 1 second."""
    store, client, movies, source, queue_mod = env
    client.submit_error = EncoderRejected("queue_full", "busy", 503, retry_after=0)

    scheduled = []
    q = _queue(store, client, mode="auto")
    original_schedule = q._schedule_requeue

    def _spy(job_id, delay):
        scheduled.append(delay)
        original_schedule(job_id, delay)

    monkeypatch.setattr(q, "_schedule_requeue", _spy)
    job = store.create_job(str(source))
    q.plan(job.id)
    q.start()
    try:
        _wait(lambda: scheduled != [])
    finally:
        q.stop()

    assert scheduled[0] >= 1.0


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def _timer_alive(name):
    return any(
        isinstance(t, threading.Timer) and t.name == name and t.is_alive()
        for t in threading.enumerate()
    )


def test_plan_new_fails_the_job_when_planning_throws_unexpectedly(env, monkeypatch):
    """An unexpected planning error must not leave the row in `settling`.

    `settling` is neither terminal nor resumable: it holds the source path
    against the unique active index forever, restart recovery skips it, and
    the watcher will not reconsider a file whose fingerprint was written with
    that row. The job silently drops out of the system while looking like work
    in progress.
    """
    store, client, movies, source, queue_mod = env
    q = _queue(store, client)

    def _boom(_job_id):
        raise RuntimeError("something unanticipated")

    monkeypatch.setattr(q, "plan", _boom)
    assert q.plan_new(str(source), 4096, 1_700_000_000_000_000_000) == "failed"

    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].stage == "failed"
    assert jobs[0].error_code == "plan_failed"
    # The message reaches the API, so an unexpected exception must not
    # carry its internal detail into it.
    assert jobs[0].error == "Internal planning error; see server logs"
    assert "unanticipated" not in (jobs[0].error or "")
    # The fingerprint is still recorded, so the file is not re-probed forever.
    assert str(source) in store.seen_fingerprints()


def test_plan_new_records_the_fingerprint_with_the_job(env):
    """Both writes land together, so a file is marked decided exactly when a
    job exists to represent that decision."""
    store, client, movies, source, _ = env
    q = _queue(store, client)

    stage = q.plan_new(str(source), 4096, 1_700_000_000_000_000_000)
    assert stage in {"pending", "queued", "skipped"}
    assert len(store.list_jobs()) == 1
    assert store.seen_fingerprints()[str(source)] == (
        4096, 1_700_000_000_000_000_000)


def test_run_skips_when_queued_file_modified_to_match_skip(env, monkeypatch):
    """If a file changes while queued and now matches SKIP, _run skips it."""
    store, client, movies, source, queue_mod = env
    q = _queue(store, client)

    st = source.stat()
    job = store.create_job(str(source), st.st_size, st.st_mtime_ns)
    store.set_plan(job.id, preset_name="NVENC", rule_id="r1", facts={"height": 1080}, original_size=st.st_size)
    store.set_stage(job.id, "queued")

    # File on disk is modified externally
    source.write_bytes(b"already encoded data")
    monkeypatch.setattr(q, "_probe_once", lambda _p: {"video_codec": "hevc", "size": 100})

    q._run(job.id)

    updated = store.get_job(job.id)
    assert updated.stage == "skipped"
    assert len(client.submitted) == 0


def test_run_fails_with_invalid_rule_when_modified_file_hits_malformed_rule(env, monkeypatch):
    """If evaluate raises RuleError when re-evaluating a modified file, tag invalid_rule."""
    store, client, movies, source, queue_mod = env
    q = _queue(store, client)

    st = source.stat()
    job = store.create_job(str(source), st.st_size, st.st_mtime_ns)
    store.set_plan(job.id, preset_name="NVENC", rule_id="r1", facts={"height": 1080}, original_size=st.st_size)
    store.set_stage(job.id, "queued")

    # File on disk is modified externally
    source.write_bytes(b"modified data")
    monkeypatch.setattr(q, "_probe_once", lambda _p: {"height": 1080, "size": 100})

    from app.encoder.rules import RuleError

    def boom(*args, **kwargs):
        raise RuleError("Operator 'contains' applies to text fields only, not 'height'")

    monkeypatch.setattr(queue_mod, "evaluate", boom)

    q._run(job.id)

    updated = store.get_job(job.id)
    assert updated.stage == "failed"
    assert updated.error_code == "invalid_rule"
    assert len(client.submitted) == 0


def test_cancel_returns_false_for_terminal_stages(env):
    """Cancelling a job that is already terminal (done, failed, skipped, cancelled)
    must return False without sending a spurious remote cancel or altering the stage."""
    store, client, *_ = env
    q = _queue(store, client)

    for terminal_stage in ("done", "failed", "skipped", "cancelled"):
        job = store.create_job(f"/media/terminal_{terminal_stage}.mkv")
        store.set_stage(job.id, terminal_stage)
        store.set_remote_job(job.id, f"remote_{terminal_stage}")

        assert q.cancel(job.id) is False
        assert store.get_job(job.id).stage == terminal_stage
        assert client.cancels == []


def test_cancel_racing_remote_failed_response_retains_cancelled(env):
    """When a cancel races a remote failed status in _await_remote,
    the job must stay 'cancelled' and not get clobbered to 'failed'."""
    store, client, *_ = env
    q = _queue(store, client)

    job = store.create_job("/media/test_racing.mkv")
    store.set_stage(job.id, "encoding")
    store.set_remote_job(job.id, "rem-1")

    # Cancel the job
    assert q.cancel(job.id) is True
    assert store.get_job(job.id).stage == "cancelled"

    # Simulate _fail() call or _await_remote receiving failed
    q._fail(job.id, "Remote failed", "encode_failed")
    assert store.get_job(job.id).stage == "cancelled"


def test_cancel_racing_remote_rejected_response_retains_cancelled(env):
    """When a cancel races an EncoderRejected error, _fail must not clobber 'cancelled'."""
    store, client, *_ = env
    q = _queue(store, client)

    job = store.create_job("/media/test_racing_rejected.mkv")
    store.set_stage(job.id, "encoding")
    store.set_remote_job(job.id, "rem-2")

    assert q.cancel(job.id) is True

    from app.encoder.client import EncoderRejected
    exc = EncoderRejected("bad_source", "Encoder error", 400)
    q._handle_dispatch_error(job.id, exc)

    assert store.get_job(job.id).stage == "cancelled"
