import os
from unittest.mock import patch


def _reload_config(**env_overrides):
    """Reload config module (and get_dirs which imports from it) with given env vars."""
    import importlib
    import app.config as config_mod
    import app.get_dirs as get_dirs_mod
    with patch.dict(os.environ, env_overrides, clear=False):
        importlib.reload(config_mod)
        importlib.reload(get_dirs_mod)  # Re-imports BASE_PATHS, BASE_PATH_LABELS
    return config_mod


def test_single_base_path():
    cfg = _reload_config(BASE_PATHS="/media")
    assert cfg.BASE_PATHS == ["/media"]
    assert cfg.BASE_PATH_LABELS == {"media": "/media"}


def test_multiple_base_paths():
    cfg = _reload_config(BASE_PATHS="/media1,/media2")
    assert cfg.BASE_PATHS == ["/media1", "/media2"]
    assert cfg.BASE_PATH_LABELS == {"media1": "/media1", "media2": "/media2"}


def test_label_from_last_segment():
    cfg = _reload_config(BASE_PATHS="/mnt/nas/media1,/mnt/usb/media2")
    assert cfg.BASE_PATH_LABELS == {"media1": "/mnt/nas/media1", "media2": "/mnt/usb/media2"}


def test_label_collision_suffixed():
    cfg = _reload_config(BASE_PATHS="/mnt/a/media,/mnt/b/media")
    assert cfg.BASE_PATH_LABELS == {"media": "/mnt/a/media", "media-2": "/mnt/b/media"}


def test_default_when_unset():
    """When BASE_PATHS is not set (and dotenv doesn't provide it), default to /media."""
    import importlib
    import app.config as config_mod
    # Must mock load_dotenv at the dotenv module level so reload() calls the mock
    with patch("dotenv.load_dotenv"), \
         patch.dict(os.environ, {}, clear=True):
        importlib.reload(config_mod)
        assert config_mod.BASE_PATHS == ["/media"]


def test_base_path_singular_fallback():
    """When only BASE_PATH (singular) is set, it should be used as fallback."""
    import importlib
    import app.config as config_mod
    with patch("dotenv.load_dotenv"), \
         patch.dict(os.environ, {"BASE_PATH": "/mnt/nas"}, clear=True):
        importlib.reload(config_mod)
        assert config_mod.BASE_PATHS == ["/mnt/nas"]


def test_base_paths_takes_precedence():
    """When both BASE_PATHS and BASE_PATH are set, BASE_PATHS wins."""
    import importlib
    import app.config as config_mod
    with patch("dotenv.load_dotenv"), \
         patch.dict(os.environ, {"BASE_PATHS": "/media1,/media2", "BASE_PATH": "/old"}, clear=True):
        importlib.reload(config_mod)
        assert config_mod.BASE_PATHS == ["/media1", "/media2"]


def test_resolve_base_valid():
    cfg = _reload_config(BASE_PATHS="/media1,/media2")
    assert cfg.resolve_base("media1") == "/media1"
    assert cfg.resolve_base("media2") == "/media2"


def test_resolve_base_unknown():
    cfg = _reload_config(BASE_PATHS="/media1")
    import pytest
    with pytest.raises(ValueError, match="Unknown base"):
        cfg.resolve_base("nonexistent")


from pathlib import Path


def _make_dir_tree(base: Path, folders: list[str]):
    """Create directory structure under base."""
    for folder in folders:
        (base / folder).mkdir(parents=True, exist_ok=True)


