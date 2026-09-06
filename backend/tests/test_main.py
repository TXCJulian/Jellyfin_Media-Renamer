"""Tests for FastAPI endpoints."""
import json
import os
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "base_paths" in data


class TestConfigEndpoint:
    def test_config_returns_base_paths(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "features" in data
        assert "base_paths" in data
        assert isinstance(data["base_paths"], list)
        assert len(data["base_paths"]) >= 1


class TestDirectoryEndpoints:
    @pytest.mark.parametrize("event_method", ["on_created", "on_deleted", "on_moved"])
    def test_directory_events_clear_every_cache(self, event_method, monkeypatch):
        import app.main as main_mod

        cleared: list[str] = []

        class Cache:
            def __init__(self, name: str) -> None:
                self.name = name

            def cache_clear(self) -> None:
                cleared.append(self.name)

        class DirectoryEvent:
            is_directory = True

        monkeypatch.setattr(main_mod, "_get_all_dirs_cached", Cache("tv"))
        monkeypatch.setattr(main_mod, "_get_music_dirs_cached", Cache("music"))
        monkeypatch.setattr(main_mod, "_get_cutter_dirs_cached", Cache("cutter"))
        monkeypatch.setattr(main_mod, "_get_download_dirs_cached", Cache("download"))

        getattr(main_mod.DirChangeHandler(), event_method)(DirectoryEvent())

        assert cleared == ["tv", "music", "cutter", "download"]

    def test_list_tvshows_empty(self, client):
        resp = client.get("/directories/tvshows")
        assert resp.status_code == 200
        assert "directories" in resp.json()

    def test_list_music_empty(self, client):
        resp = client.get("/directories/music")
        assert resp.status_code == 200
        assert "directories" in resp.json()

    def test_refresh_directories(self, client):
        resp = client.post("/directories/refresh")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_refresh_directories_clears_every_cache(self, client, monkeypatch):
        import app.main as main_mod

        cleared: list[str] = []

        class Cache:
            def __init__(self, name: str) -> None:
                self.name = name

            def cache_clear(self) -> None:
                cleared.append(self.name)

        monkeypatch.setattr(main_mod, "_get_all_dirs_cached", Cache("tv"))
        monkeypatch.setattr(main_mod, "_get_music_dirs_cached", Cache("music"))
        monkeypatch.setattr(main_mod, "_get_cutter_dirs_cached", Cache("cutter"))
        monkeypatch.setattr(main_mod, "_get_download_dirs_cached", Cache("download"))

        response = client.post("/directories/refresh")

        assert response.status_code == 200
        assert cleared == ["tv", "music", "cutter", "download"]

    def test_series_filter(self, client, tmp_media_dir):
        show_dir = tmp_media_dir / "TV Shows" / "Breaking Bad" / "Season 01"
        show_dir.mkdir(parents=True)
        (show_dir / "ep.mp4").write_bytes(b"\x00")

        # Clear cache
        client.post("/directories/refresh")

        resp = client.get("/directories/tvshows", params={"series": "Breaking"})
        assert resp.status_code == 200
        dirs = resp.json()["directories"]
        paths = [d["path"] for d in dirs]
        assert any("Breaking Bad" in p for p in paths)

    def test_season_filter(self, client, tmp_media_dir):
        show_dir = tmp_media_dir / "TV Shows" / "TestShow" / "Season 02"
        show_dir.mkdir(parents=True)
        (show_dir / "ep.mp4").write_bytes(b"\x00")

        client.post("/directories/refresh")

        resp = client.get("/directories/tvshows", params={"season": "2"})
        assert resp.status_code == 200
        dirs = resp.json()["directories"]
        paths = [d["path"] for d in dirs]
        assert all("season 02" in p.lower() for p in paths)

    def test_directories_have_base_field(self, client, tmp_media_dir):
        show_dir = tmp_media_dir / "TV Shows" / "SomeShow" / "Season 01"
        show_dir.mkdir(parents=True)
        (show_dir / "ep.mp4").write_bytes(b"\x00")

        client.post("/directories/refresh")

        resp = client.get("/directories/tvshows")
        dirs = resp.json()["directories"]
        assert len(dirs) > 0
        assert "path" in dirs[0]
        assert "base" in dirs[0]

    def test_download_directories_available_for_download_feature(self, client, monkeypatch):
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"download"})
        monkeypatch.setattr(
            main_mod,
            "_get_download_dirs_cached",
            lambda: [
                {"path": "Empty", "base": "media"},
                {"path": "Documents", "base": "media"},
            ],
        )

        response = client.get("/directories/download")

        assert response.status_code == 200
        assert response.json()["directories"] == [
            {"path": "Empty", "base": "media"},
            {"path": "Documents", "base": "media"},
        ]

    def test_download_directory_search_filters_paths(self, client, monkeypatch):
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"download"})
        monkeypatch.setattr(
            main_mod,
            "_get_download_dirs_cached",
            lambda: [
                {"path": "Movies", "base": "media"},
                {"path": "Music", "base": "media"},
            ],
        )

        response = client.get("/directories/download", params={"search": "music"})

        assert response.status_code == 200
        assert response.json()["directories"] == [{"path": "Music", "base": "media"}]

    def test_cutter_directory_endpoint_is_not_enabled_by_download_alone(self, client, monkeypatch):
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"download"})

        assert client.get("/directories/media").status_code == 404

    def test_cutter_directory_endpoint_uses_cutter_cache_and_filters(self, client, monkeypatch):
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"cutter"})
        monkeypatch.setattr(
            main_mod,
            "_get_cutter_dirs_cached",
            lambda: [
                {"path": "Movies", "base": "media"},
                {"path": "Music", "base": "media"},
            ],
        )

        response = client.get("/directories/media", params={"search": "movie"})

        assert response.status_code == 200
        assert response.json()["directories"] == [{"path": "Movies", "base": "media"}]


