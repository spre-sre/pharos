"""Task 3 seam tests — D9 facade instance views + konflux tool dispatch.

Covers:
  1. for_instance("") short-circuits: returns registry itself (never consults views)
  2. View seam: for_instance(source) returns _InstanceView whose properties resolve
     instance-specific clients (not server globals)
  3. Late-binding: swapping _resolve_k8s after view creation is reflected at access time
  4. InstanceResolutionError propagation: bad source → exception → caught → error dict
  5. Extension-active gate: named source with inactive extension → error dict
  6. Mutation checks:
     (a) Freeze late-binding mutation — seam test detects stored-snapshot bug
     (b) Swallow resolution error mutation — seam test detects silent-fallback bug

Each test is self-contained: stubs build a minimal fake registry+server and invoke
_InstanceView or the konflux tool factories directly, without loading server-mcp.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Helpers — fake server and registry stubs
# ---------------------------------------------------------------------------

def _make_fake_clientset(core=None, custom=None, apps=None):
    """Minimal fake K8sClientSet-like object."""
    ns = SimpleNamespace()
    ns.core_api = core if core is not None else object()
    ns.custom_api = custom if custom is not None else object()
    ns.apps_api = apps if apps is not None else object()
    return ns


def _make_fake_server(clientsets: dict, extension_states: dict = None):
    """Build a fake server module with _resolve_k8s and _extension_states.

    clientsets: {source_name: (fake_clientset, None)} or {source_name: (None, error_dict)}
    extension_states: {(ext_name, instance): state}
    """
    if extension_states is None:
        extension_states = {}

    async def _fake_prometheus(query, timeout=30, **kwargs):
        return {"success": True, "data": [], "captured_kwargs": kwargs}

    server = SimpleNamespace()
    server._extension_states = extension_states
    server._execute_prometheus_query_internal = _fake_prometheus

    def _resolve_k8s(source):
        if source in clientsets:
            return clientsets[source]
        # unknown → error dict
        return (None, {
            "error": f"unknown kubernetes instance {source!r}",
            "tool": "_resolve_k8s",
            "requested_source": source,
            "known_kubernetes_instances": [],
        })

    server._resolve_k8s = _resolve_k8s

    async def _fake_detect_tekton_namespaces():
        return {"active": []}

    server.detect_tekton_namespaces = _fake_detect_tekton_namespaces
    return server


def _make_fake_registry(server):
    """Build a fake ToolRegistry-like object suitable for _InstanceView."""
    from core.extension import ToolRegistry

    reg = object.__new__(ToolRegistry)
    reg._server = server
    # _InstanceView only needs _server; other attrs unused in the tests below
    return reg


# ---------------------------------------------------------------------------
# 1. for_instance("") short-circuits — never consults views
# ---------------------------------------------------------------------------


def test_for_instance_empty_returns_registry():
    """for_instance('') must return the registry object itself.

    RED: fails before for_instance is implemented (AttributeError).
    GREEN: returns self when source is empty.
    """
    from core.extension import ToolRegistry, _InstanceView

    cs = _make_fake_clientset()
    server = _make_fake_server({"default": (cs, None)})
    reg = _make_fake_registry(server)

    result = reg.for_instance("")
    assert result is reg, (
        f"for_instance('') must return the registry itself, got {type(result)}"
    )
    # Must NOT be a view
    assert not isinstance(result, _InstanceView), (
        "for_instance('') must NOT return an _InstanceView; "
        "default path must be byte-preserved"
    )


def test_for_instance_named_returns_view():
    """for_instance(non-empty) must return an _InstanceView."""
    from core.extension import ToolRegistry, _InstanceView

    cs = _make_fake_clientset()
    server = _make_fake_server({"my-cluster": (cs, None)})
    reg = _make_fake_registry(server)

    view = reg.for_instance("my-cluster")
    assert isinstance(view, _InstanceView), (
        f"for_instance('my-cluster') must return _InstanceView, got {type(view)}"
    )


# ---------------------------------------------------------------------------
# 2. View seam — instance-specific clients
# ---------------------------------------------------------------------------


def test_instance_view_k8s_core_api_from_instance_clientset():
    """_InstanceView.k8s_core_api must return the INSTANCE's core_api sentinel.

    RED: fails before _InstanceView is implemented.
    GREEN: resolves via _resolve_k8s and returns clients.core_api.
    """
    from core.extension import _InstanceView

    sentinel_core = object()
    cs = _make_fake_clientset(core=sentinel_core)
    server = _make_fake_server({"test-cluster": (cs, None)})
    reg = _make_fake_registry(server)

    view = _InstanceView(reg, "test-cluster")
    assert view.k8s_core_api is sentinel_core, (
        f"k8s_core_api must be the sentinel core_api from the instance's clientset, "
        f"got: {view.k8s_core_api!r}"
    )


def test_instance_view_k8s_custom_api_from_instance_clientset():
    """_InstanceView.k8s_custom_api must return the INSTANCE's custom_api sentinel."""
    from core.extension import _InstanceView

    sentinel_custom = object()
    cs = _make_fake_clientset(custom=sentinel_custom)
    server = _make_fake_server({"test-cluster": (cs, None)})
    reg = _make_fake_registry(server)

    view = _InstanceView(reg, "test-cluster")
    assert view.k8s_custom_api is sentinel_custom


