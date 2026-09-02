"""Task 1: bounded, off-loop pod listing for list_pods (all call sites).

Finding 2026-08-20: collect_baseline_system_data (and the affected-components
path at resource_topology.py:1059) listed pods unpaginated; rhtap-releng-tenant
on prd-rh01 returned ~54MB and died in IncompleteRead retries for ~5 minutes.
list_pods now defaults to limit=200, runs via asyncio.to_thread, and appends
the established `_truncation` sentinel (cf. server-mcp.py:1658-1728).
"""
import asyncio
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.utils import collect_baseline_system_data, list_pods  # noqa: E402
from helpers.resource_topology import identify_affected_components  # noqa: E402

logger = logging.getLogger("test")


def _fake_pod(name="p1"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="ns", creation_timestamp=None,
                                 labels={}, owner_references=None),
        status=SimpleNamespace(phase="Running", container_statuses=None,
                               conditions=None, pod_ip=None, host_ip=None,
                               start_time=None),
        spec=SimpleNamespace(node_name="n1", containers=[]),
    )


class _RecordingCore:
    """Fake CoreV1Api recording kwargs passed to list_namespaced_pod / list_node /
    list_namespaced_resource_quota."""
    def __init__(self, n_pods=3, continue_token=None):
        self.calls = []
        self.call_threads = []
        self.node_calls = []
        self.node_call_threads = []
        self.quota_calls = []
        self.quota_call_threads = []
        self._n = n_pods
        self._continue = continue_token

    def list_namespaced_pod(self, namespace, **kwargs):
        self.calls.append(kwargs)
        self.call_threads.append(threading.current_thread())
        return SimpleNamespace(
            items=[_fake_pod(f"p{i}") for i in range(self._n)],
            metadata=SimpleNamespace(_continue=self._continue),
        )

    def list_namespaced_resource_quota(self, namespace, **kwargs):
        self.quota_calls.append(kwargs)
        self.quota_call_threads.append(threading.current_thread())
        return SimpleNamespace(items=[])

    def list_node(self, **kwargs):
        self.node_calls.append(kwargs)
        self.node_call_threads.append(threading.current_thread())
        return SimpleNamespace(items=[])


def test_list_pods_bounded_by_default():
    core = _RecordingCore()
    asyncio.run(list_pods("ns", core, logger))
    assert core.calls and core.calls[0].get("limit") == 200


def test_list_pods_runs_off_the_event_loop():
    """asyncio.to_thread must actually be used — a plain sync call would run
    list_namespaced_pod on the main/event-loop thread and this must fail."""
    core = _RecordingCore()
    main_thread = threading.current_thread()
    asyncio.run(list_pods("ns", core, logger))
    assert core.call_threads, "list_namespaced_pod was never called"
    assert core.call_threads[0] is not main_thread, (
        "list_pods called list_namespaced_pod on the main thread; "
        "expected asyncio.to_thread to offload it")


def test_list_pods_explicit_limit_passes_through():
    core = _RecordingCore()
    asyncio.run(list_pods("ns", core, logger, limit=50))
    assert core.calls and core.calls[0].get("limit") == 50


def test_list_pods_limit_none_opts_out():
    core = _RecordingCore()
    asyncio.run(list_pods("ns", core, logger, limit=None))
    assert core.calls and "limit" not in core.calls[0]


def test_list_pods_appends_truncation_sentinel():
    core = _RecordingCore(n_pods=3, continue_token="more-pages")
    pods = asyncio.run(list_pods("ns", core, logger, limit=3))
    assert pods[-1].get("_truncation", {}).get("truncated") is True
    real = [p for p in pods if "_truncation" not in p]
    assert len(real) == 3


def test_list_pods_no_sentinel_when_complete():
    core = _RecordingCore(n_pods=3, continue_token=None)
    pods = asyncio.run(list_pods("ns", core, logger))
    assert all("_truncation" not in p for p in pods)


def test_baseline_strips_sentinel_and_records_note():
    core = _RecordingCore()
    calls = []

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        calls.append({"namespace": namespace, "field_selector": field_selector})
        return ([{"name": f"p{i}"} for i in range(limit)]
                + [{"_truncation": {"limit": limit, "truncated": True}}])

    scope = {"namespaces": ["big-ns"], "clusters": ["c"], "components": ["all"]}
    baseline = asyncio.run(
        collect_baseline_system_data(scope, core, None, fake_list_pods))
    ns = baseline["resource_usage"]["big-ns"]
    assert ns["pod_count"] == 200            # sentinel not counted as a pod
    assert any("big-ns" in n for n in baseline["collection_notes"])
    # Fix-loop round 1 (2026-08-21): collect_baseline_system_data now scopes
    # its list_pods_fn call to Running pods only — completed pods consume no
    # resources for a resource baseline, and this collapses the payload on
    # namespaces with many completed pods (live finding: rhtap-releng-tenant).
    assert calls and calls[0]["field_selector"] == "status.phase=Running"


