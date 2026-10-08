"""Allowlist-root path security for the file adapter (spec SS4.7).

Order is spec-mandated: resolve symlinks FIRST, then prefix-check against the
resolved roots.  The returned relpath (relative to its root) is the ONLY path
form that may reach LogRecord attributes, envelopes, or goldens."""
from __future__ import annotations

from itertools import islice
from pathlib import Path
from typing import List, Tuple

try:
    from core.errors import AdapterError as _AdapterError
except ImportError:  # pragma: no cover — fallback if core.errors not on path
    _AdapterError = ValueError  # type: ignore[assignment,misc]


class PathOutsideRoots(_AdapterError):
    """The requested path resolves outside every configured allowlist root."""


# Most matching files one pattern returns (per call, over all roots).
MAX_MATCHES = 1000
# Most glob results (files, directories, entries behind symlinks that resolve
# outside the root) one pattern may scan; stops "link/**/*.x" with a symlink
# to / and huge trees that match nothing.
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

    ``capped`` is True when more than MAX_MATCHES files matched or the glob
    scanned more than MAX_SCANNED entries; then only the files found so far
    (at most MAX_MATCHES, in filesystem order) are returned, sorted by
    relpath.

    Security properties:
    - Empty pattern raises :exc:`PathOutsideRoots` (degenerate — glob would
      crash with IndexError on Python 3.12).
    - Pattern ``"."`` returns an empty list (names the root directory itself,
      never a file; avoiding a potential IndexError on some Python versions).
    - Absolute patterns always raise :exc:`PathOutsideRoots`.
    - Patterns with a ``..`` component always raise :exc:`PathOutsideRoots`
      before any glob runs: ``../../**/*`` would otherwise walk the whole
      filesystem above the root, even though nothing outside is returned.
    - Symlinks and ``..`` components are resolved before the root prefix-check
      so that escape attempts via either mechanism are caught.
    - Glob patterns silently skip matches that resolve outside their root.
    - Non-glob (exact-path) patterns raise :exc:`PathOutsideRoots` if the
      resolved target escapes (the §4.7 negative-test behaviour).
    - A direct probe is performed for non-glob patterns even when
      ``root.glob()`` returns nothing, preserving correct behaviour on Python
      3.10/3.11 where ``glob("../x")`` may not yield matches.
    - ``root.resolve()`` is called at the top of every root iteration so that
      macOS ``/var/folders → /private/var`` symlinks in ``tmp_path`` do not
      make every resolved match look like an escape.

    Returns pairs sorted by relpath; directories are never included.
    """
    # Guard degenerate patterns before reaching glob (which would crash).
    if not pattern:
        raise PathOutsideRoots("empty pattern is not allowed")
    if pattern == ".":
        return [], False

    if Path(pattern).is_absolute():
        raise PathOutsideRoots(f"absolute paths are not allowed: {pattern!r}")
    if ".." in Path(pattern).parts:
        raise PathOutsideRoots(f"'..' is not allowed in patterns: {pattern!r}")

    out: List[Tuple[Path, str]] = []
    escaped_exact = False
    capped = False
    scanned = 0
    scan_budget_hit = False

    for root in roots:
        root = root.resolve()  # macOS /var/folders → /private/var safety

        def _inside_files():
            nonlocal escaped_exact, scanned, scan_budget_hit
            for m in root.glob(pattern):
                scanned += 1
                if scanned > MAX_SCANNED:
                    scan_budget_hit = True
                    return
                real = m.resolve()
                inside = real == root or root in real.parents
                if not inside:
                    # Non-glob pattern that escapes: remember for post-loop raise.
                    escaped_exact = escaped_exact or not _is_glob(pattern)
                    continue
                if real.is_file():
                    yield (real, str(real.relative_to(root)))

        # Lazily, so a huge tree stops at the cap instead of being listed whole
        room = MAX_MATCHES - len(out)
        found = list(islice(_inside_files(), room + 1))
        if len(found) > room or scan_budget_hit:
            capped = True
            found = found[:room]
        out.extend(sorted(found, key=lambda t: t[1]))

        # Direct probe for py3.10/3.11 portability: those versions may not
        # yield results for ``root.glob("../x")``.  We replicate the same
        # resolve-then-prefix-check on the explicit candidate path so that an
        # exact non-glob pattern that escapes is caught even when glob returns
        # nothing.  This path is exercised by the dedicated monkeypatch test.
        if not _is_glob(pattern) and not escaped_exact:
            candidate = root / pattern
            real = candidate.resolve()
            inside = real == root or root in real.parents
            if not inside and real.exists():
                escaped_exact = True

        if capped:
            break

    if not out and not _is_glob(pattern) and escaped_exact:
        raise PathOutsideRoots(
            f"{pattern!r} resolves outside the configured roots")

    return sorted(out, key=lambda t: t[1]), capped