def test_instance_view_k8s_apps_api_from_instance_clientset():
    """_InstanceView.k8s_apps_api must return the INSTANCE's apps_api sentinel."""
    from core.extension import _InstanceView

    sentinel_apps = object()
    cs = _make_fake_clientset(apps=sentinel_apps)
    server = _make_fake_server({"test-cluster": (cs, None)})
    reg = _make_fake_registry(server)

    view = _InstanceView(reg, "test-cluster")
    assert view.k8s_apps_api is sentinel_apps


def test_instance_view_query_prometheus_injects_instance_clients():
    """_InstanceView.query_prometheus must inject the instance's clients as kwargs.

    The wrapper must pass custom_api and core_api from the INSTANCE's clientset,
    not from the server globals.
    """
    from core.extension import _InstanceView

    sentinel_core = object()
    sentinel_custom = object()
    cs = _make_fake_clientset(core=sentinel_core, custom=sentinel_custom)

    captured_kwargs: dict = {}

    async def fake_prometheus(query, timeout=30, **kwargs):
        captured_kwargs.update(kwargs)
        return {"success": True, "data": []}

    server = _make_fake_server({"test-cluster": (cs, None)})
    server._execute_prometheus_query_internal = fake_prometheus
    reg = _make_fake_registry(server)

    view = _InstanceView(reg, "test-cluster")
    asyncio.run(view.query_prometheus("some_query"))

    assert captured_kwargs.get("core_api") is sentinel_core, (
        f"query_prometheus must inject core_api from the instance clientset. "
        f"Got: {captured_kwargs.get('core_api')!r}"
    )
    assert captured_kwargs.get("custom_api") is sentinel_custom, (
        f"query_prometheus must inject custom_api from the instance clientset. "
        f"Got: {captured_kwargs.get('custom_api')!r}"
    )


# ---------------------------------------------------------------------------
# 3. Late-binding — view reflects current server state at access time
# ---------------------------------------------------------------------------


def test_instance_view_late_binds_resolve_at_access_time():
    """Swapping _resolve_k8s AFTER view creation must be reflected at access time.

    Mutation check: if the view stored a snapshot of clients at construction
    instead of calling _resolve_k8s at each property access, this test fails.
    """
    from core.extension import _InstanceView

    cs_a = _make_fake_clientset()
    cs_b = _make_fake_clientset()
    sentinel_b_core = cs_b.core_api

    server = _make_fake_server({"test-cluster": (cs_a, None)})
    reg = _make_fake_registry(server)

    view = _InstanceView(reg, "test-cluster")
    # First access returns cs_a's core_api (sanity check)
    _ = view.k8s_core_api

    # Swap _resolve_k8s to return cs_b
    server._resolve_k8s = lambda src: (cs_b, None) if src == "test-cluster" else (None, {"error": "unknown"})

    # Access AFTER swap must return cs_b's clients
    result = view.k8s_core_api
    assert result is sentinel_b_core, (
        f"k8s_core_api must resolve via _resolve_k8s at access time (late-binding). "
        f"Expected cs_b.core_api after swapping _resolve_k8s, "
        f"got: {result!r} (id={id(result):#x}). "
        "This failure confirms the mutation check: storing a snapshot at construction would "
        "make this test fail — late-binding is required."
    )


