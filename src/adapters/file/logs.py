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

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adapters.file.roots import resolve_matches
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
        """
        if isinstance(selector, Matchers):
            raise SelectorNotSupported(
                requested=type(selector).__name__, supported=("Entity",)
            )
        if isinstance(selector, Native):
            raise SelectorNotSupported(
                requested=type(selector).__name__, supported=("Entity",)
            )

        pattern: str = selector.name_or_pattern  # Entity.name_or_pattern

        # PathOutsideRoots propagates to the caller unchanged (spec §4.7).
        matches: List[Tuple[Path, str]] = resolve_matches(pattern, self._roots)

        max_rec: Optional[int] = limit.max_records if limit else None
        max_bytes: Optional[int] = limit.max_bytes if limit else None
        active_window: bool = _is_active_window(window)

        records: List[LogRecord] = []
        notes: List[str] = []
        total_bytes: int = 0
        truncated: bool = False
        undated_note_added: bool = False
        done: bool = False  # flag to break out of nested loops

        for abs_path, relpath in matches:
            if done:
                break

            content = abs_path.read_text(errors="replace")
            lines = content.splitlines()

            # Sample non-blank lines for format detection.
            sample = [ln for ln in lines if ln.strip()][:_SAMPLE_LINES]
            fmt = detect_format(sample)

            for raw_line in lines:
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
        content = abs_path.read_text(errors="replace")
        for raw_line in content.splitlines():
            if not raw_line.strip():
                continue
            seen += 1
            if seen > max_rec:
                return True  # there IS content beyond what we collected
    return False
