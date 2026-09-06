import logging
import math
import os
import re
import threading
from typing import Any

from yt_dlp import YoutubeDL

from app.downloader.store import Job, JobStore
from app.downloader.transcode import (
    TranscodeCancelled,
    assert_container_supports_codec,
    container_for_codec,
    transcode_file,
)
from app.downloader.ydl import (
    assert_within_allowed_roots,
    build_ydl_opts,
    needs_transcode,
    resolve_output_root,
    unique_path,
)

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_FORMAT_ID_RE = re.compile(r"\.f\d+(?=\.[^.]+$)")

_LOGIN_HINTS = (
    "sign in",
    "login required",
    "private video",
    "members-only",
    "age-restricted",
    "confirm your age",
)

# Message recorded on an item whose destination file already existed before
# this run, so yt-dlp's overwrites=False silently skipped downloading it
# (only a "finished" hook event fires for such a file, never "downloading").
_ALREADY_EXISTS_MESSAGE = "File already existed and was not re-downloaded."

# Message recorded on an item whose source file was pre-existing (see above)
# and which was then re-encoded. The re-encode is written alongside the
# original rather than replacing it, because destroying a file this run did
# not create would be data loss.
_KEPT_SOURCE_MESSAGE = (
    "The pre-existing original was kept; the re-encode was saved alongside it."
)

# The UI's Format control sends this sentinel for "let the source decide"; it
# is both the default and the explicit "Auto" option. It is not a container
# name, so it must be reduced to "" before it can reach a filename extension.
_AUTO = "auto"


class DownloadCancelled(RuntimeError):
    """Raised when a running download job is cancelled."""


class _IndexAllocator:
    """Assigns store row indices from a single keyspace, keyed by resolved
    output path.

    Both the progress hook and the post-download reconciliation loop share
    one instance per run, so a given file always lands in the same store
    row regardless of which of them writes it first. This replaces an
    earlier design where the hook derived its index from yt-dlp's
    `playlist_index` and the reconciliation loop derived its index from
    `enumerate(entries)` position: those two index spaces only coincide in
    the simple, non-gappy case, and disagreeing between them produced
    phantom/duplicate item rows for the same underlying file.

    `for_path` and `new_index` are deliberately the only two ways to obtain
    an index. In particular there is no "most recently allocated index"
    accessor: an event that carries no path has no identity, and attributing
    it to the last-allocated row is wrong, because allocation only happens
    once a path is known - so while a *new* item is still resolving its
    destination, the last-allocated row still belongs to the *previous*,
    already-finished item. Writing to it there corrupts a completed sibling.
    Such events are dropped instead (see `_make_hook`).
    """

    def __init__(self) -> None:
        self._by_path: dict[str, int] = {}
        self._next = 0

    def for_path(self, path: str) -> int:
        """Return the row index for `path`, allocating one on first sight."""
        real = os.path.realpath(path)
        index = self._by_path.get(real)
        if index is None:
            index = self._next
            self._next += 1
            self._by_path[real] = index
        return index

    def new_index(self) -> int:
        """Allocate a row index not tied to any known path (e.g. an entry
        whose output file could not be located at all)."""
        index = self._next
        self._next += 1
        return index


def _ydl_factory(opts: dict[str, Any]):
    """Indirection point so tests can substitute a fake yt-dlp."""
    return YoutubeDL(opts)


def clean_error(text: str) -> str:
    return _ANSI_RE.sub("", str(text)).strip()


def _friendly_error(exc: Exception) -> str:
    message = clean_error(str(exc))
    lowered = message.lower()
    if any(hint in lowered for hint in _LOGIN_HINTS):
        return f"{message} — this URL needs cookies; upload a cookies.txt file."
    if "ffmpeg" in lowered and "not found" in lowered:
        return "ffmpeg is not available on the server."
    if "no space left" in lowered:
        return "The destination disk is full."
    if "requested format" in lowered or "no video formats" in lowered:
        return f"{message} — try a different quality setting."
    return message or "Download failed"


def _display_name(path: Any) -> str:
    if not path:
        return ""
    return _FORMAT_ID_RE.sub("", os.path.basename(str(path)))


