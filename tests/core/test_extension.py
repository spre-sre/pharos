"""Unit tests for core/extension.py — Extension protocol, DetectContext,
ToolRegistry facade, detect_and_register, and activate_extensions.

All server interaction is faked via types.SimpleNamespace and MagicMock.
NO import of server-mcp or any asyncio server occurs here.

Sync tests: those calling activate_extensions (which uses asyncio.run internally).
Async tests: those awaiting detect_and_register directly.
"""
import sys
import asyncio
import types
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.extension import (
    INTREE_EXTENSIONS,
    DetectContext,
    ToolRegistry,
    activate_extensions,
    detect_and_register,
)
from core.config_types import ResolvedConfig


# ── Shared test helpers ───────────────────────────────────────────────────────

def _fake_server(**attrs):
    """Return a SimpleNamespace stub. Provides a passthrough log_tool_execution
    unless the caller overrides it."""
    ns = types.SimpleNamespace(**attrs)
    if not hasattr(ns, "log_tool_execution"):
        ns.log_tool_execution = lambda fn: fn
    return ns


def _fake_mcp():
    return MagicMock()


async def _noop_discover(group: str) -> frozenset:
    return frozenset()


def _make_ctx(ext_modes: dict, instance: str = "kubernetes"):
    config = ResolvedConfig(profile="test", extensions=ext_modes)
    ctx = DetectContext(
        config=config,
        adapters=MagicMock(),
        instance=instance,
        discover_api_groups=_noop_discover,
    )
    return config, ctx


def _ext(name: str, *, detect_result=False, detect_delay=0.0, detect_raises=None):
    """Build a minimal fake Extension via SimpleNamespace + closures."""

    async def detect(ctx):
        if detect_raises is not None:
            raise detect_raises
        if detect_delay:
            await asyncio.sleep(detect_delay)
        return detect_result

    registered_calls = []

    def register(reg):
        registered_calls.append(True)

    ext = types.SimpleNamespace(
        name=name,
        detect=detect,
        register=register,
        _registered_calls=registered_calls,
    )
    return ext


def _make_facade(server=None, mcp=None, ext_modes=None, instance="kubernetes"):
    server = server or _fake_server()
    mcp = mcp or _fake_mcp()
    config = ResolvedConfig(profile="test", extensions=ext_modes or {})
    return ToolRegistry(server, mcp, config, MagicMock(), {}), server, mcp, config


# ── (a) 'on' mode: register called, state is 'active' ────────────────────────

def test_on_mode_registers_and_returns_active():
    facade, server, mcp, config = _make_facade(ext_modes={"tekton": "on"})
    ext = _ext("tekton")
    _, ctx = _make_ctx({"tekton": "on"})

    result = activate_extensions(config, [ext], facade, ctx)

    assert ext._registered_calls, "register() must have been called for mode 'on'"
    assert result == {("tekton", "kubernetes"): "active"}


# ── (b) 'off' mode: register never called, state is 'off' ────────────────────

def test_off_mode_skips_register_and_returns_off():
    facade, server, mcp, config = _make_facade(ext_modes={"tekton": "off"})
    ext = _ext("tekton")
    _, ctx = _make_ctx({"tekton": "off"})

    result = activate_extensions(config, [ext], facade, ctx)

    assert not ext._registered_calls, "register() must NOT be called for mode 'off'"
    assert result == {("tekton", "kubernetes"): "off"}


# ── (c) 'auto' + detect True → registers; tested via detect_and_register ─────

@pytest.mark.asyncio
async def test_detect_and_register_true_activates():
    facade, server, mcp, _ = _make_facade()
    ext = _ext("tekton", detect_result=True)
    _, ctx = _make_ctx({"tekton": "auto"})

    state, names = await detect_and_register(ext, facade, ctx)

    assert state == "active"
    assert ext._registered_calls, "register() must have been called"


# ── (d) 'auto' + detect False → 'not-detected: absent' ───────────────────────

