"""Step 0 RED — F-05: predictive_log_analyzer must NOT return fabricated precision/recall.

Pre-fix: `train_anomaly_model` bakes `"performance_metrics": {accuracy, precision, recall}`
into model metadata; server reads them and reports precision=0.8, recall=0.7.

Post-fix: `performance_metrics` block deleted → `if perf:` is False → honest branch fires:
precision=None, recall=None, note="Precision/recall require labeled validation data - not available".

The assertion is `precision IS None` (not absent — the honest branch SETS them to None).
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

# 5 lines × 10 = 50 log lines — enough to trigger model training (>10 required).
_SAMPLE_LOG = "\n".join(
    [
        "2026-07-20T10:00:01Z INFO starting server",
        "2026-07-20T10:00:02Z ERROR connection refused to db:5432",
        "2026-07-20T10:00:03Z WARN retrying in 5s",
        "2026-07-20T10:00:04Z ERROR connection refused to db:5432",
        "2026-07-20T10:00:05Z FATAL giving up after 2 retries",
    ]
    * 10
)


def _make_pod(name="api-1", ns="team-a", phase="Running"):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = ns
    pod.status.phase = phase
    return pod


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_fab_metrics") / "config"
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
        "server_mcp_fab_metrics", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_fab_metrics"] = mod
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
async def test_predictive_log_analyzer_no_fabricated_precision_recall(server, monkeypatch, tmp_path):
    """F-05 RED: precision and recall must be None (not 0.8 / 0.7) post-fix.

    Pre-fix: fabricated performance_metrics block produces precision=0.8, recall=0.7.
    Post-fix: block deleted → honest branch fires → precision=None, recall=None, note present.
    """
    # Redirect HOME so ML model writes land in tmp, not ~/.lumino
    monkeypatch.setenv("HOME", str(tmp_path))

    # Mock core API to return a Running pod with log data
    pod = _make_pod()
    pod_list = MagicMock()
    pod_list.items = [pod]

    fake_core = MagicMock()
    fake_core.list_namespaced_pod.return_value = pod_list
    fake_core.read_namespaced_pod_log.return_value = _SAMPLE_LOG
    fake_core.list_namespaced_event.return_value = MagicMock(
        items=[], metadata=MagicMock(_continue=None)
    )
    monkeypatch.setattr(server, "k8s_core_api", fake_core)

    result = await server.predictive_log_analyzer(namespaces=["team-a"], source="")

    perf = result.get("model_performance", {})
    # Presence assertions: the honest branch SETS these to None — they must be present,
    # not merely absent (absent would also satisfy .get() == None but means a different bug).
    assert "precision" in perf, (
        f"precision key must be present (set to None by honest branch) — keys: {sorted(perf)}"
    )
    assert "recall" in perf, (
        f"recall key must be present (set to None by honest branch) — keys: {sorted(perf)}"
    )
    assert perf["precision"] is None, (
        f"precision must be None post-fix, got {perf['precision']!r} — "
        "pre-fix: returns fabricated 0.8"
    )
    assert perf["recall"] is None, (
        f"recall must be None post-fix, got {perf['recall']!r} — "
        "pre-fix: returns fabricated 0.7"
    )
    assert "note" in perf, (
        f"Missing 'note' key in model_performance — keys present: {sorted(perf)}"
    )