def _output_key(path: str) -> str:
    """Identity of the *output* a file belongs to, ignoring which
    intermediate it is.

    yt-dlp derives every intermediate from the entry's own output template,
    so all files belonging to one entry share a directory and a stem:
    `Title.f137.mp4` and `Title.f251.webm` merge into `Title.mkv`, and
    `Title.webm` is extracted to `Title.opus`. Stripping the `.f<id>` infix
    and the extension therefore maps an entry's intermediates and its final
    file onto the same key.

    This is only used to answer "did this run actually fetch anything for
    this output", never to allocate a row index - `_IndexAllocator` stays the
    sole producer of those, keyed by exact realpath.
    """
    directory, name = os.path.split(os.path.realpath(path))
    stem = os.path.splitext(_FORMAT_ID_RE.sub("", name))[0]
    return os.path.join(directory, stem)


def _entry_path(entry: dict[str, Any]) -> str | None:
    candidate = entry.get("filepath") or entry.get("_filename")
    if candidate and os.path.isfile(str(candidate)):
        return str(candidate)
    for download in entry.get("requested_downloads") or []:
        candidate = (download or {}).get("filepath") or (download or {}).get(
            "_filename"
        )
        if candidate and os.path.isfile(str(candidate)):
            return str(candidate)
    # Thumbnail-only jobs run with `skip_download`, and in that mode yt-dlp
    # sets `info_dict['filepath']` to the *media* temp filename it never
    # writes (YoutubeDL.process_info), so the checks above find nothing. The
    # file it did write is recorded per-thumbnail as
    # `info['thumbnails'][n]['filepath']` by `YoutubeDL._write_thumbnails`;
    # `FFmpegThumbnailsConvertorPP` rewrites that same key in place when a
    # target format was requested. `_write_thumbnails` walks the list back to
    # front and stops at the first one it writes, so scanning in reverse
    # finds the written entry first.
    for thumbnail in reversed(entry.get("thumbnails") or []):
        candidate = (thumbnail or {}).get("filepath")
        if candidate and os.path.isfile(str(candidate)):
            return str(candidate)
    return None


