"""SQLite persistence for presets, rules, jobs and retention.

Mirrors ``downloader/store.py``: one connection shared across threads under a
lock, schema applied on open, ``sqlite3.Row`` factory.

State survives restart deliberately. An encode is minutes to hours of GPU time
against a file the user cares about; losing the queue on a container restart
matters far more here than for a download that can simply be re-fetched.
"""

import json
import math
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.encoder.presets import NamedPreset
from app.encoder.rules import Condition, Rule
from app.encoder.swap import purge_original

STAGES = frozenset(
    {
        "settling",
        "pending",
        "queued",
        "encoding",
        "swapping",
        "done",
        "failed",
        "blocked",
        "skipped",
        "cancelled",
    }
)

TERMINAL_STAGES = ("done", "failed", "skipped", "cancelled")

# Stages a restart must requeue. `pending` (awaiting review) and `blocked`
# (awaiting a human decision) are excluded on purpose: both mean "a person has
# not answered yet", and resuming them would answer for them.
_RESUMABLE_STAGES = ("queued", "encoding", "swapping")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                   TEXT PRIMARY KEY,
    source_path          TEXT NOT NULL,
    preset_name          TEXT,
    rule_id              TEXT,
    stage                TEXT NOT NULL,
    progress             REAL NOT NULL DEFAULT 0.0,
    eta_seconds          INTEGER,
    remote_job_id        TEXT,
    output_path          TEXT,
    error                TEXT,
    error_code           TEXT,
    facts                TEXT,
    original_size        INTEGER,
    encoded_size         INTEGER,
    original_kept_path   TEXT,
    original_expires_at  REAL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
