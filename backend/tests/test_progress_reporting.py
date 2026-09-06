"""Progress reaching API consumers, including streams without byte totals."""
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.dependencies import Cryptodome

from app.downloader.runner import _IndexAllocator, _make_hook
from app.downloader import runner
from app.downloader.store import JobStore
from app.downloader.ydl import build_ydl_opts
from app.encoder.events import job_to_payload
from app.encoder.store import EncoderStore


def test_hls_vod_uses_downloader_with_intermediate_progress(tmp_path):
    opts = build_ydl_opts({}, str(tmp_path), None)
    info = {"url": "https://example.com/video.m3u8", "protocol": "m3u8",
            "ext": "mp4", "is_live": False}
    assert get_suitable_downloader(info, opts) is HlsFD


def test_encrypted_hls_reports_progress_and_cancels_before_finishing(tmp_path, monkeypatch):
    # Real yt-dlp and AES-128 fragments, served locally. Missing crypto makes
    # this path delegate to FFmpeg, bypassing our progress/cancellation hook.
    key = b"0123456789abcdef"
    iv = bytes(16)
    fragment = Cryptodome.AES.new(key, Cryptodome.AES.MODE_CBC, iv).encrypt(
        b"a" * 32768 + bytes([16]) * 16
    )
    manifest = (
        '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n'
        '#EXT-X-MEDIA-SEQUENCE:0\n'
        '#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x00000000000000000000000000000000\n'
        '#EXTINF:10,\n0.ts\n#EXTINF:10,\n1.ts\n#EXTINF:10,\n2.ts\n#EXT-X-ENDLIST\n'
    ).encode()
    requested = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            body = manifest if self.path == "/video.m3u8" else key if self.path == "/key" else fragment
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl" if self.path.endswith(".m3u8") else "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cancel = threading.Event()
    seen = []

    def observe(job):
        for item in job.items:
            if item.stage == "downloading" and item.progress > 0:
                seen.append(item.progress)
                cancel.set()

    store = JobStore(str(tmp_path / "local.db"), on_change=observe)
    monkeypatch.setattr(runner, "resolve_output_root", lambda _opts: str(tmp_path))
    job_id = store.create_job(f"http://127.0.0.1:{server.server_port}/video.m3u8", {})
    try:
        runner.run_job(store, store.get_job(job_id), cancel)
        assert seen and 0 < seen[0] < 100
        assert store.get_job(job_id).stage == "cancelled"
        assert "/1.ts" not in requested and "/2.ts" not in requested
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("fields,expected,known", [
    ({"downloaded_bytes": 25, "total_bytes": 100}, 25, True),
    ({"downloaded_bytes": 25, "total_bytes_estimate": 200}, 12.5, True),
    ({"downloaded_bytes": 25, "fragment_index": 3, "fragment_count": 12}, 25, True),
    ({"downloaded_bytes": 25}, 0, False),
    ({"fragment_index": 0, "fragment_count": 12}, 0, True),
    ({"fragment_index": 3, "fragment_count": 0}, 0, False),
    ({"downloaded_bytes": 25, "total_bytes": float("nan"), "fragment_index": 1, "fragment_count": 4}, 25, True),
    ({"downloaded_bytes": 25, "total_bytes": -1}, 0, False),
])
def test_download_progress_reaches_stored_item(tmp_path, fields, expected, known):
    store = JobStore(str(tmp_path / "download.db"))
    job_id = store.create_job("https://example.com/video", {})
    hook = _make_hook(store, job_id, threading.Event(), set(), set(), _IndexAllocator())
    hook({"status": "downloading", "filename": str(tmp_path / "video.mp4"), **fields})
    item = store.get_job(job_id).items[0]
    assert item.progress == expected
    assert item.progress_known is known


def test_download_upgrade_and_completion_restore_known_progress(tmp_path):
    path = str(tmp_path / "download.db")
    store = JobStore(path)
    job_id = store.create_job("https://example.com/video", {})
    store.upsert_item(job_id, 0, progress=25)
    store._conn.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE items DROP COLUMN progress_known")
    store = JobStore(path)
    assert store.get_job(job_id).items[0].progress == 25
    assert store.get_job(job_id).items[0].progress_known is True
    store.upsert_item(job_id, 0, progress=0, progress_known=False)
    store.upsert_item(job_id, 0, progress=100, stage="done")
    assert store.get_job(job_id).items[0].progress_known is True


def test_encoder_eta_clears_when_job_leaves_encoding(tmp_path):
    store = EncoderStore(str(tmp_path / "encoder.db"))
    try:
        job = store.create_job("/media/video.mkv")
        store.set_stage(job.id, "encoding")
        store.set_progress(job.id, 25, eta_seconds=120)
        assert job_to_payload(store.get_job(job.id))["eta_seconds"] == 120
        store.set_stage(job.id, "swapping")
        assert job_to_payload(store.get_job(job.id))["eta_seconds"] is None
    finally:
        store.close()


def test_encoder_upgrade_keeps_existing_jobs_and_discards_old_eta(tmp_path):
    path = str(tmp_path / "encoder.db")
    store = EncoderStore(path)
    job = store.create_job("/media/video.mkv")
    store.close()
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "eta_seconds" in columns:
            conn.execute("ALTER TABLE jobs DROP COLUMN eta_seconds")
    store = EncoderStore(path)
    try:
        assert job_to_payload(store.get_job(job.id))["eta_seconds"] is None
        store.set_stage(job.id, "encoding")
        store.set_progress(job.id, 25, eta_seconds=120)
    finally:
        store.close()
    store = EncoderStore(path)
    try:
        assert store.get_job(job.id).progress == 25
        assert job_to_payload(store.get_job(job.id))["eta_seconds"] is None
    finally:
        store.close()