@pytest.mark.asyncio
async def test_detect_and_register_false_not_detected_absent():
    facade, server, mcp, _ = _make_facade()
    ext = _ext("tekton", detect_result=False)
    _, ctx = _make_ctx({"tekton": "auto"})

    state, names = await detect_and_register(ext, facade, ctx)

    assert state == "not-detected: absent"
    assert names == []
    assert not ext._registered_calls, "register() must NOT be called when detect returns False"


# ── (e) 'auto' + detect sleeping 3s; timeout_s=0.05 → 'not-detected: timeout' ─

def test_auto_detect_timeout_via_activate_extensions():
    """SYNC test: activate_extensions uses asyncio.run internally, so it must
    be a plain sync def — awaiting inside a running loop would RuntimeError."""
    facade, server, mcp, config = _make_facade(ext_modes={"tekton": "auto"})
    ext = _ext("tekton", detect_delay=3.0)
    _, ctx = _make_ctx({"tekton": "auto"})

    result = activate_extensions(config, [ext], facade, ctx, timeout_s=0.05)

    assert result == {("tekton", "kubernetes"): "not-detected: timeout"}
    assert not ext._registered_calls, "register() must NOT be called on timeout"


# ── (f) 'auto' + detect raises → 'not-detected: error: <TypeName>' ───────────

@pytest.mark.asyncio
async def test_detect_and_register_exception_not_detected_error():
    facade, server, mcp, _ = _make_facade()
    ext = _ext("tekton", detect_raises=RuntimeError("boom"))
    _, ctx = _make_ctx({"tekton": "auto"})

    state, names = await detect_and_register(ext, facade, ctx)

    assert state == "not-detected: error: RuntimeError"
    assert names == []
    assert not ext._registered_calls


# ── (g) activation order is alphabetically sorted ────────────────────────────

def test_activation_order_is_name_sorted():
    """Passes extensions in reverse order; verifies register() is called alpha-first."""
    call_order = []

    def _tracking_ext(name):
        ext = types.SimpleNamespace(name=name)

        async def detect(ctx):
            return True  # always active so register is called

        def register(reg):
            call_order.append(name)

        ext.detect = detect
        ext.register = register
        return ext

    ext_z = _tracking_ext("zeta")
    ext_a = _tracking_ext("alpha")
    config = ResolvedConfig(profile="test", extensions={"zeta": "on", "alpha": "on"})
    facade, server, mcp, _ = _make_facade(ext_modes={"zeta": "on", "alpha": "on"})
    _, ctx = _make_ctx({"zeta": "on", "alpha": "on"})

    # Deliberately pass in reverse order to prove sorting happens inside
    activate_extensions(config, [ext_z, ext_a], facade, ctx)

    assert call_order == ["alpha", "zeta"], (
        f"Expected alpha before zeta, got {call_order}"
    )


# ── (h) facade properties late-bind to server attributes ─────────────────────

def test_facade_properties_late_bind_after_construction():
    """Swap server attrs AFTER facade is built; property must return the NEW value."""
    sentinel_initial = object()
    sentinel_new = object()

    server = _fake_server(
        k8s_core_api=sentinel_initial,
        k8s_apps_api=sentinel_initial,
        k8s_custom_api=sentinel_initial,
        _execute_prometheus_query_internal=sentinel_initial,
    )
    facade, _, _, _ = _make_facade(server=server)

    # Confirm initial values are accessible
    assert facade.k8s_core_api is sentinel_initial
    assert facade.k8s_apps_api is sentinel_initial
    assert facade.k8s_custom_api is sentinel_initial
    # query_prometheus now returns a client-injecting wrapper (not the raw fn)
    assert callable(facade.query_prometheus)

    # Swap ALL attrs on the server after facade construction
    server.k8s_core_api = sentinel_new
    server.k8s_apps_api = sentinel_new
    server.k8s_custom_api = sentinel_new
    server._execute_prometheus_query_internal = sentinel_new

    # Properties must reflect the new values (late binding)
    assert facade.k8s_core_api is sentinel_new
    assert facade.k8s_apps_api is sentinel_new
    assert facade.k8s_custom_api is sentinel_new
    # query_prometheus late-binds both the underlying fn and the K8s clients;
    # each property access returns a fresh wrapper; verified by the dedicated test below.
    assert callable(facade.query_prometheus)


