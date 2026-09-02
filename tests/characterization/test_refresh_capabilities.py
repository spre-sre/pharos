"""Tests for the refresh_capabilities meta-tool (Task 7, phase 2d).

Three test groups:
  (a) no-session, konflux profile (all 'on') → deterministic no-op.
  R5  mutation check: prove the ValueError that the try/except guards against.
  (b) fake-session unit: tekton:auto + canned discovery → changed==7 names,
      send_tool_list_changed called once, state flips to active.
  (c) second call → idempotent (changed: []).

Tests (b) and (c) are structured as a single test with two sequential awaits
so that the _extension_states mutation from the first call is visible to the
second call within the same monkeypatched scope.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure src/ is importable (mirrors test_extension.py setup).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.config_types import ResolvedConfig
from core.extension import DetectContext, ToolRegistry, detect_and_register
from extensions.tekton import EXTENSION as tekton_ext
from extensions.tekton import TOOLS as TEKTON_TOOLS

pytestmark = pytest.mark.characterization


# ── (a) no-session, all-'on' profile → deterministic no-op ───────────────────

@pytest.mark.asyncio
async def test_no_session_no_op(server):
    """Konflux profile has all 3 extensions as 'on' → refresh loop skips all
    (mode != 'auto') → changed == [] → the try/except block is never entered
    → no exception raised, result is the deterministic active-×3 state.
    """
    result = await server.refresh_capabilities()

    assert result["changed"] == []
    extensions_by_name = {e["name"]: e for e in result["extensions"]}
    assert extensions_by_name["konflux"]["state"] == "active"
    assert extensions_by_name["openshift"]["state"] == "active"
    assert extensions_by_name["tekton"]["state"] == "active"
    assert extensions_by_name["konflux"]["configured"] == "on"
    assert extensions_by_name["openshift"]["configured"] == "on"
    assert extensions_by_name["tekton"]["configured"] == "on"


# ── R5 mutation check ─────────────────────────────────────────────────────────

def test_r5_guard_is_necessary(server):
    """Prove the ValueError that the try/except catches.

    mcp.get_context() (SDK 1.28.1) does NOT raise when called outside a
    request — it returns a Context(request_context=None).  The exception
    surfaces from ctx.session, which raises
    ValueError("Context is not available outside of a request").

    The guard `except (LookupError, ValueError, AttributeError): pass` in
    refresh_capabilities catches this.  Without it (mutation: remove the
    try/except), any code path that reaches the notification block with no
    live MCP session would re-raise.  This test proves the raw error exists.
    """
    ctx = server.mcp.get_context()
    with pytest.raises(ValueError, match="not available outside"):
        _ = ctx.session


# ── (b) fake-session + (c) idempotency ───────────────────────────────────────

@pytest.mark.asyncio
async def test_fake_session_and_idempotency(server, monkeypatch):
    """(b) tekton:auto + canned discovery → changed == 7 names, notification
    called once, state flips to active.
    (c) second call with state already 'active' → changed == [], no second
    notification.

    Build against a fresh ToolRegistry backed by the real server module (so
    register_server_tool can resolve the actual tekton function bodies), a
    synthetic ResolvedConfig with only tekton:auto, a DetectContext whose
    discover_api_groups returns frozenset({"tekton.dev"}), and a mock MCP
    session. The session server's _extension_states and _extension_facade
    are replaced for the duration of this test via monkeypatch (auto-reverted).

    The globals() rebind inside refresh_capabilities mutates server module
    attrs for the 7 tekton tool names; these are pre-captured by monkeypatch
    so they are also restored after the test.
    """
    # ── Pre-capture the 7 tekton server attrs so monkeypatch reverts them ──
    for name in TEKTON_TOOLS:
        if hasattr(server, name):
            monkeypatch.setattr(server, name, getattr(server, name))

    # ── Build synthetic config: only tekton in auto mode ───────────────────
    fresh_cfg = ResolvedConfig(
        profile="test",
        extensions={"tekton": "auto"},
    )

    # ── Fresh ToolRegistry backed by the real server module ─────────────────
    # Using a MagicMock for the internal mcp so add_tool() is a no-op.
    fresh_facade_mcp = MagicMock()
    fresh_facade = ToolRegistry(
        server_module=server,
        mcp=fresh_facade_mcp,
        config=fresh_cfg,
        adapters=server._source_registry,
        packs={},
    )

    # ── Canned DetectContext: discovery returns tekton.dev ──────────────────
    async def canned_discover(instance: str) -> frozenset:
        return frozenset({"tekton.dev"})

    fresh_ctx = DetectContext(
        config=fresh_cfg,
        adapters=server._source_registry,
        instance="kubernetes",
        discover_api_groups=canned_discover,
    )

    # ── Mock MCP session ────────────────────────────────────────────────────
    mock_session = MagicMock()
    mock_session.send_tool_list_changed = AsyncMock()
    mock_request_ctx = MagicMock()
    mock_request_ctx.session = mock_session
    mock_mcp_ctx = MagicMock()
    mock_mcp_ctx.session = mock_session

    # ── Patch server module globals used by refresh_capabilities ────────────
    # _lumino_config — controls the extensions loop + return value
    monkeypatch.setattr(server, "_lumino_config", fresh_cfg)
    # _extension_states — starts empty so tekton isn't yet active
    fresh_states: dict = {}
    monkeypatch.setattr(server, "_extension_states", fresh_states)
    # _extension_facade — fresh registry (no tekton tools registered yet)
    monkeypatch.setattr(server, "_extension_facade", fresh_facade)
    # _detect_ctx — returns our canned context
    monkeypatch.setattr(server, "_detect_ctx", lambda instance="kubernetes": fresh_ctx)
    # mcp.get_context() — returns mock context with mock session
    monkeypatch.setattr(server.mcp, "get_context", lambda: mock_mcp_ctx)

    # ── (b) First call: tekton detected and registered ──────────────────────
    result_b = await server.refresh_capabilities()

    assert result_b["changed"] == sorted(TEKTON_TOOLS), (
        f"expected 7 tekton tool names, got: {result_b['changed']}"
    )
    assert len(result_b["extensions"]) == 1
    tekton_entry = result_b["extensions"][0]
    assert tekton_entry["name"] == "tekton"
    assert tekton_entry["configured"] == "auto"
    assert tekton_entry["state"] == "active", (
        "state must flip to active after successful detection"
    )
    mock_session.send_tool_list_changed.assert_called_once()

    # ── (c) Second call: tekton already active → no-op ─────────────────────
    result_c = await server.refresh_capabilities()

    assert result_c["changed"] == [], (
        "second call must be idempotent: tekton is already active"
    )
    # send_tool_list_changed must NOT have been called a second time
    mock_session.send_tool_list_changed.assert_called_once()


# ── R5 guard-removal mutant killer ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_r5_guard_kills_mutant(server, monkeypatch):
    """Kill the guard-removal mutant: changed != [] AND real no-session context.

    Combines BOTH conditions that the mutant-survival analysis identified:
      (1) changed != [] — tekton:auto + canned discovery ensures tools are
          newly registered so the notification block is actually entered.
      (2) Real no-session — mcp.get_context() is NOT mocked; the real
          FastMCP instance returns Context(request_context=None), and
          ctx.session then raises ValueError("Context is not available
          outside of a request").

    The guard `except (LookupError, ValueError, AttributeError): pass`
    catches the ValueError and the call must complete without exception,
    returning the correct changed list and active state.

    Mutation check procedure (manual, documented in task-7-report.md):
      1. Remove the try/except block → this test fails with ValueError.
      2. Restore the guard → this test passes.
    """
    # ── Pre-capture the 7 tekton server attrs so monkeypatch reverts them ──
    for name in TEKTON_TOOLS:
        if hasattr(server, name):
            monkeypatch.setattr(server, name, getattr(server, name))

    # ── Build synthetic config: only tekton in auto mode ───────────────────
    fresh_cfg = ResolvedConfig(
        profile="test",
        extensions={"tekton": "auto"},
    )

    # ── Fresh ToolRegistry backed by the real server module ─────────────────
    fresh_facade_mcp = MagicMock()
    fresh_facade = ToolRegistry(
        server_module=server,
        mcp=fresh_facade_mcp,
        config=fresh_cfg,
        adapters=server._source_registry,
        packs={},
    )

    # ── Canned DetectContext: discovery returns tekton.dev ──────────────────
    async def canned_discover(instance: str) -> frozenset:
        return frozenset({"tekton.dev"})

    fresh_ctx = DetectContext(
        config=fresh_cfg,
        adapters=server._source_registry,
        instance="kubernetes",
        discover_api_groups=canned_discover,
    )

    # ── Patch server module globals (but NOT mcp.get_context) ───────────────
    monkeypatch.setattr(server, "_lumino_config", fresh_cfg)
    fresh_states: dict = {}
    monkeypatch.setattr(server, "_extension_states", fresh_states)
    monkeypatch.setattr(server, "_extension_facade", fresh_facade)
    monkeypatch.setattr(server, "_detect_ctx", lambda instance="kubernetes": fresh_ctx)
    # Deliberately do NOT patch mcp.get_context — the real FastMCP returns
    # Context(request_context=None); ctx.session raises ValueError.

    # ── Call must complete without exception (guard swallows the ValueError) ─
    result = await server.refresh_capabilities()

    # ── Verify correct outcomes despite no active session ───────────────────
    assert result["changed"] == sorted(TEKTON_TOOLS), (
        f"expected 7 tekton tool names, got: {result['changed']}"
    )
    assert len(result["extensions"]) == 1
    tekton_entry = result["extensions"][0]
    assert tekton_entry["name"] == "tekton"
    assert tekton_entry["configured"] == "auto"
    assert tekton_entry["state"] == "active", (
        "state must flip to active even when session notification is swallowed"
    )
