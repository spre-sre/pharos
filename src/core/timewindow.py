"""Pure helper: derive a TimeWindow from the standard time-parameter set.

Design notes
------------
* Precedence for ``start``:  since_seconds  >  start_time  >  time_period  >  None.
  This matches the frozen legacy ``parse_time_parameters`` order
  (helpers/utils.py:193/:198/:229) so that the same call windows identically
  on both the file-adapter path and the k8s path.

* ``parse_time_period`` (helpers/utils.py:148) is pure and small but lives in
  the helpers package.  To avoid a core→helpers module-cycle risk the import is
  performed as a lazy guarded import INSIDE the function body, executed only
  when time_period is actually needed.  This is consistent with other
  guarded-import patterns in the codebase.

* ``tail_lines`` is deliberately NOT accepted: it is a Limit, not a window.

* ``now`` is injectable for determinism in tests.  Production code leaves it
  as None; the function then resolves it to ``datetime.now(timezone.utc)``.

* Invalid ISO strings raise ``ValueError`` naming the offending parameter.

* Naive datetimes (no tzinfo) are treated as UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from core.selector import TimeWindow


def make_time_window(
    since_seconds: Optional[int] = None,
    time_period: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
) -> TimeWindow:
    """Derive a TimeWindow from the standard time-parameter set.

    Parameters
    ----------
    since_seconds:
        Compute ``start`` as ``now - timedelta(seconds=since_seconds)``.
    time_period:
        Human-readable period such as ``'1h'``, ``'30m'``, ``'2d'``.
        Parsed via ``parse_time_period`` (helpers/utils.py).  Wins over
        nothing — lowest priority among the three start-selectors.
    start_time:
        ISO-8601 string (``Z`` suffix tolerated).  Middle priority.
    end_time:
        ISO-8601 string.  Always parsed independently; never takes precedence
        over or is gated on the start-selectors.
    now:
        Reference instant.  Inject in tests for determinism; leave ``None``
        in production (resolved to ``datetime.now(timezone.utc)``).

    Returns
    -------
    TimeWindow
        A frozen dataclass with ``start`` and/or ``end`` filled in.
        Either field is ``None`` when no corresponding param was supplied.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # ── derive start ──────────────────────────────────────────────────────────
    start: Optional[datetime] = None

    if since_seconds is not None:
        start = now - timedelta(seconds=since_seconds)

    elif start_time is not None:
        start = _parse_iso(start_time, "start_time")

    elif time_period is not None:
        # Lazy guarded import — keeps core free of a direct helpers dependency.
        from helpers.utils import parse_time_period  # noqa: PLC0415
        start = now - parse_time_period(time_period)

    # ── derive end ────────────────────────────────────────────────────────────
    end: Optional[datetime] = None
    if end_time is not None:
        end = _parse_iso(end_time, "end_time")

    return TimeWindow(start=start, end=end)


def _parse_iso(value: str, param_name: str) -> datetime:
    """Parse *value* as ISO-8601 and ensure the result is timezone-aware.

    ``Z`` is rewritten to ``+00:00`` before parsing (``fromisoformat`` in
    Python < 3.11 does not accept ``Z``).  Naive datetimes are treated as UTC.

    Raises ``ValueError`` naming *param_name* on parse failure.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"Invalid ISO-8601 value for {param_name!r}: {value!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
