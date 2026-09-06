"""Tests for directory scanning logic."""
import os
import pytest
from app.get_dirs import ExpiringCache, get_dirs, has_valid_files


def test_expiring_cache_reuses_value_until_ttl_then_reloads():
    now = [0.0]
    calls: list[int] = []

    def load() -> list[int]:
        calls.append(len(calls) + 1)
        return [calls[-1]]

    cache = ExpiringCache(load, ttl_seconds=15, clock=lambda: now[0])

    assert cache() == [1]
    assert cache() == [1]
    now[0] = 15.0
    assert cache() == [2]
    assert calls == [1, 2]


def test_expiring_cache_clear_forces_immediate_reload():
    calls: list[int] = []
    cache = ExpiringCache(
        lambda: calls.append(len(calls) + 1) or [calls[-1]],
        ttl_seconds=15,
        clock=lambda: 0.0,
    )

    assert cache() == [1]
    cache.cache_clear()
    assert cache() == [2]


def test_expiring_cache_ttl_starts_after_loader_finishes():
    now = [0.0]
    calls: list[int] = []

    def slow_load() -> list[int]:
        calls.append(len(calls) + 1)
        now[0] += 20.0
        return [calls[-1]]

    cache = ExpiringCache(slow_load, ttl_seconds=15, clock=lambda: now[0])

    assert cache() == [1]
    assert cache() == [1]
    assert calls == [1]


class TestHasValidFiles:
    def test_finds_valid_extension(self, tmp_path):
        (tmp_path / "test.mp4").write_bytes(b"\x00")
        assert has_valid_files(str(tmp_path), {".mp4"}) is True

    def test_no_valid_extension(self, tmp_path):
        (tmp_path / "test.txt").write_text("hello")
        assert has_valid_files(str(tmp_path), {".mp4"}) is False

    def test_case_insensitive(self, tmp_path):
        (tmp_path / "test.MP4").write_bytes(b"\x00")
        assert has_valid_files(str(tmp_path), {".mp4"}) is True

    def test_empty_directory(self, tmp_path):
        assert has_valid_files(str(tmp_path), {".mp4"}) is False

    def test_nested_files(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "video.mkv").write_bytes(b"\x00")
        assert has_valid_files(str(tmp_path), {".mkv"}) is True


class TestGetDirs:
    def test_walks_tree_once_when_filtering_extensions(self, tmp_path, monkeypatch):
        season = tmp_path / "show" / "season01"
        season.mkdir(parents=True)
        (season / "ep.mkv").write_bytes(b"\x00")
        real_walk = os.walk
        walked_roots: list[str] = []

        def recording_walk(path):
            walked_roots.append(os.fspath(path))
            return real_walk(path)

        monkeypatch.setattr("app.get_dirs.os.walk", recording_walk)

        assert get_dirs(str(tmp_path), {".mkv"}) == ["show", "show/season01"]
        assert walked_roots == [str(tmp_path)]

    def test_none_extensions_include_empty_and_non_media_directories(self, tmp_path):
        (tmp_path / "empty").mkdir()
        text_only = tmp_path / "text-only"
        text_only.mkdir()
        (text_only / "notes.txt").write_text("notes")

        assert get_dirs(str(tmp_path), None) == ["empty", "text-only"]

    def test_finds_directories_with_valid_files(self, tmp_path):
        sub = tmp_path / "show" / "season01"
        sub.mkdir(parents=True)
        (sub / "ep.mp4").write_bytes(b"\x00")

        result = get_dirs(str(tmp_path), {".mp4"})
        assert len(result) >= 1
        assert any("season01" in d for d in result)

    def test_excludes_trickplay(self, tmp_path):
        trick = tmp_path / ".trickplay"
        trick.mkdir()
        (trick / "thumb.mp4").write_bytes(b"\x00")

        result = get_dirs(str(tmp_path), {".mp4"})
        assert not any(".trickplay" in d for d in result)

    def test_empty_base(self, tmp_path):
        result = get_dirs(str(tmp_path), {".mp4"})
        assert result == []

    def test_returns_relative_paths(self, tmp_path):
        sub = tmp_path / "artist" / "album"
        sub.mkdir(parents=True)
        (sub / "song.flac").write_bytes(b"\x00")

        result = get_dirs(str(tmp_path), {".flac"})
        for d in result:
            assert not d.startswith(str(tmp_path))
            assert "/" in d or d == os.path.basename(d)

    def test_sorted_output(self, tmp_path):
        for name in ["c_dir", "a_dir", "b_dir"]:
            d = tmp_path / name
            d.mkdir()
            (d / "file.mp4").write_bytes(b"\x00")

        result = get_dirs(str(tmp_path), {".mp4"})
        assert result == sorted(result)
