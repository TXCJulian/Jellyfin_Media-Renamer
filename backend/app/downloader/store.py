import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

STAGES = frozenset(
    {"queued", "downloading", "transcoding", "done", "cancelled", "error"}
)
ACTIVE_STAGES = frozenset({"queued", "downloading", "transcoding"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    options     TEXT NOT NULL,
    stage       TEXT NOT NULL,
    error       TEXT,
    -- Set the first time the job is handed to the queue. `queued` alone
    -- cannot distinguish a job waiting in the FIFO from one created with
    -- auto_start=false that the user has never started, and recovery must
    -- resume the former without silently starting the latter.
    enqueued    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS items (
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    path        TEXT,
    size        INTEGER,
    progress    REAL NOT NULL DEFAULT 0.0,
    progress_known INTEGER NOT NULL DEFAULT 1,
    stage       TEXT NOT NULL DEFAULT 'queued',
    error       TEXT,
    PRIMARY KEY (job_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""

_ITEM_FIELDS = ("title", "path", "size", "progress", "progress_known", "stage", "error")

# Values of `auto_start` that mean "do not start this job yet".
_AUTO_START_DISABLED = frozenset({"false", "0", "no"})


def wants_auto_start(options: Mapping[str, Any]) -> bool:
    """Whether these job options ask for the job to start immediately.

    `options` is whatever JSON the client sent, so `auto_start` can arrive as a
    JSON boolean, a string, a number, or not at all. Everything is normalised
    through `str().lower()` rather than trusted to be a bool, and an absent key
    means auto-start. The creation route and the `enqueued` backfill must agree
    exactly on this, which is why they share one implementation.
    """
    return str(options.get("auto_start", True)).lower() not in _AUTO_START_DISABLED


@dataclass
class Item:
    index: int
    title: str = ""
    path: str | None = None
    size: int | None = None
    progress: float = 0.0
    progress_known: bool = True
    stage: str = "queued"
    error: str | None = None


@dataclass
class Job:
    id: str
    url: str
    options: dict[str, Any]
    stage: str
    error: str | None
    created_at: str
    updated_at: str
    items: list[Item] = field(default_factory=list)


class JobStore:
    """SQLite-backed job persistence. The only writer of downloader state."""

    def __init__(
        self,
        db_path: str,
        on_change: Callable[[Job], None] | None = None,
        on_delete: Callable[[str], None] | None = None,
    ) -> None:
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._lock = threading.Lock()
        self._on_change = on_change
        self._on_delete = on_delete

    def _migrate(self) -> None:
        """Bring a database written by an older build up to `_SCHEMA`.

        `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a new
        column has to be added explicitly.
        """
        item_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(items)")
        }
        if "progress_known" not in item_columns:
            self._conn.execute(
                "ALTER TABLE items ADD COLUMN progress_known INTEGER NOT NULL DEFAULT 1"
            )
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if "enqueued" not in columns:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN enqueued INTEGER NOT NULL DEFAULT 0"
            )
            self._backfill_enqueued()

    def _backfill_enqueued(self) -> None:
        """Mark the pre-column rows that had genuinely reached the queue.

        A blanket `UPDATE jobs SET enqueued = 1` would re-create the very bug
        this column exists to fix, for exactly the population a migration
        serves: `create_downloads` has always honoured `auto_start`, so a
        pre-upgrade database can hold jobs resting at `queued` because the user
        deliberately never started them, and marking those enqueued makes the
        next boot recover and start them.

        Any stage past `queued` implies the job was enqueued - it could not
        have reached `downloading` otherwise. A row still at `queued` is
        ambiguous, so its stored options decide, using the same rule the
        creation route applied when the row was written. Options that will not
        parse fall back to enqueued, which is the pre-migration behaviour and
        keeps a corrupt row from bricking startup.

        The column default stays 0, so rows created from here on start
        un-enqueued and rely on `mark_enqueued`.
        """
        marked: list[tuple[str]] = []
        for row in self._conn.execute("SELECT id, stage, options FROM jobs"):
            if row["stage"] != "queued":
                marked.append((row["id"],))
                continue
            try:
                options = json.loads(row["options"])
            except (TypeError, ValueError):
                options = {}
            if not isinstance(options, dict):
                options = {}
            if wants_auto_start(options):
                marked.append((row["id"],))
        self._conn.executemany("UPDATE jobs SET enqueued = 1 WHERE id = ?", marked)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _notify_deleted(self, job_id: str) -> None:
        """Announce a removed job. Call with self._lock released.

        Like `_on_change`, this only ever reaches the event broadcaster.
        `delete_job` runs on the request thread and `purge_expired` on the TTL
        sweeper, neither of which holds `DownloadQueue._lock`, so the
        queue -> store lock order is not at risk here.
        """
        if self._on_delete is not None:
            self._on_delete(job_id)

    def _read_job_locked(self, job_id: str) -> Job | None:
        """Read job while already holding self._lock. Do not call without holding lock."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        item_rows = self._conn.execute(
            "SELECT * FROM items WHERE job_id = ? ORDER BY idx", (job_id,)
        ).fetchall()
        return _to_job(row, item_rows)

    def create_job(
        self, url: str, options: dict[str, Any], stage: str = "queued"
    ) -> str:
        job_id = str(uuid.uuid4())
        job = None
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, url, options, stage) VALUES (?, ?, ?, ?)",
                (job_id, url, json.dumps(options), stage),
            )
            self._conn.commit()
            job = self._read_job_locked(job_id)
        if job is not None and self._on_change is not None:
            self._on_change(job)
        return job_id

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._read_job_locked(job_id)

    def list_jobs(self, limit: int = 200) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            item_rows = self._conn.execute(
                "SELECT * FROM items ORDER BY idx"
            ).fetchall()
        by_job: dict[str, list[sqlite3.Row]] = {}
        for item in item_rows:
            by_job.setdefault(item["job_id"], []).append(item)
        return [_to_job(row, by_job.get(row["id"], [])) for row in rows]

    def set_job_stage(self, job_id: str, stage: str, error: str | None = None) -> None:
        if stage not in STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        job = None
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET stage = ?, error = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (stage, error, job_id),
            )
            self._conn.commit()
            job = self._read_job_locked(job_id)
        if job is not None and self._on_change is not None:
            self._on_change(job)

    def upsert_item(self, job_id: str, index: int, **fields: Any) -> None:
        if "progress" in fields:
            fields.setdefault("progress_known", True)
        unknown = set(fields) - set(_ITEM_FIELDS)
        if unknown:
            raise ValueError(f"Unknown item fields: {sorted(unknown)}")
        job = None
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO items (job_id, idx) VALUES (?, ?)",
                (job_id, index),
            )
            if fields:
                assignments = ", ".join(f"{name} = ?" for name in fields)
                self._conn.execute(
                    f"UPDATE items SET {assignments} WHERE job_id = ? AND idx = ?",
                    (*fields.values(), job_id, index),
                )
            self._conn.execute(
                "UPDATE jobs SET updated_at = datetime('now') WHERE id = ?", (job_id,)
            )
            self._conn.commit()
            job = self._read_job_locked(job_id)
        if job is not None and self._on_change is not None:
            self._on_change(job)

    def prune_items(self, job_id: str, keep: Iterable[int]) -> int:
        """Drop every item row of `job_id` whose index is not in `keep`.

        Exists for the runner's post-download reconciliation: yt-dlp writes
        intermediate files (per-stream downloads that are merged, or a
        container that audio extraction replaces) and fires a progress hook
        for each, so rows get allocated for paths that no longer exist once
        the job finishes. Those rows have to go, and only the reconciliation
        pass - which sees the real per-entry output - knows which they are.

        Fires `on_change` with the surviving job exactly as `upsert_item`
        does, so a subscriber that replaces its item list wholesale converges
        without any new event type. Nothing is announced when nothing was
        removed. Returns the number of rows deleted.
        """
        keep_indices = sorted({int(index) for index in keep})
        job = None
        with self._lock:
            if keep_indices:
                placeholders = ", ".join("?" * len(keep_indices))
                cursor = self._conn.execute(
                    f"DELETE FROM items WHERE job_id = ? "
                    f"AND idx NOT IN ({placeholders})",
                    (job_id, *keep_indices),
                )
            else:
                cursor = self._conn.execute(
                    "DELETE FROM items WHERE job_id = ?", (job_id,)
                )
            removed = cursor.rowcount
            if removed:
                self._conn.execute(
                    "UPDATE jobs SET updated_at = datetime('now') WHERE id = ?",
                    (job_id,),
                )
            self._conn.commit()
            if removed:
                job = self._read_job_locked(job_id)
        if job is not None and self._on_change is not None:
            self._on_change(job)
        return max(removed, 0)

    def mark_enqueued(self, job_id: str) -> None:
        """Record that this job has been handed to the queue at least once.

        Fires no `on_change`: this is queue bookkeeping, not user-visible job
        state, and it is written from `DownloadQueue.enqueue`.
        """
        with self._lock:
            self._conn.execute("UPDATE jobs SET enqueued = 1 WHERE id = ?", (job_id,))
            self._conn.commit()

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            self._notify_deleted(job_id)
        return deleted

    def reset_active_to_queued(self) -> list[str]:
        """After a restart, return interrupted jobs to the queue.

        Only jobs that actually reached the queue are resumed. A job created
        with auto_start=false rests at `queued` and is indistinguishable by
        stage from one that was waiting in the FIFO when the process died, so
        without the `enqueued` flag recovery would start work the user never
        asked to start. `queued` cannot simply be dropped from the selection
        either: the shutdown path writes an interrupted *running* job straight
        back to `queued`, and that one must resume.
        """
        placeholders = ", ".join("?" * len(ACTIVE_STAGES))
        active = sorted(ACTIVE_STAGES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM jobs WHERE stage IN ({placeholders}) "
                f"AND enqueued = 1",
                active,
            ).fetchall()
            job_ids = [row["id"] for row in rows if row["id"]]
            self._conn.execute(
                f"UPDATE jobs SET stage = 'queued', error = NULL "
                f"WHERE stage IN ({placeholders}) AND enqueued = 1",
                active,
            )
            self._conn.commit()
        return [jid for jid in job_ids]

    def purge_expired(self, ttl_seconds: int) -> int:
        """Drop jobs older than the TTL, announcing every one that goes.

        One statement, not a SELECT followed by a matching DELETE: each would
        evaluate `datetime('now', ?)` on its own, and `now` has one-second
        granularity, so a row crossing the boundary in between was deleted
        without ever being announced - the stuck-card symptom this
        notification exists to remove - and under-counted on the way out.
        `RETURNING` needs SQLite 3.35+; the runtime is well past that.
        """
        placeholders = ", ".join("?" * len(ACTIVE_STAGES))
        params = (*sorted(ACTIVE_STAGES), f"-{int(ttl_seconds)} seconds")
        with self._lock:
            rows = self._conn.execute(
                f"DELETE FROM jobs WHERE stage NOT IN ({placeholders}) "
                f"AND created_at < datetime('now', ?) RETURNING id",
                params,
            ).fetchall()
            job_ids = [row["id"] for row in rows if row["id"]]
            self._conn.commit()
        for job_id in job_ids:
            self._notify_deleted(job_id)
        return len(job_ids)


def _to_job(row: sqlite3.Row, item_rows: list[sqlite3.Row]) -> Job:
    return Job(
        id=row["id"],
        url=row["url"],
        options=json.loads(row["options"]),
        stage=row["stage"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        items=[
            Item(
                index=item["idx"],
                title=item["title"],
                path=item["path"],
                size=item["size"],
                progress=item["progress"],
                progress_known=bool(item["progress_known"]),
                stage=item["stage"],
                error=item["error"],
            )
            for item in item_rows
        ],
    )