class TestInputValidation:
    def test_threshold_out_of_range(self, client, base_label):
        resp = client.post("/rename/episodes", data={
            "series": "test",
            "season": 1,
            "directory": "test",
            "base": base_label,
            "dry_run": True,
            "assign_seq": False,
            "threshold": 2.0,
            "lang": "en",
        })
        assert resp.status_code == 422

    def test_negative_season(self, client, base_label):
        resp = client.post("/rename/episodes", data={
            "series": "test",
            "season": -1,
            "directory": "test",
            "base": base_label,
            "dry_run": True,
            "assign_seq": False,
            "threshold": 0.5,
            "lang": "en",
        })
        assert resp.status_code == 422

    def test_series_too_long(self, client, base_label):
        resp = client.post("/rename/episodes", data={
            "series": "x" * 300,
            "season": 1,
            "directory": "test",
            "base": base_label,
            "dry_run": True,
            "assign_seq": False,
            "threshold": 0.5,
            "lang": "en",
        })
        assert resp.status_code == 422


class TestPathTraversal:
    def test_episode_rename_path_traversal(self, client, base_label):
        resp = client.post("/rename/episodes", data={
            "series": "test",
            "season": 1,
            "directory": "../../../etc",
            "base": base_label,
            "dry_run": True,
            "assign_seq": False,
            "threshold": 0.5,
            "lang": "en",
        })
        assert resp.status_code == 400

    def test_music_rename_path_traversal(self, client, base_label):
        resp = client.post("/rename/music", data={
            "directory": "../../../etc",
            "base": base_label,
            "dry_run": True,
        })
        assert resp.status_code == 400

    def test_transcribe_files_path_traversal(self, client, base_label, monkeypatch):
        # 'lyrics' is off in the default feature set, so require_feature() would
        # short-circuit with a 404 and the traversal check would never run.
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"lyrics"})

        resp = client.get("/transcribe/files", params={
            "directory": "../../../etc",
            "base": base_label,
        })
        assert resp.status_code == 400

    def test_unknown_base_returns_400(self, client):
        resp = client.post("/rename/episodes", data={
            "series": "test",
            "season": 1,
            "directory": "test",
            "base": "nonexistent_base",
            "dry_run": True,
            "assign_seq": False,
            "threshold": 0.5,
            "lang": "en",
        })
        assert resp.status_code == 400
        assert "Unknown base" in resp.json()["detail"]


class TestDownloaderEndpoints:
    def test_download_status_shape(self, client):
        resp = client.get("/download/status")

        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {
            "yt_dlp_version",
            "cookies_present",
            "downloads_dir",
            "queue_depth",
            "workers",
        }

    def test_download_create_returns_job_ids_without_starting_work(self, client):
        resp = client.post(
            "/download",
            json={
                "urls": ["https://example.com/watch?v=demo"],
                "options": {"type": "video", "format": "mp4", "auto_start": False},
            },
        )

        assert resp.status_code == 200
        job_ids = resp.json()["job_ids"]
        assert len(job_ids) == 1

        job = client.get(f"/download/jobs/{job_ids[0]}").json()
        assert job["stage"] == "queued"
        assert job["url"] == "https://example.com/watch?v=demo"

    def test_download_create_rejects_non_http_url(self, client):
        resp = client.post(
            "/download", json={"urls": ["ftp://example.com/x"], "options": {}}
        )

        assert resp.status_code == 422

    def test_download_delete_unknown_job_returns_404(self, client):
        resp = client.delete("/download/jobs/11111111-1111-1111-1111-111111111111")

        assert resp.status_code == 404

    def test_download_cookie_upload_writes_file_bytes(self, client):
        import app.downloader.routes as routes_mod

        resp = client.post(
            "/download/cookies",
            files={"file": ("cookies.txt", b"cookie-data", "text/plain")},
        )

        assert resp.status_code == 200
        with open(routes_mod.cookie_path(), "rb") as f:
            assert f.read() == b"cookie-data"

    def test_download_routes_absent_when_feature_disabled(self, tmp_path):
        """The feature flag gates route registration, not just the handler."""
        import importlib

        with patch.dict(os.environ, {
            "BASE_PATHS": str(tmp_path),
            "TMDB_API_KEY": "test_key",
            "AUTH_USERNAME": "",
            "AUTH_PASSWORD": "",
            "SECRET_KEY": "test-secret-key",
            "ENABLED_FEATURES": "episodes",
        }):
            import app.config as config_mod
            importlib.reload(config_mod)
            import app.auth as auth_mod
            importlib.reload(auth_mod)
            import app.downloader.routes as routes_mod
            importlib.reload(routes_mod)
            import app.main as main_mod
            importlib.reload(main_mod)

            with TestClient(main_mod.app) as c:
                paths = c.get("/openapi.json").json()["paths"]
                assert not any(p.startswith("/download") for p in paths)
                assert c.get("/download/status").status_code == 404
                # No store or worker pool was built either.
                assert routes_mod._store is None
                assert routes_mod._queue is None

    def test_downloader_failing_to_start_does_not_kill_the_app(self, tmp_path):
        """An unwritable downloader data dir must degrade one feature, not all.

        The code this replaced wrapped its directory creation in try/except and
        only logged. init_downloader() opens SQLite as well, so a failure here
        must not escape lifespan startup: the app would never serve, and
        because the raise happens before the try/finally, the watchdog
        observers and the cutter cleanup task would both be orphaned.
        """
        import importlib

        media = tmp_path / "media"
        (media / "TV Shows").mkdir(parents=True)

        with patch.dict(os.environ, {
            "BASE_PATHS": str(media),
            "TMDB_API_KEY": "test_key",
            "AUTH_USERNAME": "",
            "AUTH_PASSWORD": "",
            "SECRET_KEY": "test-secret-key",
            "ENABLED_FEATURES": "episodes,cutter,download",
            "DOWNLOADS_DIR": str(tmp_path / "downloads"),
            "DOWNLOADER_DATA_DIR": str(tmp_path / "dl-data"),
            "DOWNLOADER_DB": str(tmp_path / "dl-data" / "downloader.db"),
            "CUTTER_JOBS_DIR": str(tmp_path / "cutter-jobs"),
        }):
            import app.config as config_mod
            importlib.reload(config_mod)
            import app.auth as auth_mod
            importlib.reload(auth_mod)
            import app.downloader.routes as routes_mod
            importlib.reload(routes_mod)
            import app.main as main_mod
            importlib.reload(main_mod)

            with patch.object(
                routes_mod, "JobStore", side_effect=OSError("read-only file system")
            ):
                with TestClient(main_mod.app) as c:
                    # The app serves, and the untouched features still work.
                    assert c.get("/health").status_code == 200
                    assert c.get("/directories/tvshows").status_code == 200
                    # The downloader itself is unavailable, not half-built.
                    assert routes_mod._store is None
                    assert routes_mod._queue is None

                # The watchers were actually started, so this is not vacuous...
                assert main_mod._observers
                # ...and the shutdown half of lifespan still ran, rather than
                # being skipped by an exception raised before the try block.
                assert all(not obs.is_alive() for obs in main_mod._observers)


