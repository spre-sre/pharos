"""Dump the MCP tool registry to tests/characterization/parity_reference.json.

Run from repo root inside .venv:  python scripts/dump_tool_registry.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "tests" / "characterization" / "parity_reference.json"

sys.path.insert(0, str(REPO_ROOT))
from tests.characterization.conftest import FAKE_KUBECONFIG  # noqa: E402

with tempfile.NamedTemporaryFile("w", suffix=".kubeconfig", delete=False) as f:
    f.write(FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = f.name
os.environ["KUBEARCHIVE_ENABLED"] = "false"
os.environ.pop("LUMINO_CONFIG", None)
os.environ.pop("LUMINO_PROFILE", None)

sys.path.insert(0, str(REPO_ROOT / "src"))
spec = importlib.util.spec_from_file_location(
    "server_mcp", REPO_ROOT / "src" / "server-mcp.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules["server_mcp"] = mod
spec.loader.exec_module(mod)

# NOTE: pinned_commit structurally lags its own commit by one — it stamps the
# pre-commit HEAD (the hash as-of when the script runs, before the commit that
# writes the updated parity_reference.json exists).  This is intentional and
# correct: the ancestry test (test_pinned_commit_is_valid_ancestor) asserts
# git merge-base --is-ancestor <pinned> HEAD, which holds even with the lag.
# The script was broken from 9be1eba through 98fd7bd (sys.path pointed at
# tests/characterization/ causing a relative-import error in conftest.py);
# fixed in 98fd7bd by switching to the package-form import from REPO_ROOT.
commit = subprocess.run(
    ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
    cwd=REPO_ROOT, check=True,
).stdout.strip()

tools = {
    name: {
        "description": t.description,
        "input_schema": t.parameters,
        "output_schema": t.output_schema if hasattr(t, "output_schema") else None,
    }
    for name, t in sorted(mod.mcp._tool_manager._tools.items())
}
OUT.write_text(json.dumps(
    {"pinned_commit": commit, "tools": tools}, indent=2, sort_keys=True,
) + "\n")
print(f"wrote {len(tools)} tools -> {OUT}")
