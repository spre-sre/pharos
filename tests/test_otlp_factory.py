"""Factory pin tests for the OTLP adapter wiring in server-mcp.py (phase 5, Task 2).

Pins (MINOR 6):
  (a)  _build_otlp_source via ADAPTER_FACTORIES with a synthetic SourceConfig
       → returns OtlpLogSource.
  (b)  _otlp_rings.setdefault identity: two builds for the same source name
       → same ring object (ring is cached, not recreated).
  (c)  Module-scope _otlp_listening default is False.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"

# A minimal fake kubeconfig so server-mcp.py doesn't error at import.
_FAKE_KUBECONFIG = """\
apiVersion: v1
clusters:
- cluster:
    server: https://127.0.0.1:6443
  name: fake
contexts:
- context:
    cluster: fake
    user: fake
  name: fake
current-context: fake
kind: Config
users:
- name: fake
  user: {}
"""

# Module name we register in sys.modules — must match the name server-mcp uses
# in sys.modules[__name__] to capture itself.
_MOD_NAME = "server_mcp_otlp_factory_test"


@pytest.fixture(scope="module")
def server_mod() -> ModuleType:
    """Load server-mcp.py once with env vars set, module registered in sys.modules."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as kf:
        kf.write(_FAKE_KUBECONFIG.encode())
        kube_path = kf.name

    saved = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    os.environ["KUBECONFIG"] = kube_path
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(_SRC))
    spec = importlib.util.spec_from_file_location(_MOD_NAME, str(_SRC / "server-mcp.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    yield mod

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    sys.modules.pop(_MOD_NAME, None)
    try:
        sys.path.remove(str(_SRC))
    except ValueError:
        pass
    try:
        os.unlink(kube_path)
    except OSError:
        pass


# ─── Synthetic SourceConfig helper ───────────────────────────────────────────

def _make_otlp_source_config(capacity: int = 50, max_body_bytes: int = 65536):
    """Return a minimal SourceConfig for the OTLP adapter."""
    from core.config_types import SourceConfig
    return SourceConfig(
        adapter="otlp",
        options={
            "ring_capacity": capacity,
            "max_body_bytes": max_body_bytes,
        },
    )


# ─── (c) _otlp_listening default ─────────────────────────────────────────────

def test_otlp_listening_default_false(server_mod):
    """Module-scope _otlp_listening must be False at import time (F9 flag)."""
    assert server_mod._otlp_listening is False, (
        f"_otlp_listening should default to False, got {server_mod._otlp_listening!r}"
    )


# ─── (a) ADAPTER_FACTORIES → OtlpLogSource ───────────────────────────────────

def test_adapter_factories_contains_otlp(server_mod):
    """ADAPTER_FACTORIES must have an 'otlp' key."""
    assert "otlp" in server_mod.ADAPTER_FACTORIES, (
        f"'otlp' missing from ADAPTER_FACTORIES; got {sorted(server_mod.ADAPTER_FACTORIES)}"
    )


def test_build_otlp_source_returns_otlp_log_source(server_mod):
    """_build_otlp_source via ADAPTER_FACTORIES returns an OtlpLogSource instance."""
    from adapters.otlp.logs import OtlpLogSource
    server_mod._otlp_rings.clear()
    sc = _make_otlp_source_config()
    factory = server_mod.ADAPTER_FACTORIES["otlp"]
    instance = factory("test-otlp-src", sc)
    assert isinstance(instance, OtlpLogSource), (
        f"Expected OtlpLogSource, got {type(instance)!r}"
    )


def test_factory_direct_call_returns_otlp_log_source(server_mod):
    """_build_otlp_source called directly returns OtlpLogSource."""
    from adapters.otlp.logs import OtlpLogSource
    server_mod._otlp_rings.clear()
    sc = _make_otlp_source_config()
    instance = server_mod._build_otlp_source("direct-test", sc)
    assert isinstance(instance, OtlpLogSource)


# ─── (b) _otlp_rings.setdefault identity ─────────────────────────────────────

def test_two_builds_same_source_same_ring(server_mod):
    """Building the same source name twice returns the same ring object (setdefault)."""
    from core.signals import LogRecord
    server_mod._otlp_rings.clear()
    sc = _make_otlp_source_config()
    src1 = server_mod._build_otlp_source("shared-src", sc)
    src2 = server_mod._build_otlp_source("shared-src", sc)
    ring1 = server_mod._otlp_rings["shared-src"]
    # Both adapters must wrap the SAME ring object.
    assert src1._ring is ring1
    assert src2._ring is ring1, (
        "setdefault identity broken: two builds created different ring objects"
    )
    # Confirm by appending: src2 sees the record appended via ring1.
    rec = LogRecord(timestamp=None, body="test-record")
    ring1.append(1.0, rec)
    assert len(src2._ring.snapshot()) == 1, (
        "src2's ring is not the same object as ring1"
    )


def test_different_source_names_get_different_rings(server_mod):
    """Two different source names → two different rings."""
    server_mod._otlp_rings.clear()
    sc = _make_otlp_source_config()
    server_mod._build_otlp_source("src-alpha", sc)
    server_mod._build_otlp_source("src-beta", sc)
    ring_alpha = server_mod._otlp_rings["src-alpha"]
    ring_beta = server_mod._otlp_rings["src-beta"]
    assert ring_alpha is not ring_beta, (
        "Different source names share the same ring (broken isolation)"
    )