class TestEncoderLifespan:
    def test_encoder_routes_absent_when_feature_disabled(self, tmp_path):
        """The feature flag gates route registration, not just the handler --
        mirrors test_download_routes_absent_when_feature_disabled."""
        import importlib

        with patch.dict(os.environ, {
            "BASE_PATHS": str(tmp_path),
            "TMDB_API_KEY": "test_key",
            "AUTH_USERNAME": "",
            "AUTH_PASSWORD": "",
            "SECRET_KEY": "test-secret-key",
            "ENABLED_FEATURES": "episodes",
        }):
            import app.config as config_mod
            importlib.reload(config_mod)
            import app.auth as auth_mod
            importlib.reload(auth_mod)
            import app.encoder.routes as encoder_routes_mod
            importlib.reload(encoder_routes_mod)
            import app.main as main_mod
            importlib.reload(main_mod)

            with TestClient(main_mod.app) as c:
                paths = c.get("/openapi.json").json()["paths"]
                assert not any(p.startswith("/api/encoder") for p in paths)
                assert c.get("/api/encoder/config").status_code == 404
                # No store or worker pool was built either.
                assert encoder_routes_mod._store is None
                assert encoder_routes_mod._queue is None

    def test_encoder_routes_present_when_feature_enabled(self, tmp_path):
        """The counterpart of the above: the flag being on actually mounts
        the routes, not just leaves them absent either way."""
        import importlib

        media = tmp_path / "media"
        media.mkdir(parents=True)

        with patch.dict(os.environ, {
            "BASE_PATHS": str(media),
            "TMDB_API_KEY": "test_key",
            "AUTH_USERNAME": "",
            "AUTH_PASSWORD": "",
            "SECRET_KEY": "test-secret-key",
            "ENABLED_FEATURES": "episodes,encoder",
            "ENCODER_DATA_DIR": str(tmp_path / "enc-data"),
            "ENCODER_DB": str(tmp_path / "enc-data" / "encoder.db"),
        }):
            import app.config as config_mod
            importlib.reload(config_mod)
            import app.auth as auth_mod
            importlib.reload(auth_mod)
            import app.encoder.routes as encoder_routes_mod
            importlib.reload(encoder_routes_mod)
            import app.main as main_mod
            importlib.reload(main_mod)

            with TestClient(main_mod.app) as c:
                paths = c.get("/openapi.json").json()["paths"]
                assert any(p.startswith("/api/encoder") for p in paths)
                assert c.get("/api/encoder/config").status_code == 200

    def test_encoder_does_not_persist_an_invalid_default_watch_path(self, tmp_path):
        """Environment defaults get the same BASE_PATHS boundary as PUT config."""
        import importlib

        media = tmp_path / "media"
        media.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        encoder_db = tmp_path / "enc-data" / "encoder.db"

        with patch.dict(os.environ, {
            "BASE_PATHS": str(media),
            "TMDB_API_KEY": "test_key",
            "AUTH_USERNAME": "",
            "AUTH_PASSWORD": "",
            "SECRET_KEY": "test-secret-key",
            "ENABLED_FEATURES": "episodes,encoder",
            "ENCODER_DATA_DIR": str(tmp_path / "enc-data"),
            "ENCODER_DB": str(encoder_db),
            "ENCODER_WATCH_PATHS": str(outside),
        }):
            import app.config as config_mod
            importlib.reload(config_mod)
            import app.auth as auth_mod
            importlib.reload(auth_mod)
            import app.encoder.routes as encoder_routes_mod
            importlib.reload(encoder_routes_mod)
            import app.main as main_mod
            importlib.reload(main_mod)

            with TestClient(main_mod.app) as c:
                assert c.get("/health").status_code == 200

            from app.encoder.store import EncoderStore

            store = EncoderStore(str(encoder_db))
            try:
                assert store.get_setting("watch_paths", "missing") == "missing"
            finally:
                store.close()

    def test_encoder_failing_to_start_does_not_kill_the_app(self, tmp_path):
        """A failure that happens *after* the queue has already started its
        dispatch worker thread must not leak that thread, must not take the
        rest of the app down with it, and -- critically -- must not leave
        the encoder routes handing back a fake success once the singletons
        are dead.

        Mirrors test_downloader_failing_to_start_does_not_kill_the_app for
        the "app survives" half. Without the try/except around the encoder
        startup block, the exception aborts the lifespan generator before
        the `finally` that would stop the queue is ever entered, and
        `TestClient(...)` itself would raise instead of the app coming up.

        Round 2 of review added the second half: a first fix contained the
        thread leak by stopping the queue, but left `get_queue()` free to
        silently rebuild a fresh, never-started replacement -- so
        `POST /jobs/{id}/approve` would report `200 {"stage": "queued"}` for
        a job nobody would ever dispatch. `mark_startup_failed()` is what
        closes that: this test asserts the route raises instead of quietly
        "succeeding".
        """
        import importlib
        import threading

        media = tmp_path / "media"
        media.mkdir(parents=True)
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir(parents=True)

        with patch.dict(os.environ, {
            "BASE_PATHS": str(tmp_path),
            "TMDB_API_KEY": "test_key",
            "AUTH_USERNAME": "",
            "AUTH_PASSWORD": "",
            "SECRET_KEY": "test-secret-key",
            "ENABLED_FEATURES": "episodes,encoder",
            "ENCODER_DATA_DIR": str(tmp_path / "enc-data"),
            "ENCODER_DB": str(tmp_path / "enc-data" / "encoder.db"),
            "ENCODER_WATCH_PATHS": str(watch_dir),
        }):
            import app.config as config_mod
            importlib.reload(config_mod)
            import app.auth as auth_mod
            importlib.reload(auth_mod)
            import app.encoder.routes as encoder_routes_mod
            importlib.reload(encoder_routes_mod)
            import app.encoder.runtime as encoder_runtime_mod
            import app.main as main_mod
            importlib.reload(main_mod)

            # EncoderRuntime constructs its watcher *after* encoder_queue.start(), so
            # forcing the failure here (rather than earlier) proves the
            # worker thread genuinely existed before the induced failure.
            with patch.object(
                encoder_runtime_mod, "EncoderWatcher",
                side_effect=RuntimeError("boom"),
            ):
                with TestClient(main_mod.app) as c:
                    # The app serves, and the untouched features still work.
                    assert c.get("/health").status_code == 200
                    assert c.get("/directories/tvshows").status_code == 200

                    # The singletons were torn down, not left dangling.
                    assert encoder_routes_mod._store is None
                    assert encoder_routes_mod._queue is None

                    # The caller does not get a quiet 200 -- approving (or
                    # any other store/queue-backed route) fails loud instead
                    # of silently accepting work nobody will ever process.
                    response = c.post("/api/encoder/jobs/some-id/approve")
                    assert response.status_code == 503
                    assert response.json()["code"] == "encoder_configuration_unavailable"

                # No leaked dispatch worker thread survives the failure.
                assert not any(
                    t.name == "encoder-dispatch" and t.is_alive()
                    for t in threading.enumerate()
                )

    def test_sweep_orphans_is_wired_with_remote_job_ids_not_local_ids(self, tmp_path):
        """`.hbenc-<id>` filenames carry the *encoder service's* job id
        (swap.py's OUTPUT_PREFIX docstring), stored per row as
        `remote_job_id` -- not `Job.id`, the renamer's own local uuid4. The
        two namespaces never intersect: passing local ids here would make
        every live partial look orphaned and delete it on every restart.

        This drives the actual lifespan wiring (unlike swap.py's own unit
        tests, which pass remote-shaped ids to `sweep_orphans` directly and
        would never have caught main.py handing it the wrong set) -- a job
        is seeded with deliberately different local/remote ids *before* the
        app starts, and the id set the lifespan actually passes to
        `sweep_orphans` is captured and asserted on.
        """
        import importlib

        media = tmp_path / "media"
        media.mkdir(parents=True)
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir(parents=True)
        enc_data = tmp_path / "enc-data"
        enc_db = enc_data / "encoder.db"

        with patch.dict(os.environ, {
            "BASE_PATHS": str(tmp_path),
            "TMDB_API_KEY": "test_key",
            "AUTH_USERNAME": "",
            "AUTH_PASSWORD": "",
            "SECRET_KEY": "test-secret-key",
            "ENABLED_FEATURES": "episodes,encoder",
            "ENCODER_DATA_DIR": str(enc_data),
            "ENCODER_DB": str(enc_db),
            "ENCODER_WATCH_PATHS": str(watch_dir),
        }):
            import app.config as config_mod
            importlib.reload(config_mod)
            import app.auth as auth_mod
            importlib.reload(auth_mod)
            import app.encoder.routes as encoder_routes_mod
            importlib.reload(encoder_routes_mod)
            import app.encoder.store as encoder_store_mod

            # Seed a job whose local id and remote id are deliberately
            # different, in a non-terminal stage, before lifespan (and
            # therefore the sweep) ever runs.
            seed_store = encoder_store_mod.EncoderStore(str(enc_db))
            job = seed_store.create_job(str(watch_dir / "Film.mkv"))
            seed_store.set_remote_job(job.id, "remote-xyz")
            seed_store.set_stage(job.id, "encoding")
            seed_store.close()
            assert job.id != "remote-xyz"

            import app.encoder.swap as swap_mod
            calls = []
            original_sweep = swap_mod.sweep_orphans

            def _spy(directory, active_job_ids):
                calls.append(set(active_job_ids))
                return original_sweep(directory, active_job_ids)

            with patch.object(swap_mod, "sweep_orphans", side_effect=_spy):
                import app.main as main_mod
                importlib.reload(main_mod)
                with TestClient(main_mod.app):
                    pass

            assert calls, "sweep_orphans was never called"
            for active in calls:
                assert active == {"remote-xyz"}
                assert job.id not in active

    def test_sweep_orphans_runs_before_queue_start_and_recover(self, tmp_path):
        """Sweep before recovering: a crash mid-encode leaves partials no
        live job owns. Sweeping before queue start/recover prevents racing
        with reattach/resubmit."""
        import importlib

        media = tmp_path / "media"
        media.mkdir(parents=True)
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir(parents=True)
        enc_data = tmp_path / "enc-data"
        enc_db = enc_data / "encoder.db"

        with patch.dict(os.environ, {
            "BASE_PATHS": str(tmp_path),
            "TMDB_API_KEY": "test_key",
            "AUTH_USERNAME": "",
            "AUTH_PASSWORD": "",
            "SECRET_KEY": "test-secret-key",
            "ENABLED_FEATURES": "episodes,encoder",
            "ENCODER_DATA_DIR": str(enc_data),
            "ENCODER_DB": str(enc_db),
            "ENCODER_WATCH_PATHS": str(watch_dir),
        }):
            import app.config as config_mod
            importlib.reload(config_mod)
            import app.auth as auth_mod
            importlib.reload(auth_mod)
            import app.encoder.queue as queue_mod
            import app.encoder.routes as encoder_routes_mod
            import app.encoder.swap as swap_mod
            importlib.reload(encoder_routes_mod)

            event_order = []

            original_sweep = swap_mod.sweep_orphans
            def _spy_sweep(*a, **k):
                event_order.append("sweep_orphans")
                return original_sweep(*a, **k)

            original_start = queue_mod.EncodeQueue.start
            def _spy_start(self, *a, **k):
                event_order.append("queue_start")
                return original_start(self, *a, **k)

            original_recover = queue_mod.EncodeQueue.recover
            def _spy_recover(self, *a, **k):
                event_order.append("queue_recover")
                return original_recover(self, *a, **k)

            with patch.object(swap_mod, "sweep_orphans", side_effect=_spy_sweep), \
                 patch.object(queue_mod.EncodeQueue, "start", side_effect=_spy_start, autospec=True), \
                 patch.object(queue_mod.EncodeQueue, "recover", side_effect=_spy_recover, autospec=True):
                import app.main as main_mod
                importlib.reload(main_mod)
                with TestClient(main_mod.app):
                    pass

            assert "sweep_orphans" in event_order
            assert "queue_start" in event_order
            assert "queue_recover" in event_order
            sweep_idx = event_order.index("sweep_orphans")
            start_idx = event_order.index("queue_start")
            recover_idx = event_order.index("queue_recover")
            assert sweep_idx < start_idx < recover_idx


