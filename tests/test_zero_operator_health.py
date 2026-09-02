"""Step 3 RED — F-27a/b: zero operators/components must yield 'undetermined'.

F-27b (main): get_openshift_cluster_operator_status with 0 operators.
  Pre-fix: total_operators=0, `0 < 0` is False → stays "healthy" (bug).
  Post-fix: `if total_operators == 0:` guard fires → "undetermined".

F-27a (defensive): _get_fallback_cluster_health with 0 components.
  Pre-fix: total_components=0, `0 < 0` is False → stays "healthy" (bug).
  Post-fix: `if total_components == 0:` guard fires → "undetermined".

  To create total_components=0: list_namespaced_pod raises (RBAC) AND
  logger.warning raises on the first call (inside the per-namespace except handler,
  before component_health.append). This causes the outer namespace try to catch the
  exception — no namespace entry is added. Node check also raises — no node entry.
  Result: component_health=[], total_components=0.
"""
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

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
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_zero_op") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_zero_op", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_zero_op"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    if _orig_kube_loc is not None:
        try:
            from kubernetes.config import kube_config as _k8s_kube_config
            _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = _orig_kube_loc
        except Exception:
            pass
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


@pytest.mark.asyncio
async def test_fallback_with_readable_nodes_yields_undetermined(server, monkeypatch):
    """F-27 RED: fallback_mode=True + readable nodes must yield 'undetermined'.

    Live evidence: operators denied (403), 28/28 nodes ready → overall_health: "healthy"
    (FAIL). The fix: when fallback_mode is True the operator data is unavailable, so
    overall_health must be "undetermined" per Honesty Contract Clause D(b). The
    component verdict is preserved separately under component_health_verdict.

    Pre-fix: component health (28/28 nodes ready) drives overall_health → "healthy".
    Post-fix: overall_health == "undetermined"; component_health_verdict preserved.
    """
    from kubernetes.client.exceptions import ApiException

    # Operator API denied → triggers fallback path
    fake_custom = MagicMock()
    fake_custom.list_cluster_custom_object.side_effect = ApiException(
        status=403, reason="Forbidden"
    )
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    # Nodes readable: simulate 3 ready nodes so component_health is non-empty
    fake_core = MagicMock()
    fake_core.api_client.configuration.host = "https://test:6443"
    fake_core.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

    def _make_node(ready: bool):
        cond = MagicMock()
        cond.type = "Ready"
        cond.status = "True" if ready else "False"
        node = MagicMock()
        node.status.conditions = [cond]
        return node

    fake_core.list_node.return_value = MagicMock(
        items=[_make_node(True), _make_node(True), _make_node(True)]
    )
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    result = await server.get_openshift_cluster_operator_status(source="")

    hs = result.get("health_summary", {})
    overall = hs.get("overall_health")
    assert overall == "undetermined", (
        f"fallback_mode=True + readable nodes must yield 'undetermined', got {overall!r} — "
        "component health must not drive overall_health when operator data is unavailable "
        "(Honesty Contract Clause D(b): verdict-cannot-be-reached → named enum member)"
    )
    # Component verdict must still be reported (signal not lost)
    assert "component_health_verdict" in hs, (
        f"fallback health_summary must report component_health_verdict; keys={sorted(hs)}"
    )
    assert hs.get("fallback_mode") is True, (
        f"fallback_mode must be True in health_summary; got {hs.get('fallback_mode')!r}"
    )


@pytest.mark.asyncio
async def test_zero_operators_yields_undetermined(server, monkeypatch):
    """F-27b RED: 0 operators → 'undetermined' (not 'healthy').

    Pre-fix: total_operators=0, `healthy_operators(0) < total_operators(0)` = False
    → overall_health stays "healthy" (0 < 0 is False).
    Post-fix: `if total_operators == 0:` guard fires → "undetermined".

    The mock uses plural="clusteroperators" as required (earlier drafts accidentally
    used "clusterversionoperators" which doesn't exist, leaving the real call untouched).
    """
    fake_custom = MagicMock()
    # Both cluster-object calls return empty items: clusteroperators + clusterversions
    fake_custom.list_cluster_custom_object.return_value = {"items": []}
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    result = await server.get_openshift_cluster_operator_status(source="")

    overall = result.get("health_summary", {}).get("overall_health")
    assert overall == "undetermined", (
        f"0 operators must yield 'undetermined', got {overall!r} — "
        "pre-fix: `0 < 0` is False so 'healthy' is returned incorrectly"
    )


