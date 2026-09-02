"""
tests/test_tekton_cluster_discovery.py

M-C2a pin: get_tekton_pipeline_runs_status cluster-wide discovery (F-02).

Root cause (stone-stg-rh01): the old per-namespace probe used the tenant
label-selector to discover namespaces.  That selector matched thousands of
alphabetically-early DORMANT tenants (abhindas-tenant, abiton-tenant, …),
so the max_namespaces * 2 cap was exhausted before any namespace with real
pipeline activity was reached.  total always returned 0.

Fix: ONE list_cluster_custom_object(plural="pipelineruns") for discovery;
derive active_namespaces from PR metadata; the tenant-label call is kept as
a FILTER that narrows the active set (option-a, controller ruling).

Fixture design:
• recording_list_namespace — simulates 2000 dormant tenant namespaces
• recording_cluster_wide  — returns two real PLRs in "real-ns"
• empty_ns_prs            — all per-namespace probes return empty
• _PLR_OK / _PLR_BAD      — minimal PLRs with distinct statuses

Pre-fix:  per-namespace probe probes dormant-0 … dormant-N, all empty → total=0.
Post-fix: cluster-wide call returns [_PLR_OK, _PLR_BAD] → total=2.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

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
    """Import server-mcp.py once per module against a fake kubeconfig."""
    kubeconfig = tmp_path_factory.mktemp("kube_tcd") / "config"
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

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_tcd", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_tcd"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    sys.modules.pop("server_mcp_tcd", None)
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _ns_obj(name):
    """Minimal namespace object with .metadata.name."""
    ns = types.SimpleNamespace()
    ns.metadata = types.SimpleNamespace(name=name)
    return ns


def _items_list(items):
    """Fake list-response with .items."""
    return types.SimpleNamespace(items=list(items))


_PLR_OK = {
    "apiVersion": "tekton.dev/v1", "kind": "PipelineRun",
    "metadata": {"name": "run-1", "namespace": "real-ns"},
    "status": {"conditions": [{"type": "Succeeded", "status": "True",
                                "reason": "Succeeded"}]},
}

_PLR_BAD = {
    "apiVersion": "tekton.dev/v1", "kind": "PipelineRun",
    "metadata": {"name": "run-2", "namespace": "real-ns"},
    "status": {"conditions": [{"type": "Succeeded", "status": "False",
                                "reason": "Failed"}]},
}


def _cluster_wide_prs(group, version, plural, **kw):
    """Return the two real PLRs for pipelineruns; empty for everything else."""
    if plural == "pipelineruns":
        return {"items": [_PLR_OK, _PLR_BAD]}
    return {"items": []}


def _empty_ns_prs(*a, **kw):
    """All per-namespace probes return empty — dormant namespaces have no runs."""
    return {"items": []}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cluster_wide_discovery_returns_real_runs(server, monkeypatch):
    """Post-fix: cluster-wide call finds the real PLRs in real-ns.

    Pre-fix behaviour: the per-namespace probe probes dormant-0 … dormant-1999
    (all empty) and exhausts max_namespaces * 2; total remains 0.
    Post-fix: ONE list_cluster_custom_object() returns [PLR_OK, PLR_BAD]; total=2.

    Mutation target (M-C2a): restoring the old if all_namespaces: per-namespace
    probe makes this test RED (total=0).
    """
    ns_calls = []

    def recording_list_namespace(*a, **kw):
        ns_calls.append(kw.get("label_selector", ""))
        return _items_list([_ns_obj(f"dormant-{i}") for i in range(2000)])

    cw_calls = []

    def recording_cluster_wide(*a, **kw):
        cw_calls.append(kw.get("plural", ""))
        return _cluster_wide_prs(*a, **kw)

    fake_core = MagicMock()
    fake_core.list_namespace.side_effect = recording_list_namespace
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    fake_custom = MagicMock()
    fake_custom.list_cluster_custom_object.side_effect = recording_cluster_wide
    fake_custom.list_namespaced_custom_object.side_effect = _empty_ns_prs
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    result = await server.get_tekton_pipeline_runs_status()

    # Core assertion: two PLRs found, not zero.
    assert result["pipeline_runs"]["total"] == 2, (
        f"Expected total=2 (cluster-wide found real-ns PLRs), "
        f"got total={result['pipeline_runs']['total']}; "
        f"cw_calls={cw_calls}, ns_calls={ns_calls}"
    )

    # The cluster-wide PLR call must have fired.
    assert "pipelineruns" in cw_calls, (
        f"list_cluster_custom_object(plural='pipelineruns') not called; "
        f"cw_calls={cw_calls}"
    )

    # The tenant-label filter call must fire (option-a: filter, not discovery).
    assert len(ns_calls) > 0, (
        "list_namespace(label_selector=...) not called — "
        "tenant-label filter was deleted (option-a requires it)"
    )

    # Disjoint-guard: the 2000 dormant tenant namespaces do NOT overlap with
    # "real-ns" where the PLRs live. The filter must be fail-open: when the
    # intersection is empty, active_namespaces stays {"real-ns"} so that
    # namespaces_sampled > 0 and the TaskRun loop still fires.
    # Pre-fix (if tenant_namespaces: active_namespaces &= tenant_namespaces):
    # active_namespaces becomes {} → namespaces_sampled == 0 (silent half-zero,
    # same failure class as F-02). Post-fix (only narrow when intersection is
    # non-empty): active_namespaces stays {"real-ns"} → namespaces_sampled == 1.
    assert result["sampling_info"]["namespaces_sampled"] > 0, (
        f"Disjoint tenant set must NOT zero out active_namespaces; "
        f"got namespaces_sampled={result['sampling_info']['namespaces_sampled']}; "
        f"cw_calls={cw_calls}, ns_calls={ns_calls}"
    )


@pytest.mark.asyncio
async def test_tenant_filter_drops_non_tenant_namespace(server, monkeypatch):
    """Partial-overlap filter: a namespace with PLRs but no tenant label is dropped.

    Fixture: three namespaces each have one PipelineRun.
      ns-a, ns-b — both carry the tenant label
      ns-c       — has a PLR but is NOT a tenant namespace

    Expected post-fix:
      active_namespaces narrowed to {ns-a, ns-b} → namespaces_sampled == 2
      ns-c's TaskRuns are NOT fetched (no list_namespaced_custom_object call for ns-c)

    Mutation target: deleting the narrowing block entirely leaves
      active_namespaces = {ns-a, ns-b, ns-c} → namespaces_sampled == 3 → RED.
    A pure no-op (skipping the intersection) is therefore discriminated by
    the == 2 assertion AND the "ns-c" not in tr_calls check.
    """
    _PLR_A = {
        "metadata": {"name": "run-a", "namespace": "ns-a"},
        "status": {"conditions": [{"type": "Succeeded", "status": "True",
                                   "reason": "Succeeded"}]},
    }
    _PLR_B = {
        "metadata": {"name": "run-b", "namespace": "ns-b"},
        "status": {"conditions": []},
    }
    _PLR_C = {
        "metadata": {"name": "run-c", "namespace": "ns-c"},
        "status": {"conditions": []},
    }

    tr_ns_calls = []  # spy: which namespaces had TaskRun fetches

    def recording_list_namespaced(**kw):
        if kw.get("plural") == "taskruns":
            tr_ns_calls.append(kw.get("namespace"))
        return {"items": []}

    def two_tenant_namespaces(**kw):
        """Returns ns-a and ns-b as tenant namespaces; ns-c is absent."""
        return _items_list([_ns_obj("ns-a"), _ns_obj("ns-b")])

    fake_core = MagicMock()
    fake_core.list_namespace.side_effect = two_tenant_namespaces
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    fake_custom = MagicMock()
    fake_custom.list_cluster_custom_object.return_value = {
        "items": [_PLR_A, _PLR_B, _PLR_C]}
    fake_custom.list_namespaced_custom_object.side_effect = recording_list_namespaced
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    result = await server.get_tekton_pipeline_runs_status()

    assert result["sampling_info"]["namespaces_sampled"] == 2, (
        f"Expected namespaces_sampled=2 (ns-a and ns-b); ns-c must be filtered; "
        f"got {result['sampling_info']['namespaces_sampled']}; tr_ns_calls={tr_ns_calls}"
    )
    assert "ns-c" not in tr_ns_calls, (
        f"ns-c is not a tenant namespace — its TaskRuns must not be fetched; "
        f"tr_ns_calls={tr_ns_calls}"
    )