-- One live job per file. The watcher can see the same path twice (a second
-- filesystem event, a rescan after restart); two jobs encoding one file would
-- race for the same output and one would swap over the other's result.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_source
    ON jobs(source_path)
    WHERE stage NOT IN ('done', 'failed', 'skipped', 'cancelled');
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS presets (
    name          TEXT PRIMARY KEY,
    encoder       TEXT NOT NULL,
    video_preset  TEXT NOT NULL DEFAULT '',
    file_format   TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rules (
    id          TEXT PRIMARY KEY,
    position    INTEGER NOT NULL,
    conditions  TEXT NOT NULL,
    target      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_position ON rules(position);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Files the watcher has already decided about, so a rescan does not re-probe
-- the whole library every interval.
--
-- Deliberately NOT derived from the jobs table and deliberately NOT purged by
-- ENCODER_JOB_TTL: job rows are history and expire, whereas "have I already
-- looked at this exact file" must outlive them. Deriving it from jobs meant
-- that seven days after an encode the published output became eligible for
-- processing all over again.
--
-- The fingerprint is (size, mtime_ns) rather than size alone. Re-copying a
-- file restores its byte count but not its mtime, so a size-only check
-- silently ignored a genuine replacement -- observed in testing, where a
-- recopied original was never picked up again.
CREATE TABLE IF NOT EXISTS seen_files (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    seen_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass
class Job:
    id: str
    source_path: str
    stage: str
    progress: float = 0.0
    eta_seconds: int | None = None
    preset_name: str | None = None
    rule_id: str | None = None
    remote_job_id: str | None = None
    output_path: str | None = None
    error: str | None = None
    error_code: str | None = None
    facts: dict = field(default_factory=dict)
    original_size: int | None = None
    encoded_size: int | None = None
    original_kept_path: str | None = None
    original_expires_at: float | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class StoredPreset:
    name: str
    encoder: str
    video_preset: str
    file_format: str
    body: dict


class EncoderStore:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
            if "eta_seconds" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN eta_seconds INTEGER")
            # Estimates are live telemetry; restart must not resurrect stale ones.
            self._conn.execute(
                "UPDATE jobs SET eta_seconds = NULL WHERE eta_seconds IS NOT NULL"
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- jobs ------------------------------------------------------------

    def create_job(
        self,
        source_path: str,
        size: int | None = None,
        mtime_ns: int | None = None,
    ) -> Job:
        """Create a job, recording the file's fingerprint in the same transaction.

        The two writes are deliberately atomic. Marking the file as "seen"
        before the job existed meant that if job creation or planning raised --
        or the process died between them -- the fingerprint was durable with no
        job behind it, and every later scan suppressed the file. The result was
        a file silently never processed, which is strictly worse than the
        retry loop the fingerprint exists to prevent.

        Committing them together gives the property we actually want: the file
        is marked decided exactly when a job exists to represent that decision.
        A later failure in planning leaves both rows, so the job is visible as
        `failed` and the file is not re-probed forever.
        """
        job_id = uuid.uuid4().hex
        with self._lock:
            # `with self._conn` commits on success and ROLLS BACK on any
            # exception. Committing at the end instead left the job insert
            # sitting in the open transaction when the fingerprint insert
            # raised, and the next unrelated commit persisted it: an orphan
            # job stuck in `settling`, holding the active-source index and
            # suppressing every retry -- precisely the state this method's
            # atomicity exists to prevent.
            with self._conn:
                self._conn.execute(
                    "INSERT INTO jobs (id, source_path, stage) "
                    "VALUES (?, ?, 'settling')",
                    (job_id, source_path),
                )
                if size is not None and mtime_ns is not None:
                    self._conn.execute(
                        "INSERT INTO seen_files (path, size, mtime_ns, seen_at) "
                        "VALUES (?, ?, ?, datetime('now')) "
                        "ON CONFLICT(path) DO UPDATE SET "
                        "size = excluded.size, mtime_ns = excluded.mtime_ns, "
                        "seen_at = excluded.seen_at",
                        (source_path, size, mtime_ns),
                    )
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _to_job(row)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _to_job(row) if row else None

    def list_jobs(self, limit: int = 200) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_to_job(r) for r in rows]

    def set_stage(
        self,
        job_id: str,
        stage: str,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if stage not in STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        self._update(
            job_id, stage=stage, error=error, error_code=error_code, eta_seconds=None
        )

    def set_progress(
        self, job_id: str, pct: float, eta_seconds: int | None = None
    ) -> None:
        if (
            isinstance(eta_seconds, bool)
            or not isinstance(eta_seconds, (int, float))
            or not math.isfinite(eta_seconds)
            or eta_seconds < 0
        ):
            eta_seconds = None
        self._update(
            job_id,
            progress=float(pct),
            eta_seconds=int(eta_seconds) if eta_seconds is not None else None,
        )

    def cancel_blocked_for_reprocess(self, job_id: str) -> bool:
        """Release a recoverable blocked row while retaining it as history."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET stage = 'cancelled', updated_at = datetime('now') "
                "WHERE id = ? AND stage = 'blocked' AND "
                "COALESCE(error_code, '') != 'swap_interrupted'",
                (job_id,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def transition_stage(
        self, job_id: str, allowed_stages: Iterable[str], stage: str
    ) -> bool:
        """Atomically move a job only if it is still in an allowed stage."""
        if stage not in STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        allowed = tuple(allowed_stages)
        if not allowed or any(candidate not in STAGES for candidate in allowed):
            raise ValueError("Unknown or empty allowed stage set")
        placeholders = ", ".join("?" for _ in allowed)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE jobs SET stage = ?, error = NULL, error_code = NULL, "
                f"updated_at = datetime('now') WHERE id = ? "
                f"AND stage IN ({placeholders})",
                (stage, job_id, *allowed),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_remote_job(self, job_id: str, remote_job_id: str) -> None:
        self._update(job_id, remote_job_id=remote_job_id)

    def set_result(self, job_id: str, output_path: str, encoded_size: int) -> None:
        self._update(job_id, output_path=output_path, encoded_size=encoded_size)

    def set_plan(
        self,
        job_id: str,
        preset_name: str | None,
        rule_id: str | None,
        facts: dict,
        original_size: int,
    ) -> None:
        self._update(
            job_id,
            preset_name=preset_name,
            rule_id=rule_id,
            facts=json.dumps(facts),
            original_size=original_size,
        )

    def set_retention(self, job_id: str, kept_path: str, expires_at: float) -> None:
        self._update(
            job_id, original_kept_path=kept_path, original_expires_at=expires_at
        )

    def clear_retention(self, job_id: str) -> None:
        self._update(job_id, original_kept_path=None, original_expires_at=None)

    def delete_job(self, job_id: str) -> bool:
        """Delete a job row, purging any original it retained.

        ``original_kept_path`` is the *only* pointer to a preserved original
        in the holding area -- once the row is gone, nothing else knows that
        file exists. Purging it here as part of the delete (rather than
        refusing to delete jobs with a retained original) keeps `DELETE
        /jobs/{id}` simple for callers while guaranteeing a deleted job never
        leaks its holding-area file permanently.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT original_kept_path FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            cur = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.commit()
        if row is not None and row["original_kept_path"]:
            purge_original(row["original_kept_path"])
        return cur.rowcount > 0

    def active_source_paths(self) -> set[str]:
        placeholders = ",".join("?" * len(TERMINAL_STAGES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT source_path FROM jobs WHERE stage NOT IN ({placeholders})",
                TERMINAL_STAGES,
            ).fetchall()
        return {r["source_path"] for r in rows}

    def active_jobs_by_source(self) -> dict[str, Job]:
        """Return `{source_path: Job}` for every active (non-terminal) job."""
        placeholders = ",".join("?" * len(TERMINAL_STAGES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE stage NOT IN ({placeholders}) "
                "ORDER BY created_at DESC, rowid DESC",
                TERMINAL_STAGES,
            ).fetchall()
        return {r["source_path"]: _to_job(r) for r in rows}

    def active_job_for_source(self, source_path: str) -> Job | None:
        """Return the newest non-terminal job for *source_path*, if any."""
        placeholders = ",".join("?" * len(TERMINAL_STAGES))
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM jobs WHERE source_path = ? "
                f"AND stage NOT IN ({placeholders}) "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (source_path, *TERMINAL_STAGES),
            ).fetchone()
        return _to_job(row) if row else None

    def newest_job_for_source(self, source_path: str) -> Job | None:
        """Return the newest job for *source_path*, including terminal jobs."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE source_path = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (source_path,),
            ).fetchone()
        return _to_job(row) if row else None

    def seen_fingerprints(self) -> dict[str, tuple[int, int]]:
        """`{path: (size, mtime_ns)}` for every file already decided about.

        The watcher uses this to stop reconsidering files it has judged.
        Without it, a job reaching a *terminal* stage leaves its path in
        neither `active_source_paths()` nor the settle tracker, so the next
        rescan treats the file as a brand-new arrival: an ffprobe subprocess
        and a fresh job row per file per scan interval, forever.

        Lives in its own table rather than being derived from job rows, which
        `purge_expired()` deletes -- otherwise the whole library became
        eligible for reprocessing again one job-TTL after it was encoded.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, size, mtime_ns FROM seen_files"
            ).fetchall()
        return {r["path"]: (r["size"], r["mtime_ns"]) for r in rows}

    def get_seen(self, path: str) -> tuple[int, int] | None:
        """Return `(size, mtime_ns)` if *path* has a recorded fingerprint."""
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime_ns FROM seen_files WHERE path = ?", (path,)
            ).fetchone()
        return (row["size"], row["mtime_ns"]) if row else None

    def mark_seen(self, path: str, size: int, mtime_ns: int) -> None:
        """Record that *path* has been decided about at this exact fingerprint.

        Called for the source when the watcher dispatches it, and again for
        the published file after a successful swap -- the swap replaces the
        file, so its fingerprint changes and the stale row would otherwise
        make it look like a new arrival.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO seen_files (path, size, mtime_ns, seen_at) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(path) DO UPDATE SET "
                "size = excluded.size, mtime_ns = excluded.mtime_ns, "
                "seen_at = excluded.seen_at",
                (path, size, mtime_ns),
            )
            self._conn.commit()

    def forget_seen_entries(self, entries: Iterable[tuple[str, int, int]]) -> int:
        """Drop dedup records matching exactly these `(path, size, mtime_ns)`.

        The fingerprint is part of the WHERE clause, not just the path. A
        delete keyed on the path alone races the queue: retention moves the
        original out of the way, a scan in progress sees the path briefly
        missing, the queue publishes the encode at that same path with a fresh
        fingerprint, and the scan then deletes that *new* record because the
        *old* one was in its opening snapshot. The next scan treats the
        published output as a new arrival and re-encodes it.

        Matching on the fingerprint makes that delete affect zero rows, which
        is the correct outcome: the row it wanted to remove no longer exists.

        Deliberately dumb otherwise -- it deletes what it is given and never
        re-derives the set, because choosing what is stale needs the caller's
        knowledge of which roots were walked cleanly.
        """
        rows = [(path, size, mtime_ns) for path, size, mtime_ns in entries]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "DELETE FROM seen_files "
                "WHERE path = ? AND size = ? AND mtime_ns = ?",
                rows,
            )
            self._conn.commit()
        return cur.rowcount

    def forget_seen(self, path: str) -> bool:
        """Drop the dedup record for *path* so the watcher reconsiders it.

        The explicit escape hatch behind `POST /reprocess`: without one, a
        file the user wants re-encoded under a changed rule set has no way
        back into the queue short of touching it on disk.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM seen_files WHERE path = ?", (path,))
            self._conn.commit()
        return cur.rowcount > 0

    def reset_active_for_recovery(self) -> list[Job]:
        """Requeue jobs a restart interrupted, and return them."""
        placeholders = ",".join("?" * len(_RESUMABLE_STAGES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE stage IN ({placeholders})",
                _RESUMABLE_STAGES,
            ).fetchall()
            self._conn.execute(
                f"UPDATE jobs SET stage = 'queued', updated_at = datetime('now') "
                f"WHERE stage IN ({placeholders})",
                _RESUMABLE_STAGES,
            )
            self._conn.commit()
        return [_to_job(r) for r in rows]

    def purge_expired_ids(self, ttl_seconds: int) -> list[str]:
        """Delete expired terminal jobs and return the removed IDs.

        Same reasoning as `delete_job`: `original_kept_path` is the only
        pointer to a preserved original, so a row purged here without also
        purging its file would leak that file in the holding area forever
        (or, with a job TTL shorter than the original TTL, guarantee the
        leak rather than merely risk it).
        """
        placeholders = ",".join("?" * len(TERMINAL_STAGES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, original_kept_path FROM jobs WHERE stage IN ({placeholders}) "
                f"AND updated_at <= datetime('now', ?)",
                (*TERMINAL_STAGES, f"-{int(ttl_seconds)} seconds"),
            ).fetchall()
            self._conn.execute(
                f"DELETE FROM jobs WHERE stage IN ({placeholders}) "
                f"AND updated_at <= datetime('now', ?)",
                (*TERMINAL_STAGES, f"-{int(ttl_seconds)} seconds"),
            )
            self._conn.commit()
        for row in rows:
            if row["original_kept_path"]:
                purge_original(row["original_kept_path"])
        return [row["id"] for row in rows]

    def purge_expired(self, ttl_seconds: int) -> int:
        """Delete expired terminal jobs and return the number removed."""
        return len(self.purge_expired_ids(ttl_seconds))

    def due_retentions(self, now: float) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE original_kept_path IS NOT NULL "
                "AND original_expires_at IS NOT NULL AND original_expires_at <= ?",
                (now,),
            ).fetchall()
        return [_to_job(r) for r in rows]

    def _update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {assignments}, updated_at = datetime('now') "
                f"WHERE id = ?",
                (*fields.values(), job_id),
            )
            self._conn.commit()

    # ---- presets ---------------------------------------------------------

    def replace_presets(self, presets: list[NamedPreset]) -> None:
        """Upsert by name. Additive across imports: a second preset file must
        not discard the first, but re-importing a name updates it."""
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO presets (name, encoder, video_preset, file_format, body) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "encoder = excluded.encoder, video_preset = excluded.video_preset, "
                "file_format = excluded.file_format, body = excluded.body",
                [
                    (
                        p.name,
                        p.encoder,
                        p.video_preset,
                        p.file_format,
                        json.dumps(p.body),
                    )
                    for p in presets
                ],
            )

    def list_presets(self) -> list[StoredPreset]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM presets ORDER BY name").fetchall()
        return [
            StoredPreset(
                name=r["name"],
                encoder=r["encoder"],
                video_preset=r["video_preset"],
                file_format=r["file_format"],
                body=json.loads(r["body"]),
            )
            for r in rows
        ]

    def delete_preset(self, name: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM presets WHERE name = ?", (name,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---- rules -----------------------------------------------------------

    def replace_rules(self, rules: list[Rule]) -> None:
        """Full replacement, in one transaction.

        Rule order *is* the semantics -- first match wins -- so merging would
        make the effective order depend on insertion history rather than on
        what the user arranged.

        The transaction is real, not just intended: `with self._conn` rolls
        back on any exception. Committing at the end instead meant a failing
        insert left the DELETE to be persisted by the next unrelated commit,
        silently wiping every rule the user had.
        """
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM rules")
            self._conn.executemany(
                "INSERT INTO rules (id, position, conditions, target) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        r.id,
                        i,
                        json.dumps(
                            [
                                {"field": c.field, "op": c.op, "value": c.value}
                                for c in r.conditions
                            ]
                        ),
                        r.target,
                    )
                    for i, r in enumerate(rules)
                ],
            )

    def list_rules(self) -> list[Rule]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rules ORDER BY position"
            ).fetchall()
        return [
            Rule(
                id=r["id"],
                conditions=[
                    Condition(field=c["field"], op=c["op"], value=c["value"])
                    for c in json.loads(r["conditions"])
                ],
                target=r["target"],
            )
            for r in rows
        ]

    # ---- settings --------------------------------------------------------

    def get_setting(self, key: str, default: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()


def _to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        source_path=row["source_path"],
        stage=row["stage"],
        progress=row["progress"],
        eta_seconds=row["eta_seconds"],
        preset_name=row["preset_name"],
        rule_id=row["rule_id"],
        remote_job_id=row["remote_job_id"],
        output_path=row["output_path"],
        error=row["error"],
        error_code=row["error_code"],
        facts=json.loads(row["facts"]) if row["facts"] else {},
        original_size=row["original_size"],
        encoded_size=row["encoded_size"],
        original_kept_path=row["original_kept_path"],
        original_expires_at=row["original_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
