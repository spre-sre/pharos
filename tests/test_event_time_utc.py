"""Time handling in helpers.event_analysis: aware UTC everywhere (C12, C13, H25)."""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.event_analysis import (  # noqa: E402
    ProgressiveEventAnalyzer,
    _get_namespace_events_as_dicts,
    _get_namespace_events_internal,
    extract_timestamp_from_string,
)

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


@pytest.fixture(params=["Asia/Tokyo", "Europe/Berlin", "America/Los_Angeles"])
def non_utc_host(request, monkeypatch):
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


def _event(msg, last=None, first=None, event_time=None, series=None):
    return SimpleNamespace(
        type="Warning",
        reason="Failed",
        message=msg,
        last_timestamp=last,
        first_timestamp=first,
        event_time=event_time,
        series=series,
        count=1,
        metadata=SimpleNamespace(name=msg),
        involved_object=SimpleNamespace(kind="Pod", name="p", namespace="ns", uid="u"),
    )


class _FakeCore:
    def __init__(self, events):
        self._events = events

    def list_namespaced_event(self, **kwargs):
        return SimpleNamespace(items=list(self._events), metadata=SimpleNamespace(_continue=None))


def _clients(events):
    return SimpleNamespace(core_api=_FakeCore(events))


# --------------------------------------------------------------------------
# C12
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_event_included_and_old_excluded_on_non_utc_host(non_utc_host):
    events = [
        _event("recent", last=_now() - timedelta(minutes=5)),
        _event("old", last=_now() - timedelta(hours=2)),
    ]
    out = await _get_namespace_events_internal("ns", time_period="1h", clients=_clients(events))
    assert out["errors"] == []
    assert len(out["events"]) == 1
    assert "recent" in out["events"][0]
    assert out["applied_filters"]["cutoff_time"].endswith("+00:00")


@pytest.mark.asyncio
async def test_as_dicts_time_window_on_non_utc_host(non_utc_host):
    events = [
        _event("recent", last=_now() - timedelta(minutes=5)),
        _event("old", last=_now() - timedelta(hours=2)),
    ]
    out = await _get_namespace_events_as_dicts("ns", time_period="1h", clients=_clients(events))
    assert [e["message"] for e in out] == ["recent"]


# --------------------------------------------------------------------------
# C13
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_time_only_event_included_in_window(non_utc_host):
    events = [
        _event("sched", event_time=_now() - timedelta(minutes=5)),
        _event("stale", event_time=_now() - timedelta(hours=3)),
    ]
    out = await _get_namespace_events_internal("ns", time_period="1h", clients=_clients(events))
    assert out["errors"] == []
    assert len(out["events"]) == 1
    assert "sched" in out["events"][0]


@pytest.mark.asyncio
async def test_event_time_only_event_dedicated_as_dicts(non_utc_host):
    events = [_event("sched", event_time=_now() - timedelta(minutes=5))]
    out = await _get_namespace_events_as_dicts("ns", time_period="1h", clients=_clients(events))
    assert [e["message"] for e in out] == ["sched"]


@pytest.mark.asyncio
async def test_no_time_period_sorts_event_time_only_and_undated_last():
    t = _now()
    events = [
        _event("undated"),
        _event("oldest", last=t - timedelta(hours=5)),
        _event("sched", event_time=t - timedelta(minutes=1)),
        _event("mid", last=t - timedelta(hours=1)),
    ]
    out = await _get_namespace_events_internal("ns", clients=_clients(events))
    assert out["errors"] == []
    msgs = [s.split("- ", 1)[1].split(" (")[0] for s in out["events"]]
    assert msgs == ["sched", "mid", "oldest", "undated"]


@pytest.mark.asyncio
async def test_undated_event_excluded_when_window_applies():
    events = [_event("undated"), _event("recent", last=_now() - timedelta(minutes=1))]
    out = await _get_namespace_events_internal("ns", time_period="1h", clients=_clients(events))
    assert len(out["events"]) == 1 and "recent" in out["events"][0]


# --------------------------------------------------------------------------
# extract_timestamp_from_string
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("[2026-03-11T04:44:36Z] Warning: x", datetime(2026, 3, 11, 4, 44, 36, tzinfo=UTC)),
        ("[2026-03-11T04:44:36+02:00] Warning: x", datetime(2026, 3, 11, 2, 44, 36, tzinfo=UTC)),
        ("[2026-03-11T04:44:36] Warning: x", datetime(2026, 3, 11, 4, 44, 36, tzinfo=UTC)),
        ("[2026-03-11 04:44:36+00:00] Warning: x", datetime(2026, 3, 11, 4, 44, 36, tzinfo=UTC)),
        ("[2026-03-11 04:44:36] Warning: x", datetime(2026, 3, 11, 4, 44, 36, tzinfo=UTC)),
    ],
)
def test_extract_timestamp_is_aware_utc(text, expected, non_utc_host):
    got = extract_timestamp_from_string(text)
    assert got.tzinfo is not None
    assert got == expected
    assert got.utcoffset() == timedelta(0)


def test_extract_timestamp_none_when_missing():
    assert extract_timestamp_from_string("[Unknown] Warning: no time here") is None


# --------------------------------------------------------------------------
# H25
# --------------------------------------------------------------------------


def test_progressive_time_range_filter_aware(non_utc_host):
    t = _now()
    events = [
        {"event_string": "recent", "severity": "HIGH", "category": "X", "timestamp": t - timedelta(minutes=5)},
        {"event_string": "old", "severity": "HIGH", "category": "X", "timestamp": t - timedelta(hours=3)},
        {"event_string": "undated", "severity": "HIGH", "category": "X", "timestamp": None},
        {"event_string": "nokey", "severity": "HIGH", "category": "X"},
    ]
    analyzer = ProgressiveEventAnalyzer(events)
    kept = analyzer._apply_progressive_filters(events, {"time_range": 1})
    assert [e["event_string"] for e in kept] == ["recent"]


def test_progressive_analyzer_tolerates_missing_timestamps():
    t = _now()
    events = [
        {"event_string": "a", "severity": "CRITICAL", "category": "X", "timestamp": t},
        {"event_string": "b", "severity": "HIGH", "category": "X", "timestamp": None},
        {"event_string": "c", "severity": "HIGH", "category": "X", "timestamp": t - timedelta(minutes=3)},
    ]
    analyzer = ProgressiveEventAnalyzer(events)
    overview = analyzer.get_overview()
    assert "critical_events_preview" in overview
    analyzer.get_detailed_analysis({})
    analyzer.get_correlation_analysis(None)
