import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.selector import (
    CAPABILITIES, CapabilityError, Entity, Limit, Matchers, Native,
    SelectorNotSupported, TimeWindow, make_capability_error,
)


def test_selector_variants_are_frozen_and_distinct():
    e = Entity(name_or_pattern="api-*", kind="pod")
    m = Matchers(terms={"app": "api"})
    n = Native(query='{namespace="team-a"}')
    for variant in (e, m, n):
        with pytest.raises((AttributeError, TypeError)):
            variant.mutated = True  # frozen dataclasses reject new attrs
    assert e != m and m != n


def test_entity_kind_optional():
    assert Entity(name_or_pattern="api-1").kind is None


def test_time_window_and_limit_are_optional_everywhere():
    w = TimeWindow(start=None, end=None)
    l = Limit(max_records=None, max_bytes=None)
    assert w.start is None and l.max_records is None


def test_selector_not_supported_carries_supported_variants():
    err = SelectorNotSupported(requested="Native", supported=("Entity", "Matchers"))
    assert err.requested == "Native"
    assert err.supported == ("Entity", "Matchers")
    assert "Native" in str(err) and "Entity" in str(err)


def test_capability_error_shape_is_canonical():
    """The ONE shared shape every 2b+ capability rejection uses (design
    decision: defined here once, never re-invented per tool)."""
    d = make_capability_error("query_metrics", "file-a", ["prometheus"])
    assert d == {
        "error": "source 'file-a' does not support tool 'query_metrics'",
        "tool": "query_metrics",
        "requested_source": "file-a",
        "capable_sources": ["prometheus"],
    }


def test_capable_sources_are_sorted():
    d = make_capability_error("t", "x", ["zeta", "alpha"])
    assert d["capable_sources"] == ["alpha", "zeta"]


def test_protocols_are_importable_and_runtime_checkable_free():
    """2a ships protocol TYPES only — importable, no implementations."""
    from core.selector import EventSource, InventorySource, LogSource, MetricSource
    assert all(p is not None for p in (LogSource, EventSource, MetricSource, InventorySource))


def test_capabilities_tuple():
    assert CAPABILITIES == ("Log", "Event", "Metric", "Inventory")
