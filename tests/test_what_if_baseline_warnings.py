"""Task 2: baseline collection failures must be surfaced, not swallowed.

Covers BOTH swallow points: the per-namespace except and the outer
except (utils.py:2539) that used to return an unchecked {"error": ...}.
"""
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.utils import collect_baseline_system_data  # noqa: E402
from helpers.failure_analysis import calculate_simulation_quality  # noqa: E402

logger = logging.getLogger("test")

BASELINE_KEYS = {"resource_usage", "performance_metrics", "component_health",
                 "capacity_utilization", "collection_warnings", "collection_notes"}


class _EmptyCore:
    def list_namespaced_resource_quota(self, namespace, **kw):
        return SimpleNamespace(items=[])

    def list_node(self, **kw):
        return SimpleNamespace(items=[])


def _run_baseline(list_pods_fn, namespaces, core=None):
    scope = {"namespaces": namespaces, "clusters": ["c"], "components": ["all"]}
    return asyncio.run(collect_baseline_system_data(
        scope, core or _EmptyCore(), None, list_pods_fn))


def test_failed_namespace_recorded_in_collection_warnings():
    async def exploding(namespace, k8s_core_api, log, limit=200, field_selector=None):
        raise ConnectionError("IncompleteRead(54382329 bytes read)")

    baseline = _run_baseline(exploding, ["huge-ns"])
    assert len(baseline["collection_warnings"]) == 1
    assert "huge-ns" in baseline["collection_warnings"][0]
    assert "IncompleteRead" in baseline["collection_warnings"][0]


def test_clean_run_has_empty_warnings():
    async def ok(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1"}]

    baseline = _run_baseline(ok, ["ns-a"])
    assert baseline["collection_warnings"] == []


def test_fatal_failure_returns_structured_baseline_not_error_dict():
    """Outer-except path: scope['namespaces'] of a non-iterable type makes the
    function blow up before the loop; result must still be a structured
    baseline carrying the failure, never a bare {'error': ...}."""
    async def ok(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1"}]

    class _ExplodingCore(_EmptyCore):
        def list_node(self, **kw):
            raise RuntimeError("node list exploded")

    baseline = _run_baseline(ok, ["ns-a"], core=_ExplodingCore())
    assert "error" not in baseline
    assert BASELINE_KEYS <= set(baseline)
    assert any("node list exploded" in w for w in baseline["collection_warnings"])


def test_quality_score_reflects_collection_failures():
    baseline_bad = {
        "resource_usage": {},
        "performance_metrics": {},
        "collection_warnings": ["huge-ns: IncompleteRead(...)"],
        "collection_notes": [],
        "component_health": {},
        "capacity_utilization": {},
    }
    baseline_good = {
        "resource_usage": {"ns-a": {"pod_count": 3}},
        "performance_metrics": {},
        "collection_warnings": [],
        "collection_notes": [],
        "component_health": {},
        "capacity_utilization": {},
    }
    historical = {"cpu_utilization": [50.0], "memory_utilization": [60.0],
                  "data_source": "prometheus"}
    q_bad = calculate_simulation_quality(baseline_bad, historical, {}, logger)
    q_good = calculate_simulation_quality(baseline_good, historical, {}, logger)
    # baseline_bad has 0 succeeded namespaces out of 1 attempted (1 warning), so the
    # succeeded/attempted factor must zero out data_completeness entirely. Without the
    # multiplication this would be ~0.47 (from the pre-existing namespaces/metric-coverage
    # terms) and still < q_good's ~0.52 — that inequality alone doesn't pin the fix.
    assert q_bad["data_completeness"] == 0.0
    assert q_bad["data_completeness"] < q_good["data_completeness"]
    assert q_bad["collection_warnings"] == ["huge-ns: IncompleteRead(...)"]
    assert any("huge-ns" in l for l in q_bad["limitations"])


def test_quality_data_completeness_unmultiplied_when_nothing_attempted():
    """attempted == 0 (no resource_usage entries, no warnings) must skip the
    succeeded/attempted factor entirely rather than dividing by zero or zeroing
    out a legitimately-computed completeness score."""
    baseline = {
        "resource_usage": {},
        "performance_metrics": {},
        "collection_warnings": [],
        "collection_notes": [],
        "component_health": {},
        "capacity_utilization": {},
    }
    historical = {"cpu_utilization": [50.0], "memory_utilization": [60.0],
                  "data_source": "prometheus"}
    q = calculate_simulation_quality(baseline, historical, {}, logger)
    # Pre-existing formula with 0 namespaces, 0 nodes, 2/6 metric coverage, real data:
    # min(1.0, 0*0.05 + 0*0.03 + (2/6)*0.5 + 0.3) = 0.4667 -> round(.,2) = 0.47
    assert q["data_completeness"] == pytest.approx(0.47)
    assert q["data_completeness"] > 0.0


def test_quality_includes_truncation_notes_in_limitations():
    baseline = {
        "resource_usage": {"big-ns": {"pod_count": 200}},
        "performance_metrics": {},
        "collection_warnings": [],
        "collection_notes": ["big-ns: pod list truncated at sampling limit"],
        "component_health": {},
        "capacity_utilization": {},
    }
    historical = {"cpu_utilization": [50.0], "memory_utilization": [60.0],
                  "data_source": "prometheus"}
    q = calculate_simulation_quality(baseline, historical, {}, logger)
    assert any("truncated" in l for l in q["limitations"])
