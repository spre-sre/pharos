"""
tests/test_search_resources_by_labels.py

M5/M6 correctness pins for cleanup1 task 2 (D3 + D4a/D4b).

M5 — SRBL fabrication fix: search_resources_by_labels(["pods","clustertasks"])
     with a 2-pod fake must yield exactly 2 resources, clustertasks count 0
     (key PRESENT), and exactly 1 UNSUPPORTED_CLUSTER_SCOPED_API error.
     Pre-fix this returns 4 resources and 0 errors — the stale pod response
     leaks into the clustertasks cluster-scoped dispatch because response is
     never reset to None between resource_type iterations.

M6 — Admission-webhook plural fix: get_kubernetes_resource with
     resource_type "validatingadmissionwebhook" / "mutatingadmissionwebhook"
     must call get_cluster_custom_object with plural=
     "validatingwebhookconfigurations" / "mutatingwebhookconfigurations"
     (the real API names), not the old wrong aliases "validatingadmissionwebhooks"
     / "mutatingadmissionwebhooks" that 404 against the live API.
     Spy target: server-mcp.py:2241-2247 (plural=method_name kwarg).
     Mutation: revert either string in admission_resources → assertion fails.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

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

    Uses a distinct sys.modules key ("server_mcp_srbl") so this import
    coexists with the session-scoped characterization fixture ("server_mcp")
    without collision.
    """
    _orig_kubeconfig = os.environ.get("KUBECONFIG")
    _orig_kubearchive = os.environ.get("KUBEARCHIVE_ENABLED")
    _orig_telemetry = os.environ.get("LUMINO_DISABLE_TELEMETRY")

    kubeconfig = tmp_path_factory.mktemp("kube_srbl") / "config"
    kubeconfig.write_text(FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_srbl", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_srbl"] = mod
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
    sys.modules.pop("server_mcp_srbl", None)
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


def _fake_pod_list(count, ns="test-ns"):
    """Return a SimpleNamespace with .items containing count fake pod objects."""
    pods = []
    for i in range(count):
        name = f"pod-{i + 1}"
        pod = types.SimpleNamespace()
        pod.to_dict = (
            lambda _n=name, _ns=ns: {
                "metadata": {
                    "name": _n,
                    "namespace": _ns,
                    "labels": {"app": _n},
                    "annotations": {},
                    "creation_timestamp": None,
                    "resource_version": None,
                    "uid": None,
                    "owner_references": None,
                },
                "spec": {"node_name": "node-1", "containers": []},
                "status": {
                    "phase": "Running",
                    "conditions": None,
                    "pod_ip": None,
                    "start_time": None,
                    "container_statuses": None,
                    "init_container_statuses": None,
                },
            }
        )
        pods.append(pod)
    return types.SimpleNamespace(items=pods)


# ── M5: SRBL fabrication fix ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_fabrication_mixed_pod_clustertask(server, monkeypatch):
    """M5: ["pods","clustertasks"] with 2-pod fake must not fabricate clustertask rows.

    Post-fix invariants (all four must hold simultaneously):
      • total_resources_found == 2
      • resource_type_counts == {"pods": 2, "clustertasks": 0}  (key PRESENT)
      • len(resources) == 2
      • len(error_details) == 1  and  error_details[0]["error_code"]
          == "UNSUPPORTED_CLUSTER_SCOPED_API"

    The LENGTH pin on error_details catches a double-append regression: asserting
    only "has that code" passes even when a second error row sneaks in.
    The key-PRESENT requirement for clustertasks matches the UNEXPECTED_ERROR
    shape used by today's outer except handler, so the fix changes only the
    error CODE and kills the fabrication — nothing else.
    """
    fake_core = MagicMock()
    fake_core.list_namespaced_pod.return_value = _fake_pod_list(2)
    monkeypatch.setattr(server, "k8s_core_api", fake_core)
    monkeypatch.setattr(server, "k8s_custom_api", MagicMock())
    monkeypatch.setattr(server, "k8s_apps_api", MagicMock())
    monkeypatch.setattr(server, "k8s_batch_api", MagicMock())

    result = await server.search_resources_by_labels(
        resource_types=["pods", "clustertasks"],
        label_selectors=[{"key": "app", "value": "x", "operator": "equals"}],
        namespaces=["test-ns"],
    )

    summary = result["search_summary"]
    assert summary["total_resources_found"] == 2, (
        f"expected 2 total, got {summary['total_resources_found']}; "
        f"counts={summary['resource_type_counts']}"
    )
    assert summary["resource_type_counts"] == {"pods": 2, "clustertasks": 0}, (
        f"unexpected counts: {summary['resource_type_counts']}"
    )
    assert len(result["resources"]) == 2, (
        f"expected 2 resource rows, got {len(result['resources'])}"
    )
    errors = result["error_details"]
    assert len(errors) == 1, (
        f"expected exactly 1 error entry, got {len(errors)}: {errors}"
    )
    assert errors[0]["error_code"] == "UNSUPPORTED_CLUSTER_SCOPED_API", (
        f"wrong error code: {errors[0]}"
    )


# ── M6: Admission-webhook plural fix ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_validating_webhook_uses_correct_plural(server, monkeypatch):
    """M6a: validatingadmissionwebhook must use plural='validatingwebhookconfigurations'.

    Spy target: server-mcp.py:2241-2247 (plural=method_name kwarg passed to
    get_cluster_custom_object).  Mutation: revert admission_resources key to
    'validatingadmissionwebhooks' → this assertion fails.
    """
    fake_custom = MagicMock()
    fake_custom.get_cluster_custom_object.return_value = {
        "metadata": {"name": "test-wh"}
    }
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    await server.get_kubernetes_resource(
        resource_type="validatingadmissionwebhook",
        name="test-wh",
        namespace="",
    )

    kwargs = fake_custom.get_cluster_custom_object.call_args.kwargs
    assert kwargs["plural"] == "validatingwebhookconfigurations", (
        f"wrong plural for validatingadmissionwebhook: {kwargs.get('plural')!r}"
    )


@pytest.mark.asyncio
async def test_mutating_webhook_uses_correct_plural(server, monkeypatch):
    """M6b: mutatingadmissionwebhook must use plural='mutatingwebhookconfigurations'.

    Spy target: server-mcp.py:2241-2247 (plural=method_name kwarg passed to
    get_cluster_custom_object).  Mutation: revert admission_resources key to
    'mutatingadmissionwebhooks' → this assertion fails.
    """
    fake_custom = MagicMock()
    fake_custom.get_cluster_custom_object.return_value = {
        "metadata": {"name": "test-wh"}
    }
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    await server.get_kubernetes_resource(
        resource_type="mutatingadmissionwebhook",
        name="test-wh",
        namespace="",
    )

    kwargs = fake_custom.get_cluster_custom_object.call_args.kwargs
    assert kwargs["plural"] == "mutatingwebhookconfigurations", (
        f"wrong plural for mutatingadmissionwebhook: {kwargs.get('plural')!r}"
    )
