"""
tests/test_namespace_guard.py

F-07 correctness pin for cleanup2b task 1.

F-07 — GHOST NAMESPACE HEALTHY: check_resource_constraints reports
       status "Healthy" for a namespace that does not exist, because it
       calls list_namespaced_pod (returns []) and list_namespaced_resource_quota
       (returns []) without first verifying the namespace exists — an empty
       namespace and a missing namespace look identical.

Step 7: unit test for the read_namespace existence guard in server-mcp.py.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from kubernetes.client.rest import ApiException

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# Reuse the characterization fakes (already on PYTHONPATH when the suite runs,
# but we import them explicitly with a path insert to be self-contained).
sys.path.insert(0, str(REPO_ROOT / "tests" / "characterization"))
from k8s_fakes import FakeApi, items_list  # noqa: E402

FAKE_KUBECONFIG = """\
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
    """Import server-mcp.py once per module against a fake kubeconfig.

    Uses a distinct sys.modules key ("server_mcp_nsg") so this import
    coexists with other module-scoped server fixtures without collision.
    """
    _orig_kubeconfig = os.environ.get("KUBECONFIG")
    _orig_kubearchive = os.environ.get("KUBEARCHIVE_ENABLED")
    _orig_telemetry = os.environ.get("LUMINO_DISABLE_TELEMETRY")

    kubeconfig = tmp_path_factory.mktemp("kube_nsg") / "config"
    kubeconfig.write_text(FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_nsg", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_nsg"] = mod
    spec.loader.exec_module(mod)

    yield mod

    def _restore_env(key, original):
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    _restore_env("KUBECONFIG", _orig_kubeconfig)
    _restore_env("KUBEARCHIVE_ENABLED", _orig_kubearchive)
    _restore_env("LUMINO_DISABLE_TELEMETRY", _orig_telemetry)
    sys.modules.pop("server_mcp_nsg", None)
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


# ── Step 7: F-07 RED ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_resource_constraints_nonexistent_namespace_returns_error(
    server, monkeypatch
):
    """check_resource_constraints on a missing namespace must return an error dict.

    The fake core_api has read_namespace raise ApiException(404) to simulate
    a namespace that does not exist.  The other methods are canned with empty
    responses so that, if the fix is absent, the tool can complete normally
    (returning "Healthy") — making the absence of a guard visible as a RED.

    Pre-fix: read_namespace is never called; tool returns {"status": "Healthy"}.
    Post-fix: tool returns {"error": "Namespace 'ghost-ns' does not exist",
              "namespace": "ghost-ns", "status": "Error"}.
    """
    fake_core = FakeApi(
        read_namespace=ApiException(status=404, reason="Not Found"),
        list_namespaced_pod=items_list([]),
        list_namespaced_resource_quota=items_list([]),
        list_namespaced_limit_range=items_list([]),
        list_namespaced_event=items_list([]),
    )
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    result = await server.check_resource_constraints(namespace="ghost-ns")

    assert "error" in result, (
        f"Expected 'error' key in result for non-existent namespace; "
        f"got: {sorted(result.keys())} — result: {result!r}"
    )
    assert result.get("status") != "Healthy", (
        f"Expected non-Healthy status for non-existent namespace; "
        f"got {result.get('status')!r} — result: {result!r}"
    )


# ── Final-review fix wave: F-07 shape contract ───────────────────────────────

# The eight keys documented in check_resource_constraints's docstring and
# returned by both sibling error handlers (ApiException / Exception).
_DOCUMENTED_KEYS = frozenset({
    "status",
    "summary",
    "resource_quotas",
    "pending_pods_due_to_resources",
    "oom_killed_containers",
    "container_issues",
    "high_utilization_quotas",
    "recommendations",
})


@pytest.mark.asyncio
async def test_check_resource_constraints_404_returns_full_key_set(
    server, monkeypatch
):
    """404 branch must return all 8 documented keys AND stay distinguishable from 403.

    Pre-fix: 404 returns only {error, namespace, status} — KeyError on
    result["recommendations"] — while 403 (outer ApiException handler) returns
    all 8 keys plus 'error'.  The two failure modes differ in *shape*, not
    just content.

    Post-fix: 404 returns all 8 documented keys plus 'error' and 'namespace',
    and the error text contains "does not exist" (404-specific, not the raw
    ApiException message a 403 would produce).
    """
    fake_core_404 = FakeApi(
        read_namespace=ApiException(status=404, reason="Not Found"),
        list_namespaced_pod=items_list([]),
        list_namespaced_resource_quota=items_list([]),
        list_namespaced_limit_range=items_list([]),
        list_namespaced_event=items_list([]),
    )
    monkeypatch.setattr(server, "k8s_core_api", fake_core_404)

    result = await server.check_resource_constraints(namespace="ghost-ns")

    missing = _DOCUMENTED_KEYS - result.keys()
    assert not missing, (
        f"404 result is missing documented keys {sorted(missing)}; "
        f"got keys: {sorted(result.keys())} — result: {result!r}"
    )

    # 404 must remain distinguishable from a 403 (outer ApiException handler):
    # the error text must be namespace-specific, not a raw k8s API error string.
    assert "error" in result, "404 result must include 'error' key for disambiguation"
    assert "does not exist" in result["error"].lower(), (
        f"404 error text should indicate namespace absence; got: {result['error']!r}"
    )
    assert result.get("status") != "Healthy", (
        f"404 status must not be 'Healthy'; got: {result.get('status')!r}"
    )