class TestCutterStreamValidation:
    def test_cutter_stream_rejects_invalid_audio_index(self, client, tmp_path, monkeypatch):
        import app.main as main_mod

        media_file = tmp_path / "clip.mkv"
        media_file.write_bytes(b"demo")

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})
        monkeypatch.setattr(main_mod, "decode_file_id", lambda _file_id: ("server", "", "", "clip.mkv"))
        monkeypatch.setattr(
            main_mod,
            "resolve_cutter_path",
            lambda _path, _source, _job_id="", base_label="": str(media_file),
        )
        monkeypatch.setattr(
            main_mod,
            "probe_file",
            lambda _path: {
                "audio_codec": "aac",
                "audio_streams": [{"index": 1}, {"index": 2}],
            },
        )
        monkeypatch.setattr(main_mod, "needs_transcoding", lambda *_args, **_kwargs: False)

        resp = client.get("/cutter/stream/demo", params={"audio_stream": 99})
        assert resp.status_code == 400
        assert "Invalid audio stream index" in resp.json()["detail"]

    def test_cutter_stream_allows_valid_audio_index(self, client, tmp_path, monkeypatch):
        import app.main as main_mod

        media_file = tmp_path / "clip.mkv"
        media_file.write_bytes(b"demo")

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})
        monkeypatch.setattr(main_mod, "decode_file_id", lambda _file_id: ("server", "", "", "clip.mkv"))
        monkeypatch.setattr(
            main_mod,
            "resolve_cutter_path",
            lambda _path, _source, _job_id="", base_label="": str(media_file),
        )
        monkeypatch.setattr(
            main_mod,
            "probe_file",
            lambda _path: {
                "audio_codec": "aac",
                "audio_streams": [{"index": 1}, {"index": 2}],
            },
        )
        monkeypatch.setattr(main_mod, "needs_transcoding", lambda *_args, **_kwargs: False)

        resp = client.get("/cutter/stream/demo", params={"audio_stream": 1})
        assert resp.status_code == 200
        assert resp.content == b"demo"

    def test_cutter_stream_does_not_transcode_without_flag(self, client, tmp_path, monkeypatch):
        import app.main as main_mod

        media_file = tmp_path / "clip.mkv"
        media_file.write_bytes(b"demo")

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})
        monkeypatch.setattr(main_mod, "decode_file_id", lambda _file_id: ("server", "job-1", "", "clip.mkv"))
        monkeypatch.setattr(
            main_mod,
            "resolve_cutter_path",
            lambda _path, _source, _job_id="", base_label="": str(media_file),
        )
        monkeypatch.setattr(
            main_mod,
            "probe_file",
            lambda _path: {
                "audio_codec": "dts",
                "audio_streams": [{"index": 1}],
            },
        )
        monkeypatch.setattr(main_mod, "needs_transcoding", lambda *_args, **_kwargs: True)
        transcode_called = False

        def _no_transcode(*_args, **_kwargs):
            nonlocal transcode_called
            transcode_called = True

        monkeypatch.setattr(main_mod, "start_background_transcode", _no_transcode)

        resp = client.get("/cutter/stream/demo")
        assert resp.status_code == 200
        assert resp.content == b"demo"
        assert not transcode_called, "start_background_transcode should not be called without transcode=true"

    def test_cutter_stream_transcodes_when_flag_enabled(self, client, tmp_path, monkeypatch):
        import app.main as main_mod

        media_file = tmp_path / "clip.mkv"
        preview_file = tmp_path / "preview.mp4"
        media_file.write_bytes(b"orig")
        preview_file.write_bytes(b"preview")

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})
        monkeypatch.setattr(main_mod, "decode_file_id", lambda _file_id: ("server", "job-1", "", "clip.mkv"))
        monkeypatch.setattr(
            main_mod,
            "resolve_cutter_path",
            lambda _path, _source, _job_id="", base_label="": str(media_file),
        )
        monkeypatch.setattr(
            main_mod,
            "probe_file",
            lambda _path: {
                "audio_codec": "dts",
                "audio_streams": [{"index": 1}],
            },
        )
        monkeypatch.setattr(main_mod, "needs_transcoding", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(main_mod, "get_preview_status", lambda *_args, **_kwargs: {"state": "idle"})
        monkeypatch.setattr(main_mod, "get_preview_path_if_ready", lambda *_args, **_kwargs: str(preview_file))
        transcode_called = False

        def _fake_transcode(*_args, **_kwargs):
            nonlocal transcode_called
            transcode_called = True

        monkeypatch.setattr(main_mod, "start_background_transcode", _fake_transcode)

        resp = client.get("/cutter/stream/demo", params={"transcode": "true"})
        assert resp.status_code == 200
        assert resp.content == b"preview"
        assert transcode_called, "start_background_transcode should be called when transcode=true"

    def test_stream_returns_409_when_preview_not_ready(self, client, tmp_path, monkeypatch):
        import app.main as main_mod

        media_file = tmp_path / "clip.mp4"
        media_file.write_bytes(b"demo")

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})
        monkeypatch.setattr(main_mod, "decode_file_id", lambda _file_id: ("server", "job-1", "", "clip.mp4"))
        monkeypatch.setattr(
            main_mod,
            "resolve_cutter_path",
            lambda _path, _source, _job_id="", base_label="": str(media_file),
        )
        monkeypatch.setattr(
            main_mod,
            "probe_file",
            lambda _path: {
                "audio_codec": "dts",
                "audio_streams": [{"index": 1}],
            },
        )
        monkeypatch.setattr(main_mod, "needs_transcoding", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(main_mod, "get_preview_status", lambda *_args, **_kwargs: {"state": "idle"})
        monkeypatch.setattr(main_mod, "get_preview_path_if_ready", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(main_mod, "start_background_transcode", lambda *_args, **_kwargs: None)

        resp = client.get("/cutter/stream/demo", params={"transcode": "true"})
        assert resp.status_code == 409


class TestCutterPreviewStatus:
    def test_preview_status_non_transcoding_is_done(self, client, tmp_path, monkeypatch):
        import app.main as main_mod

        media_file = tmp_path / "clip.mp4"
        media_file.write_bytes(b"demo")

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})
        monkeypatch.setattr(main_mod, "decode_file_id", lambda _file_id: ("server", "", "", "clip.mp4"))
        monkeypatch.setattr(
            main_mod,
            "resolve_cutter_path",
            lambda _path, _source, _job_id="", base_label="": str(media_file),
        )
        monkeypatch.setattr(
            main_mod,
            "probe_file",
            lambda _path: {
                "audio_codec": "aac",
                "audio_streams": [{"index": 1}],
            },
        )
        monkeypatch.setattr(main_mod, "needs_transcoding", lambda *_args, **_kwargs: False)

        resp = client.get("/cutter/preview-status/demo")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "done"
        assert data["ready"] is True
        assert data["percent"] == 100.0


