"""Honesty-contract invariant tests (Plan C1).

Three layers:
  1. Structural: every 'coverage' key in every golden is a dict.
  2. Semantic: per named adopter in ADOPTER_GOLDEN_TABLE, the block fields are correct scalars.
  3. Registry drift: the set of build_coverage( call sites == adopter table.
"""
import json
import ast
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).parent.parent
GOLDEN_DIR = REPO / "tests" / "characterization" / "golden"
SRC_DIR = REPO / "src"

# The adopter table: set of enclosing function names that contain a build_coverage( call.
# Registry drift guard fails if the observed set of function names diverges from this set.
# ADD an entry here whenever a new adopter is promoted in Plan C or Plan D.
ADOPTERS: set[str] = {
    "adaptive_namespace_investigation",
    "check_cluster_certificate_health",
    "investigate_tls_certificate_issues",
}

# Table mapping adopter function name → (golden filename, dotted path to the coverage block).
# Must stay in sync with ADOPTERS. Adding an adopter to ADOPTERS without a matching entry
# here causes test_build_coverage_semantic_adopters to fail immediately.
ADOPTER_GOLDEN_TABLE: dict[str, tuple[str, str]] = {
    "adaptive_namespace_investigation": (
        "adaptive_namespace_investigation.json",
        "adaptive_metadata.coverage",
    ),
    "check_cluster_certificate_health": (
        "check_cluster_certificate_health.json",
        "coverage",
    ),
    "investigate_tls_certificate_issues": (
        "investigate_tls_certificate_issues.json",
        "coverage",
    ),
}


def _find_coverage_values(obj, path=""):
    """Recursively yield (dotted_path, value) for every key named 'coverage'."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{path}.{k}" if path else k
            if k == "coverage":
                yield child_path, v
            yield from _find_coverage_values(v, child_path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _find_coverage_values(v, f"{path}[{i}]")


def _get_nested(obj: dict, dotted_path: str):
    """Navigate a dict by a dotted path like 'adaptive_metadata.coverage'."""
    for key in dotted_path.split("."):
        obj = obj[key]
    return obj


# ---------------------------------------------------------------------------
# AST helpers for the drift guard
# ---------------------------------------------------------------------------

def _build_parent_map(tree) -> dict:
    """Return a dict mapping id(child_node) -> parent_node for the entire AST."""
    parent: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def _is_build_coverage_call(node) -> bool:
    """Return True if node is a Call to build_coverage (bare or qualified form)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        (isinstance(func, ast.Name) and func.id == "build_coverage")
        or (isinstance(func, ast.Attribute) and func.attr == "build_coverage")
    )


def _outermost_enclosing_fn(node, parent_map):
    """Return the outermost FunctionDef/AsyncFunctionDef enclosing node, or None.

    Walks the parent chain from node toward the module root.  Each time a
    FunctionDef/AsyncFunctionDef is encountered the candidate is overwritten,
    so the final value is the outermost (not the nearest) enclosing function.
    Returns None when the call is at module scope (no enclosing function).
    """
    outermost = None
    current_id = id(node)
    while current_id in parent_map:
        p = parent_map[current_id]
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            outermost = p  # overwrite — inner-to-outer walk, last wins
        current_id = id(p)
    return outermost


