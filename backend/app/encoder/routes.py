"""HTTP surface for the auto-encoder."""

import asyncio
import json
import logging
import os
import queue as queue_mod
import time
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import config
from app.encoder.client import EncoderClient
from app.encoder.events import EventBroadcaster, job_to_payload
from app.encoder.presets import NamedPreset, PresetError, parse_document
from app.encoder.probe import ProbeError, probe
from app.encoder.queue import EncodeQueue
from app.encoder.reprocess import (
    has_excluded_ancestor,
    is_excluded_path,
    prune_excluded_dirs,
    resolve_authorized_path,
)
from app.encoder.rules import (
    _BOOL_FIELDS,
    SKIP,
    Condition,
    Rule,
    RuleError,
    evaluate,
)
from app.encoder.runtime import EncoderRuntime
from app.encoder.store import TERMINAL_STAGES, EncoderStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/encoder")

_HEARTBEAT_SECONDS = 15.0
_POLL_SECONDS = 0.25
_MAX_DIRECTORY_RESULTS = 2_000
_MAX_FILE_RESULTS = 2_000
_MAX_WALK_ENTRIES = 25_000

_store: EncoderStore | None = None
_client: EncoderClient | None = None
_events: EventBroadcaster | None = None
_queue: EncodeQueue | None = None
_runtime: EncoderRuntime | None = None

# Set by `mark_startup_failed()` when main.py's lifespan catches an exception
# partway through building the store/queue/watcher. Without this, `get_store()`
# and `get_queue()` would just lazily rebuild on the next call -- handing back
# a queue with no worker thread reading it, so `approve` would report success
# ("queued") for a job nobody will ever dispatch. Once set, both fail loud
# instead, matching the downloader's `get_store()`/`get_queue()`, which raise
# unconditionally when `init_downloader()` was never called or was undone by
# `shutdown_downloader()`.
_startup_failed = False
_startup_failure_reason: str | None = None


class EncoderConfigurationUnavailable(RuntimeError):
    """The encoder could not start with its persisted configuration."""


def _startup_error() -> EncoderConfigurationUnavailable:
    return EncoderConfigurationUnavailable(
        _startup_failure_reason
        or "Encoder configuration is unavailable; check the server logs"
    )


def get_store() -> EncoderStore:
    global _store
    if _startup_failed:
        raise _startup_error()
    if _store is None:
        _store = EncoderStore(config.ENCODER_DB)
    return _store


def get_client() -> EncoderClient:
    global _client
    if _startup_failed:
        # Guarded like the store and the queue. Without this, /health probed
        # the remote encoder and cheerfully reported "ok" while the local
        # database and dispatch queue were dead -- the one screen an operator
        # checks first, showing green during the failure it exists to reveal.
        raise _startup_error()
    if _client is None:
        _client = EncoderClient(config.ENCODER_URL)
    return _client


def get_events() -> EventBroadcaster:
    global _events
    if _startup_failed:
        raise _startup_error()
    if _events is None:
        _events = EventBroadcaster()
    return _events


def get_queue() -> EncodeQueue:
    global _queue
    if _startup_failed:
        raise _startup_error()
    if _queue is None:
        _queue = EncodeQueue(
            get_store(),
            get_client(),
            get_events(),
            mode=config.ENCODER_MODE,
            original_ttl=config.ENCODER_ORIGINAL_TTL,
            holding_dir=f"{config.ENCODER_DATA_DIR}/originals",
        )
    return _queue


def register_runtime(runtime: EncoderRuntime) -> None:
    """Expose the lifespan-owned encoder runtime to request handlers."""
    global _runtime
    _runtime = runtime


def get_runtime() -> EncoderRuntime:
    if _startup_failed:
        raise _startup_error()
    if _runtime is None:
        raise RuntimeError(
            "Encoder runtime is unavailable; check the server logs for the cause"
        )
    return _runtime


