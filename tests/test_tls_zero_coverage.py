"""RED tests for F-23(i), I-1/I-2, and scope-relative coverage ruling.

Coverage is scope-relative (controller ruling): discovered = in-scope unique namespaces,
not whole-cluster.  Full-cluster context is disclosed via **extra keys.

D2 Step 1 (original): zero-namespace case.
D2 review adds: truncation case (I-1) and RBAC-denied case (I-2).
Controller ruling: scope-relative denominator + new clean-run test proving
                   verdict=="complete" is reachable again.

Pre-fix (original):  FAILS — no 'coverage' key; all-clear fires on zero namespaces.
Post-fix (original): PASSES — 'coverage' block with verdict 'none'; all-clear absent.

Pre-fix (truncation): FAILS — discovered==scanned after [:max_namespaces] cap, verdict='complete',
                       all-clear fires despite only sampling part of the scope.
Post-fix (truncation): PASSES — discovered=len(_scope_ns)=5, skipped=3, scanned=2 → 'partial',
                        all-clear absent.

Pre-fix (RBAC):  FAILS — error-dict from list_pods_in_namespace counted as scanned, denied=0,
                 verdict='complete', all-clear fires even though every namespace was denied.
Post-fix (RBAC): PASSES — error-dict detected before append, denied > 0, verdict!='complete',
                 all-clear absent.

Pre-fix (clean-run): FAILS — fixture returns 5 namespaces (2 system + 3 tenant); whole-
                     cluster denominator sets discovered=5, scanned=2, skipped=3 → 'partial';
                     all-clear absent.  AssertionError: got 'partial' {discovered: 5, ...}.
Post-fix (clean-run): PASSES — scope-relative: discovered=2 (system only), excluded=3,
                      scanned=2, skipped=0 → verdict='complete'; all-clear present naming
                      scope and excluded count.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

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


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once per module against a fake kubeconfig.

    Uses a distinct sys.modules key ("server_mcp_tls_zero") to avoid collision.
    """
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    kubeconfig = tmp_path_factory.mktemp("kube_tls_zero") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_tls_zero", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_tls_zero"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    sys.modules.pop("server_mcp_tls_zero", None)
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# RED test — F-23(i): zero namespaces accessible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_block_present_when_zero_namespaces_accessible(
    server, monkeypatch
):
    """When list_namespaces returns [] (no accessible namespaces), result must have
    'coverage' with verdict 'none' and an insufficient-coverage recommendation.

    Pre-fix: no 'coverage' key; recommendations contain the forbidden all-clear.
    Post-fix: 'coverage' block with verdict 'none'; all-clear absent;
              insufficient-coverage message present.
    """

    async def _no_namespaces(source=""):
        return []

    async def _no_tekton(source=""):
        return {}

    monkeypatch.setattr(server, "list_namespaces", _no_namespaces)
    monkeypatch.setattr(server, "detect_tekton_namespaces", _no_tekton)

    result = await server.investigate_tls_certificate_issues(source="")

    assert "coverage" in result, "Missing coverage block"
    cov = result["coverage"]
    assert cov["verdict"] == "none", f"Expected 'none', got {cov['verdict']!r}"

    recs = result.get("recommendations", [])
    assert not any("No TLS certificate issues found" in r for r in recs), (
        "All-clear must not fire when coverage verdict is 'none' — "
        f"found the forbidden sentinel in recommendations: {recs!r}"
    )
    assert any("Coverage insufficient" in r or "0 namespaces" in r for r in recs), (
        f"Expected insufficient-coverage message, got {recs!r}"
    )


# ---------------------------------------------------------------------------
# RED tests — I-1 (truncation) and I-2 (RBAC-denied via error dict)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncation_yields_partial_verdict_not_complete(server, monkeypatch):
    """When max_namespaces caps the scan below the in-scope set, verdict must be 'partial'
    and the all-clear sentence must be absent.

    Setup: list_namespaces returns 5 system-patterned namespaces; max_namespaces=2
    so target_namespaces contains only 2; both are successfully scanned (empty pod lists).

    Pre-fix: discovered==scanned==2 after [:max_namespaces] cap → verdict='complete' (wrong);
             all-clear fires.
    Post-fix (scope-relative): discovered=len(_scope_ns)=5 (all 5 are in-scope system ns);
             scanned=2, skipped=3 → verdict='partial'; all-clear absent.
    """
    _FIVE_SYSTEM_NS = [
        "openshift-a", "openshift-b", "openshift-c", "openshift-d", "openshift-e"
    ]

    async def _five_namespaces(source=""):
        return list(_FIVE_SYSTEM_NS)

    async def _no_tekton(source=""):
        return {}

    async def _empty_pods(namespace, source=""):
        return []

    monkeypatch.setattr(server, "list_namespaces", _five_namespaces)
    monkeypatch.setattr(server, "detect_tekton_namespaces", _no_tekton)
    monkeypatch.setattr(server, "list_pods_in_namespace", _empty_pods)

    result = await server.investigate_tls_certificate_issues(
        max_namespaces=2, source=""
    )

    assert "coverage" in result, "Missing coverage block"
    cov = result["coverage"]
    assert cov["verdict"] == "partial", (
        f"Expected 'partial' when max_namespaces truncates discovery; "
        f"got {cov['verdict']!r} — coverage: {cov}"
    )
    recs = result.get("recommendations", [])
    assert not any("No TLS certificate issues found" in r for r in recs), (
        "All-clear must not fire when coverage is partial — "
        f"only {cov['scanned']} of {cov['discovered']} namespaces searched; "
        f"recommendations: {recs!r}"
    )