def test_baseline_list_node_call_carries_request_timeout():
    """Fix-loop round 2 (2026-08-21): controller diagnosis found the round-1
    fix's own re-verification measured a NEW retry storm on /api/v1/nodes
    (unrelated to list_pods) — an oc-confirmed environmental link
    degradation on prd-rh01 that day, not a payload-size problem (nodes
    list is ~1.5MB). collect_baseline_system_data's list_node() call now
    passes _request_timeout=30 so a degraded link fails fast into
    collection_warnings/limitations (Task 2's existing graceful-degradation
    path) instead of stalling for minutes."""
    core = _RecordingCore()

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1"}]

    scope = {"namespaces": ["ns-a"], "clusters": ["c"], "components": ["all"]}
    asyncio.run(collect_baseline_system_data(scope, core, None, fake_list_pods))
    assert core.node_calls and core.node_calls[0].get("_request_timeout") == 30


# ── Fix-loop round 1 (2026-08-21): field_selector + request_timeout ─────────
# Task 7 live check A measured 754s against a 60s target for the
# resource_limits/rhtap-releng-tenant scenario. Root cause: limit=200 bounds
# row count but not response payload size, and this namespace's individual
# pod manifests are bloated (old completed release pods with large
# managed-fields/annotations) — so the bounded call still read 2-5MB before
# urllib3's IncompleteRead retries burned several minutes with no
# client-side read timeout in place. Fix: list_pods gains field_selector
# (opt-in filtering, default None = no behavior change) and request_timeout
# (default 30.0 — always applied, bounds the worst case even when limit
# doesn't help).

def test_list_pods_field_selector_passes_through():
    core = _RecordingCore()
    asyncio.run(list_pods("ns", core, logger, field_selector="status.phase=Running"))
    assert core.calls and core.calls[0].get("field_selector") == "status.phase=Running"


def test_list_pods_field_selector_default_none():
    core = _RecordingCore()
    asyncio.run(list_pods("ns", core, logger))
    assert core.calls and core.calls[0].get("field_selector") is None


def test_list_pods_request_timeout_default_applied():
    """request_timeout defaults to 30.0 and is ALWAYS passed through, even
    when the caller doesn't set it — this is the always-on half of the fix
    that bounds the worst case regardless of field_selector/limit."""
    core = _RecordingCore()
    asyncio.run(list_pods("ns", core, logger))
    assert core.calls and core.calls[0].get("_request_timeout") == 30.0


def test_list_pods_request_timeout_explicit_passes_through():
    core = _RecordingCore()
    asyncio.run(list_pods("ns", core, logger, request_timeout=5.0))
    assert core.calls and core.calls[0].get("_request_timeout") == 5.0


def test_list_pods_field_selector_and_timeout_with_limit_none():
    """The limit=None (unbounded, opt-out) branch also passes field_selector
    and _request_timeout through — both branches of the if/else in list_pods
    must carry them."""
    core = _RecordingCore()
    asyncio.run(list_pods(
        "ns", core, logger, limit=None,
        field_selector="status.phase=Running", request_timeout=7.5))
    assert core.calls
    assert "limit" not in core.calls[0]
    assert core.calls[0].get("field_selector") == "status.phase=Running"
    assert core.calls[0].get("_request_timeout") == 7.5


# ── Fix-loop round 3 (2026-08-21): topology selector pin + off-loop quota ──
# Re-review of rounds 1-2 approved both but found two gaps: (1) only
# collect_baseline_system_data's field_selector was pinned by a test —
# identify_affected_components's (resource_topology.py) was implemented but
# unpinned; (2) collect_baseline_system_data's list_namespaced_resource_quota
# call was the last unbounded, event-loop-blocking transfer in the
# simulation path (no _request_timeout, not dispatched via asyncio.to_thread)
# — same class of issue list_pods and list_node already had fixed for them.

