import asyncio
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import threading
from contextlib import asynccontextmanager
from email.utils import formatdate
from urllib.parse import unquote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.auth import (
    check_session,
    clear_session_cookie,
    create_session_cookie,
    verify_login,
)
from app.config import (
    ALLOWED_ORIGINS,
    AUTH_ENABLED,
    BASE_PATH_LABELS,
    BASE_PATHS,
    CUTTER_JOBS_DIR,
    CUTTER_MAX_DIRECT_REMUX_BYTES,
    ENABLED_FEATURES,
    ENABLED_FEATURES_SET,
    ENCODER_SETTLE_SECONDS,
    ENCODER_WATCH_PATHS,
    MUSIC_FOLDER_NAME,
    TRANSCRIBER_URL,
    TVSHOW_FOLDER_NAME,
    VALID_CUTTER_EXT,
    VALID_MUSIC_EXT,
    VALID_VIDEO_EXT,
    resolve_base,
)
from app.cutter import (
    cleanup_old_jobs,
    create_job,
    cut_file,
    decode_file_id,
    delete_job,
    encode_file_id,
    generate_thumbnail_strip,
    generate_thumbnail_strip_cached,
    generate_waveform,
    get_audio_track_preview,
    get_audio_transcode_status,
    get_ffmpeg_info,
    get_job_dir,
    get_job_meta_lock,
    get_preview_path_if_ready,
    get_preview_status,
    get_track_preview,
    get_track_remux,
    list_jobs,
    load_job_metadata,
    needs_transcoding,
    probe_file,
    save_job_metadata,
    start_background_audio_transcode,
    start_background_transcode,
    wait_for_audio_transcode,
)
from app.downloader.routes import (
    get_store as get_downloader_store,
)
from app.downloader.routes import (
    init_downloader,
    shutdown_downloader,
)
from app.downloader.routes import (
    router as downloader_router,
)
from app.encoder.routes import (
    register_error_handlers as register_encoder_error_handlers,
)
from app.encoder.routes import (
    router as encoder_router,
)
from app.get_dirs import (
    _get_all_dirs_cached,
    _get_cutter_dirs_cached,
    _get_download_dirs_cached,
    _get_music_dirs_cached,
)
from app.rename_episodes import rename_episodes
from app.rename_music import (
    LYRICS_ACTIONS,
    get_first_tag_value,
    load_audio_file,
    rename_music,
)
from app.transcribe_lyrics import (
    DEFAULT_DEMUCS_MODEL,
    DEFAULT_WHISPER_MODEL,
    VALID_DEMUCS_MODELS,
    VALID_WHISPER_MODELS,
    check_existing_lyrics,
    check_transcriber_health,
    get_file_lyrics_status,
    get_music_files,
    transcribe_file,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def require_feature(name: str):
    """Raise 404 if feature is not enabled."""
    if name not in ENABLED_FEATURES_SET:
        raise HTTPException(status_code=404, detail=f"Feature '{name}' is not enabled")


def validate_path(base: str, user_input: str) -> str:
    """Validate that resolved path stays within base directory."""
    resolved = os.path.realpath(os.path.join(base, user_input))
    base_resolved = os.path.realpath(base)
    if not resolved.startswith(base_resolved + os.sep) and resolved != base_resolved:
        raise HTTPException(status_code=400, detail="Invalid directory path")
    return resolved


def _clear_directory_caches() -> None:
    for cache in (
        _get_all_dirs_cached,
        _get_music_dirs_cached,
        _get_cutter_dirs_cached,
        _get_download_dirs_cached,
    ):
        cache.cache_clear()


class DirChangeHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            _clear_directory_caches()

    def on_deleted(self, event):
        if event.is_directory:
            _clear_directory_caches()

    def on_moved(self, event):
        if event.is_directory:
            _clear_directory_caches()


# Global observer instances
_observers: list = []


async def _cleanup_cutter_jobs():
    """Periodically delete expired jobs."""
    while True:
        await asyncio.sleep(600)  # every 10 minutes
        try:
            cleanup_old_jobs()
        except Exception:
            logger.exception("Error during cutter job cleanup")


async def _cleanup_downloader_jobs():
    """Periodically purge expired download jobs."""
    from app.config import DOWNLOADER_JOB_TTL

    while True:
        await asyncio.sleep(600)  # every 10 minutes
        try:
            get_downloader_store().purge_expired(DOWNLOADER_JOB_TTL)
        except Exception:
            logger.exception("Error during download job cleanup")


async def _cleanup_encoder_jobs(encoder_queue, encoder_store):
    """Periodically purge expired retained originals and encoder job history.

    Retentions run first: `run_retentions()` is what actually reclaims disk
    (purging a retained original once its TTL has elapsed), and
    `purge_expired()` deletes job rows -- including, as of the fix for the
    retained-original-leak finding, any original a deleted row was still
    pointing at. Running retentions first means a job whose original TTL and
    job TTL happen to land in the same sweep gets the "purge on schedule"
    behaviour rather than the "purge as a side effect of row deletion"
    fallback.
    """
    from app.config import ENCODER_JOB_TTL

    while True:
        await asyncio.sleep(600)  # every 10 minutes
        try:
            encoder_queue.run_retentions()
        except Exception:
            logger.exception("Error during encoder retention purge")
        try:
            deleted_ids = encoder_store.purge_expired_ids(ENCODER_JOB_TTL)
            if deleted_ids:
                from app.encoder import routes as encoder_routes

                events = encoder_routes.get_events()
                for job_id in deleted_ids:
                    events.publish({"type": "deleted", "job_id": job_id})
        except Exception:
            logger.exception("Error during encoder job cleanup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan with startup and shutdown events."""
    global _observers

    # Startup
    handler = DirChangeHandler()
    _observers = []
    for bp in BASE_PATHS:
        if os.path.isdir(bp):
            obs = Observer()
            obs.schedule(handler, bp, recursive=True)
            obs.start()
            _observers.append(obs)
            logger.info("Watching %s for filesystem changes", bp)
        else:
            logger.warning("Base path does not exist, skipping watch: %s", bp)

    # Start cutter upload cleanup task only if cutter feature is enabled
    cleanup_task = None
    if "cutter" in ENABLED_FEATURES_SET:
        try:
            os.makedirs(CUTTER_JOBS_DIR, exist_ok=True)
        except OSError as e:
            logger.error(
                "Cannot create cutter jobs directory %s: %s — cutter feature may fail",
                CUTTER_JOBS_DIR,
                e,
            )
        from app.cutter import migrate_jobs

        migrate_jobs()
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            logger.error("Cutter feature requires ffmpeg and ffprobe on PATH")
        else:
            import threading

            from app.hwaccel import _ensure_detected

            threading.Thread(target=_ensure_detected, daemon=True).start()
        cleanup_task = asyncio.create_task(_cleanup_cutter_jobs())

    # Start the downloader worker pool only if the download feature is enabled
    downloader_started = False
    downloader_cleanup_task = None
    if "download" in ENABLED_FEATURES_SET:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            logger.error(
                "Downloader requires ffmpeg and ffprobe on PATH; jobs will fail."
            )
        try:
            init_downloader()
        except Exception:
            # A downloader that cannot reach its data directory is a broken
            # downloader, not a dead application. An unwritable /downloads bind
            # mount or an unopenable SQLite file must not stop episodes, music
            # and cutter from serving — and an exception escaping here would
            # also skip the shutdown below, orphaning the watchdog observers
            # and the cutter cleanup task. Broad on purpose: sqlite3.Error is
            # not an OSError.
            logger.exception(
                "Downloader failed to start; the feature will be unavailable"
            )
            try:
                # Release anything the failed init had already built.
                shutdown_downloader()
            except Exception:
                logger.exception("Downloader cleanup after a failed start failed")
        else:
            downloader_started = True
            downloader_cleanup_task = asyncio.create_task(_cleanup_downloader_jobs())

    # Start the encoder's queue and watcher only if the encoder feature is
    # enabled. The panel and routes stay dark without it (see the
    # conditional include_router below); this starts the background work
    # that backs those routes.
    encoder_runtime = None
    encoder_queue = None
    encoder_cleanup_task = None
    if "encoder" in ENABLED_FEATURES_SET:
        from app.encoder import routes as encoder_routes
        from app.encoder.runtime import EncoderRuntime
        from app.encoder.swap import sweep_orphans

        try:
            encoder_store = encoder_routes.get_store()
            encoder_queue = encoder_routes.get_queue()
            # Sweep before recovering: a crash mid-encode leaves partials no
            # live job owns, and they are invisible to the user because the
            # name is dotted.
            #
            # `.hbenc-<id>` filenames carry the *encoder service's* job id
            # (swap.py's OUTPUT_PREFIX docstring), stored on each row as
            # `remote_job_id` -- not `j.id`, which is this renamer's own
            # local uuid4. The two namespaces never intersect, so passing
            # `j.id` here would make every live partial look orphaned and
            # delete it on every restart, wiping out in-progress GPU work.
            encoder_runtime = EncoderRuntime(
                encoder_store,
                encoder_queue,
                default_paths=ENCODER_WATCH_PATHS,
                settle_seconds=ENCODER_SETTLE_SECONDS,
                valid_extensions=VALID_VIDEO_EXT,
                validate_paths=encoder_routes._validate_watch_paths,
            )
            active = {
                j.remote_job_id for j in encoder_store.list_jobs() if j.remote_job_id
            }
            for watch_path in encoder_runtime.watch_paths:
                sweep_orphans(watch_path, active)
            encoder_queue.start()
            encoder_queue.recover()
            encoder_runtime.start()
            encoder_routes.register_runtime(encoder_runtime)
            # Mirrors the downloader's cleanup task: without it, nothing ever
            # calls run_retentions() or purge_expired() in production, so
            # ENCODER_ORIGINAL_TTL and ENCODER_JOB_TTL are documented but
            # inert -- retained originals and job history accumulate
            # unbounded.
            encoder_cleanup_task = asyncio.create_task(
                _cleanup_encoder_jobs(encoder_queue, encoder_store)
            )
        except Exception as exc:
            # This whole block runs before `try: yield` below, so a failure
            # here (a bad ENCODER_DB path, a permissions error in
            # sweep_orphans) would otherwise abort the lifespan generator
            # before the `finally` that stops the queue/watcher is ever
            # entered -- leaking the dispatch worker thread `encoder_queue
            # .start()` may have already spawned. Mirrors the downloader's
            # containment immediately above: log it, tear down whatever
            # partially started, and let the rest of the app come up
            # normally rather than take every feature down with it.
            logger.exception("Encoder failed to start; the feature will be unavailable")
            if encoder_runtime is not None:
                try:
                    encoder_runtime.stop()
                except Exception:
                    logger.exception(
                        "Encoder watcher cleanup after a failed start failed"
                    )
                encoder_runtime = None
            if encoder_queue is not None:
                try:
                    encoder_queue.stop()
                except Exception:
                    logger.exception(
                        "Encoder queue cleanup after a failed start failed"
                    )
                encoder_queue = None
            # Discard the module singletons and make get_store()/get_queue()
            # fail loud from here on. Without this, a route handler calling
            # get_queue() would just lazily rebuild a fresh (never-started)
            # queue -- approve() would then report "queued" for a job that
            # is, silently and permanently, never going to be dispatched.
            encoder_routes.mark_startup_failed(str(exc))

    try:
        yield
    finally:
        # Shutdown. In a finally block so worker threads and the SQLite
        # connection are released even if the application errors out.
        if cleanup_task is not None:
            cleanup_task.cancel()
        if downloader_cleanup_task is not None:
            downloader_cleanup_task.cancel()
        if downloader_started:
            shutdown_downloader()
        if "encoder" in ENABLED_FEATURES_SET:
            if encoder_cleanup_task is not None:
                encoder_cleanup_task.cancel()
            if encoder_runtime is not None:
                try:
                    encoder_runtime.stop()
                except Exception:
                    logger.exception("Encoder runtime cleanup failed during shutdown")
            if encoder_queue is not None:
                try:
                    encoder_queue.stop()
                except Exception:
                    logger.exception("Encoder queue cleanup failed during shutdown")
        _stop_observers()


def _stop_observers():
    for obs in _observers:
        obs.stop()
    for obs in _observers:
        obs.join()
    if _observers:
        logger.info("File watchers stopped.")


app = FastAPI(lifespan=lifespan)

# Conditional registration is the gate: with the feature off the downloader
# routes do not exist at all, which is stronger than a per-route feature check.
if "download" in ENABLED_FEATURES_SET:
    app.include_router(downloader_router)

if "encoder" in ENABLED_FEATURES_SET:
    app.include_router(encoder_router)
    register_encoder_error_handlers(app)

_AUTH_EXEMPT_EXACT = {"/health", "/openapi.json"}
_AUTH_EXEMPT_PREFIXES = ("/auth/", "/docs", "/redoc")


class AuthMiddleware:
    """Pure ASGI middleware - avoids BaseHTTPMiddleware's streaming/SSE buffering issues."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not AUTH_ENABLED:
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path in _AUTH_EXEMPT_EXACT or any(
            path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if not check_session(request):
            response = JSONResponse(
                {"detail": "Authentication required"}, status_code=401
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# Auth added FIRST (inner), CORS added SECOND (outer) - LIFO means CORS runs first
app.add_middleware(AuthMiddleware)
_cors_credentials = AUTH_ENABLED and "*" not in ALLOWED_ORIGINS
if not _cors_credentials and AUTH_ENABLED:
    logger.warning(
        "ALLOWED_ORIGINS contains '*' but AUTH is enabled - credentials cannot be sent. "
        "Set explicit origins in ALLOWED_ORIGINS for auth to work correctly."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=_cors_credentials,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-File-Name"],
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "transcriber": bool(TRANSCRIBER_URL),
        "features": ENABLED_FEATURES,
        "base_paths": list(BASE_PATH_LABELS.keys()),
    }


@app.get("/config")
def get_config():
    return {
        "features": ENABLED_FEATURES,
        "base_paths": list(BASE_PATH_LABELS.keys()),
    }


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
async def auth_login(body: LoginRequest, request: Request):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Authentication is not enabled")
    if not verify_login(body.username, body.password):
        await asyncio.sleep(1)  # rate-limit brute-force attempts
        raise HTTPException(status_code=401, detail="Invalid credentials")
    response = JSONResponse({"ok": True})
    x_forwarded_proto = request.headers.get("x-forwarded-proto")
    if x_forwarded_proto:
        x_forwarded_proto = x_forwarded_proto.split(",")[0].strip().lower()
    is_https = request.url.scheme == "https" or x_forwarded_proto == "https"
    create_session_cookie(response, secure=is_https)
    return response


@app.post("/auth/logout")
def auth_logout():
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@app.get("/auth/status")
def auth_status(request: Request):
    return {
        "auth_enabled": AUTH_ENABLED,
        "authenticated": not AUTH_ENABLED or check_session(request),
    }


@app.get("/directories/tvshows")
def list_directories(
    series: str | None = Query(None, description="Series filter", max_length=200),
    season: int | None = Query(None, description="Season number", ge=0, le=100),
):
    require_feature("episodes")
    all_dirs = _get_all_dirs_cached()

    # nach Serie filtern
    filtered = all_dirs
    if series:
        series_lc = series.lower()
        filtered = [d for d in filtered if series_lc in d["path"].lower()]

    # nach Staffel filtern
    if season is not None:
        season_str = f"{season:02d}"
        pattern = f"/season {season_str}"
        filtered = [d for d in filtered if d["path"].lower().endswith(pattern)]

    return {"directories": filtered}


@app.get("/directories/music")
def list_music_directories(
    artist: str | None = Query(None, description="Artist filter", max_length=200),
    album: str | None = Query(None, description="Album filter", max_length=200),
):
    if "music" not in ENABLED_FEATURES_SET and "lyrics" not in ENABLED_FEATURES_SET:
        raise HTTPException(status_code=404, detail="No music features enabled")
    all_dirs = _get_music_dirs_cached()

    filtered = all_dirs
    if artist:
        artist_lc = artist.lower()
        filtered = [d for d in filtered if artist_lc in d["path"].lower()]

    if album:
        album_lc = album.lower()
        result = []
        for d in filtered:
            parts = d["path"].split("/")
            if len(parts) >= 2:
                rest_path = "/".join(parts[1:]).lower()
                if album_lc in rest_path:
                    result.append(d)
        filtered = result

    return {"directories": filtered}


@app.post("/directories/refresh")
def refresh_directories():
    _clear_directory_caches()
    logger.info("Directory cache refreshed manually.")
    return {"status": "ok"}


@app.get("/directories/media")
def list_media_directories(
    search: str | None = Query(None, description="Text filter", max_length=200),
):
    require_feature("cutter")
    all_dirs = _get_cutter_dirs_cached()

    filtered = all_dirs
    if search:
        search_lc = search.lower()
        filtered = [d for d in filtered if search_lc in d["path"].lower()]

    return {"directories": filtered}


@app.get("/directories/download")
def list_download_directories(
    search: str | None = Query(None, description="Text filter", max_length=200),
):
    require_feature("download")
    all_dirs = _get_download_dirs_cached()

    filtered = all_dirs
    if search:
        search_lc = search.lower()
        filtered = [d for d in all_dirs if search_lc in d["path"].lower()]

    return {"directories": filtered}


@app.post("/rename/episodes")
async def rename(
    series: str = Form(..., max_length=200),
    season: int = Form(..., ge=0, le=100),
    directory: str = Form(..., max_length=500),
    dry_run: bool = Form(...),
    assign_seq: bool = Form(...),
    threshold: float = Form(..., ge=0.0, le=1.0),
    lang: str = Form(..., max_length=5),
    base: str = Form(..., max_length=200),
):
    require_feature("episodes")
    try:
        base_path = resolve_base(base)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown base: '{base}'")
    tvshow_base = os.path.join(base_path, TVSHOW_FOLDER_NAME)
    path = validate_path(tvshow_base, directory)
    if not os.path.isdir(path):
        return {
            "success": False,
            "error": "Directory not found",
            "log": [],
            "directories": _get_all_dirs_cached(),
        }

    logger.info(
        "Renaming episodes: series=%s, season=%d, dir=%s, dry_run=%s",
        series,
        season,
        directory,
        dry_run,
    )

    logs, error = rename_episodes(
        series=series,
        season=season,
        directory=path,
        lang=lang,
        dry_run=dry_run,
        threshold=threshold,
        assign_seq=assign_seq,
    )

    if error:
        logger.error("Episode rename failed: %s", error)

    return {
        "success": error is None,
        "error": error,
        "log": logs,
        "directories": _get_all_dirs_cached(),
    }


@app.post("/rename/music")
async def rename_music_route(
    directory: str = Form(..., max_length=500),
    dry_run: bool = Form(...),
    base: str = Form(..., max_length=200),
    lyrics_action: str = Form("rename", max_length=10),
):
    require_feature("music")
    if lyrics_action not in LYRICS_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid lyrics action '{lyrics_action}'. Must be one of: "
                + ", ".join(sorted(LYRICS_ACTIONS))
            ),
        )
    try:
        base_path = resolve_base(base)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown base: '{base}'")
    music_base = os.path.join(base_path, MUSIC_FOLDER_NAME)
    path = validate_path(music_base, directory)
    if not os.path.isdir(path):
        return {
            "success": False,
            "error": "Directory not found",
            "log": [],
            "directories": _get_music_dirs_cached(),
        }

    logger.info(
        "Renaming music: dir=%s, dry_run=%s, lyrics_action=%s",
        directory,
        dry_run,
        lyrics_action,
    )

    logs, error = rename_music(
        directory=path, dry_run=dry_run, lyrics_action=lyrics_action
    )

    if error:
        logger.error("Music rename failed: %s", error)

    return {
        "success": error is None,
        "error": error,
        "log": logs,
        "directories": _get_music_dirs_cached(),
    }


# ── Transcriber Endpoints ─────────────────────────────────────────────────


@app.get("/transcribe/health")
def transcriber_health():
    require_feature("lyrics")
    if not TRANSCRIBER_URL:
        return {"status": "not_configured", "error": "TRANSCRIBER_URL not set"}
    return check_transcriber_health(TRANSCRIBER_URL)


@app.get("/transcribe/files")
def list_transcribable_files(
    directory: str = Query(..., max_length=500),
    base: str = Query(..., max_length=200),
):
    require_feature("lyrics")
    try:
        base_path = resolve_base(base)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown base: '{base}'")
    music_base = os.path.join(base_path, MUSIC_FOLDER_NAME)
    path = validate_path(music_base, directory)
    if not os.path.isdir(path):
        return {"files": [], "error": "Directory not found"}
    music_files = get_music_files(path, VALID_MUSIC_EXT)
    return {"files": [get_file_lyrics_status(f) for f in music_files]}


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def _transcription_error_stream(message: str) -> StreamingResponse:
    """Report a start-up failure over SSE.

    The client consumes this endpoint purely as an event stream, so a plain JSON
    body would look like a stream that ended without a `done` event and leave the
    UI spinning forever.
    """

    def gen():
        yield f"event: error_msg\ndata: {message}\n\n"
        yield "event: done\ndata: Completed: 0/0, Errors: 1\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers=_sse_headers()
    )


@app.post("/transcribe/start")
def start_transcription(
    directory: str = Form(..., max_length=500),
    files: str = Form("", max_length=10000),  # JSON array of filenames
    output_format: str = Form("lrc", max_length=5),
    skip_existing: bool = Form(True),
    language: str = Form("", max_length=10),
    no_separation: bool = Form(False),
    no_correction: bool = Form(False),
    base: str = Form(..., max_length=200),
    demucs_model: str = Form(DEFAULT_DEMUCS_MODEL, max_length=50),
    whisper_model: str = Form(DEFAULT_WHISPER_MODEL, max_length=50),
    # Genius lookup overrides — only honoured for a single-file batch, since
    # one artist/title pair cannot describe several songs.
    artist_override: str = Form("", max_length=200),
    title_override: str = Form("", max_length=200),
):
    require_feature("lyrics")
    if output_format not in ("lrc", "txt", "all"):
        raise HTTPException(
            status_code=422,
            detail="Invalid output format. Must be 'lrc', 'txt', or 'all'.",
        )
    if whisper_model not in VALID_WHISPER_MODELS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid whisper model '{whisper_model}'. Must be one of: "
                + ", ".join(sorted(VALID_WHISPER_MODELS))
            ),
        )
    if demucs_model not in VALID_DEMUCS_MODELS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid demucs model '{demucs_model}'. Must be one of: "
                + ", ".join(sorted(VALID_DEMUCS_MODELS))
            ),
        )
    if not TRANSCRIBER_URL:
        return _transcription_error_stream("TRANSCRIBER_URL not set")

    try:
        base_path = resolve_base(base)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown base: '{base}'")
    music_base = os.path.join(base_path, MUSIC_FOLDER_NAME)
    path = validate_path(music_base, directory)
    if not os.path.isdir(path):
        return _transcription_error_stream("Directory not found")

    if files:
        # A JSON array is used instead of a delimited string because filenames
        # may contain commas (e.g. "01-13 Illegal, legal, egal....flac").
        try:
            requested = json.loads(files)
            if not isinstance(requested, list) or not all(
                isinstance(f, str) for f in requested
            ):
                raise ValueError("expected a JSON array of filenames")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(
                status_code=422, detail=f"Invalid 'files' parameter: {e}"
            )
        # Sanitize filenames to basenames only (prevent path traversal via filenames)
        selected = [os.path.join(path, os.path.basename(f)) for f in requested]
        selected = [f for f in selected if os.path.isfile(f)]
    else:
        selected = get_music_files(path, VALID_MUSIC_EXT)

    if not selected:
        return _transcription_error_stream("No music files found")

    # Overrides describe one specific song, so they only apply to a single-file
    # batch — silently reusing them across a whole album would mislabel every
    # Genius lookup after the first.
    single_file = len(selected) == 1
    use_artist_override = artist_override.strip() if single_file else ""
    use_title_override = title_override.strip() if single_file else ""

    logger.info(
        "Starting transcription: dir=%s, files=%d, format=%s, model=%s, demucs=%s",
        directory,
        len(selected),
        output_format,
        whisper_model,
        demucs_model,
    )

    msg_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    cancel_event = threading.Event()

    def run_batch():
        total = len(selected)
        completed = 0
        errors = 0

        for i, filepath in enumerate(selected, 1):
            if cancel_event.is_set():
                msg_queue.put(("done", f"Cancelled. Completed: {completed}/{total}"))
                return

            filename = os.path.basename(filepath)

            if i > 1:
                msg_queue.put(("progress", ""))  # blank line between songs

            # Check if lyrics already exist
            if skip_existing:
                effective_format = check_existing_lyrics(filepath, output_format)
                if effective_format is None:
                    msg_queue.put(
                        ("progress", f"[SKIP]\t\t\t{filename} - lyrics already exist")
                    )
                    continue
            else:
                effective_format = output_format

            # Extract metadata for Genius correction; explicit overrides win.
            file_artist = None
            file_title = None
            if not no_correction:
                audio = load_audio_file(filepath)
                if audio:
                    file_artist = get_first_tag_value(audio, "artist")
                    file_title = get_first_tag_value(audio, "title")
                file_artist = use_artist_override or file_artist
                file_title = use_title_override or file_title

            def progress_cb(msg):
                msg_queue.put(("progress", msg))

            _logs, error = transcribe_file(
                filepath=filepath,
                transcriber_url=TRANSCRIBER_URL,
                output_format=effective_format,
                no_separation=no_separation,
                demucs_model=demucs_model,
                whisper_model=whisper_model,
                language=language or None,
                artist=file_artist,
                title=file_title,
                no_correction=no_correction,
                progress_callback=progress_cb,
            )

            if error:
                errors += 1
                msg_queue.put(("error_msg", error))
            else:
                completed += 1

        summary = f"Completed: {completed}/{total}"
        if errors:
            summary += f", Errors: {errors}"
        msg_queue.put(("done", summary))

    thread = threading.Thread(target=run_batch, daemon=True)
    thread.start()

    def event_generator():
        try:
            while True:
                try:
                    event_type, data = msg_queue.get(timeout=30)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                    if event_type == "done":
                        break
                except queue.Empty:
                    # Heartbeat to keep connection alive
                    yield "event: progress\ndata: heartbeat\n\n"
        finally:
            cancel_event.set()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


# ── Cutter Endpoints ────────────────────────────────────────────────────
# Workflow overview:
# 1) Probe/waveform/thumbnail endpoints inspect a selected source file.
# 2) /cutter/stream serves either original media or a browser-safe preview.
# 3) /cutter/cut starts an SSE-driven ffmpeg cut operation into job output.
# 4) Job metadata tracks readiness, errors, and downloadable outputs.


def resolve_cutter_path(
    path: str, source: str, job_id: str = "", base_label: str = ""
) -> str:
    """Resolve and validate a cutter file path based on source type."""
    if source == "server":
        try:
            base_path = resolve_base(base_label)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown base: '{base_label}'")
        return validate_path(base_path, path)
    elif source == "upload":
        if not job_id:
            raise HTTPException(
                status_code=400, detail="job_id required for upload source"
            )
        job_dir = get_job_dir(job_id)
        input_dir = os.path.join(job_dir, "input")
        return validate_path(input_dir, path)
    else:
        raise HTTPException(status_code=400, detail=f"Invalid source: '{source}'")


# Content-type mapping for common media extensions
_MEDIA_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".aiff": "audio/aiff",
    ".ac3": "audio/ac3",
    ".dts": "audio/vnd.dts",
}


@app.get("/cutter/status")
def cutter_status() -> dict:
    """Report the ffmpeg build backing the cutter (mirrors /download/status)."""
    require_feature("cutter")
    info = get_ffmpeg_info()
    return {
        "ffmpeg_available": info["available"],
        "ffmpeg_version": info["version"],
        "ffmpeg_build": info["build"],
        "ffmpeg_path": info["path"],
    }


@app.get("/cutter/files")
def list_cutter_files(
    directory: str = Query(..., max_length=500),
    base: str = Query(..., max_length=200),
):
    require_feature("cutter")
    try:
        base_path = resolve_base(base)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown base: '{base}'")
    path = validate_path(base_path, directory)
    if not os.path.isdir(path):
        return {"files": []}

    files = []
    for entry in os.scandir(path):
        try:
            if not entry.is_file():
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in VALID_CUTTER_EXT:
                rel_path = os.path.join(directory, entry.name)
                files.append(
                    {
                        "name": entry.name,
                        "size": entry.stat().st_size,
                        "extension": ext,
                        "file_id": encode_file_id(
                            "server", rel_path, job_id="", base=base
                        ),
                    }
                )
        except OSError:
            # File may disappear/change between scandir and stat on network shares.
            continue
    files.sort(key=lambda f: f["name"].lower())
    return {"files": files}


@app.get("/cutter/probe")
def cutter_probe(
    path: str = Query(..., max_length=500),
    source: str = Query(..., max_length=10),
    job_id: str = Query("", max_length=50),
    base: str = Query("", max_length=200),
):
    require_feature("cutter")
    resolved = resolve_cutter_path(path, source, job_id, base_label=base)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        info = probe_file(resolved)
        info["needs_transcoding"] = needs_transcoding(
            info.get("audio_codec", "unknown"),
            resolved,
            info.get("video_codec", "") or "",
        )

        if job_id:
            with get_job_meta_lock(job_id):
                meta = load_job_metadata(job_id)
                if meta:
                    meta["browser_ready"] = not info["needs_transcoding"]
                    save_job_metadata(job_id, meta)

        return info
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/cutter/waveform")
def cutter_waveform(
    path: str = Query(..., max_length=500),
    source: str = Query(..., max_length=10),
    peaks: int = Query(2000, ge=100, le=10000),
    job_id: str = Query("", max_length=50),
    base: str = Query("", max_length=200),
):
    require_feature("cutter")
    resolved = resolve_cutter_path(path, source, job_id, base_label=base)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        return {"peaks": generate_waveform(resolved, num_peaks=peaks)}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/cutter/thumbnails")
def cutter_thumbnails(
    path: str = Query(..., max_length=500),
    source: str = Query(..., max_length=10),
    count: int = Query(30, ge=5, le=50),
    job_id: str = Query("", max_length=50),
    base: str = Query("", max_length=200),
):
    require_feature("cutter")
    from fastapi.responses import Response

    resolved = resolve_cutter_path(path, source, job_id, base_label=base)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        if job_id:
            try:
                jpeg_bytes = generate_thumbnail_strip_cached(
                    resolved, count=count, job_id=job_id
                )
            except ValueError:
                # Optional cache only; malformed/unknown job ids still get a thumbnail.
                jpeg_bytes = generate_thumbnail_strip(resolved, count=count)
        else:
            jpeg_bytes = generate_thumbnail_strip(resolved, count=count)
        return Response(content=jpeg_bytes, media_type="image/jpeg")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/cutter/stream/{file_id}")
def cutter_stream(
    file_id: str,
    request: Request,
    audio_stream: int | None = Query(None),
    transcode: bool = Query(False),
    audio_only: bool = Query(False),
    transcode_audio_only: bool = Query(False),
):
    require_feature("cutter")
    try:
        source, job_id, base, path = decode_file_id(file_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    resolved = resolve_cutter_path(path, source, job_id, base_label=base)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")

    # Check if transcoding is needed
    try:
        probe = probe_file(resolved)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    if audio_stream is not None:
        audio_indexes = {s.get("index") for s in probe.get("audio_streams", [])}
        if audio_stream not in audio_indexes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid audio stream index {audio_stream}. "
                    f"Available indexes: {sorted(audio_indexes)}"
                ),
            )

    needs_tx = needs_transcoding(
        probe.get("audio_codec", "unknown"),
        resolved,
        probe.get("video_codec", "") or "",
    )
    source_file_size = os.path.getsize(resolved)
    audio_streams = probe.get("audio_streams", [])
    default_audio_index = audio_streams[0].get("index") if audio_streams else None

    # Reject conflicting parameters
    if transcode_audio_only and (transcode or audio_only):
        raise HTTPException(
            status_code=400,
            detail="transcode_audio_only cannot be combined with transcode or audio_only",
        )

    if transcode_audio_only:
        if audio_stream is None:
            raise HTTPException(
                status_code=400,
                detail="audio_stream required for audio-only transcode",
            )
        if not job_id:
            raise HTTPException(
                status_code=400,
                detail="job_id required for audio-only transcode",
            )
        start_background_audio_transcode(resolved, audio_stream, job_id)

        # Non-blocking: if the file is already ready, use it; otherwise
        # return 409 so the client can poll /cutter/preview-status and retry.
        audio_path = wait_for_audio_transcode(resolved, job_id, audio_stream, timeout=0)
        if not audio_path:
            status = get_audio_transcode_status(resolved, job_id, audio_stream)
            if status.get("state") == "error":
                logger.error(
                    "Stream: audio-only transcode error state: %s",
                    status.get("message"),
                )
                raise HTTPException(
                    status_code=500,
                    detail=status.get("message") or "Audio-only transcode failed",
                )
            raise HTTPException(
                status_code=409,
                detail=(
                    "Audio preview not ready yet - poll "
                    f"/cutter/preview-status/{file_id}"
                    f"?audio_transcode_stream={audio_stream} and retry"
                ),
            )
        resolved = audio_path
    elif transcode and needs_tx:
        if not job_id:
            raise HTTPException(
                status_code=400, detail="job_id required for transcoded preview"
            )

        start_background_transcode(resolved, job_id)
        status = get_preview_status(resolved, job_id)
        if status.get("state") == "error":
            logger.error(
                "Stream: cached preview error state: %s", status.get("message")
            )
            raise HTTPException(
                status_code=500,
                detail=status.get("message") or "Preview transcode failed",
            )

        # Serve the master preview if ready; otherwise return 409 so the
        # frontend can poll /preview-status and retry when ready.
        master_path = get_preview_path_if_ready(resolved, job_id)
        if not master_path:
            raise HTTPException(
                status_code=409,
                detail="Preview not ready yet - poll /cutter/preview-status and retry",
            )
        # If a specific audio track is requested, extract it from the master
        if audio_stream is not None:
            try:
                if audio_only:
                    logger.info(
                        "Stream: extracting audio-only track %d from master",
                        audio_stream,
                    )
                    resolved = get_audio_track_preview(
                        master_path, audio_stream, resolved, job_id
                    )
                else:
                    logger.info(
                        "Stream: extracting full track %d from master %s",
                        audio_stream,
                        master_path,
                    )
                    resolved = get_track_preview(
                        master_path, audio_stream, resolved, job_id
                    )
            except RuntimeError as e:
                logger.error("Stream: track extraction failed: %s", e)
                raise HTTPException(status_code=500, detail=str(e))
        else:
            resolved = master_path
    elif (
        audio_stream is not None
        and job_id
        and not needs_tx
        and audio_stream != default_audio_index
        and source_file_size <= CUTTER_MAX_DIRECT_REMUX_BYTES
    ):
        # Only remux for browser-native containers (MP4, WebM, etc.) where
        # non-default track isolation is meaningful and expected to be quick.
        # Files that need transcoding (MKV, AVI, etc.) or large originals are
        # served raw so preview startup doesn't block on a full-file remux.
        try:
            resolved = get_track_remux(resolved, audio_stream, job_id)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Serve raw file with HTTP Range support
    stat_result = os.stat(resolved)
    file_size = stat_result.st_size
    ext = os.path.splitext(resolved)[1].lower()
    content_type = _MEDIA_CONTENT_TYPES.get(
        ext,
        mimetypes.guess_type(resolved)[0] or "application/octet-stream",
    )

    # Without validators a browser cannot reuse a single byte it has already
    # fetched, so every re-buffer and every scrub re-reads the whole range from
    # disk -- expensive when the source lives on a network share. A preview file
    # is immutable for a given (mtime, size), so it is safe to let the browser
    # cache ranges and revalidate cheaply.
    etag = f'"{int(stat_result.st_mtime)}-{file_size}"'
    cache_headers = {
        "ETag": etag,
        "Last-Modified": formatdate(stat_result.st_mtime, usegmt=True),
        "Cache-Control": "private, max-age=3600",
    }

    range_header = request.headers.get("range")
    if not range_header and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)
    if range_header:
        # Parse Range: bytes=X-Y
        _range_re = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not _range_re:
            raise HTTPException(status_code=416, detail="Malformed Range header")
        try:
            start = int(_range_re.group(1)) if _range_re.group(1) else 0
            end = int(_range_re.group(2)) if _range_re.group(2) else file_size - 1
            end = min(end, file_size - 1)
        except ValueError:
            raise HTTPException(status_code=416, detail="Malformed Range header")

        if start >= file_size or start > end:
            raise HTTPException(status_code=416, detail="Range not satisfiable")

        content_length = end - start + 1

        def range_generator():
            with open(resolved, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            range_generator(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(content_length),
                "Accept-Ranges": "bytes",
                **cache_headers,
            },
        )

    # Full file response
    def file_generator():
        with open(resolved, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        file_generator(),
        media_type=content_type,
        headers={
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
            **cache_headers,
        },
    )


@app.get("/cutter/preview-status/{file_id}")
def cutter_preview_status(
    file_id: str,
    audio_transcode_stream: int | None = Query(None),
):
    require_feature("cutter")
    try:
        source, job_id, base, path = decode_file_id(file_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    resolved = resolve_cutter_path(path, source, job_id, base_label=base)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")

    # Audio-only transcode status - uses separate key, bypasses master preview check.
    # Also kicks off the transcode if not already started, so polling alone is
    # sufficient to start the work (matches master preview behavior).
    if audio_transcode_stream is not None:
        if not job_id:
            raise HTTPException(
                status_code=400,
                detail="job_id required for audio transcode status",
            )
        try:
            info = probe_file(resolved)
            stream_indices = {s["index"] for s in info.get("audio_streams", [])}
            if audio_transcode_stream not in stream_indices:
                raise HTTPException(
                    status_code=400,
                    detail=f"Audio stream index {audio_transcode_stream} not found. "
                    f"Available: {sorted(stream_indices)}",
                )
        except RuntimeError:
            pass  # Probe failed - let the transcode attempt handle it
        start_background_audio_transcode(resolved, audio_transcode_stream, job_id)
        return get_audio_transcode_status(resolved, job_id, audio_transcode_stream)

    def _done_status() -> dict:
        return {
            "state": "done",
            "ready": True,
            "percent": 100.0,
            "eta_seconds": 0.0,
            "elapsed_seconds": 0.0,
            "message": "",
        }

    # Fast path for active jobs: avoid expensive ffprobe on every poll.
    if job_id:
        status = get_preview_status(resolved, job_id)
        if status.get("state") in {"running", "error"}:
            return status
        if status.get("state") == "done" and status.get("ready"):
            return status

        meta = load_job_metadata(job_id)
        if meta and (meta.get("browser_ready") or meta.get("preview_transcoded")):
            return _done_status()

    try:
        probe = probe_file(resolved)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not needs_transcoding(
        probe.get("audio_codec", "unknown"),
        resolved,
        probe.get("video_codec", "") or "",
    ):
        return _done_status()

    if not job_id:
        raise HTTPException(
            status_code=400, detail="job_id required for transcoded preview"
        )

    start_background_transcode(resolved, job_id)
    return get_preview_status(resolved, job_id)


@app.post("/cutter/upload")
async def cutter_upload(request: Request):
    require_feature("cutter")

    content_length = request.headers.get("content-length")
    max_upload_size = 50 * 1024 * 1024 * 1024  # 50 GB
    if content_length:
        try:
            if int(content_length) > max_upload_size:
                raise HTTPException(
                    status_code=413, detail="File exceeds 50 GB size limit"
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")

    raw_name = request.headers.get("x-file-name") or "unnamed"
    raw_name = unquote(raw_name)

    # Sanitize filename: strip path components and non-printable characters
    filename = os.path.basename(raw_name)
    filename = "".join(c for c in filename if c.isprintable())
    if not filename or filename in (".", ".."):
        filename = "unnamed"

    # Validate file extension
    ext = os.path.splitext(filename)[1].lower()
    if ext not in VALID_CUTTER_EXT:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid file extension '{ext}'. Allowed: {', '.join(sorted(VALID_CUTTER_EXT))}",
        )

    # Create a job for this upload - mark as uploading until the stream completes
    job_id = create_job("upload", "", filename, initial_status="uploading")
    job_dir = get_job_dir(job_id)
    input_dir = os.path.join(job_dir, "input")
    dest = os.path.join(input_dir, filename)

    try:
        bytes_written = 0
        with open(  # noqa: ASYNC230 - streamed upload is bounded per chunk
            dest, "wb"
        ) as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                bytes_written += len(chunk)
                if bytes_written > max_upload_size:
                    raise HTTPException(
                        status_code=413, detail="File exceeds 50 GB size limit"
                    )
                f.write(chunk)
        if bytes_written == 0:
            raise HTTPException(status_code=422, detail="No file data received")
    except HTTPException:
        delete_job(job_id)
        raise
    except (
        Exception  # noqa: BLE001 - convert all upload failures to one API envelope
    ) as e:
        delete_job(job_id)
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    with get_job_meta_lock(job_id):
        meta = load_job_metadata(job_id)
        if meta:
            meta["status"] = "ready"
            save_job_metadata(job_id, meta)

    return {
        "job_id": job_id,
        "file_id": encode_file_id("upload", filename, job_id, base=""),
        "filename": filename,
    }


@app.post("/cutter/jobs")
def cutter_create_job(
    path: str = Form(..., max_length=500),
    source: str = Form("server", max_length=10),
    base: str = Form("", max_length=200),
):
    """Create a job for a server-side file (no file copy, metadata only)."""
    require_feature("cutter")
    resolved = resolve_cutter_path(path, source, base_label=base)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    filename = os.path.basename(resolved)
    job_id = create_job(source, path, filename, base=base)

    try:
        probe = probe_file(resolved)
        browser_ready = not needs_transcoding(
            probe.get("audio_codec", "unknown"),
            resolved,
            probe.get("video_codec", "") or "",
        )
        with get_job_meta_lock(job_id):
            meta = load_job_metadata(job_id)
            if meta:
                meta["browser_ready"] = browser_ready
                save_job_metadata(job_id, meta)
    except RuntimeError:
        logger.warning("Could not evaluate browser compatibility for job %s", job_id)

    return {
        "job_id": job_id,
        "file_id": encode_file_id(source, path, job_id, base=base),
    }


def _add_source_file_id(meta: dict) -> dict:
    """Attach the signed file id the player needs to stream a job's source.

    Both the list and the single-job endpoint must do this: the panel reopens
    a job straight from the list, and without this id it builds an empty
    stream URL, so the browser receives a 404 body instead of media and
    reports a format error for every file regardless of codec.
    """
    job_id = meta.get("job_id", "")
    if meta.get("source") == "server" and meta.get("original_path"):
        base_label = meta.get("base") or ""
        if not base_label:
            # Infer base label from original_path for jobs missing it.
            original_path = meta["original_path"]
            if os.path.isabs(original_path):
                candidate = os.path.realpath(original_path)
                for label, root in BASE_PATH_LABELS.items():
                    root_abs = os.path.realpath(root)
                    if candidate == root_abs or candidate.startswith(root_abs + os.sep):
                        base_label = label
                        break
            else:
                # Relative path: pick the first root where the file exists
                for label, root in BASE_PATH_LABELS.items():
                    root_abs = os.path.realpath(root)
                    candidate = os.path.realpath(os.path.join(root_abs, original_path))
                    if os.path.exists(candidate):
                        base_label = label
                        break
        if base_label:
            meta["source_file_id"] = encode_file_id(
                "server", meta["original_path"], job_id=job_id, base=base_label
            )
    elif meta.get("source") == "upload" and meta.get("original_name"):
        meta["source_file_id"] = encode_file_id(
            "upload", meta["original_name"], job_id=job_id, base=""
        )
    return meta


@app.get("/cutter/jobs")
def cutter_list_jobs():
    """List all active jobs."""
    require_feature("cutter")
    return {"jobs": [_add_source_file_id(job) for job in list_jobs()]}


@app.get("/cutter/jobs/{job_id}")
def cutter_get_job(job_id: str):
    """Get single job details."""
    require_feature("cutter")
    meta = load_job_metadata(job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Job not found")
    meta.setdefault("job_id", job_id)
    return _add_source_file_id(meta)


@app.delete("/cutter/jobs/{job_id}")
def cutter_delete_job(job_id: str):
    """Delete a job and all its files."""
    require_feature("cutter")
    try:
        delete_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "deleted"}


@app.get("/cutter/jobs/{job_id}/download/{filename}")
def cutter_download(job_id: str, filename: str):
    """Download an output file from a job."""
    require_feature("cutter")
    from fastapi.responses import FileResponse

    try:
        job_dir = get_job_dir(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Sanitize filename to prevent traversal
    safe_name = os.path.basename(filename)
    file_path = os.path.join(job_dir, "output", safe_name)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Output file not found")

    return FileResponse(
        file_path,
        filename=safe_name,
        media_type="application/octet-stream",
    )


@app.post("/cutter/jobs/{job_id}/save/{filename}")
def cutter_save_to_source(job_id: str, filename: str):
    """Copy an output file back to the original file's directory (server sources only)."""
    require_feature("cutter")
    from app.fs_utils import collision_safe_path

    meta = load_job_metadata(job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Job not found")

    if meta.get("source") != "server":
        raise HTTPException(
            status_code=400, detail="Save to Source only available for server files"
        )

    # Locate the output file in the job directory
    try:
        job_dir = get_job_dir(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    safe_name = os.path.basename(filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if ext and ext not in VALID_CUTTER_EXT:
        raise HTTPException(status_code=422, detail=f"Invalid output extension: {ext}")
    src_file = os.path.join(job_dir, "output", safe_name)
    if not os.path.isfile(src_file):
        raise HTTPException(status_code=404, detail="Output file not found")

    # Resolve the original file's directory
    original_path = meta.get("original_path", "")
    base_label = meta.get("base", "")
    try:
        base_path = resolve_base(base_label)
    except ValueError:
        raise HTTPException(status_code=400, detail="Cannot resolve original file path")
    try:
        resolved_original = validate_path(base_path, original_path)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Cannot resolve original file path")

    dest_dir = os.path.dirname(resolved_original)
    if not os.path.isdir(dest_dir):
        raise HTTPException(
            status_code=400, detail="Original directory no longer exists"
        )

    dest_path = collision_safe_path(os.path.join(dest_dir, safe_name))
    try:
        shutil.copy2(src_file, dest_path)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to save output file: {exc}"
        ) from exc

    return {"status": "saved", "filename": os.path.basename(dest_path)}


@app.post("/cutter/cut")
def cutter_cut(
    path: str = Form(..., max_length=500),
    source: str = Form(..., max_length=10),
    job_id: str = Form(..., max_length=50),
    in_point: float = Form(..., ge=0.0),
    out_point: float = Form(..., ge=0.0),
    output_name: str = Form("", max_length=300),
    stream_copy: bool = Form(True),
    codec: str = Form("", max_length=20),
    container: str = Form("", max_length=20),
    audio_tracks_json: str = Form("[]", alias="audio_tracks", max_length=5000),
    keep_quality: bool = Form(False),
    base: str = Form("", max_length=200),
):
    require_feature("cutter")

    # Validate job
    try:
        job_dir = get_job_dir(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    resolved = resolve_cutter_path(path, source, job_id, base_label=base)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")

    if out_point <= in_point:
        raise HTTPException(
            status_code=422, detail="out_point must be greater than in_point"
        )

    valid_codecs = {
        "copy",
        "aac",
        "flac",
        "opus",
        "ac3",
        "mp3",
        "vorbis",
        "pcm_s16le",
        "pcm_s24le",
        "libx264",
        "libx265",
        "libvpx-vp9",
        "libsvtav1",
    }
    valid_audio_codecs = {
        "copy",
        "aac",
        "flac",
        "opus",
        "ac3",
        "mp3",
        "vorbis",
        "pcm_s16le",
        "pcm_s24le",
        "dts",
        "truehd",
        "eac3",
    }
    valid_containers = {
        "",
        "mp4",
        "mkv",
        "mov",
        "avi",
        "webm",
        "ogg",
        "mp3",
        "flac",
        "wav",
        "aac",
        "ac3",
        "opus",
        "m4a",
        "mka",
        "ts",
        "mts",
    }

    if codec and codec not in valid_codecs:
        raise HTTPException(status_code=422, detail=f"Invalid codec: {codec}")
    if container and container not in valid_containers:
        raise HTTPException(status_code=422, detail=f"Invalid container: {container}")

    try:
        audio_tracks_parsed = json.loads(audio_tracks_json)
        if not isinstance(audio_tracks_parsed, list):
            raise TypeError("audio_tracks must be a JSON array")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid audio_tracks: {exc}")

    valid_modes = {"passthru", "reencode", "remove"}
    for track in audio_tracks_parsed:
        if not isinstance(track, dict):
            raise HTTPException(
                status_code=422, detail="Each audio track must be an object"
            )
        if track.get("mode") not in valid_modes:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid audio track mode: {track.get('mode')}",
            )
        if track["mode"] == "reencode":
            track_codec = track.get("codec", "")
            if track_codec and track_codec not in valid_audio_codecs:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid audio track codec: {track_codec}",
                )

    # Validate container/codec compatibility for audio tracks
    _container_audio_compat = {
        "mp4": {"aac", "ac3", "eac3", "mp3", "opus"},
        "m4a": {"aac", "ac3", "eac3", "mp3", "opus"},
        "mov": {"aac", "ac3", "eac3", "flac", "mp3"},
        "webm": {"opus", "vorbis"},
        "ogg": {"opus", "vorbis"},
        "mp3": {"mp3"},
        "flac": {"flac"},
        "aac": {"aac"},
        "ac3": {"ac3"},
        "opus": {"opus"},
        "wav": {"pcm_s16le", "pcm_s24le"},
    }
    # Validate against file duration and probe source
    try:
        file_info = probe_file(resolved)
        file_duration = file_info.get("duration", 0)
        if file_duration > 0 and out_point > file_duration + 0.5:
            raise HTTPException(
                status_code=422,
                detail=f"out_point ({out_point:.2f}s) exceeds file duration ({file_duration:.2f}s)",
            )
    except RuntimeError as exc:
        logger.warning("Could not probe source for cutter validation: %s", exc)
        file_info = {}

    # Validate container/codec compatibility for audio tracks
    if container and container in _container_audio_compat:
        allowed_audio = _container_audio_compat[container]
        probed_streams = {
            s["index"]: s.get("codec", "unknown").lower()
            for s in file_info.get("audio_streams", [])
        }
        for track in audio_tracks_parsed:
            if track["mode"] == "reencode":
                track_codec = track.get("codec", "")
                if track_codec and track_codec not in allowed_audio:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Audio codec '{track_codec}' is not compatible "
                        f"with container '{container}'. "
                        f"Allowed: {sorted(allowed_audio)}",
                    )
            elif track["mode"] == "passthru" and probed_streams:
                src_codec = probed_streams.get(track.get("index", -1), "")
                if src_codec and src_codec not in allowed_audio:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Cannot passthru '{src_codec}' audio into "
                        f"'{container}' container. Either re-encode the "
                        f"track or use a compatible container (e.g. mkv).",
                    )

    source_video_bitrate = file_info.get("video_bitrate") if keep_quality else None
    source_audio_bitrates = {}
    probe_audio_streams = file_info.get("audio_streams", [])
    if keep_quality:
        for stream in probe_audio_streams:
            source_audio_bitrates[stream["index"]] = stream.get("bit_rate", 0)

    # Determine output filename - use original name if no output_name given
    original_name = os.path.basename(resolved)
    original_ext = os.path.splitext(original_name)[1]  # e.g. ".mkv"

    if output_name:
        out_filename = os.path.basename(output_name)
    else:
        out_filename = original_name

    # Ensure the output filename extension matches the chosen container.
    # When stream-copying without an explicit container, keep the original
    # extension; otherwise use the container as extension.
    name_stem, name_ext = os.path.splitext(out_filename)
    if container:
        out_filename = name_stem + "." + container
    elif not name_ext:
        out_filename = name_stem + original_ext

    output_dir = os.path.join(job_dir, "output")
    output_path = os.path.join(output_dir, out_filename)

    # Update job metadata
    with get_job_meta_lock(job_id):
        meta = load_job_metadata(job_id)
        if meta:
            meta["status"] = "cutting"
            meta["cut_settings"] = {
                "in_point": in_point,
                "out_point": out_point,
                "stream_copy": stream_copy,
                "codec": codec or None,
                "container": container or None,
                "audio_tracks": audio_tracks_parsed,
                "keep_quality": keep_quality,
                "output_name": output_name or None,
            }
            save_job_metadata(job_id, meta)

    msg_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    cancel_event = threading.Event()

    def run_cut():
        try:
            if cancel_event.is_set():
                msg_queue.put(("error_msg", "Cut cancelled before start"))
                msg_queue.put(("done", "Cut failed"))
                return

            def progress_cb(msg: str):
                msg_queue.put(("progress", msg))

            final_path = cut_file(
                filepath=resolved,
                in_point=in_point,
                out_point=out_point,
                output_path=output_path,
                stream_copy=stream_copy,
                codec=codec or None,
                audio_tracks=audio_tracks_parsed if audio_tracks_parsed else None,
                container=container or None,
                progress_cb=progress_cb,
                keep_quality=keep_quality,
                source_video_bitrate=source_video_bitrate,
                source_audio_bitrates=(
                    source_audio_bitrates if source_audio_bitrates else None
                ),
                audio_streams=probe_audio_streams,
                job_id=job_id,
                cancel_event=cancel_event,
            )

            final_name = os.path.basename(final_path)

            # Update job metadata with output
            with get_job_meta_lock(job_id):
                meta = load_job_metadata(job_id)
                if meta:
                    meta["status"] = "done"
                    existing = meta.get("output_files", [])
                    if final_name not in existing:
                        existing.append(final_name)
                    meta["output_files"] = existing
                    save_job_metadata(job_id, meta)

            msg_queue.put(("done", f"Output: {final_name}"))
        except Exception as e:
            logger.exception("Cut failed")
            # Update job status to error
            with get_job_meta_lock(job_id):
                meta = load_job_metadata(job_id)
                if meta:
                    meta["status"] = "error"
                    save_job_metadata(job_id, meta)
            msg_queue.put(("error_msg", str(e)))
            msg_queue.put(("done", "Cut failed"))

    thread = threading.Thread(target=run_cut, daemon=True)
    thread.start()

    def event_generator():
        try:
            while True:
                try:
                    event_type, data = msg_queue.get(timeout=30)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                    if event_type == "done":
                        break
                except queue.Empty:
                    yield "event: progress\ndata: heartbeat\n\n"
        finally:
            cancel_event.set()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
