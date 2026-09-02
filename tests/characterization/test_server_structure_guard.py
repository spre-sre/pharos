"""Structure tripwire: pin the set of non-tool top-level functions in server-mcp.py.

Any structural addition or removal of a function (tool-decorated or plain) will
fail this test, forcing the author to update the pin explicitly and providing a
second-order safety net on top of the parity snapshot.

Detection logic
---------------
A top-level function is considered a *tool* when its first decorator is either:
  - a Call whose .func is an Attribute with .attr == "tool"
    (captures ``@mcp.tool()`` and ``@mcp.tool(description=...)`` forms), or
  - a Name whose .id == "enhanced_tool_decorator"
    (captures the ``@enhanced_tool_decorator`` wrapper used in this file).

Every other top-level FunctionDef / AsyncFunctionDef is a *non-tool* function.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_SERVER_SRC = REPO_ROOT / "src" / "server-mcp.py"

# ---------------------------------------------------------------------------
# Pinned expected set — update this when a new non-tool top-level function is
# intentionally added or removed.  Running the test after changing the pin
# constitutes the documented approval step.
# ---------------------------------------------------------------------------

_PINNED_NON_TOOL_FUNCS: frozenset[str] = frozenset({
    "_health",
    "_gate_source",
    "_gate_extension",
    "_is_cancelled_reason",
    "_make_tool_extension_map",
    "_build_file_source",
    "_build_loki_source",
    "_build_es_source",
    "_build_otlp_source",
    "_otlp_ingest_stats",
    "_build_k8s_client_set",
    "_build_k8s_client_set_from_token",
    "_resolve_k8s",
    "_query_archived_plrs_for_trace",
    "_report_sim_progress",
    "_get_adapter_instance",
    "_route_log_source",
    "_otlp_retention_or_none",
    "enhanced_tool_decorator",
    "detect_tekton_namespaces",
    "list_pipelineruns",
    "list_taskruns",
    "get_pipelinerun_logs",
    "get_konflux_components_status",
    "get_pod_logs",
    "analyze_failed_pipeline",
    "list_recent_pipeline_runs",
    "track_pipeline_across_namespaces",
    "find_pipeline",
    "get_tekton_pipeline_runs_status",
    "get_etcd_logs",
    "get_machine_config_pool_status",
    "get_openshift_cluster_operator_status",
    "_discover_api_groups",
    "_load_intree_extensions",
    "_detect_ctx",
    "_discover_kube_contexts",
    "_scan_kubeconfig_dir",
})

_PINNED_COUNT: int = 38


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_tool_decorator(dec: ast.expr) -> bool:
    """Return True if *dec* marks a function as an MCP tool."""
    # @mcp.tool() or @mcp.tool(description=...)
    if (
        isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr == "tool"
    ):
        return True
    # @enhanced_tool_decorator
    if isinstance(dec, ast.Name) and dec.id == "enhanced_tool_decorator":
        return True
    return False


def _collect_non_tool_funcs(src: pathlib.Path) -> frozenset[str]:
    """Parse *src* with ast and return the set of non-tool top-level function names."""
    tree = ast.parse(src.read_text(encoding="utf-8"))
    non_tool: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_tool_decorator(dec) for dec in node.decorator_list):
            continue
        non_tool.add(node.name)
    return frozenset(non_tool)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestServerStructureGuard:
    """Pin the non-tool top-level function set in server-mcp.py."""

    def test_non_tool_funcs_match_pinned_set(self):
        """Non-tool top-level functions must exactly match the pinned frozenset.

        If this test fails, a function was added or removed without updating the
        pin.  Update ``_PINNED_NON_TOOL_FUNCS`` and ``_PINNED_COUNT`` to approve
        the change.
        """
        actual = _collect_non_tool_funcs(_SERVER_SRC)
        added = actual - _PINNED_NON_TOOL_FUNCS
        removed = _PINNED_NON_TOOL_FUNCS - actual
        assert actual == _PINNED_NON_TOOL_FUNCS, (
            f"Non-tool function set has drifted from the pin.\n"
            f"  Added   (in src, not in pin): {sorted(added)}\n"
            f"  Removed (in pin, not in src): {sorted(removed)}"
        )

    def test_non_tool_func_count_matches_pin(self):
        """The count of non-tool functions must equal _PINNED_COUNT.

        A separate count assertion makes failures unambiguous when the set
        assertion and the count assertion disagree (e.g. a rename that keeps
        count stable but swaps names).
        """
        actual = _collect_non_tool_funcs(_SERVER_SRC)
        assert len(actual) == _PINNED_COUNT, (
            f"Expected {_PINNED_COUNT} non-tool top-level functions, "
            f"found {len(actual)}: {sorted(actual)}"
        )

    def test_pin_matches_reality(self):
        """The pinned frozenset must itself be consistent with the source file.

        Catches the case where _PINNED_NON_TOOL_FUNCS was edited incorrectly
        (e.g. a typo or stale name that never existed in the file).
        """
        actual = _collect_non_tool_funcs(_SERVER_SRC)
        ghost_names = _PINNED_NON_TOOL_FUNCS - actual
        assert not ghost_names, (
            f"Pin contains names that do not exist in server-mcp.py: {sorted(ghost_names)}"
        )

    def test_mutation_detection(self, tmp_path):
        """Adding a dummy function to a copy of the source must cause the guard to fail.

        This is the mutation test: it verifies that the detector is actually
        sensitive to structural changes.
        """
        original = _SERVER_SRC.read_text(encoding="utf-8")
        mutated = original + "\ndef _dummy_mutation_sentinel(): pass\n"
        mutated_path = tmp_path / "server-mcp-mutated.py"
        mutated_path.write_text(mutated, encoding="utf-8")

        actual_mutated = _collect_non_tool_funcs(mutated_path)
        # The mutated set must differ from the pinned set (sentinel detected)
        assert "_dummy_mutation_sentinel" in actual_mutated, (
            "_collect_non_tool_funcs failed to detect the injected sentinel function"
        )
        assert actual_mutated != _PINNED_NON_TOOL_FUNCS, (
            "The pinned set incorrectly matches the mutated source — "
            "guard would not catch a new top-level function"
        )