def mark_startup_failed(reason: str | None = None) -> None:
    """Record a contained lifespan startup failure and discard live state.

    Called from `main.py`'s lifespan *after* it has already stopped whatever
    of the queue/watcher had managed to start. Resets the singletons (so
    nothing hands back a half-built or already-stopped object) and flips the
    guard `get_store()`/`get_queue()` check, so every subsequent call -- from
    a route handler or anywhere else -- fails loud instead of silently
    reconstructing (a fresh `EncodeQueue()` whose `.start()` nobody calls
    again is exactly as dead as the stopped one it would replace).
    """
    global _store, _queue, _events, _runtime, _startup_failed, _startup_failure_reason
    if _store is not None:
        _store.close()
    _store = None
    _queue = None
    _events = None
    _runtime = None
    _startup_failed = True
    _startup_failure_reason = (
        f"Encoder configuration is unavailable: {reason}"
        if reason
        else "Encoder configuration is unavailable; check the server logs"
    )


def reset_state_for_tests() -> None:
    global _store, _events, _queue, _runtime, _startup_failed, _startup_failure_reason
    if _store is not None:
        _store.close()
    _store = None
    _events = None
    _queue = None
    _runtime = None
    _startup_failed = False
    _startup_failure_reason = None


class PresetImport(BaseModel):
    document: dict
    include_names: list[str] | None = None


class PresetPreview(BaseModel):
    document: dict


class ReprocessIn(BaseModel):
    path: str


class BulkRunIn(BaseModel):
    paths: list[str]


class PresetLeaf(BaseModel):
    body: dict


class EncoderUnavailable(RuntimeError):
    """The encoder service supplied no capability information."""


class UnsupportedEncoder(ValueError):
    """A preset requests an encoder absent from the connected service."""


class ConditionIn(BaseModel):
    field: str
    op: str
    value: Any


class RuleIn(BaseModel):
    id: str
    conditions: list[ConditionIn]
    target: str


class RulesIn(BaseModel):
    rules: list[RuleIn]
    fallback: str


class TestIn(BaseModel):
    path: str


class WatchPathsIn(BaseModel):
    watch_paths: list[str]


