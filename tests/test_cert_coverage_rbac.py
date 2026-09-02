"""RED test for F-06: check_cluster_certificate_health RBAC DenyAll.

Step 0 of D2.

Pre-fix:  FAILS — result has no 'coverage' key; security finding severity is 'info';
          old 'scan_coverage' key has wrong shape.
Post-fix: PASSES — 'coverage' block present with verdict 'none';
          RBAC security finding severity 'high'.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from kubernetes.client.rest import ApiException

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
CHAR_TESTS = REPO_ROOT / "tests" / "characterization"

# Import FakeApi via the characterization helpers.
sys.path.insert(0, str(CHAR_TESTS))
from k8s_fakes import FakeApi  # noqa: E402

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

    Uses a distinct sys.modules key ("server_mcp_cert_rbac") so this import
    coexists with other module-scoped server fixtures without collision.
    """
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    kubeconfig = tmp_path_factory.mktemp("kube_cert_rbac") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_cert_rbac", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_cert_rbac"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    sys.modules.pop("server_mcp_cert_rbac", None)
    for path in (str(SRC), str(CHAR_TESTS)):
        try:
            sys.path.remove(path)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# RED test — F-06
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_denial_is_scope_relative(server, monkeypatch):
    """Partial-denial: caller names 3 namespaces, 2 succeed, 1 denied.
    The tool's 5 own system-namespace additions are also denied.

    Pre-fix: verdict == "partial" with denied == 6 (5 system + 1 caller), which
    means the caller's fully-satisfied namespaces get blamed for the tool's
    additions.  security_findings mentions 5 RBAC denials even though the
    caller's request was 2/3 served.

    Post-fix: verdict == "partial" with denied == 1 (caller-only); the five
    system namespaces are in coverage extras (system_namespaces_denied == 5) as
    an informational note, not in the caller-facing denied count.
    """
    call_count = {"n": 0}
    allowed = {"team-a", "team-b"}

    def list_secrets(namespace, **kwargs):
        call_count["n"] += 1
        if namespace not in allowed:
            raise ApiException(status=403, reason="Forbidden")
        # Return empty list — we only care about coverage shape, not certs found.
        return _make_items([])

    def _make_items(items):
        ns = type("FakeList", (), {"items": items})()
        return ns

    monkeypatch.setattr(server, "k8s_core_api", FakeApi(
        list_namespaced_secret=list_secrets,
    ))

    result = await server.check_cluster_certificate_health(
        namespaces=["team-a", "team-b", "team-c"],
        source="",
    )

    cov = result.get("coverage", {})
    assert cov.get("verdict") == "partial", (
        f"Expected 'partial' (2/3 caller namespaces scanned), got {cov.get('verdict')!r}; "
        f"full coverage: {cov}"
    )
    # Caller-relative counts: 1 denied (team-c), 2 scanned (team-a, team-b)
    assert cov.get("denied") == 1, (
        f"Expected denied==1 (caller's team-c only), got {cov.get('denied')}; "
        f"coverage: {cov}"
    )
    assert cov.get("scanned") == 2, (
        f"Expected scanned==2 (caller's team-a + team-b), got {cov.get('scanned')}; "
        f"coverage: {cov}"
    )
    assert cov.get("discovered") == 3, (
        f"Expected discovered==3 (all 3 caller namespaces), got {cov.get('discovered')}; "
        f"coverage: {cov}"
    )
    # System namespaces are in extras, not in the caller denominator
    assert cov.get("system_namespaces_denied", -1) == 5, (
        f"Expected system_namespaces_denied==5, got {cov.get('system_namespaces_denied')}; "
        f"coverage: {cov}"
    )
    # The RBAC security_finding (if any) must mention the caller's denied namespace count
    # (1), not 5+ from the tool's own additions.
    caller_finding = next(
        (f for f in result.get("security_findings", []) if f.get("type") == "rbac_limitation"),
        None,
    )
    if caller_finding:
        msg = caller_finding.get("message", "")
        assert "1 namespaces" in msg or "1 namespace" in msg, (
            f"RBAC finding should mention 1 caller-denied namespace, not tool additions; "
            f"got: {msg!r}"
        )


@pytest.mark.asyncio
async def test_coverage_block_present_on_rbac_deny_all(server, monkeypatch):
    """When every namespace scan is RBAC-denied, result must have 'coverage' with
    verdict 'none' and a 'high' severity security finding.

    Pre-fix: 'scan_coverage' key (wrong shape), severity 'info', no 'coverage'.
    Post-fix: 'coverage' block with verdict 'none', severity 'high'.
    """
    # FakeApi: list_namespaced_secret raises 403 for every call.
    deny_all = FakeApi(
        list_namespaced_secret=ApiException(status=403, reason="Forbidden"),
    )
    monkeypatch.setattr(server, "k8s_core_api", deny_all)

    result = await server.check_cluster_certificate_health(
        namespaces=["openshift-etcd", "build-service"], source=""
    )

    assert "coverage" in result, "Missing coverage block"
    assert result["coverage"]["verdict"] == "none", (
        f"Expected 'none', got {result['coverage']['verdict']!r}"
    )
    assert any(
        f.get("severity") == "high" for f in result.get("security_findings", [])
    ), (
        "RBAC-denied scan should emit 'high' severity finding, not 'info'; "
        f"security_findings: {result.get('security_findings', [])}"
    )
