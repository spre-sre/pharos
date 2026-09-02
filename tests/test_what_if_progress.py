"""Task 4: what_if reports MCP progress and never fails on a broken ctx.

Fixture pattern copied from tests/test_6_final_source_additions.py:60
(module-scoped importlib load of server-mcp.py). Internals stubbed like
_stub_what_if_internals (same file, :152-206) so no cluster is touched.
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"
for p in (str(SRC), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

_FAKE_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster: {server: "https://127.0.0.1:1"}
  name: fake
contexts:
- context: {cluster: fake, user: fake}
  name: fake
current-context: fake
users:
- name: fake
  user: {token: "fake-token"}
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    # COPIED from tests/test_6_final_source_additions.py:60
    # (importlib.util.spec_from_file_location with a unique module name,
    # required env/kubeconfig setup identical to that file).
    kubeconfig = tmp_path_factory.mktemp("kube_task4") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_task4", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_task4"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    if _orig_kube_loc is not None:
        try:
            from kubernetes.config import kube_config as _k8s_kube_config
            _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = _orig_kube_loc
        except Exception:
            pass
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


def _stub_internals(monkeypatch, server):
    """Mirror _stub_what_if_internals from test_6_final_source_additions.py:152:
    one monkeypatch.setattr(server, <name>, AsyncMock/MagicMock) per helper:
    collect_baseline_system_data, build_system_behavior_models,
    load_historical_performance_data, calibrate_simulation_models,
    run_monte_carlo_simulation, analyze_system_impact,
    identify_affected_components, perform_risk_assessment,
    calculate_simulation_quality, generate_simulation_recommendations —
    same return shapes as that helper uses."""

    async def fake_collect_baseline(scope, core_api, list_ns_fn, list_pods_fn, progress_cb=None):
        return {"nodes": [], "namespaces": [], "pods": []}

    async def fake_build_models(baseline, scenario_type):
        return {"type": scenario_type}

    async def fake_load_historical(scope, duration, prometheus_query_fn=None):
        return {}

    def fake_calibrate(models, hist, load_profile):
        return models

    async def fake_monte_carlo(models, changes, stype, duration, risk_tol):
        return {"mean": {}, "confidence": {}, "p95": {}, "worst": {}, "scenarios": [], "convergence": {}}

    def fake_impact(sim_results, baseline, stype):
        return {}

    async def fake_affected(changes, scope, stype, core_api, apps_api, list_pods_fn, list_ns_fn):
        return []

    def fake_risk(sim_results, impact, affected, risk_tol):
        return {}

    def fake_quality(baseline, hist, models, logger):
        return {}

    def fake_recommendations(impact, risk, quality, stype, logger):
        return []

    monkeypatch.setattr(server, "collect_baseline_system_data", fake_collect_baseline)
    monkeypatch.setattr(server, "build_system_behavior_models", fake_build_models)
    monkeypatch.setattr(server, "load_historical_performance_data", fake_load_historical)
    monkeypatch.setattr(server, "calibrate_simulation_models", fake_calibrate)
    monkeypatch.setattr(server, "run_monte_carlo_simulation", fake_monte_carlo)
    monkeypatch.setattr(server, "analyze_system_impact", fake_impact)
    monkeypatch.setattr(server, "identify_affected_components", fake_affected)
    monkeypatch.setattr(server, "perform_risk_assessment", fake_risk)
    monkeypatch.setattr(server, "calculate_simulation_quality", fake_quality)
    monkeypatch.setattr(server, "generate_simulation_recommendations", fake_recommendations)


def _stub_internals_except_baseline(monkeypatch, server):
    """Same as _stub_internals but leaves collect_baseline_system_data real
    so its per-namespace progress_cb calls (H-5, final fix wave) actually
    reach ctx.report_progress. Caller must monkeypatch server.k8s_core_api
    and server.list_pods to fakes before invoking the tool."""

    async def fake_build_models(baseline, scenario_type):
        return {"type": scenario_type}

    async def fake_load_historical(scope, duration, prometheus_query_fn=None):
        return {}

    def fake_calibrate(models, hist, load_profile):
        return models

    async def fake_monte_carlo(models, changes, stype, duration, risk_tol):
        return {"mean": {}, "confidence": {}, "p95": {}, "worst": {}, "scenarios": [], "convergence": {}}

    def fake_impact(sim_results, baseline, stype):
        return {}

    async def fake_affected(changes, scope, stype, core_api, apps_api, list_pods_fn, list_ns_fn):
        return []

    def fake_risk(sim_results, impact, affected, risk_tol):
        return {}

    def fake_quality(baseline, hist, models, logger):
        return {}

    def fake_recommendations(impact, risk, quality, stype, logger):
        return []

    monkeypatch.setattr(server, "build_system_behavior_models", fake_build_models)
    monkeypatch.setattr(server, "load_historical_performance_data", fake_load_historical)
    monkeypatch.setattr(server, "calibrate_simulation_models", fake_calibrate)
    monkeypatch.setattr(server, "run_monte_carlo_simulation", fake_monte_carlo)
    monkeypatch.setattr(server, "analyze_system_impact", fake_impact)
    monkeypatch.setattr(server, "identify_affected_components", fake_affected)
    monkeypatch.setattr(server, "perform_risk_assessment", fake_risk)
    monkeypatch.setattr(server, "calculate_simulation_quality", fake_quality)
    monkeypatch.setattr(server, "generate_simulation_recommendations", fake_recommendations)


class _FakeCoreApiForBaseline:
    """Minimal fake satisfying collect_baseline_system_data's core-api calls
    (list_namespaced_resource_quota, list_node) without touching a cluster."""

    def list_namespaced_resource_quota(self, namespace, **kwargs):
        return SimpleNamespace(items=[])

    def list_node(self, **kwargs):
        return SimpleNamespace(items=[])


class _RecordingCtx:
    def __init__(self):
        self.calls = []

    async def report_progress(self, progress, total=None, message=None):
        self.calls.append((progress, total, message))


class _ExplodingCtx:
    async def report_progress(self, *a, **kw):
        raise RuntimeError("client went away")


def test_progress_reported_at_phase_boundaries(server, monkeypatch):
    _stub_internals(monkeypatch, server)
    ctx = _RecordingCtx()
    result = asyncio.run(server.what_if_scenario_simulator(
        scenario_type="scaling",
        changes={"replicas": {"before": 1, "after": 2}},
        ctx=ctx,
    ))
    assert "error" not in result
    assert len(ctx.calls) >= 4
    steps = [c[0] for c in ctx.calls]
    assert steps == sorted(steps)  # monotonic


def test_progress_reported_per_namespace_during_baseline(server, monkeypatch):
    """H-5 (final fix wave): the two slow phases used to emit no progress
    between the 5 coarse boundaries. collect_baseline_system_data now takes
    a progress_cb and what_if_scenario_simulator wires it to
    _report_sim_progress(ctx, 1, 5, ...) — a multi-namespace scope must
    produce at least one 'baseline: namespace ...' message per namespace."""
    _stub_internals_except_baseline(monkeypatch, server)
    monkeypatch.setattr(server, "k8s_core_api", _FakeCoreApiForBaseline())

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1"}]

    monkeypatch.setattr(server, "list_pods", fake_list_pods)

    ctx = _RecordingCtx()
    scope = {"clusters": ["c"], "namespaces": ["ns-a", "ns-b"], "components": ["all"]}
    result = asyncio.run(server.what_if_scenario_simulator(
        scenario_type="scaling",
        changes={"replicas": {"before": 1, "after": 2}},
        scope=scope,
        ctx=ctx,
    ))
    assert "error" not in result
    baseline_msgs = [c[2] for c in ctx.calls if c[2] and "baseline: namespace" in c[2]]
    assert len(baseline_msgs) >= 2, f"expected per-namespace baseline progress, got {ctx.calls}"
    assert any("ns-a" in m for m in baseline_msgs)
    assert any("ns-b" in m for m in baseline_msgs)


def test_progress_failure_never_fails_the_tool(server, monkeypatch):
    _stub_internals(monkeypatch, server)
    result = asyncio.run(server.what_if_scenario_simulator(
        scenario_type="scaling",
        changes={"replicas": {"before": 1, "after": 2}},
        ctx=_ExplodingCtx(),
    ))
    assert "error" not in result


def test_no_ctx_still_works(server, monkeypatch):
    _stub_internals(monkeypatch, server)
    result = asyncio.run(server.what_if_scenario_simulator(
        scenario_type="scaling",
        changes={"replicas": {"before": 1, "after": 2}},
    ))
    assert "error" not in result