@pytest.mark.asyncio
async def test_query_prometheus_property_late_binds_clients():
    """query_prometheus wrapper must read k8s_custom_api / k8s_core_api from the
    server's current attrs at CALL time, not at property-access time.
    This verifies that connect_cluster updates are visible to subsequent calls."""
    sentinel_custom_a = object()
    sentinel_core_a = object()
    sentinel_custom_b = object()
    sentinel_core_b = object()

    calls = []

    async def _fake_fn(query: str, timeout: int = 30, *, custom_api=None, core_api=None):
        calls.append({"custom_api": custom_api, "core_api": core_api})
        return {"success": False, "data": [], "error": "test"}

    server = _fake_server(
        k8s_custom_api=sentinel_custom_a,
        k8s_core_api=sentinel_core_a,
        _execute_prometheus_query_internal=_fake_fn,
    )
    facade, _, _, _ = _make_facade(server=server)

    # Call once with initial clients
    await facade.query_prometheus("up")
    assert calls[-1]["custom_api"] is sentinel_custom_a
    assert calls[-1]["core_api"] is sentinel_core_a

    # Simulate connect_cluster updating the K8s clients
    server.k8s_custom_api = sentinel_custom_b
    server.k8s_core_api = sentinel_core_b

    # Second call must see the NEW clients (late-binding)
    await facade.query_prometheus("up")
    assert calls[-1]["custom_api"] is sentinel_custom_b
    assert calls[-1]["core_api"] is sentinel_core_b


# ── (i) tool() applies log_tool_execution, calls mcp.add_tool, rebinds attr ──

def test_tool_decorator_wraps_logs_registers_and_rebinds():
    """Verifies the full decoration pipeline:
    fn -> log_tool_execution(fn) -> mcp.add_tool(logged) -> server.fn_name = logged
    -> registered dict updated -> return logged.
    """
    logged_registry = []

    def fake_log(fn):
        # Wrap but keep __name__ so rebind works
        def wrapped(*args, **kwargs):
            return fn(*args, **kwargs)
        wrapped.__name__ = fn.__name__
        logged_registry.append(wrapped)
        return wrapped

    server = _fake_server(log_tool_execution=fake_log)
    mcp = _fake_mcp()
    config = ResolvedConfig(profile="test")
    facade = ToolRegistry(server, mcp, config, MagicMock(), {})

    @facade.tool()
    def my_tool():
        return "result"

    # 1. log_tool_execution was applied (one logged fn produced)
    assert len(logged_registry) == 1
    logged_fn = logged_registry[0]

    # 2. mcp.add_tool called with the logged fn (NOT the original)
    mcp.add_tool.assert_called_once_with(logged_fn)

    # 3. server attr rebound to the logged fn
    assert server.my_tool is logged_fn

    # 4. registered dict records the logged fn
    assert facade.registered["my_tool"] is logged_fn

    # 5. decorator return value is the logged fn
    assert my_tool is logged_fn


# ── (j) facade exposes NO public or _-prefixed mcp attribute ─────────────────

def test_facade_hides_raw_mcp_object():
    """ToolRegistry must NOT expose the FastMCP object as 'mcp' or '_mcp'.
    The attribute is stored with name-mangling (__mcp -> _ToolRegistry__mcp)
    so neither plain name is reachable from extension code."""
    facade, _, _, _ = _make_facade()

    assert not hasattr(facade, "mcp"), (
        "ToolRegistry must not expose a public 'mcp' attribute"
    )
    assert not hasattr(facade, "_mcp"), (
        "_mcp must not exist as a plain instance attr; "
        "store as self.__mcp (name-mangled to _ToolRegistry__mcp)"
    )