def _observed_adopters(tree) -> set[str]:
    """Return the set of outermost-enclosing function names that contain a
    build_coverage( call, labelled "UNREGISTERED:<name>" when not in ADOPTERS.

    Each call is attributed to its OUTERMOST enclosing FunctionDef/AsyncFunctionDef
    so that a closure inside a registered adopter is correctly attributed to the
    adopter rather than generating a spurious UNREGISTERED: entry. A closure inside
    an UNREGISTERED function is attributed to that outer function — the bypass is
    closed. build_coverage( calls at module or class-body scope are silently skipped
    (pre-existing known limitation; all current call sites are inside async def tools).
    """
    parent_map = _build_parent_map(tree)
    observed: set[str] = set()
    for node in ast.walk(tree):
        if not _is_build_coverage_call(node):
            continue
        outer = _outermost_enclosing_fn(node, parent_map)
        if outer is None:
            continue  # module-scope call — out of scope, skip
        name = outer.name
        if name in ADOPTERS:
            observed.add(name)
        else:
            observed.add(f"UNREGISTERED:{name}")
    return observed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_golden_coverage_is_dict():
    """Structural: every 'coverage' key in every golden must be a dict.

    Excluded keys: focus_area_coverage, timestamp_coverage, metric_coverage,
    coverage_ratio — these are a different semantic domain and use different
    shapes by design.  Only the EXACT key 'coverage' is checked.
    """
    failures = []
    for f in sorted(GOLDEN_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        for path, value in _find_coverage_values(data):
            if not isinstance(value, dict):
                failures.append(
                    f"{f.name}/{path}: not a dict -> {value!r}"
                )
    assert not failures, "Coverage contract violation(s):\n" + "\n".join(failures)


def test_build_coverage_semantic_adopters():
    """Semantic: each named adopter's golden has the correct scalar field shapes.

    Driven by ADOPTER_GOLDEN_TABLE so that adding a new adopter without a
    corresponding semantic case causes this test to fail immediately.
    """
    sys.path.insert(0, str(SRC_DIR))
    from helpers import build_coverage

    # Table must match ADOPTERS exactly — missing entry = missing semantic gate.
    assert set(ADOPTER_GOLDEN_TABLE) == ADOPTERS, (
        f"ADOPTER_GOLDEN_TABLE keys do not match ADOPTERS.\n"
        f"  Table:    {sorted(ADOPTER_GOLDEN_TABLE)}\n"
        f"  ADOPTERS: {sorted(ADOPTERS)}"
    )

    for fn_name, (golden_file, coverage_path) in ADOPTER_GOLDEN_TABLE.items():
        data = json.loads((GOLDEN_DIR / golden_file).read_text())
        cov = _get_nested(data, coverage_path)
        assert isinstance(cov, dict), (
            f"{fn_name}: expected dict at {coverage_path}, got {type(cov)}"
        )
        # Validate mandatory scalar keys
        for key in ("unit", "requested", "discovered", "scanned", "denied", "skipped", "verdict"):
            assert key in cov, f"{fn_name}: missing key '{key}' in coverage block"
        assert isinstance(cov["unit"], str) and cov["unit"], (
            f"{fn_name}: unit must be non-empty string"
        )
        for int_key in ("requested", "discovered", "scanned", "denied", "skipped"):
            assert isinstance(cov[int_key], int), (
                f"{fn_name}: '{int_key}' must be int"
            )
        assert cov["verdict"] in ("none", "partial", "complete"), (
            f"{fn_name}: invalid verdict {cov['verdict']!r}"
        )
        # OQ-1: requested_mode must be present and in the allowed set
        assert "requested_mode" in cov, (
            f"{fn_name}: missing requested_mode in coverage block"
        )
        assert cov["requested_mode"] in ("all", "explicit"), (
            f"{fn_name}: requested_mode must be 'all' or 'explicit', got {cov['requested_mode']!r}"
        )
        # Consistency: complete verdict requires scanned == discovered, denied == 0, skipped == 0
        if cov["verdict"] == "complete":
            assert (
                cov["scanned"] == cov["discovered"]
                and cov["denied"] == 0
                and cov["skipped"] == 0
            ), f"{fn_name}: 'complete' verdict inconsistent: {cov}"

    # Unit tests for build_coverage correctness
    block = build_coverage(
        "pods", requested=0, discovered=2, scanned=2, denied=0, requested_mode="all"
    )
    assert block["verdict"] == "complete"

    block_partial = build_coverage(
        "pods", requested=0, discovered=7, scanned=3, denied=1, requested_mode="all"
    )
    assert block_partial["verdict"] == "partial"

    block_none = build_coverage(
        "pods", requested=0, discovered=0, scanned=0, requested_mode="all"
    )
    assert block_none["verdict"] == "none"

    # I-1: caller-supplied verdict in **extra must NOT override the computed verdict
    block_sabotage = build_coverage(
        "pods", requested=0, discovered=0, scanned=0, requested_mode="all",
        verdict="complete",
    )
    assert block_sabotage["verdict"] == "none", (
        f"Caller-supplied verdict must not override computed; got {block_sabotage['verdict']!r}"
    )

    # I-2: skipped > 0 yields "partial" even when scanned == discovered
    block_skipped = build_coverage(
        "pods", requested=0, discovered=10, scanned=10, skipped=2, requested_mode="all"
    )
    assert block_skipped["verdict"] == "partial", (
        f"skipped=2 with scanned==discovered should yield 'partial', got {block_skipped['verdict']!r}"
    )


def test_build_coverage_registry_drift():
    """Registry drift: the set of outermost enclosing function names that call
    build_coverage( must equal ADOPTERS.

    The guard is FUNCTION-granular, not file-granular. Each build_coverage( call is
    attributed to its OUTERMOST enclosing function via _observed_adopters(), so:
    - A new unregistered top-level function that calls build_coverage( → detected.
    - A closure inside a registered adopter → attributed to the adopter → no spurious UNREGISTERED:.
    - A closure inside an UNREGISTERED function → attributed to that outer name → detected.
    Adding an entry to ADOPTERS without a matching call is equally caught.

    Note: build_coverage( calls at module or class-body scope (outside any def) are
    not attributed to any function and are silently ignored. This is a pre-existing
    known limitation; all current and planned call sites are inside async def tools.
    """
    observed: set[str] = set()
    for py_file in SRC_DIR.rglob("*.py"):
        text = py_file.read_text()
        if "build_coverage(" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        observed |= _observed_adopters(tree)

    assert observed == ADOPTERS, (
        f"build_coverage( enclosing functions diverge from adopter table.\n"
        f"  Observed:      {sorted(observed)}\n"
        f"  Adopter table: {sorted(ADOPTERS)}"
    )


def test_build_coverage_drift_closure_attribution():
    """Verify that build_coverage( calls inside closures are attributed to the
    OUTERMOST enclosing function by _observed_adopters() — the same production helper
    used by test_build_coverage_registry_drift.

    Case A: closure inside a REGISTERED adopter → only the adopter name observed.
    Case B: closure inside an UNREGISTERED function → UNREGISTERED:<outer> observed.

    Mutation M-C1c (nearest-def revert) produces:
      Case A: {'UNREGISTERED:_emit'}   — inner def name leaks, bypass re-opens
      Case B: {'UNREGISTERED:_emit'}   — outer unregistered name lost, bypass re-opens
    Both assertions fail under the mutation, confirming the test is a live oracle for
    the production loop, not just the AST helpers.
    """
    # Case A: build_coverage( lives ONLY inside a closure of a registered adopter.
    # The outer adopter name must be reported; the inner def name must not appear.
    observed_a = _observed_adopters(ast.parse(textwrap.dedent("""\
        async def adaptive_namespace_investigation(ns):
            def _emit(n):
                return build_coverage("certs", requested=0, discovered=n, scanned=n)
            return _emit(10)
    """)))
    assert observed_a == {"adaptive_namespace_investigation"}, (
        f"Closure inside registered adopter should report only the adopter; got {observed_a}"
    )

    # Case B: build_coverage( lives ONLY inside a closure of an UNREGISTERED function.
    # The outer unregistered name must surface, not the inner def name.
    observed_b = _observed_adopters(ast.parse(textwrap.dedent("""\
        async def brand_new_unregistered_tool():
            def _emit(n):
                return build_coverage("certs", requested=0, discovered=n, scanned=n)
            return _emit(10)
    """)))
    assert observed_b == {"UNREGISTERED:brand_new_unregistered_tool"}, (
        f"Closure inside unregistered function should report UNREGISTERED:<outer>; got {observed_b}"
    )