class TestCutterDeleteJob:
    def test_delete_job_returns_conflict_for_busy_job(self, client, monkeypatch):
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})

        def fake_delete_job(_job_id):
            raise RuntimeError("Job is still busy and could not be deleted")

        monkeypatch.setattr(main_mod, "delete_job", fake_delete_job)

        resp = client.delete("/cutter/jobs/11111111-1111-1111-1111-111111111111")

        assert resp.status_code == 409
        assert "still busy" in resp.json()["detail"]


class TestCutterStatusEndpoint:
    """/cutter/status reports the ffmpeg build the cutter will actually use."""

    @pytest.fixture(autouse=True)
    def _clear_ffmpeg_cache(self):
        """The probe is cached for the process, so no fake may outlive a test."""
        import app.cutter as cutter_mod

        cutter_mod.get_ffmpeg_info.cache_clear()
        yield
        cutter_mod.get_ffmpeg_info.cache_clear()

    @staticmethod
    def _patch_ffmpeg(
        monkeypatch, *, which, banner, on_run=None, returncode_value=0, raises=None
    ):
        """Point the ffmpeg probe at a fake binary and banner.

        Patched on app.cutter's own namespace rather than the shutil/subprocess
        modules so nothing else running in-process sees the fakes.
        """
        import subprocess as subprocess_mod
        import types
        import app.cutter as cutter_mod

        class FakeCompleted:
            stdout = banner
            stderr = ""
            returncode = returncode_value

        def fake_run(*args, **kwargs):
            if on_run is not None:
                on_run(args)
            if raises is not None:
                raise raises
            return FakeCompleted()

        monkeypatch.setattr(
            cutter_mod, "shutil", types.SimpleNamespace(which=lambda name: which)
        )
        monkeypatch.setattr(
            cutter_mod,
            "subprocess",
            types.SimpleNamespace(
                run=fake_run, SubprocessError=subprocess_mod.SubprocessError
            ),
        )

    def test_reports_jellyfin_build(self, client, monkeypatch):
        self._patch_ffmpeg(
            monkeypatch,
            which="/usr/local/bin/ffmpeg",
            banner=(
                "ffmpeg version 7.1.1-Jellyfin Copyright (c) 2000-2025 the FFmpeg developers\n"
                "built with gcc 12\n"
            ),
        )

        resp = client.get("/cutter/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ffmpeg_available"] is True
        assert data["ffmpeg_version"] == "7.1.1-Jellyfin"
        assert data["ffmpeg_build"] == "jellyfin"

    def test_reports_plain_build(self, client, monkeypatch):
        self._patch_ffmpeg(
            monkeypatch,
            which="/usr/bin/ffmpeg",
            banner=(
                "ffmpeg version 5.1.6-0+deb12u1 Copyright (c) 2000-2024 the FFmpeg developers\n"
            ),
        )

        resp = client.get("/cutter/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ffmpeg_available"] is True
        assert data["ffmpeg_version"] == "5.1.6-0+deb12u1"
        assert data["ffmpeg_build"] == "standard"

    def test_reports_missing_ffmpeg_without_raising(self, client, monkeypatch):
        self._patch_ffmpeg(monkeypatch, which=None, banner="")

        resp = client.get("/cutter/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ffmpeg_available"] is False
        assert data["ffmpeg_version"] == ""
        assert data["ffmpeg_build"] == ""

    def test_reports_unavailable_when_the_probe_exits_non_zero(self, client, monkeypatch):
        """A binary on PATH that fails to run is not a usable ffmpeg."""
        self._patch_ffmpeg(
            monkeypatch, which="/usr/bin/ffmpeg", banner="", returncode_value=1
        )

        resp = client.get("/cutter/status")

        assert resp.status_code == 200
        assert resp.json()["ffmpeg_available"] is False

    def test_reports_unavailable_when_the_probe_raises(self, client, monkeypatch):
        import subprocess as subprocess_mod

        self._patch_ffmpeg(
            monkeypatch,
            which="/usr/bin/ffmpeg",
            banner="",
            raises=subprocess_mod.TimeoutExpired(cmd="ffmpeg", timeout=10),
        )

        resp = client.get("/cutter/status")

        assert resp.status_code == 200
        assert resp.json()["ffmpeg_available"] is False

    def test_result_is_cached_across_requests(self, client, monkeypatch):
        calls = []
        self._patch_ffmpeg(
            monkeypatch,
            which="/usr/local/bin/ffmpeg",
            banner="ffmpeg version 7.1.1-Jellyfin Copyright (c) 2000-2025\n",
            on_run=calls.append,
        )

        client.get("/cutter/status")
        client.get("/cutter/status")

        assert len(calls) == 1

    def test_absent_when_cutter_feature_disabled(self, client, monkeypatch):
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes"})

        resp = client.get("/cutter/status")

        assert resp.status_code == 404