def test_get_tvshow_dirs_multi_path(tmp_path):
    media1 = tmp_path / "media1"
    media2 = tmp_path / "media2"
    _make_dir_tree(media1, ["TV Shows/Show A/Season 01", "TV Shows/Show A/Season 02"])
    _make_dir_tree(media2, ["TV Shows/Show B/Season 01"])
    # Create video files so has_valid_files finds them
    (media1 / "TV Shows/Show A/Season 01/ep1.mp4").touch()
    (media1 / "TV Shows/Show A/Season 02/ep1.mp4").touch()
    (media2 / "TV Shows/Show B/Season 01/ep1.mp4").touch()

    _reload_config(
        BASE_PATHS=f"{media1},{media2}",
        TVSHOW_FOLDER_NAME="TV Shows",
        VALID_VIDEO_EXT=".mp4,.mkv",
    )

    from app.get_dirs import get_tvshow_dirs
    result = get_tvshow_dirs()
    paths = [d["path"] for d in result]
    bases = [d["base"] for d in result]

    assert "Show A/Season 01" in paths
    assert "Show A/Season 02" in paths
    assert "Show B/Season 01" in paths
    assert "media1" in bases
    assert "media2" in bases


def test_get_music_dirs_multi_path(tmp_path):
    media1 = tmp_path / "media1"
    media2 = tmp_path / "media2"
    _make_dir_tree(media1, ["Music/Artist A/Album 1"])
    _make_dir_tree(media2, ["Music/Artist B/Album 2"])
    (media1 / "Music/Artist A/Album 1/track.mp3").touch()
    (media2 / "Music/Artist B/Album 2/track.flac").touch()

    _reload_config(
        BASE_PATHS=f"{media1},{media2}",
        MUSIC_FOLDER_NAME="Music",
        VALID_MUSIC_EXT=".mp3,.flac",
    )

    from app.get_dirs import get_music_dirs
    result = get_music_dirs()
    paths = [d["path"] for d in result]

    assert "Artist A/Album 1" in paths
    assert "Artist B/Album 2" in paths


def test_get_cutter_dirs_multi_path(tmp_path):
    media1 = tmp_path / "media1"
    media2 = tmp_path / "media2"
    (media1 / "Movies").mkdir(parents=True)
    (media2 / "Videos").mkdir(parents=True)
    (media1 / "Movies/test.mp4").touch()
    (media2 / "Videos/test.mkv").touch()

    _reload_config(
        BASE_PATHS=f"{media1},{media2}",
        VALID_CUTTER_EXT=".mp4,.mkv",
    )

    from app.get_dirs import get_cutter_dirs
    result = get_cutter_dirs()
    paths = [d["path"] for d in result]

    assert "Movies" in paths
    assert "Videos" in paths


def test_get_download_dirs_includes_empty_directories_from_every_base(tmp_path):
    media1 = tmp_path / "media1"
    media2 = tmp_path / "media2"
    (media1 / "Empty").mkdir(parents=True)
    (media2 / "Documents").mkdir(parents=True)
    (media2 / "Documents" / "readme.txt").write_text("not media")

    _reload_config(BASE_PATHS=f"{media1},{media2}")

    from app.get_dirs import get_download_dirs

    assert get_download_dirs() == [
        {"path": "Documents", "base": "media2"},
        {"path": "Empty", "base": "media1"},
    ]


def test_missing_subfolder_skipped(tmp_path):
    """If a base path doesn't have the subfolder, it's just skipped."""
    media1 = tmp_path / "media1"
    media2 = tmp_path / "media2"
    _make_dir_tree(media1, ["TV Shows/Show A/Season 01"])
    (media1 / "TV Shows/Show A/Season 01/ep1.mp4").touch()
    media2.mkdir()  # No TV Shows subfolder

    _reload_config(
        BASE_PATHS=f"{media1},{media2}",
        TVSHOW_FOLDER_NAME="TV Shows",
        VALID_VIDEO_EXT=".mp4",
    )

    from app.get_dirs import get_tvshow_dirs
    result = get_tvshow_dirs()
    # media2 has no TV Shows subfolder so it contributes 0 entries
    bases = [d["base"] for d in result]
    assert "media2" not in bases
    assert all(d["base"] == "media1" for d in result)
    paths = [d["path"] for d in result]
    assert "Show A/Season 01" in paths