def _entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise a yt-dlp info dict to a flat list of downloaded entries."""
    raw = info.get("entries")
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    return [info] if info else []


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def run_job(
    store: JobStore,
    job: Job,
    cancel_event: threading.Event,
    cookie_path: str | None = None,
) -> None:
    """Run one job to completion. Never raises; terminal state lands in the store."""
    try:
        store.set_job_stage(job.id, "downloading")
        output_root = resolve_output_root(job.options)
        os.makedirs(output_root, exist_ok=True)
        opts = build_ydl_opts(job.options, output_root, cookie_path)
        # Single source of truth for both "which store row is this file"
        # and "was this path genuinely fetched this run". One allocator
        # instance is shared by the hook and the reconciliation loop below,
        # so a given output file always lands in exactly one row no matter
        # which of them writes it first.
        allocator = _IndexAllocator()
        # Realpaths that received at least one "downloading" hook event this
        # run, i.e. were genuinely fetched rather than skipped because the
        # destination already existed (see overwrites=False in
        # build_ydl_opts and _ALREADY_EXISTS_MESSAGE below).
        downloaded_paths: set[str] = set()
        # The same information at output granularity (see `_output_key`). A
        # merged or extracted final file never receives a "downloading" event
        # of its own - its *intermediates* do - so keying the pre-existing
        # check on the exact path alone flagged every merged download as
        # "already existed", which then made the transcode stage preserve the
        # source and write its encode alongside instead of replacing it.
        downloaded_outputs: set[str] = set()
        # ...but that inference only holds when a file downloader runs at all.
        # `skip_download` (thumbnail jobs) bypasses it entirely, so no progress
        # hook ever fires and "never saw a downloading event" stops carrying
        # any information. Flagging every thumbnail as a skipped pre-existing
        # file would be a plain lie, now visible to the user in the item row.
        track_pre_existing = not opts.get("skip_download")
        opts["progress_hooks"].append(
            _make_hook(
                store,
                job.id,
                cancel_event,
                downloaded_paths,
                downloaded_outputs,
                allocator,
                track_pre_existing,
            )
        )

        # A failing entry (e.g. one unavailable track in a playlist) makes
        # extract_info raise before it ever returns, so the entries loop
        # below never runs at all. Items that already completed via the
        # hook's "finished" branch were already written durably to the
        # store (path/size/stage="done") the moment they finished, so an
        # exception here does not lose them - it only sets the job-level
        # error via the outer except below.
        with _ydl_factory(opts) as ydl:
            info = ydl.extract_info(job.url, download=True)

        if cancel_event.is_set():
            raise DownloadCancelled("Cancelled by user")

        entries = _entries(info if isinstance(info, dict) else {})
        if not entries:
            raise RuntimeError("Download produced no output")

        # Reconcile with whatever the hook already wrote durably during the
        # run: each entry's row index comes from the same path-keyed
        # allocator the hook used, so an entry the hook already completed
        # is simply re-written with the (equivalent or better) data
        # available here - never a different row. Entries the hook never
        # saw a "finished" event for (no hook events at all in some test
        # doubles) get their row allocated here for the first time.
        recorded = 0
        # Indices that describe a real entry. Everything else the hook
        # allocated is an intermediate (a per-stream download that was merged
        # away, or a container that audio extraction replaced) whose file no
        # longer exists, and gets pruned below.
        entry_indices: set[int] = set()
        for entry in entries:
            path = _entry_path(entry)
            if path is None:
                index = allocator.new_index()
                entry_indices.add(index)
                title = str(entry.get("title") or f"Item {index + 1}")
                store.upsert_item(
                    job.id,
                    index,
                    title=title,
                    stage="error",
                    error="Output file could not be located",
                )
                continue
            assert_within_allowed_roots(path)
            index = allocator.for_path(path)
            entry_indices.add(index)
            title = str(
                entry.get("title") or _display_name(path) or f"Item {index + 1}"
            )
            item_fields: dict[str, Any] = {
                "title": title,
                "path": path,
                "size": _file_size(path),
                "progress": 100.0,
                "stage": "done",
            }
            fetched = (
                os.path.realpath(path) in downloaded_paths
                or _output_key(path) in downloaded_outputs
            )
            if track_pre_existing and not fetched:
                # No "downloading" event was ever seen for this file or for
                # any intermediate of it, yet a file exists at the
                # destination: yt-dlp's overwrites=False skipped it because it
                # was already there. Record it as done (the file is present
                # and usable) but don't claim it was freshly downloaded.
                item_fields["error"] = _ALREADY_EXISTS_MESSAGE
            store.upsert_item(job.id, index, **item_fields)
            recorded += 1

        # Only reachable once extract_info has returned, i.e. every entry
        # succeeded and `entries` is authoritative about what this job
        # produced. On the failure path extract_info raises and this never
        # runs, so a mid-playlist failure still leaves its completed
        # siblings' rows exactly as the hook wrote them.
        store.prune_items(job.id, entry_indices)

        if recorded == 0:
            store.set_job_stage(job.id, "error", "No output files were produced")
            return

        if needs_transcode(job.options):
            _run_transcode_stage(store, job, cancel_event)

        store.set_job_stage(job.id, "done")

    except (DownloadCancelled, TranscodeCancelled):
        _drop_vanished_items(store, job.id)
        store.set_job_stage(job.id, "cancelled", "Cancelled by user")
    except Exception as exc:
        logger.exception("Download job %s failed", job.id)
        _drop_vanished_items(store, job.id)
        store.set_job_stage(job.id, "error", _friendly_error(exc))


def _drop_vanished_items(store: JobStore, job_id: str) -> None:
    """Drop rows that promise a finished file which is not on disk.

    The happy path already removes intermediates: `prune_items` runs once
    `extract_info` returns and keeps only the rows that correspond to real
    entries. But when `extract_info` raises - a mid-playlist failure, or a
    cancel - that pruning is deliberately skipped so completed siblings
    survive, and the per-stream rows the progress hook wrote (`Title.f137.mp4`,
    `Title.f251.webm`) are left behind at stage="done" pointing at files ffmpeg
    merged away and deleted. The panel renders a Save link for every done row,
    so those became a 404 whose JSON body the browser saved as `file.json`.

    Only done rows whose file is missing are removed, which is exactly the set
    that can produce that 404. A completed sibling still has its file, so it is
    kept - that guarantee is what the failure path exists to provide. Rows in
    any other stage are left untouched: they are not offered for download, and
    an in-flight or errored row still tells the user what happened.
    """
    try:
        job = store.get_job(job_id)
        if job is None:
            return
        keep = {
            item.index
            for item in job.items
            if item.stage != "done" or not item.path or os.path.isfile(item.path)
        }
        if len(keep) != len(job.items):
            store.prune_items(job_id, keep)
    except Exception:
        # Best-effort cleanup on a path that is already reporting a failure;
        # it must never displace the terminal stage the caller is about to set.
        logger.warning(
            "Could not prune vanished items for job %s", job_id, exc_info=True
        )


def _download_progress(data: dict[str, Any]) -> float | None:
    def number(key: str) -> float | None:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) and value >= 0 else None

    total = number("total_bytes") or number("total_bytes_estimate")
    downloaded = number("downloaded_bytes")
    if total and downloaded is not None:
        return min(downloaded / total * 100, 100.0)
    count, index = number("fragment_count"), number("fragment_index")
    if count and index is not None:
        return min(index / count * 100, 100.0)
    return None


def _make_hook(
    store: JobStore,
    job_id: str,
    cancel_event: threading.Event,
    downloaded_paths: set[str],
    downloaded_outputs: set[str],
    allocator: "_IndexAllocator",
    track_pre_existing: bool = True,
):
    def hook(data: dict[str, Any]) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Cancelled by user")
        status = str(data.get("status") or "")
        filename = data.get("filename")
        if status == "downloading":
            if not filename:
                # A percentage with no file attached identifies nothing. The
                # allocator only knows a row once a path is known, so the
                # most recently allocated row belongs to the *previous*,
                # already-finished item whenever a new item is still
                # resolving its destination - writing here would stamp this
                # item's fractional progress onto a completed sibling
                # (upsert_item is a partial update, so that row would keep
                # stage="done" while its progress rewound). Real yt-dlp
                # always supplies `filename` on progress events, so this is
                # not a path production ever takes; drop the event.
                return
            progress = _download_progress(data)
            downloaded_paths.add(os.path.realpath(str(filename)))
            downloaded_outputs.add(_output_key(str(filename)))
            index = allocator.for_path(str(filename))
            store.upsert_item(
                job_id,
                index,
                title=_display_name(filename),
                progress=progress if progress is not None else 0.0,
                progress_known=progress is not None,
                stage="downloading",
            )
        elif status == "finished":
            path = str(filename) if filename and os.path.isfile(str(filename)) else None
            if path is None:
                # Same reasoning: a "finished" event with no usable path
                # (missing filename, or one that never landed on disk)
                # identifies no file and must not be written to any row. The
                # post-extract_info reconciliation loop below has
                # entry-derived identity and already covers entries that
                # produced no usable hook event.
                return
            assert_within_allowed_roots(path)
            index = allocator.for_path(path)
            fields: dict[str, Any] = {
                "title": _display_name(path),
                "path": path,
                "size": _file_size(path),
                "progress": 100.0,
                "stage": "done",
            }
            if track_pre_existing and not (
                os.path.realpath(path) in downloaded_paths
                or _output_key(path) in downloaded_outputs
            ):
                fields["error"] = _ALREADY_EXISTS_MESSAGE
            # Record durably now: if a later playlist entry fails,
            # extract_info raises before the post-loop below ever runs, and
            # this write is what keeps this item's result from being lost.
            store.upsert_item(job_id, index, **fields)

    return hook


def _remove_quietly(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        logger.warning("Could not remove %s", path)


def _finalise_transcode(
    source: str, scratch: str, stem: str, extension: str, keep_source: bool
) -> str:
    """Move a finished encode from its scratch name to its real one.

    Returns the path the output ended up at. Never overwrites an existing
    file other than `source` itself, and never removes `source` when
    `keep_source` is set.

    The source is only ever removed *after* the encode is safely in place. The
    ordering matters: unlinking it first meant that a failure in the move (or
    in the root check before it) left the caller unlinking the scratch file
    too, destroying both the original and its re-encode.
    """
    destination = f"{stem}.{extension}"
    replaces_source = os.path.exists(source) and os.path.realpath(
        destination
    ) == os.path.realpath(source)

    if replaces_source and keep_source:
        # The encode would land exactly on a file that must survive, so it
        # gets its own name instead.
        destination = unique_path(f"{stem}.transcoded.{extension}")
    elif not replaces_source:
        # Any pre-existing file at this name belongs to somebody else.
        destination = unique_path(destination)

    assert_within_allowed_roots(destination)
    # Same container and the source is being replaced: `os.replace` swaps the
    # encode in atomically, so the name never has to be freed up front and
    # there is no window where neither file exists.
    os.replace(scratch, destination)
    if not keep_source and not replaces_source:
        _remove_quietly(source)
    return destination


def _run_transcode_stage(
    store: JobStore, job: Job, cancel_event: threading.Event
) -> None:
    codec = str(job.options.get("codec") or "").lower()
    container = str(job.options.get("format") or "").lower()
    if container == _AUTO:
        # Mirrors `needs_transcode`, which already collapses codec "auto" and
        # "" to the same thing. Left as "auto" this becomes the destination's
        # file extension, ffmpeg cannot infer a muxer from it, and every
        # re-encode job fails - including the common case of picking a codec
        # in Advanced without touching the Format control at all.
        container = ""
    # A contradictory pairing (say codec FLAC with container M4A) is a
    # job-level property, not a per-item one, so it is settled here - before
    # the stage is entered and before any file is touched - rather than
    # discovered part-way through a playlist with some items already re-encoded.
    assert_container_supports_codec(container, codec)
    store.set_job_stage(job.id, "transcoding")

    current = store.get_job(job.id)
    if current is None:
        # Deleted while the download stage was running; nothing to transcode
        # and nowhere to record a result.
        return

    for item in current.items:
        if item.stage != "done" or not item.path:
            continue

        source = item.path
        # An item the download stage flagged as pre-existing was on disk
        # before this run and was never fetched. Removing it after the encode
        # would silently destroy a file the user already had - exactly what
        # the spec's "an existing file is never silently destroyed" forbids.
        keep_source = item.error == _ALREADY_EXISTS_MESSAGE
        stem, _source_ext = os.path.splitext(source)
        # The extension must describe the *codec being encoded*, never the
        # source file's. Falling back to the source extension is what muxed a
        # FLAC stream into an Ogg container still named `.opus`: legal for
        # ffmpeg, silent in every player. An explicit container survives
        # because it was already checked against the codec above.
        extension = container or container_for_codec(codec)

        # Encode to a scratch name first. Choosing the final name up front
        # cannot work: the natural destination `{stem}.{extension}` *is* the
        # source for a same-container re-encode, so `unique_path` would divert
        # to `Title (1).ext` every single time, having collided with nothing
        # but the file about to be replaced.
        scratch = unique_path(f"{stem}.transcoding.{extension}")
        assert_within_allowed_roots(scratch)

        store.upsert_item(job.id, item.index, stage="transcoding", progress=0.0)

        def report(percent: float, index: int = item.index) -> None:
            store.upsert_item(job.id, index, progress=percent)

        try:
            transcode_file(source, scratch, codec, cancel_event, report)
            destination = _finalise_transcode(
                source, scratch, stem, extension, keep_source
            )
        except Exception:
            # `transcode_file` already unlinks its own output on failure and
            # on cancellation; this also covers a failure in the rename step,
            # so a scratch file never outlives the stage that created it.
            _remove_quietly(scratch)
            raise

        store.upsert_item(
            job.id,
            item.index,
            path=destination,
            size=_file_size(destination),
            progress=100.0,
            stage="done",
            # The download stage's "already existed" note no longer describes
            # the current file, but the fact that the original was preserved
            # beside it does, and the user needs to know it is still there.
            error=_KEPT_SOURCE_MESSAGE if keep_source else None,
        )
