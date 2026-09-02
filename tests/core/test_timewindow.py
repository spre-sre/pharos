"""Unit tests for make_time_window (src/core/timewindow.py).

All tests inject ``now`` for determinism.  No live cluster needed.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure src/ is on the path before importing the module under test.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.timewindow import make_time_window
from core.selector import TimeWindow

_NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


# ─── basic cases ─────────────────────────────────────────────────────────────


def test_no_params_returns_null_window():
    """No params → TimeWindow(None, None)."""
    w = make_time_window(now=_NOW)
    assert w == TimeWindow(start=None, end=None)


def test_since_seconds_sets_start_relative_to_now():
    """since_seconds=3600 → start == now − 1 h."""
    w = make_time_window(since_seconds=3600, now=_NOW)
    assert w.start == _NOW - timedelta(seconds=3600)
    assert w.end is None


def test_time_period_sets_start_relative_to_now():
    """time_period='2h' → start == now − 2 h."""
    w = make_time_window(time_period="2h", now=_NOW)
    assert w.start == _NOW - timedelta(hours=2)
    assert w.end is None


def test_start_time_iso_z_parsed_to_aware_datetime():
    """start_time='2026-01-01T00:00:02Z' → aware UTC datetime."""
    w = make_time_window(start_time="2026-01-01T00:00:02Z", now=_NOW)
    assert w.start == datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
    assert w.end is None


# ─── precedence: since_seconds > start_time > time_period ────────────────────


def test_precedence_since_seconds_beats_start_time_and_time_period():
    """When all three given, since_seconds wins (matches legacy parse_time_parameters)."""
    w = make_time_window(
        since_seconds=60,
        start_time="2026-01-01T00:00:02Z",
        time_period="2h",
        now=_NOW,
    )
    assert w.start == _NOW - timedelta(seconds=60)


def test_precedence_start_time_beats_time_period():
    """When start_time + time_period given (no since_seconds), start_time wins."""
    w = make_time_window(
        start_time="2026-01-01T00:00:02Z",
        time_period="2h",
        now=_NOW,
    )
    assert w.start == datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)


# ─── end_time ────────────────────────────────────────────────────────────────


def test_end_time_parsed_independently():
    """end_time is parsed regardless of start params."""
    w = make_time_window(end_time="2026-01-02T12:00:00Z", now=_NOW)
    assert w.start is None
    assert w.end == datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_start_and_end_both_set():
    """Both start_time and end_time present → both set on the window."""
    w = make_time_window(
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T06:00:00Z",
        now=_NOW,
    )
    assert w.start == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)


# ─── invalid ISO → ValueError naming the param ───────────────────────────────


def test_invalid_start_time_raises_value_error_naming_param():
    """Invalid ISO string for start_time → ValueError mentioning 'start_time'."""
    with pytest.raises(ValueError, match="start_time"):
        make_time_window(start_time="not-a-date", now=_NOW)


def test_invalid_end_time_raises_value_error_naming_param():
    """Invalid ISO string for end_time → ValueError mentioning 'end_time'."""
    with pytest.raises(ValueError, match="end_time"):
        make_time_window(end_time="bad-date", now=_NOW)


# ─── naive datetimes get UTC ──────────────────────────────────────────────────


def test_naive_start_time_gets_utc_attached():
    """Naive start_time (no tz suffix) → datetime with UTC tzinfo."""
    w = make_time_window(start_time="2026-01-01T00:00:02", now=_NOW)
    assert w.start is not None
    assert w.start.tzinfo is not None
    assert w.start == datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)


def test_naive_end_time_gets_utc_attached():
    """Naive end_time (no tz suffix) → datetime with UTC tzinfo."""
    w = make_time_window(end_time="2026-01-01T06:00:00", now=_NOW)
    assert w.end is not None
    assert w.end.tzinfo is not None
    assert w.end == datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)


# ─── tail_lines is NOT mapped (it is a Limit, not a window) ─────────────────


def test_tail_lines_not_a_parameter():
    """make_time_window has no tail_lines param; call must not accept it."""
    import inspect
    sig = inspect.signature(make_time_window)
    assert "tail_lines" not in sig.parameters


# ─── now defaults to current UTC time ────────────────────────────────────────


def test_no_now_kwarg_defaults_to_utc_aware():
    """Calling without now= still returns a TimeWindow (start is UTC-aware)."""
    w = make_time_window(since_seconds=60)
    assert w.start is not None
    assert w.start.tzinfo is not None
