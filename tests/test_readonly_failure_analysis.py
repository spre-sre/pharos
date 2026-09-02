"""Behavioral proof: CoreV1 reads in helpers/failure_analysis.py route through
ReadOnlyCoreV1 via param-local reassignment (spec SS4.7).

These helpers receive k8s_core_api as a PARAMETER (callers pass the raw global),
so the wrap must live inside each function.  Spy = monkeypatching the
module-local ReadOnlyCoreV1 name, same technique as test_readonly_log_path.py.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Insert order is load-bearing: src/ MUST end up at sys.path[0].  This file
# imports helpers.failure_analysis (guarded `from core.readonly_client import`)
# at collection time; if tests/ preceded src/, the tests/core/ package would
# shadow src/core and poison `import core` for _readonly_spy.  (The
# characterization test files use the opposite order safely — they do no bare
# `import core`-dependent helpers import at module level.)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kubernetes.client.rest import ApiException

import helpers.failure_analysis as fa
from _readonly_spy import make_spy


def _fake_custom_all_404():
    """Custom-objects client where every lookup 404s (not a PLR, not a TR)."""
    fake = MagicMock()
    fake.get_namespaced_custom_object.side_effect = ApiException(status=404)
    return fake


@pytest.mark.asyncio
async def test_identify_pod_read_routes_readonly(monkeypatch):
    """identify_failure_context's read_namespaced_pod (:73) goes through the wrapper."""
    record = []
    monkeypatch.setattr(fa, "ReadOnlyCoreV1", make_spy(record))

    fake_core = MagicMock()
    fake_core.read_namespaced_pod.return_value = MagicMock()  # found as a pod

    result = await fa.identify_failure_context(
        "some-pod",
        MagicMock(),            # detect func unused when namespace= given
        _fake_custom_all_404(),
        fake_core,
        MagicMock(),            # logger
        namespace="team-a",
    )

    assert "read_namespaced_pod" in record, (
        f"read_namespaced_pod not routed through ReadOnlyCoreV1; record={record}"
    )
    assert result["found"] is True and result["type"] == "pod"


@pytest.mark.asyncio
async def test_identify_event_fallback_routes_readonly(monkeypatch):
    """identify_failure_context's GC-fallback list_namespaced_event (:95) goes
    through the wrapper.  Reached when PLR, pod, and TR lookups all 404."""
    record = []
    monkeypatch.setattr(fa, "ReadOnlyCoreV1", make_spy(record))

    fake_core = MagicMock()
    fake_core.read_namespaced_pod.side_effect = ApiException(status=404)
    fake_core.list_namespaced_event.return_value = MagicMock(items=[])

    await fa.identify_failure_context(
        "gc-ed-resource",
        MagicMock(),
        _fake_custom_all_404(),
        fake_core,
        MagicMock(),
        namespace="team-a",
    )

    assert "list_namespaced_event" in record, (
        f"list_namespaced_event (GC fallback) not routed through "
        f"ReadOnlyCoreV1; record={record}"
    )


@pytest.mark.asyncio
async def test_analyze_pod_failure_routes_readonly(monkeypatch):
    """analyze_pod_failure's read_namespaced_pod (:230) goes through the wrapper.

    Downstream analysis funcs are AsyncMocks; even if a later step errors, the
    function's own except returns {"error": ...} AFTER the read we assert on.
    """
    record = []
    monkeypatch.setattr(fa, "ReadOnlyCoreV1", make_spy(record))

    pod = MagicMock()
    pod.status.phase = "Failed"
    pod.status.container_statuses = []
    fake_core = MagicMock()
    fake_core.read_namespaced_pod.return_value = pod

    await fa.analyze_pod_failure(
        "team-a",
        "crashed-pod",
        "basic",
        fake_core,
        AsyncMock(return_value={"logs": {}}),   # get_pod_logs_func
        AsyncMock(return_value={}),             # analyze_logs_func
        AsyncMock(return_value={}),             # detect_log_anomalies_func
        AsyncMock(return_value={"events": []}), # get_namespace_events_func
        MagicMock(),                            # logger
    )

    assert "read_namespaced_pod" in record, (
        f"read_namespaced_pod not routed through ReadOnlyCoreV1; record={record}"
    )


@pytest.mark.asyncio
async def test_analyze_resource_constraints_routes_readonly(monkeypatch):
    """analyze_resource_constraints' list_namespaced_resource_quota (:491)
    goes through the wrapper."""
    record = []
    monkeypatch.setattr(fa, "ReadOnlyCoreV1", make_spy(record))

    fake_core = MagicMock()
    fake_core.list_namespaced_resource_quota.return_value = MagicMock(items=[])

    result = await fa.analyze_resource_constraints(
        "team-a", "some-id", fake_core, MagicMock()
    )

    assert "list_namespaced_resource_quota" in record, (
        f"list_namespaced_resource_quota not routed through ReadOnlyCoreV1; "
        f"record={record}"
    )
    assert result["resource_quotas"] == []


@pytest.mark.asyncio
async def test_identify_plr_read_routes_readonly(monkeypatch):
    """identify_failure_context's get_namespaced_custom_object PLR lookup (:54-ish)
    goes through ReadOnlyK8sClient.  NOTE: monkeypatches ReadOnlyK8sClient (the
    custom-api wrap name), not ReadOnlyCoreV1 (the core-api wrap name)."""
    record = []
    monkeypatch.setattr(fa, "ReadOnlyK8sClient", make_spy(record))

    fake_custom = MagicMock()
    fake_custom.get_namespaced_custom_object.return_value = {"metadata": {}}

    result = await fa.identify_failure_context(
        "plr-x", MagicMock(), fake_custom, MagicMock(), MagicMock(),
        namespace="team-a",
    )

    assert "get_namespaced_custom_object" in record, (
        f"PLR lookup not routed through ReadOnlyK8sClient; record={record}")
    assert result["found"] is True and result["type"] == "pipelinerun"


@pytest.mark.asyncio
async def test_identify_tr_read_routes_readonly(monkeypatch):
    """The taskrun fallback lookup also goes through ReadOnlyK8sClient.
    Reached when PLR lookup and pod read both 404: first custom get raises,
    pod read raises, second custom get succeeds."""
    record = []
    monkeypatch.setattr(fa, "ReadOnlyK8sClient", make_spy(record))

    fake_custom = MagicMock()
    fake_custom.get_namespaced_custom_object.side_effect = [
        ApiException(status=404),   # PLR lookup
        {"metadata": {}},           # TR lookup
    ]
    fake_core = MagicMock()
    fake_core.read_namespaced_pod.side_effect = ApiException(status=404)

    result = await fa.identify_failure_context(
        "tr-x", MagicMock(), fake_custom, fake_core, MagicMock(),
        namespace="team-a",
    )

    assert record.count("get_namespaced_custom_object") == 2, (
        f"Expected both custom lookups (PLR 404 + TR hit) through "
        f"ReadOnlyK8sClient; record={record}")
    assert result["found"] is True and result["type"] == "taskrun"