@pytest.mark.asyncio
async def test_fallback_zero_components_yields_undetermined(server, monkeypatch):
    """F-27a RED: _get_fallback_cluster_health with 0 components → 'undetermined'.

    Pre-fix: total_components=0, `0 < 0` is False → overall_health stays "healthy".
    Post-fix: `if total_components == 0:` guard fires → "undetermined".

    To achieve total_components=0, the per-namespace except handler must fail before
    reaching component_health.append. This is done by making list_namespaced_pod raise
    (RBAC) AND making logger.warning raise on its first call. The outer namespace try
    then catches the propagated error (adds only to critical_issues). Node check also
    raises. Result: component_health=[], total_components=0.
    """
    from kubernetes.client.exceptions import ApiException

    fake_core = MagicMock()
    fake_core.api_client.configuration.host = "https://test:6443"
    fake_core.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")
    fake_core.list_node.side_effect = ApiException(status=403, reason="Forbidden")

    # COUPLING NOTE: This setup is tightly coupled to the logging call ORDER inside
    # _get_fallback_cluster_health. The strategy is: make the FIRST logger.warning call
    # raise (which fires inside the per-namespace except handler, before
    # component_health.append), so no namespace entry is added. If any warning is added
    # BEFORE the per-namespace except handler in a future refactor, this test will break
    # loudly (RuntimeError propagates). That is the intended failure mode — loud beats
    # silent — but the maintainer should update warning_calls[0] == N accordingly.
    warning_calls = [0]

    def _first_warning_raises(*args, **kwargs):
        warning_calls[0] += 1
        if warning_calls[0] == 1:
            raise RuntimeError("test: force bypass of component_health.append")
        # Subsequent calls are no-ops (outer handlers log and continue)

    monkeypatch.setattr(server.logger, "warning", _first_warning_raises)

    result = await server._get_fallback_cluster_health(fake_core)

    overall = result.get("health_summary", {}).get("overall_health")
    assert overall == "undetermined", (
        f"0 components must yield 'undetermined', got {overall!r} — "
        "pre-fix: `0 < 0` is False so 'healthy' is returned incorrectly"
    )


@pytest.mark.asyncio
async def test_fallback_cluster_health_receives_core_api(server, monkeypatch):
    """F-R2-1: _get_fallback_cluster_health call sites forward server.k8s_core_api.

    Both fallback branches in get_openshift_cluster_operator_status (403 and 404)
    pass `k8s_core_api` to _get_fallback_cluster_health. This test pins the identity
    of that argument using the 403-triggered path.

    A required-kwarg change to the function signature would NOT catch this regression
    (None is a legal value for core_api); only an identity check on the captured
    argument proves the call-site wiring is intact.

    Non-vacuity: temporarily mutate the call site to pass None → this test FAILS.
    Restore → this test PASSES.
    """
    from kubernetes.client.exceptions import ApiException

    # Sentinel: a unique object whose identity we can verify at assertion time.
    sentinel_core = object()
    monkeypatch.setattr(server, "k8s_core_api", sentinel_core)

    # Capturing stub: records the argument it receives, then returns the minimum
    # dict shape the caller needs (it calls .insert(0, ...) on critical_issues).
    captured = {}

    async def _capturing_stub(core_api):
        captured["core_api"] = core_api
        return {
            "fallback_mode": True,
            "cluster_info": {},
            "operator_status": [],
            "component_health": [],
            "health_summary": {
                "fallback_mode": True,
                "overall_health": "undetermined",
                "total_operators": 0,
                "healthy_operators": 0,
                "degraded_operators": 0,
                "total_components": 0,
                "healthy_components": 0,
                "degraded_components": 0,
                "component_health_verdict": "undetermined",
            },
            "critical_issues": [],
            "dependencies": None,
        }

    monkeypatch.setattr(server, "_get_fallback_cluster_health", _capturing_stub)

    # Trigger the 403 branch so the fallback is invoked.
    fake_custom = MagicMock()
    fake_custom.list_cluster_custom_object.side_effect = ApiException(
        status=403, reason="Forbidden"
    )
    monkeypatch.setattr(server, "k8s_custom_api", fake_custom)

    await server.get_openshift_cluster_operator_status(source="")

    assert "core_api" in captured, (
        "_get_fallback_cluster_health stub was never called; "
        "the 403 branch may not have been triggered correctly"
    )
    assert captured["core_api"] is server.k8s_core_api, (
        f"_get_fallback_cluster_health received core_api={captured['core_api']!r} "
        f"but server.k8s_core_api is {server.k8s_core_api!r}. "
        "The 403/404 call sites must pass k8s_core_api (the live server global), "
        "not None or a stale cached value. "
        "Non-vacuity: mutate the call site to pass None → this fails; restore → passes."
    )