# ---------------------------------------------------------------------------
# 4. InstanceResolutionError — bad source → exception → error dict
# ---------------------------------------------------------------------------


def test_instance_view_bad_source_raises_instance_resolution_error():
    """_InstanceView with unknown source must raise InstanceResolutionError on property access.

    RED: fails before InstanceResolutionError and _InstanceView are implemented.
    """
    from core.extension import _InstanceView, InstanceResolutionError

    error_dict = {
        "error": "no such cluster",
        "requested_source": "no-such",
        "known_kubernetes_instances": [],
    }
    server = _make_fake_server({})  # no known clusters
    server._resolve_k8s = lambda src: (None, error_dict)
    reg = _make_fake_registry(server)

    view = _InstanceView(reg, "no-such")
    with pytest.raises(InstanceResolutionError) as exc_info:
        _ = view.k8s_core_api

    assert exc_info.value.error_dict is error_dict, (
        "InstanceResolutionError.error_dict must be the exact dict from _resolve_k8s"
    )


def test_instance_resolution_error_carries_full_dict():
    """InstanceResolutionError must propagate the full error_dict unchanged."""
    from core.extension import InstanceResolutionError

    err = {"error": "test", "requested_source": "x", "hint": "y"}
    exc = InstanceResolutionError(err)
    assert exc.error_dict is err


def test_konflux_tool_bad_source_returns_error_dict():
    """Konflux tool with extension-active but broken _resolve_k8s returns resolution error.

    Scenario: extension IS marked active (gate passes), but every actual property
    access on the view raises InstanceResolutionError because _resolve_k8s fails.
    Proves: InstanceResolutionError is caught at the tool top level and returned
    as a structured dict, NOT swallowed by the outer except Exception handler.

    Mutation check: if InstanceResolutionError is not specifically caught (falls
    through to the outer handler), the returned dict has 'error': str(exc) which
    is repr(error_dict) — not the clean error_dict value. This test distinguishes
    the two shapes.
    """
    from extensions.konflux.tools import make_ci_cd_performance_baselining_tool
    from core.extension import InstanceResolutionError

    error_dict = {
        "error": "unknown kubernetes instance 'bad-cluster'",
        "tool": "_resolve_k8s",
        "requested_source": "bad-cluster",
        "known_kubernetes_instances": [],
    }
    # Extension IS active (so the gate passes), but _resolve_k8s always fails.
    # This forces InstanceResolutionError to be raised during property access
    # (e.g. ireg.query_prometheus(...)).
    server = _make_fake_server(
        {},
        extension_states={("konflux", "bad-cluster"): "active"},
    )
    server._resolve_k8s = lambda src: (None, error_dict)

    reg = _make_fake_registry(server)
    tool = make_ci_cd_performance_baselining_tool(reg)

    result = asyncio.run(tool(source="bad-cluster"))

    # Must be the structured error dict, not a stringified exception
    assert "error" in result, (
        f"Tool must return error dict on InstanceResolutionError; got: {sorted(result.keys())}"
    )
    assert result.get("error") == error_dict["error"], (
        f"Tool must return the structured error from InstanceResolutionError "
        f"(not str(exc) which would be repr(error_dict)). "
        f"Got error={result.get('error')!r}"
    )


# ---------------------------------------------------------------------------
# 5. Extension-active gate
# ---------------------------------------------------------------------------


def test_extension_active_returns_true_when_state_active():
    """extension_active('konflux') must return True when state is 'active'."""
    from core.extension import _InstanceView

    server = _make_fake_server(
        {},
        extension_states={("konflux", "my-cluster"): "active"},
    )
    reg = _make_fake_registry(server)
    view = _InstanceView(reg, "my-cluster")
    assert view.extension_active("konflux") is True


