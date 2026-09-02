"""Session-scoped import of src/server-mcp.py with no live cluster."""
import importlib.util
import os
import random
import sys
from pathlib import Path

import pytest

from .k8s_fakes import FakeApi

ALL_CLIENT_GLOBALS = (
    "k8s_core_api", "k8s_custom_api", "k8s_apps_api", "k8s_batch_api",
    "k8s_storage_api", "k8s_autoscaling_api", "k8s_networking_api",
)

# Golden stability requires deterministic str hashing: several tools select
# namespaces via list(set(...))[:N] (e.g. server-mcp.py:6476, 10985), which
# is PYTHONHASHSEED-dependent. The seed must be set before interpreter
# start, so re-exec once if it isn't.
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except OSError:  # sandboxed environments may block execv
        raise RuntimeError(
            "PYTHONHASHSEED=0 is required for stable goldens; "
            "re-run as: PYTHONHASHSEED=0 pytest ..."
        )
# NOTE: this guard assumes a plain `pytest` invocation; the suite is NOT
# pytest-xdist-safe (workers have execnet argv). Run it single-process.

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

FAKE_KUBECONFIG = """\
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


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """Import server-mcp.py exactly once, against a fake kubeconfig.

    Import-time side effects neutralized:
    - load_incluster_config fails (no service account) -> load_kube_config
      reads our fake file -> clients construct without any network I/O.
    - KUBEARCHIVE_ENABLED=false so KubeArchiveEndpointDiscovery (created at
      import, server-mcp.py:315) does not auto-port-forward.
    """
    # Save originals so the finalizer can restore them (or delete if absent).
    _orig_kubeconfig = os.environ.get("KUBECONFIG")
    _orig_kubearchive = os.environ.get("KUBEARCHIVE_ENABLED")
    _orig_telemetry = os.environ.get("LUMINO_DISABLE_TELEMETRY")
    _orig_lumino_config = os.environ.get("LUMINO_CONFIG")
    _orig_lumino_profile = os.environ.get("LUMINO_PROFILE")
    _orig_kube_loc = None

    kubeconfig = tmp_path_factory.mktemp("kube") / "config"
    kubeconfig.write_text(FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")

    # Pin KUBE_CONFIG_DEFAULT_LOCATION to the absolute fake path *before* the
    # server module is loaded.  If any legacy test file was collected before
    # the server fixture ran it may have triggered an early import of
    # kubernetes.config (e.g. via helpers.kubearchive_integration), which
    # bakes KUBE_CONFIG_DEFAULT_LOCATION = "~/.kube/config" into the module.
    # Later, when the deterministic fixture redirects HOME to a per-test
    # tmpdir, "~/.kube/config" expands to a non-existent path, causing
    # list_kube_config_contexts() to raise and get_current_cluster_id() to
    # return "unknown" instead of "fake".  Setting the constant here (using
    # the absolute path, immune to HOME changes) fixes the drift.
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass  # kubernetes not installed — tests that need it will fail later

    # Phase 2a: server-mcp.py runs load_config() at import (module-level
    # _lumino_config).  Pin the profile source so an exported LUMINO_CONFIG/
    # LUMINO_PROFILE cannot drift list_sources.json or (with an invalid value)
    # crash the import.  Absent both -> built-in konflux profile, zero I/O.
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp"] = mod
    spec.loader.exec_module(mod)

    yield mod

    # Finalizer: restore env vars and sys.path to their pre-fixture state.
    def _restore_env(key, original):
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    _restore_env("KUBECONFIG", _orig_kubeconfig)
    _restore_env("KUBEARCHIVE_ENABLED", _orig_kubearchive)
    _restore_env("LUMINO_DISABLE_TELEMETRY", _orig_telemetry)
    _restore_env("LUMINO_CONFIG", _orig_lumino_config)
    _restore_env("LUMINO_PROFILE", _orig_lumino_profile)

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


def registered_tool_names(server) -> set[str]:
    return set(server.mcp._tool_manager._tools.keys())


@pytest.fixture(autouse=True)
def _pin_default_client_views(server):
    """Pin _DefaultClientView sentinels in moved helper modules to the session
    characterization server's class for every test.

    helpers.log_analysis and helpers.event_analysis each hold a _DefaultClientView
    sentinel (set at server import time). test_multicluster.py loads three additional
    in-process server modules whose import-time wiring overwrites the sentinel —
    "last module loaded wins". Because test_multicluster.py sorts before all
    test_readonly_* files, every readonly test ran after multicluster would silently
    use the wrong server module's k8s_core_api if the sentinel is not repinned here.

    This autouse fixture runs before each test and restores the original value after,
    following the same teardown convention as the server fixture finalizer.
    """
    import helpers.log_analysis as _log_analysis
    import helpers.event_analysis as _event_analysis

    orig_la = _log_analysis._DefaultClientView
    orig_ea = _event_analysis._DefaultClientView

    _log_analysis._DefaultClientView = server._DefaultClientView
    _event_analysis._DefaultClientView = server._DefaultClientView

    yield

    _log_analysis._DefaultClientView = orig_la
    _event_analysis._DefaultClientView = orig_ea


def apply_determinism(server, monkeypatch, tmp_path):
    """Shared determinism reset callable for golden and parity tests.

    Extracted from test_golden_tools.py's ``deterministic`` autouse fixture so
    that test_canonical_parity.py can invoke it directly (plain function, NOT a
    fixture — must be imported explicitly; see round-2 V1 in the task brief).

    Call this plus the per-case patch loop fresh before EACH tool call in any
    test that makes two sequential calls to the same seam — otherwise cache
    population from call #1 leaks into call #2 and may mask routing bugs or
    cause spurious deep-equal mismatches.
    """
    random.seed(0)
    try:
        import numpy
        numpy.random.seed(0)
    except ImportError:
        pass
    # Clear the analysis_cache singleton (helpers/log_analysis.py:856).
    # analyze_pod_logs_hybrid stores results here; without a reset, call #2
    # in a fresh-before-each-call sequence gets a cache HIT that returns a
    # structurally different result (no cache_enabled/cache_key_generated,
    # adds cache_age_seconds) that the normalizer cannot reconcile.
    # Lazy import: SRC is only on sys.path after the session-scoped server
    # fixture runs, so the import cannot be at module level.
    try:
        from helpers.log_analysis import analysis_cache as _ac
        _ac.cache.clear()
        _ac.access_times.clear()
    except ImportError:
        pass
    for _var in ("THANOS_URL", "PROMETHEUS_URL", "PROMETHEUS_TOKEN", "OPENSHIFT_TOKEN", "OC_TOKEN"):
        monkeypatch.delenv(_var, raising=False)
    monkeypatch.setattr(server, "_namespace_cache", {}, raising=False)
    prom_cache = getattr(server, "_prometheus_endpoint_cache", None)
    if isinstance(prom_cache, dict):
        monkeypatch.setattr(server, "_prometheus_endpoint_cache", {},
                            raising=False)
    elif prom_cache is not None and hasattr(prom_cache, "_cache"):
        prom_cache._cache.clear()
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ALL_CLIENT_GLOBALS:
        monkeypatch.setattr(server, name, FakeApi(), raising=False)
