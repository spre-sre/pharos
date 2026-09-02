"""Phase 2e Task 1: k8s instance registry tests.

Tests (a)-(f) from the brief Step 1 (RED), plus (d) variant.

Module-scoped fixture loads server-mcp.py once under the name
`server_mcp_k8s_instances` (a unique name so it does not collide with the
session-scoped `server_mcp` fixture in characterization/conftest.py).
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
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_k8s_instances") / "config"
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

    # F9 harness-bleed guard: pin KUBE_CONFIG_DEFAULT_LOCATION to the fake
    # kubeconfig so _discover_kube_contexts reads only harness contexts.
    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_k8s_instances", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_k8s_instances"] = mod
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


# ─── (a) _DefaultClientView late-binds ───────────────────────────────────────

def test_default_view_late_binds_custom_api(server, monkeypatch):
    """Swapping server.k8s_custom_api AFTER view creation must be reflected.

    The view must resolve the module global at ACCESS time (property), not at
    construction time (stored reference).
    """
    sentinel = object()
    view = server._DefaultClientView()
    monkeypatch.setattr(server, "k8s_custom_api", sentinel)
    assert view.custom_api is sentinel, (
        "_DefaultClientView.custom_api must resolve server.k8s_custom_api at "
        "access time, not at construction time"
    )


def test_default_view_late_binds_core_api(server, monkeypatch):
    """Same late-binding guarantee for core_api."""
    sentinel = object()
    view = server._DefaultClientView()
    monkeypatch.setattr(server, "k8s_core_api", sentinel)
    assert view.core_api is sentinel


# ─── (b) _resolve_k8s("") and default name both return the view ──────────────

def test_resolve_k8s_empty_string_returns_view(server):
    """'' -> (_DefaultClientView(), None)."""
    view, err = server._resolve_k8s("")
    assert err is None, f"Expected no error for '', got: {err}"
    assert isinstance(view, server._DefaultClientView), (
        f"Expected _DefaultClientView, got {type(view)}"
    )


def test_resolve_k8s_default_name_returns_view(server):
    """The default kubernetes instance name -> same view, same error=None."""
    default_name = server._source_registry.default_kubernetes_instance()
    assert default_name is not None, "konfux profile must have a default kubernetes instance"
    view, err = server._resolve_k8s(default_name)
    assert err is None, f"Expected no error for default name {default_name!r}, got: {err}"
    assert isinstance(view, server._DefaultClientView), (
        f"Expected _DefaultClientView for {default_name!r}, got {type(view)}"
    )


# ─── (c) unknown name -> structured error dict, no exception ─────────────────

def test_resolve_k8s_unknown_name_returns_error_dict(server):
    """Unknown kubernetes instance -> (None, error_dict), no exception raised."""
    result = server._resolve_k8s("totally-unknown-cluster-xyz")
    assert result is not None, "_resolve_k8s must return a tuple, not None"
    view, err = result
    assert view is None, f"Expected view=None for unknown source, got: {type(view)}"
    assert err is not None, "Expected an error dict for unknown source"
    assert "error" in err, f"Error dict must have 'error' key, got: {sorted(err.keys())}"
    assert "requested_source" in err, (
        f"Error dict must have 'requested_source' key, got: {sorted(err.keys())}"
    )


def test_resolve_k8s_unknown_name_has_empty_instances_list(server):
    """Error dict for unknown name must have known_kubernetes_instances as empty list.

    F-01 fix: invalid source must not enumerate the inventory. The key is retained
    (downstream callers key on its presence) but the content is always [].
    """
    _, err = server._resolve_k8s("no-such-cluster")
    assert err is not None
    # Key must be present (contract: callers rely on its existence)
    assert "known_kubernetes_instances" in err, (
        f"Error dict must have 'known_kubernetes_instances', got: {sorted(err.keys())}"
    )
    known = err["known_kubernetes_instances"]
    # Content must be empty — invalid source must not enumerate the inventory (F-01)
    assert known == [], (
        f"known_kubernetes_instances must be [] for invalid source; got: {known}"
    )


# ─── (d) non-default instance: lazy build + construct-once cache ──────────────

def test_non_default_instance_lazy_build(server, monkeypatch):
    """First resolve of a non-default instance calls _build_k8s_client_set once;
    second resolve uses the cache (call count does not increase)."""
    from core.registry import SourceEntry, ADAPTER_CAPABILITIES

    # Add a second kubernetes instance to the registry
    extra = SourceEntry(
        name="extra-cluster",
        adapter="kubernetes",
        capabilities=ADAPTER_CAPABILITIES["kubernetes"],
        state="configured",
        default=False,
    )
    # Ensure the instance isn't already registered (test isolation)
    if "extra-cluster" not in server._source_registry._entries:
        server._source_registry.add_instance(extra)

    # Clear any cached set for this instance
    server._k8s_instances.pop("extra-cluster", None)

    # Monkeypatch _build_k8s_client_set with a counting fake
    call_count = [0]

    def fake_build(context, kubeconfig_path=None):
        call_count[0] += 1
        from dataclasses import dataclass
        from typing import Any
        # Return a minimal sentinel object that looks like a K8sClientSet
        return object()  # the caching logic is on identity

    monkeypatch.setattr(server, "_build_k8s_client_set", fake_build)

    # First resolve must call _build_k8s_client_set
    view1, err1 = server._resolve_k8s("extra-cluster")
    assert err1 is None, f"Unexpected error on first resolve: {err1}"
    assert call_count[0] == 1, f"Expected 1 build call, got {call_count[0]}"

    # Second resolve must use the cache (no additional build call)
    view2, err2 = server._resolve_k8s("extra-cluster")
    assert err2 is None, f"Unexpected error on second resolve: {err2}"
    assert call_count[0] == 1, (
        f"Expected still 1 build call after second resolve, got {call_count[0]}"
    )
    assert view1 is view2, "Both resolves of the same instance must return the same object"


# ─── (e) ReadOnlyK8sClient.wrap idempotency pin ──────────────────────────────

def test_wrap_idempotency(server):
    """wrap(wrap(raw)) is wrap(raw) — already idempotent at readonly_client.py:27-28.

    The correct assertion shape from the brief:
      w = wrap(raw); assert wrap(w) is w
    (wrap(raw) mints a fresh proxy each call, so wrap(wrap(x)) is NOT wrap(x) for raw x)
    """
    from core.readonly_client import ReadOnlyK8sClient

    class _RawFake:
        """Minimal fake raw kubernetes api."""
        def read_something(self): ...

    raw = _RawFake()
    w = ReadOnlyK8sClient.wrap(raw)
    assert not isinstance(raw, ReadOnlyK8sClient), "raw must not already be a proxy"
    assert isinstance(w, ReadOnlyK8sClient), "wrap(raw) must return a proxy"
    # Idempotency: wrapping an already-wrapped proxy returns the SAME proxy
    assert ReadOnlyK8sClient.wrap(w) is w, (
        "wrap(wrap(raw)) must return the same proxy object (idempotent)"
    )


def test_wrap_raw_not_cached(server):
    """Wrapping a raw client twice produces different proxy objects.

    Documents that raw clients are NOT cached — only already-proxied clients
    pass through unchanged.
    """
    from core.readonly_client import ReadOnlyK8sClient

    class _RawFake:
        def read_something(self): ...

    raw = _RawFake()
    w1 = ReadOnlyK8sClient.wrap(raw)
    w2 = ReadOnlyK8sClient.wrap(raw)
    assert w1 is not w2, (
        "wrap(raw) called twice on the same raw object must produce different "
        "proxy objects (raw clients are not cached)"
    )


# ─── (f) default path never increments _dial_call_count ─────────────────────

def test_default_resolve_does_not_increment_dial_count(server, monkeypatch):
    """Resolving the default instance ('') must NOT call _build_k8s_client_set.

    _dial_call_count is incremented ONLY inside _build_k8s_client_set.
    After resolving '' or the default name, the count must not change.
    """
    initial_count = server._dial_call_count

    server._resolve_k8s("")
    assert server._dial_call_count == initial_count, (
        f"_dial_call_count increased from {initial_count} to "
        f"{server._dial_call_count} during _resolve_k8s('') — "
        "the default path must never call _build_k8s_client_set"
    )

    default_name = server._source_registry.default_kubernetes_instance()
    if default_name:
        server._resolve_k8s(default_name)
        assert server._dial_call_count == initial_count, (
            f"_dial_call_count increased when resolving the default name "
            f"{default_name!r}"
        )
