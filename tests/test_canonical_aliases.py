"""Phase 2c Task 1: canonical tool name registration tests.

Module-scoped fixture loads server-mcp.py once under the name
`server_mcp_canonical_aliases` (unique name to avoid collision with the
session-scoped `server_mcp` fixture in characterization/conftest.py).
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path

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
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_canonical_aliases") / "config"
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

    # F9 harness-bleed guard: pin KUBE_CONFIG_DEFAULT_LOCATION to the fake
    # kubeconfig so _discover_kube_contexts reads only harness contexts.
    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_canonical_aliases", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_canonical_aliases"] = mod
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


PAIRS = {
    "analyze_pod_logs_hybrid": "analyze_logs_hybrid",
    "live_system_topology_mapper": "topology_mapper",
    "prometheus_query": "query_metrics",
    "smart_get_namespace_events": "get_events_smart",
    "smart_summarize_pod_logs": "smart_summarize_logs",
    "stream_analyze_pod_logs": "stream_analyze_logs",
}


def test_map_matches_spec_table(server):
    assert server._CANONICAL_ALIASES == PAIRS


@pytest.mark.parametrize("old,canonical", sorted(PAIRS.items()))
def test_m1_shared_body(server, old, canonical):
    tools = server.mcp._tool_manager._tools
    assert canonical in tools, f"{canonical} not registered"
    assert tools[canonical].fn is tools[old].fn          # ONE body, two names


@pytest.mark.parametrize("old,canonical", sorted(PAIRS.items()))
def test_m5_schema_equal_except_name(server, old, canonical):
    tools = server.mcp._tool_manager._tools
    assert tools[canonical].parameters == tools[old].parameters
    assert tools[canonical].description == tools[old].description
    assert tools[canonical].name == canonical and tools[old].name == old


def test_r1_duplicate_add_returns_existing_without_overwrite(server, caplog):
    # Pin the SDK dedup semantic that makes the R1 collision guard necessary:
    # ToolManager.add_tool (mcp 1.28.1 lines 67-72) warns and returns the
    # EXISTING tool without overwriting when the name is already taken.
    # If the SDK ever changes to silently overwrite, this test fails loud,
    # proving the raise-guard in _CANONICAL_ALIASES loop is still required.
    tools = server.mcp._tool_manager._tools
    original_fn = tools["query_metrics"].fn  # shared fn registered by PAIRS loop

    foreign_fn = server.list_namespaces  # a fn NOT part of any alias pair
    assert foreign_fn is not original_fn, "precondition: different function objects"

    with caplog.at_level(logging.WARNING, logger="mcp.server.fastmcp.tools.tool_manager"):
        returned = server.mcp._tool_manager.add_tool(foreign_fn, name="query_metrics")

    # SDK must have warned about the duplicate
    assert any(
        "Tool already exists" in r.message for r in caplog.records
    ), "add_tool must warn on duplicate registration"

    # add_tool must return the EXISTING tool, not wrap the foreign fn
    assert returned.fn is original_fn, (
        "add_tool must return existing tool object, not a new one for the foreign fn"
    )

    # registry must remain unchanged — no overwrite
    assert tools["query_metrics"].fn is original_fn, (
        "_tools['query_metrics'] must not be overwritten by a duplicate add"
    )
