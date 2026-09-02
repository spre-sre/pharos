"""
Tests for get_etcd_logs since_time → since_seconds conversion (F-03, M-C3a, M-C3b).

Red criteria (pre-fix):
  test_no_since_key_in_kwargs    — FAILS: 'since' key IS present in recorded kwargs
  test_since_seconds_present     — FAILS: 'since_seconds' NOT in recorded kwargs
  test_since_seconds_value_approx — FAILS: no 'since_seconds' key
  test_floor_future_timestamp    — FAILS: no 'since_seconds' key

test_no_since_time_key_in_kwargs passes pre-fix too ('since_time' was never
forwarded to read_namespaced_pod_log).

Post-fix: all five pass.
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# Module fixture — same pattern as test_output_bounding.py
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Load server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_etcd_since") / "config"
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

    # Pin KUBE_CONFIG_DEFAULT_LOCATION so _discover_kube_contexts reads only
    # the fake config (guard against dev machine ~/.kube/config bleed).
    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_etcd_since", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_etcd_since"] = mod
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


# ---------------------------------------------------------------------------
# Spy fake CoreV1Api
# ---------------------------------------------------------------------------

def _make_spy_api():
    """Return (api, recorded_calls).

    Each call to read_namespaced_pod_log appends the full kwargs dict.
    """
    recorded = []

    class _SpyApi:
        def list_namespaced_pod(self, namespace, label_selector=None,
                                timeout_seconds=None):
            item = SimpleNamespace(
                metadata=SimpleNamespace(name="etcd-node-0", namespace=namespace),
                spec=SimpleNamespace(node_name="node-0"),
                status=SimpleNamespace(phase="Running"),
            )
            return SimpleNamespace(
                items=[item],
                metadata=SimpleNamespace(_continue=None),
            )

        def read_namespaced_pod_log(self, **kwargs):
            recorded.append(dict(kwargs))
            return "etcd log line\n"

    return _SpyApi(), recorded


# ---------------------------------------------------------------------------
# Helper — call _get_logs_with_k8s_client directly
# ---------------------------------------------------------------------------

def _call_get_logs(server_mod, since_time, mock_now=None):
    """Invoke _get_logs_with_k8s_client and return the recorded kwargs list."""
    fn = getattr(server_mod, "_get_logs_with_k8s_client", None)
    assert fn is not None, "_get_logs_with_k8s_client not found in server module"

    spy_api, recorded = _make_spy_api()
    target = {}
    log_params = {
        "since_time": since_time,
        "since_seconds": None,
        "tail_lines": None,
        "timestamps": True,
        "follow": False,
        "previous": False,
        "clean_logs": False,
    }

    if mock_now is not None:
        import unittest.mock as _mock
        import helpers.log_analysis as _log_analysis
        # _get_logs_with_k8s_client now lives in helpers.log_analysis; patch time
        # there so the since_seconds computation uses the mock wall-clock value.
        with _mock.patch.object(_log_analysis, "time") as mock_time_mod:
            mock_time_mod.time.return_value = mock_now
            fn(spy_api, ["etcd-node-0"], "openshift-etcd", "etcd", target, log_params)
    else:
        fn(spy_api, ["etcd-node-0"], "openshift-etcd", "etcd", target, log_params)

    return recorded


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEtcdSinceTimeKwarg:
    """since_time must be converted to since_seconds, never 'since'."""

    def test_no_since_key_in_kwargs(self, server):
        """
        Pre-fix: 'since' key IS in read_namespaced_pod_log kwargs → FAILS.
        Post-fix: 'since' NOT in kwargs.
        """
        recorded = _call_get_logs(server, since_time="2024-01-15T10:30:00Z")
        assert recorded, "spy was never called — check pod iteration"
        for call_kwargs in recorded:
            assert "since" not in call_kwargs, (
                f"'since' must not be passed to read_namespaced_pod_log; "
                f"got kwargs keys: {sorted(call_kwargs)}"
            )

    def test_since_seconds_present(self, server):
        """
        Pre-fix: 'since_seconds' NOT in kwargs → FAILS.
        Post-fix: 'since_seconds' IS in kwargs.
        """
        recorded = _call_get_logs(server, since_time="2024-01-15T10:30:00Z")
        assert recorded, "spy was never called"
        for call_kwargs in recorded:
            assert "since_seconds" in call_kwargs, (
                f"'since_seconds' must be passed to read_namespaced_pod_log; "
                f"got kwargs keys: {sorted(call_kwargs)}"
            )

    def test_no_since_time_key_in_kwargs(self, server):
        """
        'since_time' must never reach read_namespaced_pod_log (no such K8s kwarg).
        Passes both pre- and post-fix.
        """
        recorded = _call_get_logs(server, since_time="2024-01-15T10:30:00Z")
        for call_kwargs in recorded:
            assert "since_time" not in call_kwargs, (
                f"'since_time' must not be passed to read_namespaced_pod_log; "
                f"got kwargs keys: {sorted(call_kwargs)}"
            )

    def test_since_seconds_value_approx(self, server):
        """
        Pin the computed value.

        since_time="2024-01-15T10:30:00Z"; mock time.time() to since_epoch + 300.
        → since_seconds should be ≈ 300 (within ±2 s for int truncation).

        Pre-fix: no 'since_seconds' key → FAILS.
        Post-fix: value within ±2 s of 300.
        """
        from datetime import datetime, timezone
        since_epoch = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp()
        mock_now = since_epoch + 300.0

        recorded = _call_get_logs(
            server, since_time="2024-01-15T10:30:00Z", mock_now=mock_now
        )
        assert recorded, "spy was never called"
        for call_kwargs in recorded:
            assert "since_seconds" in call_kwargs, (
                f"'since_seconds' must be present; got: {sorted(call_kwargs)}"
            )
            val = call_kwargs["since_seconds"]
            assert abs(val - 300) <= 2, (
                f"Expected since_seconds≈300, got {val}"
            )

    def test_floor_future_timestamp(self, server):
        """
        Future since_time must yield since_seconds >= 1 (max(1,...) floor).
        Mutation M-C3b: removing max(1,...) → negative value → RED.

        Pre-fix: no 'since_seconds' key → FAILS.
        Post-fix: since_seconds >= 1.
        """
        recorded = _call_get_logs(server, since_time="2099-01-01T00:00:00Z")
        assert recorded, "spy was never called"
        for call_kwargs in recorded:
            assert "since_seconds" in call_kwargs, (
                f"'since_seconds' must be present for future timestamps; "
                f"got: {sorted(call_kwargs)}"
            )
            val = call_kwargs["since_seconds"]
            assert val >= 1, (
                f"since_seconds floor must be >= 1 for future timestamps, got {val}"
            )


class TestNaiveSinceTimeAsUTC:
    """Naive since_time must be treated as UTC, not the host's local timezone.

    Pre-fix: _since_dt.tzinfo is None → .timestamp() uses local tz → silent
    window skew proportional to the UTC offset.  On UTC+2 (Europe/Berlin
    summer) the window shifts by 7200 s; on UTC−5 it shifts the other way.
    Post-fix: naive is clamped to UTC before .timestamp() → no skew.
    """

    def test_naive_equals_z_suffix(self, server):
        """
        '2026-07-27T10:00:00' (naive) and '2026-07-27T10:00:00Z' must produce
        equal since_seconds (within ±1 s for int truncation).

        TZ is forced to Europe/Berlin so the test is deterministic on any CI
        host (including UTC ones where the bug would otherwise be vacuous).

        Pre-fix route (non-UTC host): naive.timestamp() is 7200 s off → FAILS.
        Pre-fix route (UTC host):     without TZ forcing, skew = 0 → would pass
                                      vacuously; TZ forcing makes it also FAIL.
        Post-fix: naive clamped to UTC → skew = 0 → PASSES.
        """
        import time as _time_mod
        from datetime import datetime, timezone as _tz

        # Force Europe/Berlin (UTC+2 in summer) regardless of CI host.
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Berlin"
        _time_mod.tzset()
        try:
            # Anchor: 2026-07-27T10:00:00Z + 3600 s
            anchor = datetime(2026, 7, 27, 10, 0, 0, tzinfo=_tz.utc).timestamp()
            mock_now = anchor + 3600.0

            rec_z = _call_get_logs(
                server, since_time="2026-07-27T10:00:00Z", mock_now=mock_now
            )
            rec_naive = _call_get_logs(
                server, since_time="2026-07-27T10:00:00", mock_now=mock_now
            )
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            _time_mod.tzset()

        assert rec_z and rec_naive, "spy must be called for both inputs"

        ss_z = rec_z[0]["since_seconds"]
        ss_naive = rec_naive[0]["since_seconds"]
        skew = ss_naive - ss_z

        assert abs(skew) <= 1, (
            f"Naive since_time was interpreted as local time instead of UTC. "
            f"since_seconds: Z-form={ss_z}, naive-form={ss_naive}, "
            f"skew={skew} s. "
            f"Pre-fix: on Europe/Berlin (UTC+2) the skew is -7200 s."
        )
