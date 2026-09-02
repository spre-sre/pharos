"""Phase 6 Task 5: profile-documentation drift-defence test (M5).

test_profile_docs_match_registry loads server-mcp.py once per built-in
profile and asserts that:
  - the documented tool counts (DOCUMENTED_COUNTS) equal the live
    mcp._tool_manager._tools key counts
  - the documented 12-name extension delta (EXTENSION_DELTA) equals the
    live (konflux − kubernetes) key set

Env handling is the INVERSE of characterization/conftest.py:97-98, which
POPs LUMINO_PROFILE — here we SET it.  Other harness hygiene mirrors
test_canonical_aliases.py:

  - pop LUMINO_CONFIG to prevent a stale file overriding the profile
  - pin KUBE_CONFIG_DEFAULT_LOCATION to a tmp fake kubeconfig (F9
    harness-bleed guard: prevents reading the developer's real kubeconfig)
  - register each load under a distinct sys.modules key
    `server_mcp_profile_<name>` (avoids clobbering session `server_mcp`
    or each other across the three loads)
  - restore all mutated env keys in the finalizer

M5 mutation mandates:
  - change DOCUMENTED_COUNTS["konflux"] → 47 → test_profile_counts_match_docs fails
  - drop any name from EXTENSION_DELTA → test_extension_delta_matches_docs fails
  - revert each to restore green
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
README = REPO_ROOT / "README.md"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.yaml"

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

# ── Documented facts ──────────────────────────────────────────────────────────
# These constants are the machine-checkable source of truth for the README
# "Profiles" section.  Changing either triggers a test failure (M5).

DOCUMENTED_COUNTS: dict[str, int] = {
    "konflux": 49,
    "kubernetes": 37,
    "standalone": 37,
}

# The 12 extension tools registered under `konflux` but absent under
# `kubernetes` and `standalone`.  kubernetes ≡ standalone in tool set
# (identical; the profile difference between them is source declaration).
EXTENSION_DELTA: frozenset[str] = frozenset([
    "analyze_failed_pipeline",
    "ci_cd_performance_baselining_tool",
    "find_pipeline",
    "get_etcd_logs",
    "get_machine_config_pool_status",
    "get_openshift_cluster_operator_status",
    "get_pipelinerun_logs",
    "get_tekton_pipeline_runs_status",
    "list_pipelineruns",
    "list_recent_pipeline_runs",
    "list_taskruns",
    "pipeline_tracer",
])

# ── Fixture ───────────────────────────────────────────────────────────────────

_ENV_KEYS = ("KUBECONFIG", "KUBEARCHIVE_ENABLED", "LUMINO_CONFIG", "LUMINO_PROFILE")


def _load_under_profile(profile_name: str, kubeconfig: Path) -> set[str]:
    """Load server-mcp.py under *profile_name*; return the registered tool names.

    Uses a distinct sys.modules key `server_mcp_profile_<name>` to avoid
    colliding with the session-scoped `server_mcp` fixture in
    characterization/conftest.py or with other calls to this helper.

    Env handling is the INVERSE of conftest.py:97-98: we SET LUMINO_PROFILE
    instead of popping it.  All four env keys are saved and restored.
    KUBE_CONFIG_DEFAULT_LOCATION is pinned to the fake file so no
    kubernetes.config call reads the developer's real ~/.kube/config.
    """
    _save = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ["LUMINO_PROFILE"] = profile_name  # SET — inverse of conftest pop

    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _kc
        _orig_kube_loc = _kc.KUBE_CONFIG_DEFAULT_LOCATION
        _kc.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    module_key = f"server_mcp_profile_{profile_name}"
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(module_key, SRC / "server-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    tool_names: set[str] = set(mod.mcp._tool_manager._tools.keys())

    # Restore env keys exactly.
    for k, v in _save.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # Restore KUBE_CONFIG_DEFAULT_LOCATION.
    if _orig_kube_loc is not None:
        try:
            from kubernetes.config import kube_config as _kc
            _kc.KUBE_CONFIG_DEFAULT_LOCATION = _orig_kube_loc
        except Exception:
            pass

    return tool_names


@pytest.fixture(scope="module")
def profile_tools(tmp_path_factory):
    """Import server-mcp.py under each built-in profile (module scope).

    Returns a dict mapping profile name → frozenset of registered tool names.
    The three imports run sequentially with full env restore between each load
    and register under distinct sys.modules keys; they do not interfere with
    the session-scoped characterization fixture.
    """
    kubeconfig = tmp_path_factory.mktemp("kube_profile_docs") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)

    return {
        profile: _load_under_profile(profile, kubeconfig)
        for profile in ("konflux", "kubernetes", "standalone")
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_profile_counts_match_docs(profile_tools):
    """Documented tool count per profile matches the live registry.

    M5 mutation: change DOCUMENTED_COUNTS["konflux"] to 47 → this test fails.
    """
    for profile, expected in DOCUMENTED_COUNTS.items():
        actual = len(profile_tools[profile])
        assert actual == expected, (
            f"Profile '{profile}': README documents {expected} tools but "
            f"the live registry has {actual}.  "
            f"Update DOCUMENTED_COUNTS in this test AND the README Profiles section."
        )


def test_kubernetes_standalone_tool_sets_are_identical(profile_tools):
    """kubernetes and standalone register the same tool set.

    The profile difference between them is source declaration, not tool surface.
    """
    kube_tools = profile_tools["kubernetes"]
    standalone_tools = profile_tools["standalone"]
    assert kube_tools == standalone_tools, (
        "kubernetes and standalone profiles must register identical tool sets.\n"
        f"  Only in kubernetes: {sorted(kube_tools - standalone_tools)}\n"
        f"  Only in standalone: {sorted(standalone_tools - kube_tools)}"
    )


def test_extension_delta_matches_docs(profile_tools):
    """The 12-name extension delta equals the live konflux − kubernetes delta.

    M5 mutation: drop any name from EXTENSION_DELTA → this test fails.
    """
    live_delta = frozenset(profile_tools["konflux"]) - frozenset(profile_tools["kubernetes"])
    assert live_delta == EXTENSION_DELTA, (
        "Extension delta mismatch between documented list and live registry.\n"
        f"  Documented names absent from live delta: {sorted(EXTENSION_DELTA - live_delta)}\n"
        f"  Live delta names not in documented list: {sorted(live_delta - EXTENSION_DELTA)}\n"
        "Update EXTENSION_DELTA in this test AND the README Profiles section."
    )


# ── Documentation drift-defence: README and config.example.yaml parsing ──────
# These tests parse the documentation files and verify that every stated tool
# count and extension-delta name list matches DOCUMENTED_COUNTS / EXTENSION_DELTA.
# Together with the live-registry tests above, all three pillars are locked:
#   live registry  ↔  test constants  ↔  documentation files
#
# None of these tests require the `profile_tools` fixture (no server load).
# Each assert is structured so a missing/unparseable section FAILS loudly.
#
# M5 mutation map (new tests only):
#   (a) README profile-table konflux **48** → **47**  →  test_readme_profile_table_counts_match_docs FAILS
#   (b) README prose "48 specialized tools" → "47"    →  test_readme_overview_prose_count_matches_docs FAILS
#   (c) README env-table "(48 tools)" → "(47 tools)"  →  test_readme_env_table_counts_match_docs FAILS
#   (d) drop a name from README 12-name delta list     →  test_readme_extension_delta_names_match_docs FAILS


def test_readme_profile_table_counts_match_docs():
    """README Profiles section table states the same counts as DOCUMENTED_COUNTS.

    Parses rows of the form: | `<profile>` [suffix] | **<N>** | ...
    A missing row is a hard failure (assert, not skip).

    M5 mutation (a): README profile-table konflux **48** → **47** → this fails.
    """
    text = README.read_text(encoding="utf-8")
    # Anchor to line start (MULTILINE) so stray table-like patterns in prose don't match.
    pattern = re.compile(r"^\|\s*`(\w+)`[^|]*\|\s*\*\*(\d+)\*\*\s*\|", re.MULTILINE)
    found: dict[str, int] = {}
    for m in pattern.finditer(text):
        name, count = m.group(1), int(m.group(2))
        if name in DOCUMENTED_COUNTS:
            found[name] = count
    for profile, expected in DOCUMENTED_COUNTS.items():
        assert profile in found, (
            f"README profile table row for '{profile}' not found in README.md. "
            "The row must not be removed or renamed."
        )
        assert found[profile] == expected, (
            f"README profile table: '{profile}' states {found[profile]} tools "
            f"but DOCUMENTED_COUNTS says {expected}. "
            "Update both the README Profiles table AND DOCUMENTED_COUNTS."
        )


def test_readme_overview_prose_count_matches_docs():
    """README Overview prose 'exposing N specialized tools' matches konflux count.

    M5 mutation (b): README prose '48 specialized tools' → '47' → this fails.
    """
    text = README.read_text(encoding="utf-8")
    m = re.search(r"exposing\s+(\d+)\s+specialized\s+tools", text)
    assert m, (
        "README Overview prose 'exposing N specialized tools' not found in README.md. "
        "The sentence must not be removed or rephrased beyond recognition."
    )
    stated = int(m.group(1))
    expected = DOCUMENTED_COUNTS["konflux"]
    assert stated == expected, (
        f"README Overview prose states {stated} specialized tools "
        f"but DOCUMENTED_COUNTS['konflux'] is {expected}. "
        "Update both the README Overview prose AND DOCUMENTED_COUNTS."
    )


def test_readme_env_table_counts_match_docs():
    """README env-table LUMINO_PROFILE row states the correct per-profile counts.

    The cell reads: Built-in profile: `konflux` (48 tools), `kubernetes` (36), `standalone` (36).

    M5 mutation (c): README env-table '(48 tools)' → '(47 tools)' → this fails.
    """
    text = README.read_text(encoding="utf-8")
    m = re.search(
        r"Built-in profile:.*?`konflux`\s*\((\d+)\s+tools\)"
        r".*?`kubernetes`\s*\((\d+)\)"
        r".*?`standalone`\s*\((\d+)\)",
        text,
    )
    assert m, (
        "README env-table LUMINO_PROFILE 'Built-in profile: ...' cell not found in README.md. "
        "The row must not be removed or reformatted."
    )
    stated = {
        "konflux": int(m.group(1)),
        "kubernetes": int(m.group(2)),
        "standalone": int(m.group(3)),
    }
    for profile, expected in DOCUMENTED_COUNTS.items():
        assert stated[profile] == expected, (
            f"README env-table LUMINO_PROFILE row states {stated[profile]} tools "
            f"for '{profile}' but DOCUMENTED_COUNTS says {expected}. "
            "Update both the README env table AND DOCUMENTED_COUNTS."
        )


def test_readme_architecture_count_matches_docs():
    """README Architecture section 'server-mcp.py # MCP server with all N tools' matches konflux count."""
    text = README.read_text(encoding="utf-8")
    m = re.search(r"server-mcp\.py[^\n]*all\s+(\d+)\s+tools", text)
    assert m, (
        "README Architecture section 'server-mcp.py # MCP server with all N tools' "
        "not found in README.md. The comment must not be removed or rephrased."
    )
    stated = int(m.group(1))
    expected = DOCUMENTED_COUNTS["konflux"]
    assert stated == expected, (
        f"README Architecture section states all {stated} tools "
        f"but DOCUMENTED_COUNTS['konflux'] is {expected}. "
        "Update both the README Architecture comment AND DOCUMENTED_COUNTS."
    )


def test_readme_available_tools_count_matches_docs():
    """README sample output 'Available tools: N' matches konflux count."""
    text = README.read_text(encoding="utf-8")
    m = re.search(r"Available tools:\s+(\d+)", text)
    assert m, (
        "README sample output 'Available tools: N' not found in README.md. "
        "The line must not be removed."
    )
    stated = int(m.group(1))
    expected = DOCUMENTED_COUNTS["konflux"]
    assert stated == expected, (
        f"README sample output states 'Available tools: {stated}' "
        f"but DOCUMENTED_COUNTS['konflux'] is {expected}. "
        "Update both the README sample output AND DOCUMENTED_COUNTS."
    )


def test_readme_extension_delta_names_match_docs():
    """README '**The 12 extension tools**' fenced block matches EXTENSION_DELTA.

    M5 mutation (d): drop a name from README's 12-name delta list → this fails.
    """
    text = README.read_text(encoding="utf-8")
    m = re.search(
        r"\*\*The 12 extension tools\*\*.*?```\n(.*?)```",
        text,
        re.DOTALL,
    )
    assert m, (
        "README '**The 12 extension tools**' fenced block not found in README.md. "
        "The section must not be removed or reformatted."
    )
    readme_delta = frozenset(m.group(1).split())
    assert readme_delta == EXTENSION_DELTA, (
        "README 12-name extension delta does not match EXTENSION_DELTA.\n"
        f"  In README but not EXTENSION_DELTA: {sorted(readme_delta - EXTENSION_DELTA)}\n"
        f"  In EXTENSION_DELTA but not README: {sorted(EXTENSION_DELTA - readme_delta)}\n"
        "Update the README list AND EXTENSION_DELTA."
    )


def test_config_example_profile_counts_match_docs():
    """config.example.yaml profile-comment counts match DOCUMENTED_COUNTS.

    Comment format: # profile: <name>  ... — <count> tools
    A missing comment is a hard failure.
    """
    text = CONFIG_EXAMPLE.read_text(encoding="utf-8")
    pattern = re.compile(r"#\s+profile:\s+(\w+).*?—\s*(\d+)\s+tools")
    found: dict[str, int] = {}
    for m in pattern.finditer(text):
        name, count = m.group(1), int(m.group(2))
        if name in DOCUMENTED_COUNTS:
            found[name] = count
    for profile, expected in DOCUMENTED_COUNTS.items():
        assert profile in found, (
            f"config.example.yaml profile comment for '{profile}' not found. "
            "The comment block must not be removed."
        )
        assert found[profile] == expected, (
            f"config.example.yaml states {found[profile]} tools for '{profile}' "
            f"but DOCUMENTED_COUNTS says {expected}. "
            "Update both config.example.yaml AND DOCUMENTED_COUNTS."
        )


def test_config_example_extension_delta_names_match_docs():
    """config.example.yaml 12-tool extension delta comment list matches EXTENSION_DELTA."""
    text = CONFIG_EXAMPLE.read_text(encoding="utf-8")
    m = re.search(
        r"# The 12 extension tools present only under the konflux profile:\n"
        r"((?:#.*\S.*\n)+)",
        text,
    )
    assert m, (
        "config.example.yaml '# The 12 extension tools...' section not found. "
        "The comment block must not be removed or reformatted."
    )
    config_delta: set[str] = set()
    for line in m.group(1).splitlines():
        content = line.lstrip("#").strip()
        for token in content.split():
            if "_" in token:  # all 12 extension tool names contain at least one underscore
                config_delta.add(token)
    config_delta_frozen = frozenset(config_delta)
    assert config_delta_frozen == EXTENSION_DELTA, (
        "config.example.yaml 12-name extension delta does not match EXTENSION_DELTA.\n"
        f"  In config but not EXTENSION_DELTA: {sorted(config_delta_frozen - EXTENSION_DELTA)}\n"
        f"  In EXTENSION_DELTA but not config: {sorted(EXTENSION_DELTA - config_delta_frozen)}\n"
        "Update config.example.yaml AND EXTENSION_DELTA."
    )
