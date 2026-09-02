"""Tests for timezone-aware UTC timestamps at the 4 remaining datetime.utcnow() sites.

Sites under test:
  (a1) src/helpers/log_analysis.py: LogStreamProcessor._analyze_chunk → result["timestamp"]
  (a2) src/server-mcp.py (success): check_cluster_certificate_health
       → scan_summary["scan_timestamp"]
  (a3) src/server-mcp.py (error):   check_cluster_certificate_health
       → scan_summary["scan_timestamp"] in the outer except handler
  (b)  src/helpers/utils.py: parse_certificate expiry arithmetic → regression guard

Tests (a1)–(a3) FAIL before the fix (timestamp is naive: tzinfo is None).
Test (b) PASSES before the fix — it is the regression guard ensuring that the swap
(datetime.utcnow()/cert.not_valid_after → datetime.now(timezone.utc)/cert.not_valid_after_utc)
preserves the correct day-count and raises nothing. Documented explicitly here per task brief.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
CHAR_TESTS = REPO_ROOT / "tests" / "characterization"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(CHAR_TESTS) not in sys.path:
    sys.path.insert(0, str(CHAR_TESTS))

from k8s_fakes import FakeApi  # noqa: E402


# ---------------------------------------------------------------------------
# (a1) LogStreamProcessor._analyze_chunk — log_analysis.py
# ---------------------------------------------------------------------------


def test_log_stream_processor_chunk_timestamp_is_aware():
    """LogStreamProcessor._analyze_chunk must produce a timezone-aware timestamp.

    Pre-fix:  datetime.utcnow().isoformat() is naive (tzinfo is None).
    Post-fix: datetime.now(timezone.utc).isoformat() is aware.
    """
    from helpers.log_analysis import LogStreamProcessor  # noqa: PLC0415

    proc = LogStreamProcessor(chunk_size=1)
    result = proc.add_line("error: something failed")
    assert result is not None, "Expected a chunk result after 1 line (chunk_size=1)"
    ts_str = result["timestamp"]
    ts = datetime.fromisoformat(ts_str)
    assert ts.tzinfo is not None, (
        f"_analyze_chunk timestamp must be timezone-aware; "
        f"got {ts_str!r} with tzinfo=None"
    )


# ---------------------------------------------------------------------------
# Server fixture for (a2) and (a3)
# ---------------------------------------------------------------------------

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

    Uses a distinct sys.modules key ("server_mcp_utcnow_aware") so this import
    coexists with other module-scoped server fixtures without collision.
    """
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    kubeconfig = tmp_path_factory.mktemp("kube_utcnow") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_utcnow_aware", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_utcnow_aware"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    sys.modules.pop("server_mcp_utcnow_aware", None)
    for path in (str(SRC), str(CHAR_TESTS)):
        try:
            sys.path.remove(path)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# (a2) check_cluster_certificate_health — success path scan_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cert_health_success_scan_timestamp_is_aware(server, monkeypatch):
    """Success path: check_cluster_certificate_health scan_summary.scan_timestamp
    must be timezone-aware.

    Pre-fix:  datetime.utcnow().isoformat() → naive (tzinfo is None).
    Post-fix: datetime.now(timezone.utc).isoformat() → aware.

    Providing a single explicit namespace and returning empty secrets lets the
    function complete normally so the success-path scan_timestamp (line ~7167)
    is the one in the returned result.
    """

    def list_secrets(namespace, **kwargs):
        return type("FakeList", (), {"items": []})()

    monkeypatch.setattr(server, "k8s_core_api", FakeApi(
        list_namespaced_secret=list_secrets,
    ))

    result = await server.check_cluster_certificate_health(
        namespaces=["test-ns"], source=""
    )

    ts_str = result["scan_summary"]["scan_timestamp"]
    ts = datetime.fromisoformat(ts_str)
    assert ts.tzinfo is not None, (
        f"Success-path scan_timestamp must be timezone-aware; "
        f"got {ts_str!r} with tzinfo=None"
    )


# ---------------------------------------------------------------------------
# (a3) check_cluster_certificate_health — error path scan_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cert_health_error_scan_timestamp_is_aware(server, monkeypatch):
    """Error path: scan_timestamp in the outer except handler must also be aware.

    A RuntimeError raised from list_namespaced_secret is not caught by the inner
    `except ApiException` guard, so it propagates to the outer `except Exception`
    which builds a fresh result dict with its own scan_timestamp.

    Pre-fix:  datetime.utcnow().isoformat() → naive.
    Post-fix: datetime.now(timezone.utc).isoformat() → aware.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("injected error for utcnow test")

    monkeypatch.setattr(server, "k8s_core_api", FakeApi(
        list_namespaced_secret=explode,
    ))

    result = await server.check_cluster_certificate_health(
        namespaces=["test-ns"], source=""
    )

    ts_str = result["scan_summary"]["scan_timestamp"]
    ts = datetime.fromisoformat(ts_str)
    assert ts.tzinfo is not None, (
        f"Error-path scan_timestamp must be timezone-aware; "
        f"got {ts_str!r} with tzinfo=None"
    )


# ---------------------------------------------------------------------------
# (b) parse_certificate expiry arithmetic — utils.py  REGRESSION GUARD
# ---------------------------------------------------------------------------


def test_parse_certificate_expiry_no_type_error():
    """Regression guard for the naive/aware swap in parse_certificate.

    parse_certificate subtracts `now` from `expiry_date` to compute days_remaining.
    Pre-fix: `datetime.utcnow()` (naive) minus `cert.not_valid_after` (also naive in
    cryptography 49.x) — arithmetic succeeds, no TypeError.
    Post-fix: `datetime.now(timezone.utc)` (aware) minus `cert.not_valid_after_utc`
    (aware) — arithmetic also succeeds, and no deprecation TypeError is raised.

    This test PASSES pre-fix — it is the regression guard ensuring that the
    naive/aware pair swap preserves the correct day-count and raises nothing.
    If it starts failing after the fix, the arithmetic pair is mismatched
    (e.g. aware minus naive or naive minus aware → TypeError).
    """
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.backends import default_backend  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415
    from cryptography.x509.oid import NameOID  # noqa: PLC0415
    from helpers.utils import parse_certificate  # noqa: PLC0415

    # Build a self-signed cert valid for exactly 90 days from now.
    key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-utcnow")])
    now_utc = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now_utc)
        .not_valid_after(now_utc + timedelta(days=90))
        .sign(key, hashes.SHA256(), default_backend())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    parsed = parse_certificate(pem)
    assert parsed is not None, "parse_certificate must not return None for a valid cert"
    assert "days_remaining" in parsed, "parse_certificate must include days_remaining"

    days = parsed["days_remaining"]
    # Allow ±1 day for clock-boundary crossings during test execution.
    assert 88 <= days <= 91, (
        f"Expected ~90 days_remaining; got {days}. "
        "The arithmetic pair (utcnow→now(utc), not_valid_after→not_valid_after_utc) "
        "must preserve the computed day-count."
    )
