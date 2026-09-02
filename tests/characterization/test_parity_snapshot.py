import json
import re
import subprocess
from pathlib import Path

REFERENCE = Path(__file__).parent / "parity_reference.json"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _current_registry(server) -> dict:
    tools = {}
    for name, tool in server.mcp._tool_manager._tools.items():
        tools[name] = {
            "description": tool.description,
            "input_schema": tool.parameters,
            "output_schema": tool.output_schema if hasattr(tool, "output_schema") else None,
        }
    return tools


def test_reference_exists():
    assert REFERENCE.exists(), (
        "parity_reference.json missing - generate with: "
        "python scripts/dump_tool_registry.py"
    )


def test_pinned_commit_is_valid_ancestor():
    """pinned_commit must be a full 40-hex SHA and an ancestor of HEAD."""
    ref = json.loads(REFERENCE.read_text())
    pinned = ref.get("pinned_commit", "")
    assert re.fullmatch(r"[0-9a-f]{40}", pinned), (
        f"pinned_commit is not a 40-hex SHA: {pinned!r}"
    )
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", pinned, "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"pinned_commit {pinned} is not an ancestor of HEAD "
        "(regenerate with: python scripts/dump_tool_registry.py)"
    )


def test_registry_matches_reference(server):
    ref = json.loads(REFERENCE.read_text())
    current = _current_registry(server)
    assert set(current) == set(ref["tools"]), (
        f"tool set drifted; added={set(current)-set(ref['tools'])} "
        f"removed={set(ref['tools'])-set(current)}"
    )
    for name, entry in ref["tools"].items():
        assert current[name]["description"] == entry["description"], (
            f"description drifted for {name}"
        )
        assert current[name]["input_schema"] == entry["input_schema"], (
            f"schema drifted for {name}"
        )
        assert current[name]["output_schema"] == entry.get("output_schema"), (
            f"output_schema drifted for {name}"
        )