def _error(status: int, code: str, reason: str) -> JSONResponse:
    """Build an error response with the code/reason at the top level.

    FastAPI's default HTTPException handler wraps ``detail`` under a
    ``"detail"`` key, which would bury ``code``/``reason`` a level deeper
    than every consumer of this API (including this module's own tests)
    expects. Returning the response directly, rather than raising an
    HTTPException, keeps the wire shape flat.
    """
    return JSONResponse(status_code=status, content={"code": code, "reason": reason})


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Reshape FastAPI's own validation-error response to match `_error()`.

    A malformed body (a missing required field, a wrong JSON type) never
    reaches `_error()` -- Pydantic rejects it before an endpoint function
    runs, and FastAPI's default handler for that returns
    ``{"detail": [...]}``. Every other error path in this module returns
    ``{"code", "reason"}`` at the top level, so leaving this one alone would
    give plan 2b's UI a different shape for this entire class of error.

    Registered globally (exception handlers can only be attached to a
    ``FastAPI`` app, not a bare ``APIRouter``), but scoped by path prefix so
    it only changes behaviour for the encoder's own routes -- any other
    feature's validation errors fall through to FastAPI's unmodified
    default handler.
    """
    path = request.url.path
    # Segment-bounded, not a bare prefix check: `path == router.prefix`
    # covers the (currently unused) bare "/api/encoder" path itself, and
    # `path.startswith(router.prefix + "/")` requires the next character
    # after the prefix to be a path separator. Without the separator, a
    # future route like "/api/encoderXYZ" would have its validation errors
    # silently reshaped into this envelope too.
    if not (path == router.prefix or path.startswith(router.prefix + "/")):
        return await request_validation_exception_handler(request, exc)
    errors = exc.errors()
    reason = (
        "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()) if p != 'body')}: {e.get('msg', '')}"
            for e in errors
        )
        or "Invalid request body"
    )
    return JSONResponse(
        status_code=422, content={"code": "invalid_request", "reason": reason}
    )


async def _configuration_exception_handler(
    request: Request, exc: EncoderConfigurationUnavailable
) -> JSONResponse:
    """Expose startup configuration failures as a stable encoder envelope."""
    path = request.url.path
    if path == router.prefix or path.startswith(router.prefix + "/"):
        return _error(503, "encoder_configuration_unavailable", str(exc))
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def register_error_handlers(app: FastAPI) -> None:
    """Attach the encoder's validation-error reshaping to *app*.

    Must be called explicitly (from `main.py`'s app setup, and from this
    module's own test fixtures) because FastAPI has no mechanism to carry an
    `APIRouter`'s exception handlers along when it is `include_router()`-ed.
    """
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(
        EncoderConfigurationUnavailable, _configuration_exception_handler
    )


def _vendor(encoders: list[str]) -> str:
    """Which hardware family this encoder can actually use.

    Derived rather than read from the service: /health has no gpu_name, and
    what the machine can encode with is a more useful thing to show than a
    device string anyway.
    """
    for prefix, label in (("nvenc_", "NVENC"), ("qsv_", "QSV"), ("vce_", "VCE")):
        if any(e.startswith(prefix) for e in encoders):
            return label
    return "CPU"


def _validate_condition_value(condition: ConditionIn) -> None:
    """Reject a condition whose value/operator cannot mean what it claims to.

    `rules.py`'s evaluation semantics compare booleans with
    ``bool(condition.value)`` -- and ``bool("false")`` is ``True`` in Python.
    A client sending the JSON *string* ``"false"`` for a bool field would
    silently invert the rule. Likewise `contains` is documented as
    text-fields-only, but `rules.py` only guards it against numeric fields,
    not bool ones. Both are cheap to catch here, at save time, before a rule
    is stored and silently misbehaves in production.
    """
    if condition.field not in _BOOL_FIELDS:
        return
    if condition.op == "contains":
        raise RuleError(
            f"Operator 'contains' applies to text fields only, not "
            f"{condition.field!r}"
        )
    if not isinstance(condition.value, bool):
        raise RuleError(
            f"Field {condition.field!r} is a true/false field; got "
            f"{condition.value!r} of type {type(condition.value).__name__}"
        )


@router.get("/health")
def encoder_health() -> dict:
    body = get_client().health()
    body["vendor"] = _vendor(body.get("encoders") or [])
    return body


@router.get("/config")
def encoder_config() -> dict:
    return {
        "watch_paths": get_runtime().watch_paths,
        "mode": config.ENCODER_MODE,
        "settle_seconds": config.ENCODER_SETTLE_SECONDS,
        "original_ttl": config.ENCODER_ORIGINAL_TTL,
        "job_ttl": config.ENCODER_JOB_TTL,
    }


def _base_label(path: str) -> str:
    labels = {value: label for label, value in config.BASE_PATH_LABELS.items()}
    return labels.get(path, os.path.basename(path.rstrip("/\\")) or path)


def _ignore_walk_error(_error: OSError) -> None:
    """Skip directories that disappear or cannot be read during a picker scan."""


@router.get("/directories")
def encoder_directories(
    search: str | None = Query(None, max_length=200),
) -> dict[str, Any]:
    """List safe absolute directories for the watch-folder picker."""
    needle = (search or "").strip().lower()
    found: list[dict[str, str]] = []
    visited = 0
    truncated = False
    for base in config.BASE_PATHS:
        resolved_base = os.path.realpath(base)
        if not os.path.isdir(resolved_base):
            continue
        for root, dirs, _files in os.walk(resolved_base, onerror=_ignore_walk_error):
            visited += 1
            if visited > _MAX_WALK_ENTRIES:
                truncated = True
                break
            if is_excluded_path(root, resolved_base) or has_excluded_ancestor(
                root, resolved_base
            ):
                dirs[:] = []
                continue
            prune_excluded_dirs(root, dirs, [resolved_base])
            resolved = os.path.realpath(root)
            if needle and needle not in resolved.lower():
                continue
            found.append({"path": resolved, "base": _base_label(base)})
            if len(found) >= _MAX_DIRECTORY_RESULTS:
                truncated = True
                break
        if truncated:
            break
    unique = {entry["path"]: entry for entry in found}
    return {
        "directories": sorted(unique.values(), key=lambda entry: entry["path"].lower()),
        "truncated": truncated,
    }


@router.get("/files")
def encoder_files(
    directory: str = Query(..., max_length=1000),
    search: str | None = Query(None, max_length=200),
) -> dict[str, Any]:
    """List video files below a configured directory for the test picker."""
    bases = list(config.BASE_PATHS)
    resolved = resolve_authorized_path(directory, bases, bases)
    if resolved is None or not os.path.isdir(resolved):
        return {"files": [], "truncated": False}
    needle = (search or "").strip().lower()
    valid_extensions = {extension.lower() for extension in config.VALID_VIDEO_EXT}
    files: list[dict[str, str]] = []
    visited = 0
    truncated = False
    if any(has_excluded_ancestor(resolved, base) for base in bases) or any(
        is_excluded_path(resolved, base) for base in bases
    ):
        return {"files": [], "truncated": False}
    for root, dirs, names in os.walk(resolved, onerror=_ignore_walk_error):
        visited += 1
        if visited > _MAX_WALK_ENTRIES:
            truncated = True
            break
        if any(has_excluded_ancestor(root, base) for base in bases) or any(
            is_excluded_path(root, base) for base in bases
        ):
            dirs[:] = []
            continue
        prune_excluded_dirs(root, dirs, bases)
        for name in names:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in valid_extensions:
                continue
            path = resolve_authorized_path(os.path.join(root, name), bases, bases)
            if path is None:
                continue
            if needle and needle not in path.lower():
                continue
            files.append({"path": path, "name": os.path.relpath(path, resolved)})
            if len(files) >= _MAX_FILE_RESULTS:
                truncated = True
                break
        if truncated:
            break
    return {
        "files": sorted(files, key=lambda entry: entry["name"].lower()),
        "truncated": truncated,
    }


def _validate_watch_paths(paths: list[str]) -> list[str]:
    """Return existing, de-duplicated watch directories inside a base path."""
    bases = list(config.BASE_PATHS)
    validated: list[str] = []
    seen: set[str] = set()
    for path in paths:
        resolved = resolve_authorized_path(path, bases, bases)
        if resolved is None or not os.path.isdir(resolved):
            raise ValueError(
                "Watch path is not an existing directory under BASE_PATHS: " f"{path}"
            )
        if any(has_excluded_ancestor(resolved, base) for base in bases) or any(
            is_excluded_path(resolved, base) for base in bases
        ):
            raise ValueError(f"Watch path is inside an excluded directory: {path}")
        if resolved not in seen:
            seen.add(resolved)
            validated.append(resolved)
    return validated


@router.put("/config", response_model=None)
def replace_config(payload: WatchPathsIn) -> dict | JSONResponse:
    try:
        paths = _validate_watch_paths(payload.watch_paths)
    except ValueError as exc:
        return _error(400, "invalid_watch_path", str(exc))
    try:
        return {"watch_paths": get_runtime().replace_watch_paths(paths)}
    except EncoderConfigurationUnavailable:
        raise
    except RuntimeError as exc:
        return _error(500, "watch_paths_replace_failed", str(exc))


@router.get("/presets")
def list_presets() -> list[dict]:
    return [
        {
            "name": p.name,
            "encoder": p.encoder,
            "video_preset": p.video_preset,
            "file_format": p.file_format,
        }
        for p in get_store().list_presets()
    ]


def _validate_preset_leaf(name: str, body: dict) -> NamedPreset:
    """Parse and capability-check one editable HandBrake preset leaf."""
    if not isinstance(name, str) or not name.strip():
        raise PresetError("PresetName must be a non-empty string")
    for field in ("PresetName", "VideoEncoder", "VideoPreset", "FileFormat"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PresetError(f"Preset {name!r} must include a non-empty {field}")
    presets = parse_document({"PresetList": [body]})
    if len(presets) != 1 or presets[0].name != name:
        raise PresetError("PresetName must match the URL name")
    available = set(get_client().health().get("encoders") or [])
    if not available:
        raise EncoderUnavailable("The encoder did not report its encoders")
    if presets[0].encoder not in available:
        raise UnsupportedEncoder(
            f"The connected encoder does not provide {presets[0].encoder!r}"
        )
    return presets[0]


def _preset_candidates(document: dict) -> tuple[list[NamedPreset], set[str]]:
    """Parse leaves once and pair them with the encoder's capabilities."""
    presets = parse_document(document)
    if not presets:
        raise PresetError("The document contains no presets")
    available = set(get_client().health().get("encoders") or [])
    if not available:
        raise EncoderUnavailable(
            "Cannot validate presets: the encoder did not report its encoders"
        )
    return presets, available


def _unsupported_preset_reason(preset: NamedPreset) -> str:
    return f"The connected encoder does not provide {preset.encoder!r}"


def _preset_error_response(exc: PresetError | EncoderUnavailable) -> JSONResponse:
    if isinstance(exc, EncoderUnavailable):
        return _error(503, "encoder_unreachable", str(exc))
    return _error(400, "invalid_preset", str(exc))


@router.get("/presets/{name}", response_model=None)
def get_preset(name: str) -> dict | JSONResponse:
    preset = next((p for p in get_store().list_presets() if p.name == name), None)
    if preset is None:
        return _error(404, "preset_not_found", f"No preset named {name!r}")
    return {"body": preset.body}


@router.put("/presets/{name}", response_model=None)
def replace_preset(name: str, payload: PresetLeaf) -> dict | JSONResponse:
    try:
        preset = _validate_preset_leaf(name, payload.body)
    except PresetError as exc:
        return _error(400, "invalid_preset", str(exc))
    except EncoderUnavailable as exc:
        return _error(503, "encoder_unreachable", str(exc))
    except UnsupportedEncoder as exc:
        return _error(400, "encoder_unavailable", str(exc))
    get_store().replace_presets([preset])
    return {"body": preset.body}


@router.post("/presets/preview", response_model=None)
def preview_presets(payload: PresetPreview) -> dict | JSONResponse:
    """Report which document leaves the connected encoder can import."""
    try:
        presets, available = _preset_candidates(payload.document)
    except (PresetError, EncoderUnavailable) as exc:
        return _preset_error_response(exc)
    return {
        "presets": [
            {
                "name": preset.name,
                "encoder": preset.encoder,
                "supported": preset.encoder in available,
                "reason": (
                    None
                    if preset.encoder in available
                    else _unsupported_preset_reason(preset)
                ),
            }
            for preset in presets
        ]
    }


@router.post("/presets", response_model=None)
def import_presets(payload: PresetImport) -> dict | JSONResponse:
    try:
        presets, available = _preset_candidates(payload.document)
    except (PresetError, EncoderUnavailable) as exc:
        return _preset_error_response(exc)

    document_names = {preset.name for preset in presets}
    selected_names = (
        document_names if payload.include_names is None else set(payload.include_names)
    )
    if payload.include_names is not None and not payload.include_names:
        return _error(
            400,
            "invalid_preset_selection",
            "Select at least one supported preset to import",
        )
    unknown_names = selected_names - document_names
    if unknown_names:
        return _error(
            400,
            "invalid_preset",
            f"The document contains no presets named: {', '.join(sorted(unknown_names))}",
        )

    selected = [preset for preset in presets if preset.name in selected_names]
    usable = [preset for preset in selected if preset.encoder in available]
    skipped = [
        {
            "name": p.name,
            "encoder": p.encoder,
            "reason": _unsupported_preset_reason(p),
        }
        for p in selected
        if p.encoder not in available
    ]
    if not usable:
        if payload.include_names is None:
            return _error(
                400,
                "encoder_unavailable",
                "None of the presets in this document can run on the connected "
                f"encoder; it does not provide: "
                f"{', '.join(sorted({p.encoder for p in presets}))}",
            )
        return _error(
            400,
            "encoder_unavailable",
            "None of the selected presets can run on the connected "
            f"encoder; it does not provide: "
            f"{', '.join(sorted({p.encoder for p in selected}))}",
        )

    get_store().replace_presets(usable)
    return {
        "imported": [p.name for p in usable],
        "skipped": skipped,
        "unselected": [p.name for p in presets if p.name not in selected_names],
    }


@router.delete("/presets/{name}", status_code=204, response_model=None)
def delete_preset(name: str):
    if not get_store().delete_preset(name):
        return _error(404, "preset_not_found", f"No preset named {name!r}")


@router.get("/rules")
def list_rules() -> dict:
    store = get_store()
    return {
        "rules": [
            {
                "id": r.id,
                "conditions": [
                    {"field": c.field, "op": c.op, "value": c.value}
                    for c in r.conditions
                ],
                "target": r.target,
            }
            for r in store.list_rules()
        ],
        "fallback": store.get_setting("fallback_target", SKIP),
    }


@router.put("/rules", response_model=None)
def replace_rules(payload: RulesIn) -> dict | JSONResponse:
    store = get_store()
    known = {p.name for p in store.list_presets()} | {SKIP}

    # Reject bool-field conditions whose value/operator don't mean what they
    # claim (carry-overs A and B) before anything else touches them.
    for rule_in in payload.rules:
        for condition_in in rule_in.conditions:
            try:
                _validate_condition_value(condition_in)
            except RuleError as exc:
                return _error(400, "invalid_rule", str(exc))

    rules = [
        Rule(
            id=r.id,
            conditions=[Condition(c.field, c.op, c.value) for c in r.conditions],
            target=r.target,
        )
        for r in payload.rules
    ]
    for rule in [*rules]:
        if rule.target not in known:
            return _error(
                400,
                "unknown_target",
                f"Rule {rule.id!r} targets {rule.target!r}, which is "
                "not a stored preset. Import or save that preset before saving rules.",
            )
    if payload.fallback not in known:
        return _error(
            400,
            "unknown_target",
            f"Fallback targets {payload.fallback!r}, which is not a stored preset. "
            "Import or save that preset before saving rules.",
        )

    # Validate fields and operators now, against a probe-shaped sample. A typo
    # would otherwise be a rule that silently never fires.
    # Every field must be present, not just named: `_holds` returns False for a
    # missing fact before it can reject, say, `contains` on a numeric field.
    sample = {
        "height": 0,
        "width": 0,
        "size": 0,
        "bit_rate": 0,
        "bit_depth": 0,
        "frame_rate": 0.0,
        "duration": 0.0,
        "video_codec": "",
        "profile": "",
        "hdr": False,
        "dolby_vision": False,
        "source_tool": "",
        "encoder_tag": "",
    }
    try:
        evaluate(sample, rules, payload.fallback)
    except RuleError as exc:
        return _error(400, "invalid_rule", str(exc))

    store.replace_rules(rules)
    store.set_setting("fallback_target", payload.fallback)
    return {"saved": len(rules)}


def _resolve_probe_path(path: str) -> str | None:
    """Resolve *path* against the readable-roots allow-list, or None if it escapes.

    Mirrors `resolve_cutter_path`/`validate_path` in main.py: realpath first
    so a symlink cannot point outside the configured roots, then require
    containment under one of them. Kept as a plain absolute-path check --
    rather than adopting the cutter's `{path, base}` + label shape -- because
    `ENCODER_WATCH_PATHS` has no label system the way `BASE_PATH_LABELS`
    does; every entry is already a full in-container path.

    The roots are `BASE_PATHS` plus the runtime's *effective* persisted watch
    paths, not the raw environment default. This endpoint only reads: it
    answers "which rule would win for this file", which is exactly the
    question you ask about a folder you are considering adding to the watch
    list. `BASE_PATHS` remains the application's boundary for paths a user may
    point at, while the runtime paths keep the allow-list correct after a
    configuration update.
    """
    roots = list(config.BASE_PATHS)
    try:
        roots.extend(get_runtime().watch_paths)
    except RuntimeError:
        # A failed runtime is already surfaced by /config; retaining the base
        # roots keeps the read-only test endpoint deterministic during startup.
        pass
    return resolve_authorized_path(path, roots, config.BASE_PATHS)


@router.post("/test", response_model=None)
def test_against_file(payload: TestIn) -> dict | JSONResponse:
    """Probe a candidate file against the stored rules, without encoding it.

    Unauthenticated by default (auth is off unless AUTH_USERNAME/PASSWORD are
    set) and ffprobe resolves protocols, not just paths -- so without this
    containment check this endpoint would let any caller who can reach the
    API make the server issue outbound requests (`{"path":
    "http://internal-host/..."}` ) or use the probe-failure message as a
    file-existence/permission oracle for arbitrary paths on the container.
    Requiring the resolved path to be a real file under a configured watch
    root closes both.
    """
    store = get_store()
    resolved = _resolve_probe_path(payload.path)
    if resolved is None:
        return _error(
            400, "invalid_path", "Path is not within a configured readable root"
        )
    if not os.path.isfile(resolved):
        return _error(400, "invalid_path", "No such file")
    try:
        facts = probe(resolved)
    except ProbeError as exc:
        # ffprobe's stderr goes to the log, not to the caller: it quotes the
        # path and distinguishes "no such file" from "permission denied",
        # which turns this endpoint into a filesystem oracle for anyone who
        # can reach it -- and auth is off unless credentials are configured.
        logger.warning("Probe of %s failed: %s", resolved, exc)
        return _error(400, "probe_failed", "Could not probe this file")
    rules = store.list_rules()
    fallback = store.get_setting("fallback_target", SKIP)
    try:
        match = evaluate(facts, rules, fallback)
    except RuleError as exc:
        return _error(400, "invalid_rule", str(exc))
    return {
        "facts": facts,
        "matched_rule": match.rule_id,
        "target": match.target,
        "evaluated": match.evaluated,
        "not_evaluated": [r.id for r in rules if r.id not in match.evaluated],
    }


@router.post("/reprocess", response_model=None)
def reprocess(payload: ReprocessIn) -> dict | JSONResponse:
    """Immediately re-evaluate a source path with the current rules."""
    resolved = _resolve_probe_path(payload.path)
    if resolved is None:
        return _error(
            400, "invalid_path", "Path is not within a configured readable root"
        )
    if not os.path.isfile(resolved):
        return _error(400, "invalid_path", "No such file")
    return get_queue().reprocess_path(resolved)


@router.post("/reprocess-all", response_model=None)
def reprocess_all() -> dict | JSONResponse:
    """Start one background pass that re-evaluates every configured source."""
    try:
        return get_runtime().start_reprocess_all()
    except EncoderConfigurationUnavailable:
        raise
    except RuntimeError as exc:
        return _error(500, "reprocess_start_failed", str(exc))


@router.delete("/reprocess-all", response_model=None)
def stop_reprocess_all() -> dict | JSONResponse:
    """Stop an in-flight background bulk re-evaluation pass."""
    try:
        return get_runtime().stop_reprocess_all()
    except EncoderConfigurationUnavailable:
        raise
    except RuntimeError as exc:
        return _error(500, "reprocess_stop_failed", str(exc))


@router.get("/reprocess-all/status")
def reprocess_all_status() -> dict:
    """Recover bulk progress after an SSE disconnect or missed terminal event."""
    return get_runtime().reprocess_status()


@router.get("/jobs")
def list_jobs() -> list[dict]:
    return [job_to_payload(j) for j in get_store().list_jobs()]


@router.post("/jobs/{job_id}/reprocess", response_model=None)
def reprocess_job(job_id: str) -> dict | JSONResponse:
    """Re-evaluate a job's source while retaining its prior row as history."""
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
        return _error(404, "job_not_found", f"No job {job_id!r}")
    if job.stage == "swapping":
        return _error(
            409,
            "job_swapping",
            "This job is publishing its result and cannot be reprocessed yet",
        )
    if job.error_code == "swap_interrupted":
        return _error(
            409,
            "swap_interrupted",
            "The previous publish was interrupted; confirm the file on disk manually",
        )
    if job.stage == "blocked" and job.remote_job_id:
        return _error(
            409,
            "remote_job_unresolved",
            "This job still references work on the remote encoder; cancel or "
            "resolve that work before re-evaluating it",
        )

    resolved = _resolve_probe_path(job.source_path)
    if resolved is None:
        return _error(
            400, "invalid_path", "Path is not within a configured readable root"
        )
    if not os.path.isfile(resolved):
        return _error(400, "invalid_path", "No such file")
    if job.stage == "blocked" and store.cancel_blocked_for_reprocess(job.id):
        get_events().publish(job_to_payload(store.get_job(job.id)))
    return get_queue().reprocess_path(resolved)


@router.post("/jobs/{job_id}/approve", response_model=None)
def approve_job(job_id: str):
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
        return _error(404, "job_not_found", f"No job {job_id!r}")
    if job.stage not in {"pending", "blocked"}:
        return _error(
            409, "not_awaiting_approval", f"Job is {job.stage}, not awaiting approval"
        )
    if job.error_code == "swap_interrupted":
        # This job carries a remote_job_id pointing at a remote encode that
        # is already terminal (its swap was interrupted by a restart, per
        # queue.py's recover()). EncodeQueue._run() routes any job with a
        # remote_job_id through _reattach rather than a fresh submit, which
        # here would poll that finished remote job and could re-run
        # `_publish_result` against a source that may already be the encoded
        # file. The job's own error message already tells the user to
        # confirm the file's state manually; approving here would bypass
        # that. Deleting the job (which does not touch the file) lets the
        # watcher pick the source up fresh once it has been checked.
        return _error(
            409,
            "swap_interrupted",
            "This job's swap was interrupted by a restart and its on-disk "
            "state is unknown. Confirm the file manually, then delete this "
            "job to let the watcher re-plan it.",
        )

    try:
        stat = os.stat(job.source_path)
        seen_fp = store.get_seen(job.source_path)
        if seen_fp is not None and (stat.st_size, stat.st_mtime_ns) != seen_fp:
            store.set_stage(
                job.id,
                "cancelled",
                error="The source file was modified on disk after this job was created",
                error_code="file_modified",
            )
            get_events().publish(job_to_payload(store.get_job(job.id)))
            get_queue().reprocess_path(job.source_path)
            return _error(
                409,
                "file_modified",
                "The source file was modified on disk since this job was created. "
                "The stale job has been cancelled and the file re-evaluated.",
            )
    except OSError:
        pass

    if not get_queue().enqueue_if_stage(job_id, {"pending", "blocked"}):
        current = store.get_job(job_id)
        stage = current.stage if current is not None else "missing"
        return _error(
            409, "not_awaiting_approval", f"Job is {stage}, not awaiting approval"
        )
    return {"stage": "queued"}


@router.delete("/jobs/{job_id}", status_code=204, response_model=None)
def delete_job(job_id: str):
    store = get_store()
    job = store.get_job(job_id)
    if job is None:
        return _error(404, "job_not_found", f"No job {job_id!r}")
    if job.stage == "swapping":
        return _error(
            409,
            "job_swapping",
            "This job is publishing its result and cannot be deleted yet",
        )
    if job.stage not in TERMINAL_STAGES and not get_queue().cancel(job_id):
        current = store.get_job(job_id)
        if current is None:
            return _error(404, "job_not_found", f"No job {job_id!r}")
        if current.stage == "swapping":
            return _error(
                409,
                "job_swapping",
                "This job is publishing its result and cannot be deleted yet",
            )
        if current.stage not in TERMINAL_STAGES:
            return _error(
                409, "job_not_cancellable", "The job changed state; try again"
            )
    if store.delete_job(job_id):
        get_events().publish({"type": "deleted", "job_id": job_id})


@router.get("/events")
async def events() -> StreamingResponse:
    broadcaster = get_events()
    # Resolved *before* the StreamingResponse is constructed -- once that
    # response starts, the 200 status and SSE headers are already committed,
    # so a store lookup that fails inside the generator would surface as a
    # stream that dies mid-body instead of a normal error status. Calling it
    # here lets a contained startup failure (get_store() raising) become the
    # ordinary 500 every other guarded route already gives.
    store = get_store()
    subscription = broadcaster.subscribe()

    async def stream():
        last_beat = time.monotonic()
        try:
            # A connection always starts with one authoritative snapshot,
            # including an empty one. This gives clients a liveness frame and
            # lets reconnects remove jobs that were purged while offline.
            jobs = await asyncio.to_thread(store.list_jobs)
            snapshot = {
                "type": "snapshot",
                "jobs": [job_to_payload(job) for job in jobs],
            }
            yield f"data: {json.dumps(snapshot)}\n\n"
            while True:
                try:
                    event = subscription.get_nowait()
                    yield f"data: {json.dumps(event)}\n\n"
                    continue
                except queue_mod.Empty:
                    pass
                if time.monotonic() - last_beat >= _HEARTBEAT_SECONDS:
                    last_beat = time.monotonic()
                    yield ": ping\n\n"
                await asyncio.sleep(_POLL_SECONDS)
        finally:
            broadcaster.unsubscribe(subscription)

    return StreamingResponse(stream(), media_type="text/event-stream")
