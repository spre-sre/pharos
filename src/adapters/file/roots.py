"""Allowlist-root path security for the file adapter (spec SS4.7).

Order is spec-mandated: resolve symlinks FIRST, then prefix-check against the
resolved roots.  The returned relpath (relative to its root) is the ONLY path
form that may reach LogRecord attributes, envelopes, or goldens."""
from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import Path
from typing import FrozenSet, List, Set, Tuple

try:
    from core.errors import AdapterError as _AdapterError
except ImportError:  # pragma: no cover — fallback if core.errors not on path
    _AdapterError = ValueError  # type: ignore[assignment,misc]


class PathOutsideRoots(_AdapterError):
    """The requested path resolves outside every configured allowlist root."""


# Most matching files one pattern returns (per call, over all roots).
MAX_MATCHES = 1000
# Most directory entries one pattern may read while walking (every entry of
# every directory listed counts, matching or not), so a huge tree that matches
# nothing cannot be walked whole.
MAX_SCANNED = 50_000


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def resolve_matches(pattern: str, roots: Tuple[Path, ...]) -> List[Tuple[Path, str]]:
    """Return (abs_path, relpath) pairs for *pattern* inside *roots*
    (at most MAX_MATCHES; see :func:`resolve_matches_bounded`)."""
    return resolve_matches_bounded(pattern, roots)[0]


def resolve_matches_bounded(pattern: str,
                            roots: Tuple[Path, ...]) -> Tuple[List[Tuple[Path, str]], bool]:
    """Return ((abs_path, relpath) pairs for *pattern* inside *roots*, capped).

    ``capped`` is True when more than MAX_MATCHES files matched or the walk
    read more than MAX_SCANNED directory entries; then only the files found
    so far (at most MAX_MATCHES) are returned. Pairs are sorted by relpath;
    directories are never included.

    Security properties:
    - Empty pattern raises :exc:`PathOutsideRoots`; ``"."`` returns nothing.
    - Absolute patterns and patterns with a ``..`` component raise
      :exc:`PathOutsideRoots` before any filesystem access.
    - Exact (non-glob) patterns are a direct path check: the target is
      resolved, then prefix-checked; one that escapes raises.
    - Glob patterns are matched by our own walker (not ``Path.glob``): it
      descends only into directories that can still match the pattern,
      counts every entry it reads against MAX_SCANNED, follows a symlinked
      directory only when it resolves inside the root (and never twice),
      and silently skips files that resolve outside the root.
    - Each root is resolved first (macOS ``/var/folders -> /private/var``).
    """
    if not pattern:
        raise PathOutsideRoots("empty pattern is not allowed")
    if pattern == ".":
        return [], False
    if Path(pattern).is_absolute():
        raise PathOutsideRoots(f"absolute paths are not allowed: {pattern!r}")
    parts = Path(pattern).parts
    if ".." in parts:
        raise PathOutsideRoots(f"'..' is not allowed in patterns: {pattern!r}")

    resolved_roots = [root.resolve() for root in roots]

    if not _is_glob(pattern):
        out: List[Tuple[Path, str]] = []
        escaped = False
        for root in resolved_roots:
            real = (root / pattern).resolve()
            if real == root or root in real.parents:
                if real.is_file():
                    out.append((real, str(real.relative_to(root))))
            elif real.exists():
                escaped = True
        if not out and escaped:
            raise PathOutsideRoots(f"{pattern!r} resolves outside the configured roots")
        return sorted(out, key=lambda t: t[1]), False

    walker = _Walker(tuple(parts))
    for root in resolved_roots:
        walker.walk_root(root)
        if walker.capped:
            break
    return sorted(walker.found, key=lambda t: t[1]), walker.capped


class _Walker:
    """Bounded glob over directory trees with pathlib-like segment matching
    (``**`` = zero or more directories, other segments via fnmatchcase)."""

    def __init__(self, segments: Tuple[str, ...]):
        self.segments = segments
        self.found: List[Tuple[Path, str]] = []
        self.capped = False
        self.scanned = 0

    def _closure(self, states: Set[int]) -> FrozenSet[int]:
        out = set(states)
        stack = list(states)
        while stack:
            i = stack.pop()
            if i < len(self.segments) and self.segments[i] == "**" and i + 1 not in out:
                out.add(i + 1)
                stack.append(i + 1)
        return frozenset(out)

    def _step(self, states: FrozenSet[int], name: str, is_dir: bool) -> FrozenSet[int]:
        nxt: Set[int] = set()
        for i in states:
            if i >= len(self.segments):
                continue
            seg = self.segments[i]
            if seg == "**":
                if is_dir:
                    nxt.add(i)  # '**' consumes this directory level
            elif fnmatchcase(name, seg):
                nxt.add(i + 1)
        return self._closure(nxt)

    def walk_root(self, root: Path) -> None:
        visited: Set[Path] = {root}
        stack = [(root, self._closure({0}))]
        end = len(self.segments)
        while stack and not self.capped:
            directory, states = stack.pop()
            try:
                with os.scandir(directory) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue  # unreadable or vanished: skipped, as glob does
            subdirs = []
            for entry in entries:
                self.scanned += 1
                if self.scanned > MAX_SCANNED:
                    self.capped = True
                    return
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                    is_file = not is_dir and entry.is_file(follow_symlinks=True)
                    is_link = entry.is_symlink()
                except OSError:
                    continue
                if is_dir:
                    sub_states = self._step(states, entry.name, True)
                    if not any(i < end for i in sub_states):
                        continue  # nothing below can match
                    real = Path(entry.path).resolve() if is_link else Path(entry.path)
                    if real in visited or not (real == root or root in real.parents):
                        continue  # loop, or a symlink out of the root
                    visited.add(real)
                    subdirs.append((real, sub_states))
                elif is_file and end in self._step(states, entry.name, False):
                    real = Path(entry.path).resolve() if is_link else Path(entry.path)
                    if not (root in real.parents):
                        continue  # symlink out of the root: silently skipped
                    if len(self.found) >= MAX_MATCHES:
                        self.capped = True
                        return
                    self.found.append((real, str(real.relative_to(root))))
            stack.extend(reversed(subdirs))