def test_extension_active_returns_false_when_state_missing():
    """extension_active must return False when extension has no state for this instance."""
    from core.extension import _InstanceView

    server = _make_fake_server({}, extension_states={})
    reg = _make_fake_registry(server)
    view = _InstanceView(reg, "my-cluster")
    assert view.extension_active("konflux") is False


def test_extension_active_returns_false_when_state_not_active():
    """extension_active must return False for any state other than 'active'."""
    from core.extension import _InstanceView

    for state in ("not-detected: absent", "not-detected: timeout", "off", None):
        server = _make_fake_server(
            {},
            extension_states={("konflux", "my-cluster"): state} if state is not None else {},
        )
        reg = _make_fake_registry(server)
        view = _InstanceView(reg, "my-cluster")
        assert view.extension_active("konflux") is False, (
            f"extension_active must return False for state={state!r}"
        )


def test_ci_cd_baselining_extension_not_active_returns_gate_error():
    """ci_cd_performance_baselining_tool with named source + inactive extension → gate error.

    The gate must copy _gate_extension's dict shape.
    """
    from extensions.konflux.tools import make_ci_cd_performance_baselining_tool

    cs = _make_fake_clientset()
    # Extension IS resolved (clients exist) but NOT active
    server = _make_fake_server(
        {"my-cluster": (cs, None)},
        extension_states={},  # no entry → extension_active("konflux") == False
    )
    reg = _make_fake_registry(server)
    tool = make_ci_cd_performance_baselining_tool(reg)

    result = asyncio.run(tool(source="my-cluster"))

    assert "error" in result, f"Expected error dict, got: {result}"
    assert "extension" in result, f"Expected 'extension' key in error dict: {result}"
    assert result.get("extension") == "konflux", (
        f"Expected extension='konflux', got: {result.get('extension')!r}"
    )
    assert result.get("requested_source") == "my-cluster", (
        f"Expected requested_source='my-cluster', got: {result.get('requested_source')!r}"
    )


def test_pipeline_tracer_extension_not_active_returns_gate_error():
    """pipeline_tracer with named source + inactive extension → gate error."""
    from extensions.konflux.tools import make_pipeline_tracer

    cs = _make_fake_clientset()
    server = _make_fake_server(
        {"my-cluster": (cs, None)},
        extension_states={},  # not active
    )
    reg = _make_fake_registry(server)
    tool = make_pipeline_tracer(reg)

    result = asyncio.run(tool(
        trace_identifier="abc123",
        trace_type="commit",
        source="my-cluster",
    ))

    assert "error" in result, f"Expected error dict, got: {result}"
    assert result.get("extension") == "konflux"
    assert result.get("requested_source") == "my-cluster"


def test_pipeline_tracer_detect_tekton_namespaces_receives_source():
    """detect_tekton_namespaces must receive source=source when pipeline_tracer is called
    with a named source.

    Seam test: patches get_multi_cluster_clients so the tool reaches the namespace-
    detection branch, then verifies the tracking stub was called with source='test-cluster'.

    Mutation check: if the caller omitted source= (the B1 bug), the stub would receive
    source='' (the default), and this assertion would catch it.
    """
    from unittest.mock import AsyncMock, patch
    from extensions.konflux.tools import make_pipeline_tracer

    cs = _make_fake_clientset()
    detected_sources: list = []

    async def tracking_detect_tekton_namespaces(source=""):
        detected_sources.append(source)
        return {"active": []}

    server = _make_fake_server(
        {"test-cluster": (cs, None)},
        extension_states={("konflux", "test-cluster"): "active"},
    )
    server.detect_tekton_namespaces = tracking_detect_tekton_namespaces
    reg = _make_fake_registry(server)
    tool = make_pipeline_tracer(reg)

    with patch(
        "extensions.konflux.tools.get_multi_cluster_clients",
        new=AsyncMock(return_value={"test-cluster": cs}),
    ):
        asyncio.run(tool(
            trace_identifier="abc123",
            trace_type="commit",
            source="test-cluster",
        ))

    assert detected_sources, (
        "detect_tekton_namespaces was never called — "
        "check that get_multi_cluster_clients returned a non-empty dict"
    )
    assert detected_sources[0] == "test-cluster", (
        f"detect_tekton_namespaces must receive source='test-cluster', "
        f"got source={detected_sources[0]!r}. "
        "This is the B1 regression guard: omitting source= passes '' instead."
    )