class TestCutterValidation:
    def test_cutter_cut_rejects_out_point_before_in_point(self, client, tmp_path, monkeypatch):
        import app.main as main_mod

        media_file = tmp_path / "clip.mp4"
        media_file.write_bytes(b"demo")

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})
        monkeypatch.setattr(main_mod, "get_job_dir", lambda _job_id: str(tmp_path))
        monkeypatch.setattr(main_mod, "resolve_cutter_path", lambda *_args, **_kwargs: str(media_file))

        resp = client.post(
            "/cutter/cut",
            data={
                "path": "clip.mp4",
                "source": "server",
                "base": "",
                "job_id": "11111111-1111-1111-1111-111111111111",
                "in_point": "10",
                "out_point": "5",
                "stream_copy": "true",
            },
        )

        assert resp.status_code == 422
        assert "out_point" in resp.json()["detail"]

    def test_resolve_cutter_path_blocks_server_traversal(self, client, base_label):
        import app.main as main_mod
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            main_mod.resolve_cutter_path("../../../etc/passwd", "server", base_label=base_label)

        assert exc_info.value.status_code == 400

    def test_cutter_cut_rejects_invalid_audio_track_codec(self, client, tmp_path, monkeypatch):
        import app.main as main_mod

        media_file = tmp_path / "test.mp4"
        media_file.write_bytes(b"demo")

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"episodes", "music", "cutter"})
        monkeypatch.setattr(main_mod, "get_job_dir", lambda _job_id: str(tmp_path))
        monkeypatch.setattr(main_mod, "resolve_cutter_path", lambda *_args, **_kwargs: str(media_file))

        audio_tracks = json.dumps([
            {"index": 1, "mode": "reencode", "codec": "evil_codec"}
        ])
        response = client.post(
            "/cutter/cut",
            data={
                "path": "test.mp4",
                "source": "server",
                "base": "",
                "job_id": "11111111-1111-1111-1111-111111111111",
                "in_point": "0",
                "out_point": "30",
                "stream_copy": "false",
                "codec": "libx264",
                "container": "mp4",
                "audio_tracks": audio_tracks,
                "keep_quality": "false",
            },
        )
        assert response.status_code == 422
        assert "Invalid audio track codec" in response.json()["detail"]


