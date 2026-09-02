"""M2: for each alias pair, mcp.call_tool(old) == mcp.call_tool(canonical)."""
import json

import pytest

from .cases import CASES  # package-relative, same as test_golden_tools.py:5 (round-1 F4)
from .conftest import apply_determinism  # extracted shared reset (F3 contract; round-2 V1 — plain function, NOT a fixture, so it must be imported)
from .golden_utils import normalize  # reused to mask volatile timestamps/IDs (round-2 fallback)

PAIRS = {  # keep in sync with test_canonical_aliases.PAIRS (asserted below)
    "analyze_pod_logs_hybrid": "analyze_logs_hybrid",
    "live_system_topology_mapper": "topology_mapper",
    "prometheus_query": "query_metrics",
    "smart_get_namespace_events": "get_events_smart",
    "smart_summarize_pod_logs": "smart_summarize_logs",
    "stream_analyze_pod_logs": "stream_analyze_logs",
}


def _first_case(name):
    for c in CASES:
        if c.name == name:
            return c
    raise AssertionError(f"no golden ToolCase for {name}")


def _serialize(obj):
    """Convert mcp.call_tool results into normalize()-friendly plain Python.

    mcp.call_tool returns Sequence[ContentBlock] | dict (or a 2-element list
    wrapping both for structured tools).  ContentBlock objects (Pydantic models)
    are not directly JSON-serialisable.  Strategy, in order:
      1. Pydantic model → model_dump() dict.  If the ``text`` field is a valid
         JSON string (tool output encoded as text), parse it so that
         _is_volatile_key() in normalize() can mask dict keys like
         ``execution_time`` and ``processing_time_seconds`` — those are missed
         when the payload stays an opaque string.
      2. List → recurse.
      3. Everything else → pass through (normalize() handles str/float/etc.).
    """
    if hasattr(obj, "model_dump"):
        d = obj.model_dump()
        if d.get("type") == "text" and isinstance(d.get("text"), str):
            try:
                d["text"] = json.loads(d["text"])
            except (json.JSONDecodeError, ValueError):
                pass
        return d
    if isinstance(obj, (list, tuple)):
        return [_serialize(item) for item in obj]
    return obj


def _norm(result):
    """Normalize a mcp.call_tool result for deterministic comparison.

    Serialises ContentBlock objects (parsing their embedded JSON text so that
    _is_volatile_key masking works on embedded dict keys like execution_time),
    then applies the golden harness normalize() — the same normalizer used by
    assert_matches_golden — so volatile timestamps, IDs, and timing floats
    are masked identically on both sides before the deep-equal check.
    """
    return normalize(json.loads(json.dumps(_serialize(result), default=str)))


def test_pairs_in_sync(server):
    assert PAIRS == server._CANONICAL_ALIASES


@pytest.mark.parametrize("old,canonical", sorted(PAIRS.items()))
@pytest.mark.asyncio
async def test_m2_through_mcp_identity(server, monkeypatch, tmp_path, old, canonical):
    case = _first_case(old)
    # COMPLETE reset (F3 contract): shared apply_determinism + case patches,
    # fresh before EACH call — see the determinism contract in the task brief.
    apply_determinism(server, monkeypatch, tmp_path)
    for attr, repl in case.patches.items():
        monkeypatch.setattr(server, attr, repl)
    r_old = await server.mcp.call_tool(old, case.kwargs)
    apply_determinism(server, monkeypatch, tmp_path)     # full reset again
    for attr, repl in case.patches.items():
        monkeypatch.setattr(server, attr, repl)
    r_new = await server.mcp.call_tool(canonical, case.kwargs)
    assert _norm(r_old) == _norm(r_new)                 # deep-equal through the REAL dispatch layer
