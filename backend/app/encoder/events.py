"""SSE fan-out of encode-job state.

Same contract as the downloader's broadcaster: publishing never blocks, a
subscriber that cannot keep up drops its oldest event, and the store remains
authoritative so a client that misses a delta converges on reconnect.
"""

import queue
import threading
from typing import Any

from app.encoder.store import Job

_TERMINAL_REPROCESS_STATUSES = frozenset({"completed", "failed", "cancelled"})
_REPROCESS_STATUSES = frozenset({"started", "running"}) | _TERMINAL_REPROCESS_STATUSES


def reprocess_to_payload(
    run_id: str,
    status: str,
    *,
    scanned: int,
    created: int,
    skipped: int,
    failed: int,
    path: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the complete, serialisable SSE contract for a reprocess run."""
    if not isinstance(run_id, str) or not run_id or status not in _REPROCESS_STATUSES:
        raise ValueError("Invalid reprocess event")
    counts = (scanned, created, skipped, failed)
    # Use `type(count) is not int` to explicitly reject `bool` (since bool is a subclass of int)
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("Reprocess counts must be non-negative integers")
    if path is not None and not isinstance(path, str):
        raise ValueError("Reprocess path must be a string or null")
    if error is not None and not isinstance(error, str):
        raise ValueError("Reprocess error must be a string or null")
    return {
        "type": "reprocess",
        "run_id": run_id,
        "status": status,
        "scanned": scanned,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "path": path,
        "error": error,
    }


def job_to_payload(job: Job) -> dict[str, Any]:
    """Serialise a job for the wire.

    ``saved_bytes`` is computed here rather than in the client because the done
    card leads with it -- it is the number that says whether a preset is worth
    keeping -- and two clients should not each reimplement the arithmetic.
    """
    saved = None
    if job.original_size and job.encoded_size:
        saved = job.original_size - job.encoded_size
    return {
        "job_id": job.id,
        "source_path": job.source_path,
        "stage": job.stage,
        "progress": round(job.progress, 1),
        "eta_seconds": job.eta_seconds if job.stage == "encoding" else None,
        "preset_name": job.preset_name,
        "rule_id": job.rule_id,
        "error": job.error,
        "error_code": job.error_code,
        "remote_job_id": job.remote_job_id,
        "output_path": job.output_path,
        "facts": job.facts,
        "original_size": job.original_size,
        "encoded_size": job.encoded_size,
        "saved_bytes": saved,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


class EventBroadcaster:
    def __init__(self, maxsize: int = 200) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass
