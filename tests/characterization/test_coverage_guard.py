import pytest

from .cases import CASES
from .conftest import registered_tool_names

pytestmark = pytest.mark.characterization

# Deleted by spec rev 4 (SS2): the logan pair is intentionally uncharacterized.
# They cannot register in this workspace (logan is never installed), but list
# them explicitly so a stray logan install can't silently widen the surface.
DROPPED_TOOLS = {"templatize_pod_logs", "deep_analyze_pod_logs"}

# Phase 2c: canonical aliases register against the SAME function body as their
# old-name counterpart.  The golden harness dispatches via getattr(server, name)
# (module attribute) — canonical names are NOT module attrs, so golden cases
# would be structurally impossible.  Behavior identity is proved by
# test_m1_shared_body (fn is identity) and Task 2 through-mcp tests.
#
# Derived from server._CANONICAL_ALIASES (single source of truth) rather than
# a hardcoded literal to prevent stale exclusions if the map grows.


def test_every_registered_tool_has_a_characterization_case(server):
    registered = registered_tool_names(server)
    cased = {c.name for c in CASES}
    canonical_aliases = set(server._CANONICAL_ALIASES.values())
    uncovered = registered - cased - DROPPED_TOOLS - canonical_aliases
    assert not uncovered, f"tools without characterization: {sorted(uncovered)}"


def test_dropped_tools_not_registered(server):
    present = registered_tool_names(server) & DROPPED_TOOLS
    assert not present, (
        f"logan extra is installed in this workspace: {sorted(present)} - "
        f"remove it; spec rev 4 drops these tools"
    )


def test_no_stale_cases(server):
    stale = {c.name for c in CASES} - registered_tool_names(server)
    assert not stale, f"cases for unregistered tools: {sorted(stale)}"