def test_identify_affected_components_pins_running_pods_selector():
    """resource_topology.py's list_pods call (scenario_type="resource_limits")
    must carry the same field_selector="status.phase=Running" pin as
    collect_baseline_system_data's — only the baseline's was covered by a
    test before this round."""
    calls = []

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        calls.append({"namespace": namespace, "field_selector": field_selector})
        return [{"name": "p1", "containers": []}]

    async def fake_list_namespaces():
        return ["ns-a"]

    class _StubApi:
        def list_namespaced_deployment(self, namespace, **kw):
            return SimpleNamespace(items=[])

    scope = {"namespaces": ["ns-a"]}
    asyncio.run(identify_affected_components(
        changes={"memory_limit": {"before": "512Mi", "after": "1Gi"}},
        scope=scope,
        scenario_type="resource_limits",
        k8s_core_api=_StubApi(),
        k8s_apps_api=_StubApi(),
        list_pods=fake_list_pods,
        list_namespaces=fake_list_namespaces,
    ))
    assert calls and calls[0]["field_selector"] == "status.phase=Running"


def test_baseline_resource_quota_call_carries_request_timeout():
    core = _RecordingCore()

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1"}]

    scope = {"namespaces": ["ns-a"], "clusters": ["c"], "components": ["all"]}
    asyncio.run(collect_baseline_system_data(scope, core, None, fake_list_pods))
    assert core.quota_calls and core.quota_calls[0].get("_request_timeout") == 30


def test_baseline_resource_quota_call_runs_off_the_event_loop():
    """asyncio.to_thread must actually be used for list_namespaced_resource_quota
    too — same off-loop guarantee list_pods already has."""
    core = _RecordingCore()
    main_thread = threading.current_thread()

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1"}]

    scope = {"namespaces": ["ns-a"], "clusters": ["c"], "components": ["all"]}
    asyncio.run(collect_baseline_system_data(scope, core, None, fake_list_pods))
    assert core.quota_call_threads, "list_namespaced_resource_quota was never called"
    assert core.quota_call_threads[0] is not main_thread, (
        "collect_baseline_system_data called list_namespaced_resource_quota on "
        "the main thread; expected asyncio.to_thread to offload it")


# ── Final fix-wave (2026-08-21): I-1/I-2/I-3 — remaining on-loop/unbounded
# calls in the what-if simulation path, plus per-namespace failure
# containment in identify_affected_components.
#
# I-2: list_node was already bounded (_request_timeout=30) but still ran
# synchronously on the event loop — same class of bug list_pods and the
# resource-quota call already had fixed for them.
#
# I-1: identify_affected_components's scaling branch
# (k8s_apps_api.list_namespaced_deployment) and configuration/deployment
# branch (k8s_core_api.list_namespaced_service) were synchronous, unbounded,
# and on-loop.
#
# I-3: the per-namespace handler caught only ApiException, so a urllib3
# ReadTimeoutError/MaxRetryError on a degraded link escaped to the outer
# handler, which replaced ALL namespaces' results with a single opaque
# error stub instead of containing the failure to one namespace.

def test_baseline_list_node_runs_off_the_event_loop():
    """asyncio.to_thread must actually be used for list_node too (I-2) — it
    was already bounded by _request_timeout=30 but still ran on-loop."""
    core = _RecordingCore()
    main_thread = threading.current_thread()

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1"}]

    scope = {"namespaces": ["ns-a"], "clusters": ["c"], "components": ["all"]}
    asyncio.run(collect_baseline_system_data(scope, core, None, fake_list_pods))
    assert core.node_call_threads, "list_node was never called"
    assert core.node_call_threads[0] is not main_thread, (
        "collect_baseline_system_data called list_node on the main thread; "
        "expected asyncio.to_thread to offload it")


def test_baseline_list_node_runs_on_the_dedicated_node_listing_pool():
    """Bug 7: this site duplicated the to_thread treatment inline instead of
    calling list_nodes_bounded, so it drew from the shared default executor
    (and had no caller-bound wait_for) even after the dedicated pool landed
    for every other node-list call site. Must be consolidated onto the same
    helper — same isolation, same caller bound, one place to fix next time."""
    core = _RecordingCore()

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1"}]

    scope = {"namespaces": ["ns-a"], "clusters": ["c"], "components": ["all"]}
    asyncio.run(collect_baseline_system_data(scope, core, None, fake_list_pods))
    name = core.node_call_threads[0].name
    assert "node-listing" in name, (
        f"expected the dedicated node-listing executor's thread naming, got {name!r}"
    )


