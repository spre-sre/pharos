"""Phase 2e-b Task 1: per-instance extension gate unit tests (RED then GREEN).

Tests (a)-(e) from the brief Step 1.

Fixture loads server-mcp.py once under the name
`server_mcp_gate_extension` (unique; avoids collision with
characterization's session-scoped `server_mcp` fixture).
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"

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
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_gate_ext") / "config"
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
        "server_mcp_gate_extension", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_gate_extension"] = mod
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


# ─── (a) active / not-detected per-instance gate ─────────────────────────────

def test_gate_returns_none_when_active(server, monkeypatch):
    """_gate_extension returns None (passes) when extension is 'active' on instance."""
    monkeypatch.setattr(
        server, "_extension_states",
        {("tekton", "A"): "active", ("tekton", "B"): "not-detected: absent"},
    )
    result = server._gate_extension("list_pipelineruns", "A")
    assert result is None, (
        f"Expected None (gate passes) when extension is 'active'; got {result!r}"
    )


def test_gate_returns_error_dict_when_not_detected(server, monkeypatch):
    """_gate_extension returns error dict when extension is 'not-detected: absent'."""
    monkeypatch.setattr(
        server, "_extension_states",
        {("tekton", "A"): "active", ("tekton", "B"): "not-detected: absent"},
    )
    result = server._gate_extension("list_pipelineruns", "B")
    assert result is not None, "Expected error dict when extension is 'not-detected: absent'"
    assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
    assert result.get("extension") == "tekton", (
        f"Expected extension='tekton'; got {result.get('extension')!r}"
    )
    assert result.get("instance") == "B", (
        f"Expected instance='B'; got {result.get('instance')!r}"
    )
    assert result.get("extension_state") == "not-detected: absent", (
        f"Expected extension_state='not-detected: absent'; got {result.get('extension_state')!r}"
    )
    assert "error" in result, f"Error dict must have 'error' key; got: {sorted(result)}"
    assert "tool" in result, f"Error dict must have 'tool' key; got: {sorted(result)}"
    assert result.get("tool") == "list_pipelineruns", (
        f"Expected tool='list_pipelineruns'; got {result.get('tool')!r}"
    )
    assert "requested_source" in result, (
        f"Error dict must have 'requested_source' key; got: {sorted(result)}"
    )
    assert "hint" in result, (
        f"Error dict must have 'hint' key; got: {sorted(result)}"
    )


# ─── (b) M1 seed: missing key → 'unknown' (fail-closed D4) ──────────────────

def test_gate_unknown_state_for_missing_instance(server, monkeypatch):
    """(M1 seed) Instance not in _extension_states → extension_state == 'unknown' (fail-closed)."""
    # Instance C has NO entry at all
    monkeypatch.setattr(
        server, "_extension_states",
        {("tekton", "A"): "active"},
    )
    result = server._gate_extension("list_pipelineruns", "C")
    assert result is not None, "Expected error dict for unknown instance (fail-closed)"
    assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
    assert result.get("extension_state") == "unknown", (
        f"Expected extension_state='unknown' for missing key; got {result.get('extension_state')!r}"
    )
    assert result.get("instance") == "C", (
        f"Expected instance='C'; got {result.get('instance')!r}"
    )


# ─── (c) source="" resolves to default_kubernetes_instance ───────────────────

def test_gate_empty_source_resolves_default(server, monkeypatch):
    """source='' resolves to default_kubernetes_instance(); if 'active' there, gate passes."""
    default_name = server._source_registry.default_kubernetes_instance()
    assert default_name is not None, "konfux profile must have a default kubernetes instance"
    monkeypatch.setattr(
        server, "_extension_states",
        {("tekton", default_name): "active"},
    )
    result = server._gate_extension("list_pipelineruns", "")
    assert result is None, (
        f"Expected None (gate passes) for source='' when (tekton, default) is 'active'; "
        f"got {result!r} (default_name={default_name!r})"
    )


def test_gate_empty_source_not_active_returns_error(server, monkeypatch):
    """source='' with inactive extension returns error dict with instance=default name."""
    default_name = server._source_registry.default_kubernetes_instance()
    monkeypatch.setattr(
        server, "_extension_states",
        {("tekton", default_name): "not-detected: absent"},
    )
    result = server._gate_extension("list_pipelineruns", "")
    assert result is not None, "Expected error dict when extension is not active on default"
    assert result.get("instance") == default_name, (
        f"Expected instance={default_name!r} (resolved from ''); got {result.get('instance')!r}"
    )


# ─── (d) _TOOL_EXTENSION contents: 7 tekton + 3 openshift, no konflux ────────

def test_tool_extension_exists_and_is_dict(server):
    """_TOOL_EXTENSION must be a module-level dict."""
    assert hasattr(server, "_TOOL_EXTENSION"), (
        "server must expose _TOOL_EXTENSION dict"
    )
    assert isinstance(server._TOOL_EXTENSION, dict), (
        f"Expected dict, got {type(server._TOOL_EXTENSION).__name__}"
    )


def test_tool_extension_has_exactly_7_tekton_tools(server):
    """_TOOL_EXTENSION must map exactly 7 tekton tool names to 'tekton'."""
    tekton_names = [n for n, ext in server._TOOL_EXTENSION.items() if ext == "tekton"]
    expected_tekton = sorted([
        "analyze_failed_pipeline",
        "find_pipeline",
        "get_pipelinerun_logs",
        "get_tekton_pipeline_runs_status",
        "list_pipelineruns",
        "list_recent_pipeline_runs",
        "list_taskruns",
    ])
    assert sorted(tekton_names) == expected_tekton, (
        f"Expected tekton tools {expected_tekton}; got {sorted(tekton_names)}"
    )


def test_tool_extension_has_exactly_3_openshift_tools(server):
    """_TOOL_EXTENSION must map exactly 3 openshift tool names to 'openshift'."""
    openshift_names = [n for n, ext in server._TOOL_EXTENSION.items() if ext == "openshift"]
    expected_openshift = sorted([
        "get_etcd_logs",
        "get_machine_config_pool_status",
        "get_openshift_cluster_operator_status",
    ])
    assert sorted(openshift_names) == expected_openshift, (
        f"Expected openshift tools {expected_openshift}; got {sorted(openshift_names)}"
    )


def test_tool_extension_total_count_is_ten(server):
    """_TOOL_EXTENSION must have exactly 10 entries (7 tekton + 3 openshift)."""
    assert len(server._TOOL_EXTENSION) == 10, (
        f"Expected 10 entries (7 tekton + 3 openshift); got {len(server._TOOL_EXTENSION)}"
    )


def test_tool_extension_no_konflux_tools(server):
    """_TOOL_EXTENSION must NOT contain any konflux tool names (R7 atomicity, D9)."""
    konflux_names = [n for n, ext in server._TOOL_EXTENSION.items() if ext == "konflux"]
    assert konflux_names == [], (
        f"Expected no konflux tools in _TOOL_EXTENSION (D9 deferral); "
        f"got {sorted(konflux_names)}"
    )


# ─── (e) dial-free: _dial_call_count unchanged across gate calls ──────────────

def test_gate_is_dial_free(server, monkeypatch):
    """_gate_extension must never increment _dial_call_count (dial-free D4)."""
    monkeypatch.setattr(
        server, "_extension_states",
        {
            ("tekton", "A"): "active",
            ("tekton", "B"): "not-detected: absent",
        },
    )
    before = server._dial_call_count

    server._gate_extension("list_pipelineruns", "A")
    server._gate_extension("list_pipelineruns", "B")
    server._gate_extension("list_pipelineruns", "C")  # missing key → unknown

    assert server._dial_call_count == before, (
        f"_gate_extension must never dial; count went from {before} to "
        f"{server._dial_call_count}"
    )


# ─── error dict shape completeness ───────────────────────────────────────────

def test_gate_error_dict_has_all_required_keys(server, monkeypatch):
    """Error dict from _gate_extension must have all 7 required keys."""
    monkeypatch.setattr(
        server, "_extension_states",
        {("tekton", "A"): "not-detected: error:SomeException"},
    )
    result = server._gate_extension("list_pipelineruns", "A")
    assert result is not None, "Expected error dict for non-active state"
    required_keys = {"error", "tool", "requested_source", "extension", "instance",
                     "extension_state", "hint"}
    missing = required_keys - set(result)
    assert not missing, (
        f"Error dict missing required keys: {sorted(missing)}; got keys: {sorted(result)}"
    )