class TestTranscribeStart:
    """Regression tests for /transcribe/start file selection."""

    def _setup(self, monkeypatch, tmp_media_dir, names):
        import app.main as main_mod

        album = tmp_media_dir / "Music" / "SSIO" / "BB.U.M.SS.N"
        album.mkdir(parents=True)
        for name in names:
            (album / name).write_bytes(b"\x00" * 100)

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"lyrics", "music"})
        monkeypatch.setattr(main_mod, "TRANSCRIBER_URL", "http://transcriber:3334")
        monkeypatch.setattr(
            main_mod, "transcribe_file", lambda **_kwargs: ([], None)
        )
        return "SSIO/BB.U.M.SS.N"

    def test_filename_with_comma_is_selected(
        self, client, tmp_media_dir, base_label, monkeypatch
    ):
        comma_name = "01-13 Illegal, legal, egal....flac"
        directory = self._setup(
            monkeypatch, tmp_media_dir, [comma_name, "01-01 Other.flac"]
        )

        resp = client.post(
            "/transcribe/start",
            data={
                "directory": directory,
                "base": base_label,
                "files": json.dumps([comma_name]),
                "output_format": "lrc",
                "skip_existing": "false",
            },
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert "event: done" in resp.text
        assert "Completed: 1/1" in resp.text

    def test_no_matching_files_still_returns_sse(
        self, client, tmp_media_dir, base_label, monkeypatch
    ):
        directory = self._setup(monkeypatch, tmp_media_dir, ["01-01 Other.flac"])

        resp = client.post(
            "/transcribe/start",
            data={
                "directory": directory,
                "base": base_label,
                "files": json.dumps(["does-not-exist.flac"]),
                "output_format": "lrc",
            },
        )

        assert resp.headers["content-type"].startswith("text/event-stream")
        assert "event: error_msg" in resp.text
        assert "event: done" in resp.text

    def test_rejects_unknown_whisper_model(
        self, client, tmp_media_dir, base_label, monkeypatch
    ):
        directory = self._setup(monkeypatch, tmp_media_dir, ["01-01 Other.flac"])

        resp = client.post(
            "/transcribe/start",
            data={
                "directory": directory,
                "base": base_label,
                "output_format": "lrc",
                "whisper_model": "definitely-not-a-model",
            },
        )

        assert resp.status_code == 422
        assert "Invalid whisper model" in resp.json()["detail"]

    def test_overrides_applied_for_single_file(
        self, client, tmp_media_dir, base_label, monkeypatch
    ):
        import app.main as main_mod

        directory = self._setup(monkeypatch, tmp_media_dir, ["01-01 Other.flac"])
        calls: list[dict] = []
        monkeypatch.setattr(
            main_mod, "transcribe_file", lambda **kw: (calls.append(kw), ([], None))[1]
        )

        resp = client.post(
            "/transcribe/start",
            data={
                "directory": directory,
                "base": base_label,
                "output_format": "lrc",
                "skip_existing": "false",
                "whisper_model": "medium",
                "artist_override": "SSIO",
                "title_override": "Nullkommaneun",
            },
        )

        assert "event: done" in resp.text
        assert len(calls) == 1
        assert calls[0]["whisper_model"] == "medium"
        assert calls[0]["artist"] == "SSIO"
        assert calls[0]["title"] == "Nullkommaneun"

    def test_overrides_ignored_for_multi_file(
        self, client, tmp_media_dir, base_label, monkeypatch
    ):
        import app.main as main_mod

        directory = self._setup(
            monkeypatch, tmp_media_dir, ["01-01 A.flac", "01-02 B.flac"]
        )
        calls: list[dict] = []
        monkeypatch.setattr(
            main_mod, "transcribe_file", lambda **kw: (calls.append(kw), ([], None))[1]
        )

        resp = client.post(
            "/transcribe/start",
            data={
                "directory": directory,
                "base": base_label,
                "output_format": "lrc",
                "skip_existing": "false",
                "artist_override": "SSIO",
                "title_override": "Nullkommaneun",
            },
        )

        assert "event: done" in resp.text
        assert len(calls) == 2
        assert all(c["title"] != "Nullkommaneun" for c in calls)


class TestRenameMusicLyricsAction:
    def test_rejects_unknown_lyrics_action(self, client, base_label):
        resp = client.post("/rename/music", data={
            "directory": "ABBA",
            "base": base_label,
            "dry_run": True,
            "lyrics_action": "shred",
        })
        assert resp.status_code == 422
        assert "Invalid lyrics action" in resp.json()["detail"]

    def test_action_is_forwarded(self, client, tmp_media_dir, base_label, monkeypatch):
        import app.main as main_mod

        album = tmp_media_dir / "Music" / "ABBA"
        album.mkdir(parents=True)
        (album / "song.flac").write_bytes(b"\x00")

        seen: dict = {}
        monkeypatch.setattr(
            main_mod, "rename_music", lambda **kw: (seen.update(kw), ([], None))[1]
        )

        resp = client.post("/rename/music", data={
            "directory": "ABBA",
            "base": base_label,
            "dry_run": True,
            "lyrics_action": "delete",
        })

        assert resp.status_code == 200
        assert seen["lyrics_action"] == "delete"

    def test_defaults_to_rename(self, client, tmp_media_dir, base_label, monkeypatch):
        import app.main as main_mod

        album = tmp_media_dir / "Music" / "ABBA"
        album.mkdir(parents=True)
        (album / "song.flac").write_bytes(b"\x00")

        seen: dict = {}
        monkeypatch.setattr(
            main_mod, "rename_music", lambda **kw: (seen.update(kw), ([], None))[1]
        )

        client.post("/rename/music", data={
            "directory": "ABBA",
            "base": base_label,
            "dry_run": True,
        })

        assert seen["lyrics_action"] == "rename"


def test_upload_cookies_rejects_oversized_file(client):
    """Cookie files should be limited to 1 MB."""
    huge = b"x" * (1024 * 1024 + 1)
    response = client.post(
        "/download/cookies",
        files={"file": ("cookies.txt", huge, "text/plain")},
    )
    assert response.status_code == 413


def test_upload_cookies_accepts_valid_netscape_format(client):
    import app.downloader.routes as routes_mod

    content = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n"
    response = client.post(
        "/download/cookies",
        files={"file": ("cookies.txt", content, "text/plain")},
    )
    assert response.status_code == 200
    with open(routes_mod.cookie_path(), "rb") as f:
        assert f.read() == content


class TestCutterJobListEnrichment:
    """A job reopened from the Jobs list must carry a usable source_file_id.

    `/cutter/jobs/{id}` computes `source_file_id`; `/cutter/jobs` used to
    return raw metadata without it. The panel reopens jobs from the *list*,
    so `fileId` arrived empty, the player requested `/cutter/stream/` and got
    a 404 JSON body, and the browser reported MEDIA_ELEMENT_ERROR 4 for every
    file regardless of codec.
    """

    def _write_job(self, jobs_dir, job_id, base_label):
        import json

        job_dir = jobs_dir / job_id
        job_dir.mkdir(parents=True)
        meta = {
            "job_id": job_id,
            "source": "server",
            "original_path": "TV Shows/Example.mkv",
            "original_name": "Example.mkv",
            "base": base_label,
            "status": "ready",
            "output_files": [],
            "created_at": "2026-08-06T00:00:00+00:00",
        }
        (job_dir / "job.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_listed_job_carries_the_same_source_file_id_as_the_single_job(
        self, client, tmp_path, monkeypatch, base_label
    ):
        import app.cutter as cutter_mod
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "ENABLED_FEATURES_SET", {"cutter"})
        jobs_dir = tmp_path / "cutter-jobs"
        job_id = "11111111-1111-1111-1111-111111111111"
        self._write_job(jobs_dir, job_id, base_label)
        monkeypatch.setattr(cutter_mod, "CUTTER_JOBS_DIR", str(jobs_dir))

        single = main_mod.cutter_get_job(job_id)
        listed = main_mod.cutter_list_jobs()["jobs"]

        assert single.get("source_file_id"), "single-job endpoint should compute a file id"
        assert len(listed) == 1
        assert listed[0].get("source_file_id") == single["source_file_id"]