class _RecordingAppsApi:
    """Fake AppsV1Api recording kwargs + thread for list_namespaced_deployment."""

    def __init__(self, items=None):
        self.calls = []
        self.call_threads = []
        self._items = items if items is not None else []

    def list_namespaced_deployment(self, namespace, **kwargs):
        self.calls.append(kwargs)
        self.call_threads.append(threading.current_thread())
        return SimpleNamespace(items=self._items)


class _RecordingCoreApiForServices:
    """Fake CoreV1Api recording kwargs + thread for list_namespaced_service."""

    def __init__(self, items=None):
        self.calls = []
        self.call_threads = []
        self._items = items if items is not None else []

    def list_namespaced_service(self, namespace, **kwargs):
        self.calls.append(kwargs)
        self.call_threads.append(threading.current_thread())
        return SimpleNamespace(items=self._items)


async def _fake_list_pods_empty(namespace, k8s_core_api, log, limit=200, field_selector=None):
    return []


async def _fake_list_namespaces_single():
    return ["ns-a"]


def test_identify_affected_components_scaling_deployment_call_bounded_and_off_thread():
    apps_api = _RecordingAppsApi()
    main_thread = threading.current_thread()

    asyncio.run(identify_affected_components(
        changes={"replicas": {"before": 1, "after": 2}},
        scope={"namespaces": ["ns-a"]},
        scenario_type="scaling",
        k8s_core_api=SimpleNamespace(),
        k8s_apps_api=apps_api,
        list_pods=_fake_list_pods_empty,
        list_namespaces=_fake_list_namespaces_single,
    ))
    assert apps_api.calls, "list_namespaced_deployment was never called"
    assert apps_api.calls[0].get("limit") == 200
    assert apps_api.calls[0].get("_request_timeout") == 30
    assert apps_api.call_threads[0] is not main_thread, (
        "identify_affected_components called list_namespaced_deployment on "
        "the main thread; expected asyncio.to_thread to offload it")


def test_identify_affected_components_service_call_bounded_and_off_thread():
    core_api = _RecordingCoreApiForServices()
    main_thread = threading.current_thread()

    asyncio.run(identify_affected_components(
        changes={"replicas": {"before": 1, "after": 2}},
        scope={"namespaces": ["ns-a"]},
        scenario_type="configuration",
        k8s_core_api=core_api,
        k8s_apps_api=SimpleNamespace(),
        list_pods=_fake_list_pods_empty,
        list_namespaces=_fake_list_namespaces_single,
    ))
    assert core_api.calls, "list_namespaced_service was never called"
    assert core_api.calls[0].get("limit") == 200
    assert core_api.calls[0].get("_request_timeout") == 30
    assert core_api.call_threads[0] is not main_thread, (
        "identify_affected_components called list_namespaced_service on "
        "the main thread; expected asyncio.to_thread to offload it")


def test_identify_affected_components_namespace_failure_contained():
    """A non-ApiException failure (e.g. ConnectionError from a degraded
    urllib3 link) in one namespace must not wipe out results for the other
    namespaces with a single whole-function error stub (I-3)."""

    def _fake_deployment(name="dep1"):
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name),
            status=SimpleNamespace(replicas=1),
            spec=SimpleNamespace(replicas=1),
        )

    class _FlakyAppsApi:
        def list_namespaced_deployment(self, namespace, **kwargs):
            if namespace == "bad-ns":
                raise ConnectionError("connection reset by peer")
            return SimpleNamespace(items=[_fake_deployment()])

    async def fake_list_namespaces():
        return ["bad-ns", "good-ns"]

    result = asyncio.run(identify_affected_components(
        changes={"replicas": {"before": 1, "after": 2}},
        scope={"namespaces": ["bad-ns", "good-ns"]},
        scenario_type="scaling",
        k8s_core_api=SimpleNamespace(),
        k8s_apps_api=_FlakyAppsApi(),
        list_pods=_fake_list_pods_empty,
        list_namespaces=fake_list_namespaces,
    ))

    # NOT the whole-function error stub (a single {"component": "unknown", ...}).
    assert not any(c.get("component") == "unknown" for c in result), (
        f"expected per-namespace containment, got whole-function error stub: {result}")

    # good-ns was still processed despite bad-ns's failure.
    assert any(
        c.get("namespace") == "good-ns" and c.get("component") == "deployment/dep1"
        for c in result
    ), f"good-ns results missing: {result}"

    # bad-ns's failure is surfaced in the returned components list.
    assert any(
        c.get("component") == "namespace/bad-ns"
        and c.get("impact_type") == "collection_error"
        and c.get("severity") == "unknown"
        for c in result
    ), f"bad-ns collection_error entry missing: {result}"
