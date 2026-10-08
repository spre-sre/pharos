"""File-based LogSource adapter (spec §4.7 + phase-3 plan).

``FileLogSource`` is the canonical reference implementation of the LogSource
protocol for local file trees.  All output honours the RELPATH INVARIANT:
``LogRecord.attributes["file"]`` is always root-relative — no absolute path
may ever appear there, in an envelope key, or in a golden.

Security: roots are resolved with ``strict=True`` at construction so that any
missing root raises immediately.  Matches are resolved and prefix-checked via
``resolve_matches`` before any content is read (resolve-then-check, spec §4.7).
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import adapters.file.roots as _roots
from adapters.file.roots import resolve_matches_bounded
from adapters.file.sniff import detect_format, parse_line
from core.selector import (
    Entity,
    Limit,
    Matchers,
    Native,
    SelectorNotSupported,
    TimeWindow,
)
from core.signals import LogBatch, LogRecord, Provenance

# Number of non-blank lines fed to detect_format (mirrors sniff._SAMPLE_LINES).
_SAMPLE_LINES: int = 20
# Longest piece of a line read at once; a longer line becomes several records,
# so a file with no newlines cannot be loaded into memory whole. The pieces
# after the first carry no timestamp (kept as undated under a time window)
# and a split JSON line does not parse as JSON.
MAX_LINE_CHARS: int = 1 << 20
# File globs and reads run here, not on the event loop or the shared default
# executor that other to_thread users rely on.
_FILE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="file-source")


def _iter_lines(path: Path) -> Iterator[str]:
    """Stream the lines of *path* with the boundaries of ``str.splitlines()``.

    Reads at most MAX_LINE_CHARS at a time, so the limits in ``fetch_logs``
    stop the read instead of applying after a whole-file ``read_text``.
    """
    with open(path, errors="replace") as fh:
        while True:
            chunk = fh.readline(MAX_LINE_CHARS)
            if not chunk:
                return
            # splitlines also breaks on \x0b, \x0c, \x1c-\x1e, \x85, \u2028 ...
            yield from chunk.splitlines()


def _is_active_window(window: Optional[TimeWindow]) -> bool:
    """Return True when *window* imposes at least one time bound."""
    if window is None:
        return False
    return window.start is not None or window.end is not None


def _try_parse_dt(ts_str: str) -> Optional[datetime]:
    """Try to parse *ts_str* as an ISO-8601 datetime; return None on failure.

    The ``Z`` suffix is normalised to ``+00:00`` for Python 3.10 compatibility
    (``datetime.fromisoformat`` gained full ISO-8601 support only in 3.11).
    """
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _in_window(ts: datetime, window: TimeWindow) -> bool:
    """Return True when *ts* falls within the half-open [start, end] interval.

    A ``TypeError`` (mixed aware/naive comparison) is caught conservatively:
    the record is KEPT (better to include than silently drop).
    """
    try:
        if window.start is not None and ts < window.start:
            return False
        if window.end is not None and ts > window.end:
            return False
        return True
    except TypeError:
        # Mixed aware/naive datetimes — can't filter, keep the record.
        return True


class FileLogSource:
    """LogSource backed by local file trees restricted to configured roots.

    Selector support: :class:`~core.selector.Entity` only — ``name_or_pattern``
    is a glob relative to the configured roots.  :class:`~core.selector.Matchers`
    and :class:`~core.selector.Native` raise :exc:`~core.selector.SelectorNotSupported`.

    Construction raises :exc:`FileNotFoundError` when any root does not exist
    (``Path.resolve(strict=True)``).
    """

    def __init__(self, roots: Tuple[str, ...]) -> None:
        # strict=True: missing root raises FileNotFoundError immediately.
        self._roots: Tuple[Path, ...] = tuple(
            Path(r).resolve(strict=True) for r in roots
        )

    async def fetch_logs(
        self,
        selector: Any,
        window: Optional[TimeWindow],
        limit: Optional[Limit],
    ) -> LogBatch:
        """Fetch log records for *selector* filtered by *window* and *limit*.

        Returns a :class:`~core.signals.LogBatch`.  An empty glob produces an
        empty batch (never raises).  :exc:`~adapters.file.roots.PathOutsideRoots`
        propagates from :func:`~adapters.file.roots.resolve_matches` unchanged.

        The glob and the file reads run in a worker thread, never on the
        event loop.
        """
        if isinstance(selector, Matchers):
            raise SelectorNotSupported(
                requested=type(selector).__name__, supported=("Entity",)
            )
        if isinstance(selector, Native):
            raise SelectorNotSupported(
                requested=type(selector).__name__, supported=("Entity",)
            )

        loop = asyncio.get_running_loop()
        call = functools.partial(
            contextvars.copy_context().run, self._fetch_sync, selector, window, limit)
        return await loop.run_in_executor(_FILE_EXECUTOR, call)

    def _fetch_sync(
        self,
        selector: Any,
        window: Optional[TimeWindow],
        limit: Optional[Limit],
    ) -> LogBatch:
        pattern: str = selector.name_or_pattern  # Entity.name_or_pattern

        # PathOutsideRoots propagates to the caller unchanged (spec §4.7).
        matches: List[Tuple[Path, str]]
        matches, capped = resolve_matches_bounded(pattern, self._roots)

        max_rec: Optional[int] = limit.max_records if limit else None
        max_bytes: Optional[int] = limit.max_bytes if limit else None
        active_window: bool = _is_active_window(window)

        records: List[LogRecord] = []
        notes: List[str] = []
        if capped:
            notes.append(
                f"search stopped at a limit ({_roots.MAX_MATCHES} files or "
                f"{_roots.MAX_SCANNED} directory entries per pattern): "
                f"{len(matches)} files selected, more may match")
        total_bytes: int = 0
        truncated: bool = False
        undated_note_added: bool = False
        done: bool = False  # flag to break out of nested loops

        for abs_path, relpath in matches:
            if done:
                break

            line_iter = _iter_lines(abs_path)
            # Sample the first non-blank lines for format detection.
            head: List[str] = []
            sample: List[str] = []
            for ln in line_iter:
                head.append(ln)
                if ln.strip():
                    sample.append(ln)
                    if len(sample) >= _SAMPLE_LINES:
                        break
            fmt = detect_format(sample)

            for raw_line in chain(head, line_iter):
                if not raw_line.strip():
                    continue  # skip blank lines — no meaningful body

                parsed: Dict[str, Any] = parse_line(raw_line, fmt, relpath)
                record = LogRecord(
                    timestamp=parsed["timestamp"],
                    body=parsed["body"],
                    severity=parsed["severity"],
                    attributes=parsed["attributes"],
                )

                # ── TimeWindow filtering ──────────────────────────────────
                if active_window:
                    ts_str = record.timestamp
                    if ts_str is not None:
                        ts_dt = _try_parse_dt(ts_str)
                        if ts_dt is not None:
                            # Dated record: apply window filter.
                            if not _in_window(ts_dt, window):
                                continue
                        # ts_str present but not ISO-parseable → treat as
                        # undated for filtering purposes → keep + note.
                        else:
                            if not undated_note_added:
                                notes.append(
                                    "undated records kept without time filtering: "
                                    "timestamp absent or not ISO-parseable"
                                )
                                undated_note_added = True
                    else:
                        # timestamp=None → undated → keep + note.
                        if not undated_note_added:
                            notes.append(
                                "undated records kept without time filtering: "
                                "timestamp absent or not ISO-parseable"
                            )
                            undated_note_added = True

                # ── max_bytes accumulation (cutoff if limit set) ──────────
                total_bytes += len(record.body)
                if max_bytes is not None and total_bytes > max_bytes:
                    truncated = True
                    done = True
                    break

                records.append(record)

                # ── max_records head-N cutoff ─────────────────────────────
                if max_rec is not None and len(records) >= max_rec:
                    # Peek ahead: if there are more lines/files, we truncated.
                    # Conservatively mark truncated=True here; we clear it
                    # after the loop if no more content exists.
                    truncated = True
                    done = True
                    break

        # ── Resolve conservative truncated flag ───────────────────────────
        # If we hit max_rec exactly but it was the last record in the last
        # file, reset truncated to False.
        if truncated and max_rec is not None and not (max_bytes is not None and total_bytes > (max_bytes or 0)):
            # Check whether there are ANY remaining records after what we kept.
            # The done flag was set when len(records) == max_rec; if the
            # remaining content (current file tail + subsequent files) has zero
            # more non-blank lines, it wasn't really truncated.
            remaining = _has_more_content(matches, records, max_rec)
            if not remaining:
                truncated = False
        if capped:
            truncated = True  # files beyond the cap were not read

        return LogBatch(
            records=records,
            provenance=Provenance(
                adapter="file",
                query={"pattern": pattern, "total_bytes": total_bytes},
                truncated=truncated,
                notes=tuple(notes),
            ),
        )


def _has_more_content(
    matches: List[Tuple[Path, str]],
    collected: List[LogRecord],
    max_rec: int,
) -> bool:
    """Return True if there are more non-blank lines in *matches* beyond
    the first *max_rec* records already collected.

    Used to distinguish "hit limit with more to go" from "last record was
    exactly the limit" so that ``provenance.truncated`` is not set when the
    stream was exhausted naturally at the limit.
    """
    seen: int = 0
    for abs_path, _ in matches:
        for raw_line in _iter_lines(abs_path):
            if not raw_line.strip():
                continue
            seen += 1
            if seen > max_rec:
                return True  # there IS content beyond what we collected
    return False
