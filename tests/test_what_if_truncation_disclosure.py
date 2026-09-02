"""Final fix-wave (2026-08-21): I-4 — truncation sentinel must reach the tool
RESPONSE, not just a server-side logger.warning.

check_resource_constraints and track_pipeline_across_namespaces both strip
the `_truncation` sentinel appended by list_pods (server-mcp.py:1658-1728 /
helpers.utils.list_pods) after only logging a warning. A client calling
either tool over MCP never sees that its pod analysis covered a sample, not
the whole namespace. Both now add a top-level `pod_analysis_truncated` key
to the returned dict when truncation occurred.

Fixture pattern copied from tests/test_what_if_progress.py (module-scoped
importlib load of server-mcp.py so no real cluster is touched).
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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
    # COPIED from tests/test_what_if_progress.py (itself copied from
    # tests/test_6_final_source_additions.py:60).
    kubeconfig = tmp_path_factory.mktemp("kube_trunc") / "config"
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
        "server_mcp_truncation_disclosure", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_truncation_disclosure"] = mod
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


class _FakeCoreApiForConstraints:
    """Minimal fake for check_resource_constraints: namespace exists, no
    quotas. Pods carrying status "Succeeded" skip the detailed
    read_namespaced_pod branch so this fake never needs to implement it."""

    def read_namespace(self, name, **kwargs):
        return SimpleNamespace(metadata=SimpleNamespace(name=name))

    def list_namespaced_resource_quota(self, namespace, **kwargs):
        return SimpleNamespace(items=[])


_SENTINEL = {"_truncation": {"limit": 200, "truncated": True}}


def test_check_resource_constraints_discloses_truncation(server, monkeypatch):
    monkeypatch.setattr(server, "k8s_core_api", _FakeCoreApiForConstraints())

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1", "status": "Succeeded"}, dict(_SENTINEL)]

    monkeypatch.setattr(server, "list_pods", fake_list_pods)

    result = asyncio.run(server.check_resource_constraints(namespace="ns-a"))
    assert "error" not in result, result
    assert result.get("pod_analysis_truncated") == {
        "limit": 200,
        "note": "namespace has more pods than the analysis limit; results cover a sample",
    }


def test_check_resource_constraints_no_truncation_key_when_complete(server, monkeypatch):
    monkeypatch.setattr(server, "k8s_core_api", _FakeCoreApiForConstraints())

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1", "status": "Succeeded"}]

    monkeypatch.setattr(server, "list_pods", fake_list_pods)

    result = asyncio.run(server.check_resource_constraints(namespace="ns-a"))
    assert "error" not in result, result
    assert "pod_analysis_truncated" not in result


def test_track_pipeline_across_namespaces_discloses_truncation(server, monkeypatch):
    async def fake_detect_tekton_namespaces(source: str = ""):
        return {"group": ["ns-a"]}

    async def fake_get_pipeline_details(namespace, pipeline_id, custom_api, list_taskruns_fn, calc_duration, log):
        return {"error": "not found"}

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1", "status": "Running", "labels": {}}, dict(_SENTINEL)]

    monkeypatch.setattr(server, "detect_tekton_namespaces", fake_detect_tekton_namespaces)
    monkeypatch.setattr(server, "get_pipeline_details", fake_get_pipeline_details)
    monkeypatch.setattr(server, "list_pods", fake_list_pods)
    monkeypatch.setattr(server, "k8s_core_api", object())

    result = asyncio.run(server.track_pipeline_across_namespaces(pipeline_id="pl-123"))
    assert "error" not in result, result
    assert result.get("pod_analysis_truncated") == {
        "limit": 200,
        "namespaces": ["ns-a"],
        "note": "namespace has more pods than the analysis limit; results cover a sample",
    }


def test_track_pipeline_across_namespaces_no_truncation_key_when_complete(server, monkeypatch):
    async def fake_detect_tekton_namespaces(source: str = ""):
        return {"group": ["ns-a"]}

    async def fake_get_pipeline_details(namespace, pipeline_id, custom_api, list_taskruns_fn, calc_duration, log):
        return {"error": "not found"}

    async def fake_list_pods(namespace, k8s_core_api, log, limit=200, field_selector=None):
        return [{"name": "p1", "status": "Running", "labels": {}}]

    monkeypatch.setattr(server, "detect_tekton_namespaces", fake_detect_tekton_namespaces)
    monkeypatch.setattr(server, "get_pipeline_details", fake_get_pipeline_details)
    monkeypatch.setattr(server, "list_pods", fake_list_pods)
    monkeypatch.setattr(server, "k8s_core_api", object())

    result = asyncio.run(server.track_pipeline_across_namespaces(pipeline_id="pl-123"))
    assert "error" not in result, result
    assert "pod_analysis_truncated" not in result