@pytest.mark.asyncio
async def test_divergent_namespace_lists_do_not_crash(server, monkeypatch):
    """Scope-relative union guards against the obligation-1 underflow crash.

    Divergence path: list_namespaces() is not cached on failure (server-mcp.py:1546-1558).
    If the first call returns [] but detect_tekton_namespaces() populates system_namespaces
    via a second list_namespaces() call that succeeds, then _scope_ns is non-empty while
    all_namespaces is [].

    Pre-fix: discovered=len(all_namespaces)=0, scanned=N → build_coverage raises ValueError
             (scanned > discovered) → outer except swallows → result={"error": "TLS
             investigation failed: ..."}.
    Post-fix (scope-relative): _scope_ns from system_namespaces; _all_known_ns = {} | _scope_ns;
             cluster_namespaces_total=3, excluded_by_scope=0, discovered=3, skipped=0,
             scanned=3 → verdict='complete'; real result returned (no error key).
    """

    async def _empty_namespaces(source=""):
        return []

    async def _tekton_namespaces(source=""):
        # Simulates detect_tekton_namespaces returning results via its own list_namespaces call.
        return {"core_tekton": ["tekton-a", "tekton-b", "tekton-c"]}

    async def _empty_pods(namespace, source=""):
        return []

    monkeypatch.setattr(server, "list_namespaces", _empty_namespaces)
    monkeypatch.setattr(server, "detect_tekton_namespaces", _tekton_namespaces)
    monkeypatch.setattr(server, "list_pods_in_namespace", _empty_pods)

    result = await server.investigate_tls_certificate_issues(source="")

    assert "error" not in result, (
        f"Tool must return a real result, not an error string — got: {result.get('error', '')!r}"
    )
    assert "coverage" in result, "Missing coverage block"
    cov = result["coverage"]
    assert cov["skipped"] >= 0, (
        f"skipped must be >= 0 (obligation-1: no negative counts): {cov}"
    )
    assert cov["scanned"] <= cov["discovered"], (
        f"scanned must be <= discovered: {cov}"
    )


@pytest.mark.asyncio
async def test_rbac_denied_pods_not_counted_as_scanned(server, monkeypatch):
    """When list_pods_in_namespace returns an error dict (RBAC-denied), those namespaces
    must NOT be counted as scanned; denied must be > 0; verdict must not be 'complete';
    all-clear must be absent.

    list_pods_in_namespace (server-mcp.py:2031) catches ApiException and returns
    [{"error": "API Error: Forbidden", "namespace": ns}] — a non-empty list that
    passes the existing isinstance check and was incorrectly appended to searched_namespaces.

    Pre-fix: error dict passes isinstance check, namespace appended, scanned=N denied=0
             verdict='complete', all-clear fires.
    Post-fix: error dict detected before append, denied=N, scanned=0,
              verdict='none' (or 'partial' if some scanned), all-clear absent.
    """
    _SYSTEM_NS = ["openshift-x", "openshift-y"]

    async def _two_namespaces(source=""):
        return list(_SYSTEM_NS)

    async def _no_tekton(source=""):
        return {}

    async def _forbidden_pods(namespace, source=""):
        return [{"error": "API Error: Forbidden", "namespace": namespace}]

    async def _no_events(*args, **kwargs):
        # Pre-fix: the error-dict path doesn't early-continue, so smart_get_namespace_events
        # is called for each namespace.  Post-fix: the early continue skips this call.
        return {"events": []}

    monkeypatch.setattr(server, "list_namespaces", _two_namespaces)
    monkeypatch.setattr(server, "detect_tekton_namespaces", _no_tekton)
    monkeypatch.setattr(server, "list_pods_in_namespace", _forbidden_pods)
    monkeypatch.setattr(server, "smart_get_namespace_events", _no_events)

    result = await server.investigate_tls_certificate_issues(source="")

    assert "coverage" in result, "Missing coverage block"
    cov = result["coverage"]
    assert cov.get("denied", 0) > 0, (
        f"denied must be > 0 when list_pods_in_namespace returns error dicts; "
        f"coverage: {cov}"
    )
    assert cov["verdict"] != "complete", (
        f"verdict must not be 'complete' when access was denied to all namespaces; "
        f"coverage: {cov}"
    )
    recs = result.get("recommendations", [])
    assert not any("No TLS certificate issues found" in r for r in recs), (
        "All-clear must not fire when all namespaces were denied; "
        f"recommendations: {recs!r}"
    )


