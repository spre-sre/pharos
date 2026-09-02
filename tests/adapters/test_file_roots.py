"""Tests for src/adapters/file/roots.py — allowlist-root path security.

TDD order: tests written first, implementation follows.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from adapters.file.roots import PathOutsideRoots, resolve_matches


# ─── basic glob behaviour ─────────────────────────────────────────────────────


def test_simple_glob_returns_relpath_sorted(tmp_path):
    """*.log matches all log files in root, returned sorted by relpath."""
    (tmp_path / "b.log").write_text("beta")
    (tmp_path / "a.log").write_text("alpha")
    results = resolve_matches("*.log", (tmp_path,))
    relpaths = [r for _, r in results]
    assert relpaths == sorted(relpaths), "results must be sorted by relpath"
    assert set(relpaths) == {"a.log", "b.log"}
    for abs_p, rel in results:
        assert abs_p.is_file()
        assert abs_p == tmp_path.resolve() / rel


def test_subdir_glob_pattern(tmp_path):
    """sub/*.log returns the nested file with the correct relative path."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.log").write_text("cee")
    results = resolve_matches("sub/*.log", (tmp_path,))
    assert len(results) == 1
    _, relpath = results[0]
    assert relpath == "sub/c.log"


def test_nonexistent_pattern_returns_empty_list(tmp_path):
    """A glob that matches nothing returns an empty list without raising."""
    results = resolve_matches("*.log", (tmp_path,))
    assert results == []


def test_directories_are_never_returned(tmp_path):
    """Directories matching the pattern are excluded; regular files are included."""
    (tmp_path / "dir.log").mkdir()
    (tmp_path / "file.log").write_text("real content")
    results = resolve_matches("*.log", (tmp_path,))
    relpaths = [r for _, r in results]
    assert "dir.log" not in relpaths
    assert "file.log" in relpaths


# ─── security: absolute pattern ───────────────────────────────────────────────


def test_absolute_pattern_raises(tmp_path):
    """An absolute path pattern always raises PathOutsideRoots."""
    with pytest.raises(PathOutsideRoots, match="absolute"):
        resolve_matches("/etc/passwd", (tmp_path,))


# ─── security: exact-path escapes ────────────────────────────────────────────


def test_exact_dotdot_pattern_outside_raises(tmp_path):
    """A non-glob '../outside.log' that escapes the root raises PathOutsideRoots."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("secret")
    with pytest.raises(PathOutsideRoots):
        resolve_matches("../outside.log", (root_dir,))


def test_exact_symlink_pointing_outside_raises(tmp_path):
    """A non-glob pattern naming a symlink that resolves outside the root raises."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    outside = tmp_path / "secret.log"
    outside.write_text("secret")
    link = root_dir / "evil.log"
    os.symlink(outside, link)
    with pytest.raises(PathOutsideRoots):
        resolve_matches("evil.log", (root_dir,))


# ─── security: glob silently skips escapes ───────────────────────────────────


def test_symlink_to_outside_excluded_from_glob(tmp_path):
    """A glob pattern silently skips symlinks that resolve outside the root."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "safe.log").write_text("safe")
    outside = tmp_path / "real.log"
    outside.write_text("outside content")
    link = root_dir / "evil.log"
    os.symlink(outside, link)
    results = resolve_matches("*.log", (root_dir,))
    relpaths = [r for _, r in results]
    assert "evil.log" not in relpaths, "symlink escaping root must be silently skipped"
    assert "safe.log" in relpaths, "legitimate file inside root must still appear"


# ─── py3.10/3.11 portability: direct probe path ──────────────────────────────


def test_forced_probe_via_monkeypatched_glob(tmp_path, monkeypatch):
    """Direct probe raises PathOutsideRoots even when Path.glob returns [] (py3.10/3.11)."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    outside = tmp_path / "probe_target.log"
    outside.write_text("data outside root")

    original_glob = Path.glob

    def patched_glob(self, pattern, **kw):
        # Simulate py3.10/3.11: glob("../x") yields nothing for non-glob patterns
        if not any(ch in pattern for ch in "*?["):
            return iter([])
        return original_glob(self, pattern, **kw)

    monkeypatch.setattr(Path, "glob", patched_glob)

    with pytest.raises(PathOutsideRoots):
        resolve_matches("../probe_target.log", (root_dir,))


# ─── carry-in hardening: degenerate patterns ─────────────────────────────────


def test_empty_pattern_raises_path_outside_roots(tmp_path):
    """Empty string pattern raises PathOutsideRoots (not a raw ValueError/error)."""
    with pytest.raises(PathOutsideRoots):
        resolve_matches("", (tmp_path,))


def test_dot_pattern_returns_empty_list(tmp_path):
    """Pattern '.' returns an empty list — it names a directory, never a file."""
    (tmp_path / "file.txt").write_text("content")
    result = resolve_matches(".", (tmp_path,))
    assert result == []
