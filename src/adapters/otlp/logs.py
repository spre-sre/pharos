"""OtlpLogSource: LogSource backed by an in-process LogRing (phase 5 Task 2).

The adapter reads from a bounded FIFO ring buffer populated by the OTLP
receiver (Task 3).  This module makes ZERO outbound HTTP calls — all I/O
is in-process ring access.

Fetch semantics
  ``fetch = (window ∩ snapshot) filtered by selector``.  The snapshot is taken
  under the ring's lock (via ``ring.snapshot()``); all window/selector
  filtering is done outside the lock (M9b).

Tail-N divergence from file adapter
  File adapter uses head-N (first-N records in file order — oldest first).
  OtlpLogSource uses tail-N: the NEWEST N records from the window-filtered
  set, returned in ascending (oldest-first) order.  The ring is a recency
  buffer; head-N would return the OLDEST records rather than the most recent,
  which is incorrect for the OTLP ingest use-case.

Selector support
  - :class:`~core.selector.Entity`: ``fnmatch`` on the ``"entity"`` record
    attribute (set by the parser from resource attributes).
  - :class:`~core.selector.Matchers`: exact equality on ALL ``terms``.
  - :class:`~core.selector.Native`: raises :exc:`~core.selector.SelectorNotSupported`.

Undated records (mirrors file/logs.py:148–173 verbatim)
  When the window is active (at least one bound set):
    - ``timestamp=None`` → keep + add the undated note once.
    - timestamp string that cannot be parsed as ISO → same.
  When the window is unbounded: all records are kept without comment.

Skew note (security, §4.2.1)
  If a record's sender timestamp falls outside ``[covered.start, now]``
  while window-filtering is active, a note is added containing ONLY the
  boundary ISO-Z strings — no attacker-controlled record content.

Truncated-records note
  If ``ring.stats()["truncated_records"] > 0``, a note is added reporting
  the cumulative count.  These records were truncated during ingest.

Provenance windows (V6 pinned rule)
  ``requested_window``:
    start → ``"unbounded"`` when ``window.start is None``,
             else ``iso_z(window.start)``
    end   → ``iso_z(now_fn())`` when ``window.end is None``,
             else ``iso_z(window.end)``
  ``covered_window``: ``ring.covered_window()`` (receiver-side timestamps).

Comparisons use tz-aware datetimes — NEVER ISO strings (V6).

Outbound calls
  ZERO — pure in-process ring access.
"""
from __future__ import annotations

import fnmatch
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from adapters.otlp.rings import LogRing, iso_z
from core.selector import Entity, Limit, Matchers, Native, SelectorNotSupported, TimeWindow
from core.signals import LogBatch, LogRecord, Provenance

# Shared undated-record note text — mirrors file/logs.py verbatim.
_UNDATED_NOTE = (
    "undated records kept without time filtering: "
    "timestamp absent or not ISO-parseable"
)


