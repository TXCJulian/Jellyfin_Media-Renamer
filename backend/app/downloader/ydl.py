import os
from typing import Any

from app.config import BASE_PATHS, DOWNLOADS_DIR, resolve_base

VIDEO_CONTAINERS = frozenset({"mp4", "mkv", "webm", "mov"})
AUDIO_CONTAINERS = frozenset({"mp3", "m4a", "flac", "opus", "wav", "aac"})
THUMBNAIL_FORMATS = frozenset({"jpg", "png", "webp"})
VIDEO_CODECS = frozenset({"h264", "h265", "vp9", "av1"})
AUDIO_CODECS = frozenset({"mp3", "flac", "aac", "opus", "wav"})

_QUALITY_HEIGHTS = {
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "1440p": 1440,
    "2160p": 2160,
}
_AUDIO_QUALITY_KBPS = {
    "320kbps": 320,
    "256kbps": 256,
    "192kbps": 192,
    "128kbps": 128,
    "96kbps": 96,
}


def safe_component(value: Any) -> str:
    """Reduce a user-supplied name fragment to a single safe path component."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    for sep in ("/", "\\", os.sep):
        raw = raw.replace(sep, "_")
    if set(raw) == {"."}:
        raw = "_" * len(raw)
    return raw


def unique_path(path: str) -> str:
    """Return `path`, or the first free ` (n)` variant if it already exists."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{stem} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def video_format_selector(quality: str) -> str:
    key = str(quality or "best").lower()
    if key == "worst":
        return "wv*+wa/worst"
    height = _QUALITY_HEIGHTS.get(key)
    if height:
        return f"bv*[height<={height}]+ba/b[height<={height}]"
    return "bv*+ba/best"


def audio_format_selector(quality: str) -> str:
    key = str(quality or "best").lower()
    if key == "worst":
        return "worstaudio/worst"
    kbps = _AUDIO_QUALITY_KBPS.get(key)
    if kbps:
        return f"bestaudio[abr<={kbps}]/bestaudio/best"
    return "bestaudio/best"


def _contain(root: str, relative: str) -> str:
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, relative))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise ValueError("Path escapes its allowed root")
    return target


def allowed_roots() -> list[str]:
    return [os.path.realpath(DOWNLOADS_DIR)] + [os.path.realpath(p) for p in BASE_PATHS]


def assert_within_allowed_roots(path: str) -> None:
    resolved = os.path.realpath(path)
    for root in allowed_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return
    raise ValueError(f"Path is outside the allowed roots: {path}")


def resolve_output_root(options: dict[str, Any]) -> str:
    base_label = str(options.get("base") or "").strip()
    output_dir = str(options.get("output_dir") or "").strip()
    sub_folder = str(options.get("sub_folder") or "").strip()

    if base_label and output_dir:
        root = _contain(resolve_base(base_label), output_dir)
    else:
        root = os.path.realpath(DOWNLOADS_DIR)

    if sub_folder:
        root = _contain(root, sub_folder)

    assert_within_allowed_roots(root)
    return root


def needs_transcode(options: dict[str, Any]) -> bool:
    """A re-encode runs only when a concrete codec was chosen for A/V."""
    media_type = str(options.get("type") or "video").lower()
    if media_type == "thumbnail":
        return False
    codec = str(options.get("codec") or "").lower()
    if codec in ("", "auto"):
        return False
    return codec in VIDEO_CODECS or codec in AUDIO_CODECS


def build_ydl_opts(
    options: dict[str, Any], output_root: str, cookie_path: str | None
) -> dict[str, Any]:
    media_type = str(options.get("type") or "video").lower()
    container = str(options.get("format") or "").lower()
    quality = str(options.get("quality") or "best")
    prefix = safe_component(options.get("custom_prefix"))
    filename = safe_component(options.get("custom_filename"))

    stem = f"{prefix}{filename}" if filename else f"{prefix}%(title)s"
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        # FFmpeg's HLS downloader emits only a finished hook. Native HLS
        # supplies progress during VOD downloads and can fall back if needed.
        "hls_prefer_native": True,
        "overwrites": False,
        "outtmpl": os.path.join(output_root, f"{stem}.%(ext)s"),
        "progress_hooks": [],
        "postprocessor_hooks": [],
    }

    if cookie_path and os.path.isfile(cookie_path):
        opts["cookiefile"] = cookie_path

    try:
        item_limit = int(options.get("item_limit") or 0)
    except (TypeError, ValueError):
        item_limit = 0
    if item_limit > 0:
        opts["playlistend"] = item_limit

    if media_type == "audio":
        opts["format"] = audio_format_selector(quality)
        pp: dict[str, Any] = {"key": "FFmpegExtractAudio"}
        pp["preferredcodec"] = container if container in AUDIO_CONTAINERS else "best"
        kbps = _AUDIO_QUALITY_KBPS.get(quality.lower())
        if kbps:
            pp["preferredquality"] = str(kbps)
        opts["postprocessors"] = [pp]
    elif media_type == "thumbnail":
        opts["format"] = "best"
        opts["skip_download"] = True
        opts["writethumbnail"] = True
        if container in THUMBNAIL_FORMATS:
            opts["postprocessors"] = [
                {"key": "FFmpegThumbnailsConvertor", "format": container}
            ]
    else:
        opts["format"] = video_format_selector(quality)
        if container in VIDEO_CONTAINERS:
            opts["merge_output_format"] = container

    return opts
