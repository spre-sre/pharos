"""Task 6: historical calibration executes the range query (was dead code)."""
import asyncio
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.utils import load_historical_performance_data  # noqa: E402

logger = logging.getLogger("test")
SCOPE = {"namespaces": ["ns-a"], "clusters": ["c"], "components": ["all"]}


def _matrix_fn(recorded):
    """Fake prometheus_query_fn: 24-point matrix for range (subquery) queries,
    single instant value otherwise."""
    async def fn(query):
        recorded.append(query)
        if ":1h]" in query:  # subquery/range form
            return {"success": True, "data": [{
                "values": [[1700000000 + i * 3600, str(40.0 + i)] for i in range(24)]
            }]}
        return {"success": True, "data": [{"value": [1700000000, "55.0"]}]}
    return fn


def test_cpu_range_query_is_executed_and_matrix_parsed():
    recorded = []
    hist = asyncio.run(load_historical_performance_data(
        SCOPE, "24h", prometheus_query_fn=_matrix_fn(recorded)))
    assert any(":1h]" in q for q in recorded), "range subquery never executed"
    assert len(hist["cpu_utilization"]) >= 24


def test_memory_range_query_is_executed():
    recorded = []
    hist = asyncio.run(load_historical_performance_data(
        SCOPE, "24h", prometheus_query_fn=_matrix_fn(recorded)))
    assert len(hist["memory_utilization"]) >= 24


def test_instant_fallback_when_range_fails():
    async def failing_range(query):
        if ":1h]" in query:
            return {"success": False, "error": "subqueries disabled"}
        return {"success": True, "data": [{"value": [1700000000, "55.0"]}]}

    hist = asyncio.run(load_historical_performance_data(
        SCOPE, "24h", prometheus_query_fn=failing_range))
    assert hist["cpu_utilization"] == [55.0]


def test_instant_shape_still_parsed_by_range_path():
    """The characterization fake (_fake_prom_exec, tests/characterization/
    cases.py:509) ignores query text and returns instant shape for EVERY
    query — the range-first path must still extract those values so the
    per-metric counts stay identical (golden stability)."""
    async def instant_only(query):
        return {"success": True, "data": [{"value": [1700000000, "42.0"]}]}

    hist = asyncio.run(load_historical_performance_data(
        SCOPE, "24h", prometheus_query_fn=instant_only))
    assert hist["cpu_utilization"] == [42.0]