# ---------------------------------------------------------------------------
# Controller ruling test — scope-relative denominator makes "complete" reachable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_in_scope_run_yields_complete_verdict_with_scope_named_all_clear(
    server, monkeypatch
):
    """A clean scan of all in-scope namespaces must produce verdict='complete' and
    include the scope-qualified all-clear sentence.

    This case was unreachable when discovered used the whole-cluster denominator: on any
    real cluster, all_namespaces >> system_namespaces, so scanned < discovered always,
    giving permanently-partial coverage regardless of scan quality.  The scope-relative
    ruling restores three distinct verdicts.

    Setup: list_namespaces returns 2 system namespaces; detect_tekton returns nothing;
    list_pods_in_namespace returns [] (empty — no pods, so scanned counts the ns but
    finds 0 issues); max_namespaces default (20).

    The fixture MUST include non-system namespaces so that scope genuinely narrows:
    the whole-cluster denominator (pre-fix) would see discovered=5, scanned=2, skipped=3
    → 'partial'; the scope-relative one (post-fix) sees discovered=2, scanned=2, skipped=0
    → 'complete'.  Without the non-system namespaces, both denominators produce the same
    numbers and the test cannot discriminate the two approaches.

    Pre-fix (whole-cluster denominator):
        AssertionError: Clean in-scope run must yield verdict='complete' (scope-relative
        ruling); got 'partial' — coverage: {'discovered': 5, 'scanned': 2, 'skipped': 3,
        'verdict': 'partial', ...}

    Post-fix (scope-relative): discovered=len(_scope_ns)=2; cluster_namespaces_total=5;
             excluded_by_scope=3; scanned=2; skipped=0; denied=0 → verdict='complete';
             all-clear IS present with scope and excluded count named.
    """
    # 2 system namespaces + 3 tenant namespaces — scope narrows to 2, excluding 3.
    # Without the tenant entries the whole-cluster denominator and scope-relative
    # denominator yield identical numbers and the RED/GREEN distinction is lost.
    _ALL_NS = ["openshift-x", "openshift-y", "team-a", "team-b", "team-c"]

    async def _mixed_namespaces(source=""):
        return list(_ALL_NS)

    async def _no_tekton(source=""):
        return {}

    async def _empty_pods(namespace, source=""):
        # Empty pods → searched_namespaces.append(namespace) then continue;
        # smart_get_namespace_events is NOT called (continue skips it).
        return []

    monkeypatch.setattr(server, "list_namespaces", _mixed_namespaces)
    monkeypatch.setattr(server, "detect_tekton_namespaces", _no_tekton)
    monkeypatch.setattr(server, "list_pods_in_namespace", _empty_pods)

    result = await server.investigate_tls_certificate_issues(source="")

    assert "coverage" in result, "Missing coverage block"
    cov = result["coverage"]
    assert cov["verdict"] == "complete", (
        f"Clean in-scope run must yield verdict='complete' (scope-relative ruling); "
        f"got {cov['verdict']!r} — coverage: {cov}"
    )
    assert cov["scanned"] == cov["discovered"], (
        f"scanned must equal discovered on a clean run; coverage: {cov}"
    )
    recs = result.get("recommendations", [])
    assert any("No TLS certificate issues found" in r for r in recs), (
        "All-clear must fire on a clean complete-coverage run; "
        f"recommendations: {recs!r}"
    )
    # excluded_by_scope > 0 is required — if it is 0 the fixture is wrong (no scope narrowing)
    # and the generic fallback "...searched namespaces" would satisfy the next assertion.
    excl = cov.get("excluded_by_scope", 0)
    assert excl > 0, (
        f"Fixture must produce excluded_by_scope > 0 to validate the scope-named all-clear branch; "
        f"coverage: {cov}"
    )
    # The all-clear sentence must name both the scope AND the excluded count.
    # The generic fallback ("...searched namespaces") must NOT satisfy this predicate.
    assert any("system namespaces" in r and "excluded by scope" in r for r in recs), (
        "All-clear sentence must contain 'system namespaces' AND 'excluded by scope' "
        "when excluded_by_scope > 0; the generic fallback must not satisfy this assertion; "
        f"recommendations: {recs!r}"
    )