def test_ci_cd_baselining_default_source_skips_extension_gate():
    """ci_cd_performance_baselining_tool with source='' must NOT check extension_active.

    The default path is byte-preserved: no gate fires for source=''.
    Even when _extension_states is empty, the tool proceeds (Prometheus may fail,
    but the gate is not the reason).
    """
    from extensions.konflux.tools import make_ci_cd_performance_baselining_tool

    cs = _make_fake_clientset()
    server = _make_fake_server(
        {},  # no instance registered — irrelevant for default path
        extension_states={},  # empty — gate would block if consulted
    )
    # For the default path (source=""), reg is used directly, not a view
    # query_prometheus will be called; return a minimal failure so the tool
    # exits gracefully rather than returning a gate error.
    async def failing_prometheus(query, timeout=30, **kwargs):
        return {"success": False, "error": "prometheus unavailable"}

    server._execute_prometheus_query_internal = failing_prometheus
    reg = _make_fake_registry(server)

    tool = make_ci_cd_performance_baselining_tool(reg)
    result = asyncio.run(tool())  # source="" by default

    # Should NOT be an extension gate error (the gate error has 'extension' key)
    assert "extension" not in result, (
        f"Default path (source='') must NOT trigger the extension gate. "
        f"Got: {result}"
    )


# ---------------------------------------------------------------------------
# 6. Mutation checks (documented inline)
# ---------------------------------------------------------------------------


def test_mutation_freeze_late_binding_detected():
    """Mutation check: storing clients at construction would fail the late-binding test.

    This test documents the mutation (doesn't apply it, since that would require
    modifying the production code). Instead, it verifies the INVERSE: a view that
    DOES late-bind correctly passes, so if we froze the binding, it would fail.

    The authoritative late-binding test is test_instance_view_late_binds_resolve_at_access_time.
    """
    from core.extension import _InstanceView

    cs_a = _make_fake_clientset()
    cs_b = _make_fake_clientset()

    resolve_calls: list[str] = []

    def tracking_resolve(src):
        resolve_calls.append(src)
        return (cs_a, None)

    server = _make_fake_server({})
    server._resolve_k8s = tracking_resolve
    reg = _make_fake_registry(server)
    view = _InstanceView(reg, "test-cluster")

    # Access the property 3 times
    _ = view.k8s_core_api
    _ = view.k8s_core_api
    _ = view.k8s_core_api

    # If late-binding is correct, _resolve_k8s is called exactly once per access
    assert len(resolve_calls) == 3, (
        "k8s_core_api must call _resolve_k8s exactly once per property access (late-binding). "
        "A frozen-snapshot implementation would call it once at construction and 0 times "
        "on subsequent accesses — giving count < 3, which this exact assertion detects."
    )


def test_mutation_swallow_resolution_error_detected():
    """Mutation check: swallowing InstanceResolutionError returns confusing dict shape.

    The outer except Exception catches InstanceResolutionError if it is not
    specifically handled first. The outer handler wraps str(e) — not a structured
    error dict. test_konflux_tool_bad_source_returns_error_dict already asserts
    the structured dict; this test verifies the shape distinguisher.
    """
    from core.extension import InstanceResolutionError

    # A swallowed error would produce str(InstanceResolutionError(dict)) as the message
    # which is repr(dict), not the structured dict keys.
    err = {"error": "no such cluster", "requested_source": "x", "known_kubernetes_instances": []}
    exc = InstanceResolutionError(err)

    # str(exc) is repr(err) — not the clean 'error' value
    assert str(exc) != err["error"], (
        "InstanceResolutionError.__str__ must differ from err['error'] — this is how "
        "the seam test distinguishes a swallowed error (str(exc)) from a structured "
        "error dict (exc.error_dict['error'])."
    )
