"""Behavioral proof: EVENT-path k8s reads route through ReadOnlyCoreV1 (spec SS4.7).

test_namespace_events_internal_routes_readonly: BOTH pagination branches of
    _get_namespace_events_internal (first page + continuation page).
test_namespace_events_as_dicts_routes_readonly: the single read in
    _get_namespace_events_as_dicts.

Note: both functions live in helpers/event_analysis.py (moved in round 2, item 11).
ReadOnlyCoreV1 is resolved from that module, so spies patch helpers.event_analysis.
_DefaultClientView sentinel pinning is handled by the autouse fixture in conftest.py.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from _readonly_spy import make_spy

import helpers.event_analysis as _event_analysis


@pytest.mark.asyncio
async def test_namespace_events_internal_routes_readonly(server, monkeypatch):
    """Both list_namespaced_event branches (first page + continuation page)
    go through ReadOnlyCoreV1.

    A first page carrying a continue token forces the loop around: call 1
    exercises the else-branch, call 2 the continuation branch.
    Empty items[] keeps every filter branch inert.  Before the wrap the spy
    record is empty (0); after, exactly 2.
    """
    record = []
    monkeypatch.setattr(_event_analysis, "ReadOnlyCoreV1", make_spy(record))

    page1 = MagicMock(items=[], metadata=MagicMock(_continue="tok-1"))
    page2 = MagicMock(items=[], metadata=MagicMock(_continue=None))
    fake = MagicMock()
    fake.list_namespaced_event.side_effect = [page1, page2]
    monkeypatch.setattr(server, "k8s_core_api", fake)

    result = await server._get_namespace_events_internal("team-a")

    assert record.count("list_namespaced_event") == 2, (
        f"Expected 2 list_namespaced_event calls through ReadOnlyCoreV1 "
        f"(first page + continuation page); got "
        f"{record.count('list_namespaced_event')}.  full record={record}"
    )
    # Envelope sanity: gateway still returns its normal dict shape.
    assert result["namespace"] == "team-a"
    assert result["events"] == []


@pytest.mark.asyncio
async def test_namespace_events_as_dicts_routes_readonly(server, monkeypatch):
    """_get_namespace_events_as_dicts' single list_namespaced_event read
    goes through ReadOnlyCoreV1.  This is the event path of
    predictive_log_analyzer and manage_prediction_training_data."""
    record = []
    monkeypatch.setattr(_event_analysis, "ReadOnlyCoreV1", make_spy(record))

    fake = MagicMock()
    fake.list_namespaced_event.return_value = MagicMock(
        items=[], metadata=MagicMock(_continue=None)
    )
    monkeypatch.setattr(server, "k8s_core_api", fake)

    result = await server._get_namespace_events_as_dicts("team-a")

    assert "list_namespaced_event" in record, (
        f"list_namespaced_event not routed through ReadOnlyCoreV1 in "
        f"_get_namespace_events_as_dicts; record={record}"
    )
    # Envelope sanity: empty items -> empty list (not an error sentinel).
    assert result == []