def _try_parse_dt(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-Z timestamp string to a tz-aware datetime; None on failure."""
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _in_window(ts: datetime, window: TimeWindow) -> bool:
    """Return True when ``ts`` falls within ``[window.start, window.end]``.

    ``TypeError`` (naive/aware comparison mismatch) → keep the record
    conservatively (same as file adapter).
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


class OtlpLogSource:
    """LogSource backed by an in-process :class:`~adapters.otlp.rings.LogRing`.

    Parameters
    ----------
    ring:
        Ring buffer populated by the OTLP receiver.
    opts:
        Validated options dict from :func:`~adapters.otlp.config.validate_otlp_options`.
        Currently reserved (not used for fetching in Task 2).
    now_fn:
        Injectable clock returning POSIX float.  Defaults to ``time.time``.
        Used ONLY for ``requested_window`` end rendering — the ring uses its
        own ``now_fn`` for ``covered_window``.
    """

    def __init__(
        self,
        ring: LogRing,
        opts: Dict[str, Any],
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._ring = ring
        self._opts = opts
        self._now_fn = now_fn

    async def fetch_logs(
        self,
        selector: Any,
        window: Optional[TimeWindow],
        limit: Optional[Limit],
    ) -> LogBatch:
        """Fetch log records from the ring.

        Parameters
        ----------
        selector:
            :class:`~core.selector.Entity` (fnmatch on ``"entity"`` attribute),
            :class:`~core.selector.Matchers` (exact equality on all terms), or
            :class:`~core.selector.Native` (raises
            :exc:`~core.selector.SelectorNotSupported`).
        window:
            Optional :class:`~core.selector.TimeWindow`.  Filtering is on
            SENDER timestamps (``record.timestamp``), not receiver timestamps.
        limit:
            Optional :class:`~core.selector.Limit`.  ``max_records`` → tail-N
            (newest N in ascending order).  ``max_bytes`` is ignored.

        Returns
        -------
        :class:`~core.signals.LogBatch`
        """
        if isinstance(selector, Native):
            raise SelectorNotSupported(
                requested=type(selector).__name__,
                supported=("Entity", "Matchers"),
            )

        now_ts = self._now_fn()
        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)

        # ── Provenance windows (V6 pinned rule) ───────────────────────────
        if window is None or (window.start is None and window.end is None):
            requested_window: Tuple[str, str] = ("unbounded", iso_z(now_ts))
        else:
            w_start = (
                "unbounded" if window.start is None else iso_z(window.start)
            )
            w_end = (
                iso_z(now_ts) if window.end is None else iso_z(window.end)
            )
            requested_window = (w_start, w_end)

        covered_window = self._ring.covered_window()

        # Parse covered_window start for skew detection (ISO-Z → tz-aware dt).
        covered_start_dt: Optional[datetime] = _try_parse_dt(covered_window[0])

        # ── Snapshot (list copy taken under ring lock; filter OUTSIDE) ────
        snapshot: List[Tuple[float, LogRecord]] = self._ring.snapshot()

        active_window = (
            window is not None
            and (window.start is not None or window.end is not None)
        )

        records: List[LogRecord] = []
        notes: List[str] = []
        undated_noted = False
        skew_noted = False

        for _recv_ts, record in snapshot:
            # ── Selector filtering ────────────────────────────────────────
            if isinstance(selector, Entity):
                entity_val = str(record.attributes.get("entity", ""))
                if not fnmatch.fnmatch(entity_val, selector.name_or_pattern):
                    continue
            elif isinstance(selector, Matchers):
                if not all(
                    record.attributes.get(k) == v
                    for k, v in selector.terms.items()
                ):
                    continue

            # ── Window filtering on SENDER timestamps ─────────────────────
            include = True
            if active_window:
                ts_str = record.timestamp
                if ts_str is not None:
                    ts_dt = _try_parse_dt(ts_str)
                    if ts_dt is not None:
                        # Dated record: apply window filter.
                        if not _in_window(ts_dt, window):
                            include = False  # outside window — skip
                        elif not skew_noted and covered_start_dt is not None:
                            # Skew: sender ts outside [covered.start, now].
                            # Note: timestamps ONLY — no attacker strings.
                            if ts_dt < covered_start_dt or ts_dt > now_dt:
                                skew_noted = True
                                notes.append(
                                    "skew: sender timestamp outside covered window "
                                    f"[{covered_window[0]}, {iso_z(now_ts)}]"
                                )
                    else:
                        # Non-ISO-parseable string → undated → keep + note.
                        if not undated_noted:
                            notes.append(_UNDATED_NOTE)
                            undated_noted = True
                else:
                    # timestamp=None → undated → keep + note (mirror file adapter).
                    if not undated_noted:
                        notes.append(_UNDATED_NOTE)
                        undated_noted = True

            if include:
                records.append(record)

        # ── Tail-N (newest N, returned in ascending order) ────────────────
        # snapshot() is oldest-first; records[-N:] keeps newest N in ascending
        # order.  Differs from file adapter's head-N (oldest-first cut).
        max_rec = limit.max_records if limit else None
        prov_truncated = False
        if max_rec is not None and len(records) > max_rec:
            records = records[-max_rec:]
            prov_truncated = True

        # ── Truncated-records note (from ring stats) ──────────────────────
        ring_stats = self._ring.stats()
        if ring_stats["truncated_records"] > 0:
            notes.append(
                f"{ring_stats['truncated_records']} record(s) truncated "
                f"since receiver start"
            )

        return LogBatch(
            records=records,
            provenance=Provenance(
                adapter="otlp",
                query={},
                requested_window=requested_window,
                covered_window=covered_window,
                truncated=prov_truncated,
                notes=tuple(notes),
                grouping_attr="entity",
            ),
        )
