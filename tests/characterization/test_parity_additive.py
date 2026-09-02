"""Operationalizes spec SS7 + the SS4.4 superset-schema rule: every phase-0
tool must still register, with a schema that is a SUPERSET of its phase-0
schema — properties may be ADDED (each with a default: new params must not
change existing call sites); none may be removed, renamed, or re-typed.

parity_baseline_phase0.json is IMMUTABLE.  parity_reference.json tracks the
current surface and is regenerated (dump_tool_registry.py) in dedicated,
audited commits."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.characterization

BASELINE = Path(__file__).parent / "parity_baseline_phase0.json"


def _tools(server):
    return {
        name: {"description": t.description, "input_schema": t.parameters}
        for name, t in server.mcp._tool_manager._tools.items()
    }


def test_baseline_exists_and_pins_39():
    ref = json.loads(BASELINE.read_text())
    assert len(ref["tools"]) == 39


# Audited, deliberate default-value changes (tool_name, param_name).
# Every entry must cite its reason:
# - get_pipelinerun_logs.max_token_budget: 120000 -> 18000 (bug 2,
#   2026-08-21 — old default overflowed the 25k-token MCP client cap on
#   every real pipeline).
_ALLOWED_DEFAULT_CHANGES: frozenset[tuple[str, str]] = frozenset({
    ("get_pipelinerun_logs", "max_token_budget"),
})


def test_parity_additive_only(server):
    baseline = json.loads(BASELINE.read_text())["tools"]
    current = _tools(server)
    missing = set(baseline) - set(current)
    assert not missing, f"phase-0 tools dropped: {sorted(missing)}"
    for name, entry in baseline.items():
        base_props = entry["input_schema"].get("properties", {})
        cur_schema = current[name]["input_schema"]
        cur_props = cur_schema.get("properties", {})
        removed = set(base_props) - set(cur_props)
        assert not removed, f"{name}: params removed/renamed: {sorted(removed)}"
        for pname, pschema in base_props.items():
            # Defaults ARE semantics — the guard checks them exactly. A
            # deliberate default change must be listed here explicitly so
            # every such change is an audited decision (review MINOR-7:
            # silently ignoring default values would blind the guard to
            # drift like source="" -> "prod" across all phase-0 tools).
            if (name, pname) in _ALLOWED_DEFAULT_CHANGES:
                base_cmp = {k: v for k, v in pschema.items() if k != "default"}
                cur_cmp = {k: v for k, v in cur_props[pname].items() if k != "default"}
                assert cur_cmp == base_cmp, (
                    f"{name}.{pname}: schema re-typed (was {pschema}, "
                    f"now {cur_props[pname]})")
                assert "default" in cur_props[pname], (
                    f"{name}.{pname}: default removed (param became required)")
            else:
                assert cur_props[pname] == pschema, (
                    f"{name}.{pname}: schema re-typed (was {pschema}, "
                    f"now {cur_props[pname]})")
        base_req = set(entry["input_schema"].get("required", []))
        cur_req = set(cur_schema.get("required", []))
        new_required = cur_req - base_req
        added_props = set(cur_props) - set(base_props)
        assert not (new_required & added_props), (
            f"{name}: newly added params must have defaults (not required): "
            f"{sorted(new_required & added_props)}")
        assert cur_req <= base_req | added_props, (
            f"{name}: previously-optional param became required: "
            f"{sorted(cur_req - base_req - added_props)}")
