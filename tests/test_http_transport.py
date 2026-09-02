"""Phase 2f Task 1: transport/bind resolution + construction-neutrality + /health.
Phase 2f Task 2: resolve_http_serving + verify_bearer + BearerASGIMiddleware.

Server fixture mirrors test_canonical_aliases.py's pattern but adds the five-var
LUMINO env scrub BEFORE exec_module (construction reads env at import time — an
after-the-fact assert cannot protect against harness bleed; precedent:
tests/characterization/conftest.py:97-98).
"""
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# src/ must be on the path for the module-level resolve_transport import below
# (http_transport.py has no server imports — safe to import at collection time).
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

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

# All LUMINO vars that server-mcp.py reads at import time (construction + config).
_LUMINO_IMPORT_VARS = [
    "LUMINO_TRANSPORT",
    "LUMINO_BIND_HOST",
    "LUMINO_BIND_PORT",
    "LUMINO_STATELESS_HTTP",
    "LUMINO_HTTP_TOKEN",
    # Phase 5: OTLP receiver vars — must be scrubbed so a developer's shell
    # does not inadvertently supply a token or bind address (env hygiene, 2f F1).
    "LUMINO_OTLP_TOKEN",
    "LUMINO_OTLP_BIND_HOST",
    "LUMINO_OTLP_BIND_PORT",
]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Load server-mcp.py once with a fake kubeconfig and scrubbed LUMINO env."""
    kubeconfig = tmp_path_factory.mktemp("kube_http_transport") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)

    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    for v in _LUMINO_IMPORT_VARS:
        _orig[v] = os.environ.get(v)

    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    # MUST precede exec_module — construction reads these at import time.
    for v in _LUMINO_IMPORT_VARS:
        os.environ.pop(v, None)

    # F9 harness-bleed guard: pin KUBE_CONFIG_DEFAULT_LOCATION to fake kubeconfig.
    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_http_transport", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_http_transport"] = mod
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
# (a) resolve_transport unit tests
# ---------------------------------------------------------------------------

from core.http_transport import resolve_transport  # noqa: E402 — imported for unit tests


@pytest.mark.parametrize("env,expected", [
    ({"LUMINO_TRANSPORT": "stdio"}, "stdio"),
    ({"LUMINO_TRANSPORT": "streamable-http"}, "streamable-http"),
    ({"KUBERNETES_NAMESPACE": "x"}, "streamable-http"),
    ({"K8S_NAMESPACE": "x"}, "streamable-http"),
    ({}, "stdio"),
    ({"LUMINO_TRANSPORT": "stdio", "KUBERNETES_NAMESPACE": "x"}, "stdio"),
])
def test_resolve_transport_valid(env, expected):
    assert resolve_transport(env) == expected


def test_resolve_transport_invalid():
    with pytest.raises(ValueError):
        resolve_transport({"LUMINO_TRANSPORT": "bogus"})


# ---------------------------------------------------------------------------
# (b) M5 construction-neutrality: env-scrubbed fixture -> SDK defaults
# ---------------------------------------------------------------------------

def test_m5_construction_neutrality_host(server):
    assert server.mcp.settings.host == "127.0.0.1"


def test_m5_construction_neutrality_port(server):
    assert server.mcp.settings.port == 8000


def test_m5_construction_neutrality_stateless_http(server):
    assert server.mcp.settings.stateless_http is False


# ---------------------------------------------------------------------------
# (c) /health is NOT a tool; tool count unchanged at 48
# ---------------------------------------------------------------------------

def test_health_not_a_tool(server):
    tools = server.mcp._tool_manager._tools
    assert "_health" not in tools, "_health must not be registered as a tool"
    assert "health" not in tools, "health must not be registered as a tool"


def test_tool_count_unchanged(server):
    tools = server.mcp._tool_manager._tools
    assert len(tools) == 49, f"Expected 49 tools, got {len(tools)}"


# ---------------------------------------------------------------------------
# (d) /health HTTP route: responds 200 {"status": "ok"}
# ---------------------------------------------------------------------------

def test_health_route_responds(server):
    from starlette.testclient import TestClient
    app = server.mcp.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Task 2 imports (resolve_http_serving, verify_bearer, BearerASGIMiddleware)
# ---------------------------------------------------------------------------

from core.http_transport import (  # noqa: E402
    BearerASGIMiddleware,
    resolve_http_serving,
    verify_bearer,
)


# ---------------------------------------------------------------------------
# (a) resolve_http_serving truth table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host,token,expected", [
    ("0.0.0.0", None, "refuse"),
    ("0.0.0.0", "", "refuse"),
    ("0.0.0.0", "t", "serve_authed"),
    ("127.0.0.1", None, "serve_open"),
    ("127.0.0.1", "t", "serve_authed"),
    ("localhost", None, "serve_open"),
    ("::1", None, "serve_open"),
    ("10.0.0.5", None, "refuse"),
    ("::", None, "refuse"),           # T2-review pin: "::" is NOT localhost → refuse
])
def test_resolve_http_serving(host, token, expected):
    assert resolve_http_serving(host, token) == expected


# ---------------------------------------------------------------------------
# (b) verify_bearer units
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header,expected_token,expected", [
    (None, "t", False),
    ("bearer t", "t", False),           # lowercase scheme
    ("Bearer wrong", "t", False),        # wrong token
    ("Bearer t", "t", True),             # exact match
    ("", "t", False),                    # empty header
    ("Bearerx-token", "x-token", False), # T2-review pin: no space → strict prefix rejection
])
def test_verify_bearer(header, expected_token, expected):
    assert verify_bearer(header, expected_token) == expected


# ---------------------------------------------------------------------------
# (c) tier-1 in-process: BearerASGIMiddleware via TestClient
# ---------------------------------------------------------------------------

_TEST_TOKEN = "test-token-2f"


@pytest.fixture(scope="module")
def bearer_client(server):
    """TestClient for BearerASGIMiddleware — no lifespan.

    The StreamableHTTPSessionManager is a singleton per FastMCP instance: once
    started-and-stopped (by test_health_route_responds), it cannot be restarted.
    Using TestClient without the context manager skips lifespan events entirely.
    This is correct for auth middleware tests:
      - 401 cases never reach the inner app (middleware short-circuits).
      - /health exemption goes to the inner app's lightweight route (no session
        manager needed).
      - Correct-token test only asserts status != 401; inner-app behaviour
        without an active session is acceptable (400/503 are fine).
    """
    from starlette.testclient import TestClient
    app = BearerASGIMiddleware(server.mcp.streamable_http_app(), _TEST_TOKEN)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def mcp_path(server):
    """Read the actual MCP path from settings (default '/mcp')."""
    return getattr(server.mcp.settings, "streamable_http_path", "/mcp")


def test_bearer_health_no_auth_200(bearer_client):
    """GET /health with no auth must be exempt → 200."""
    resp = bearer_client.get("/health")
    assert resp.status_code == 200


def test_bearer_mcp_no_auth_401(bearer_client, mcp_path):
    """POST to MCP path with no Authorization → 401."""
    resp = bearer_client.post(mcp_path, json={})
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert resp.json() == {"error": "unauthorized"}


def test_bearer_mcp_wrong_token_401(bearer_client, mcp_path):
    """POST to MCP path with wrong token → 401 (same shape)."""
    resp = bearer_client.post(
        mcp_path, json={},
        headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert resp.json() == {"error": "unauthorized"}


def test_bearer_mcp_correct_token_passes(bearer_client, mcp_path):
    """POST to MCP path with correct Bearer token → auth passes (status != 401)."""
    resp = bearer_client.post(
        mcp_path, json={},
        headers={"Authorization": f"Bearer {_TEST_TOKEN}"}
    )
    # Protocol-level 400/406 is acceptable; the assertion is that auth passed.
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# (d) M4 token-leak pin: caplog at DEBUG; token absent from all log records
# and from 401 response bodies
# ---------------------------------------------------------------------------

def test_bearer_no_token_leak(bearer_client, mcp_path, caplog):
    """Token must never appear in log records or 401 response bodies."""
    with caplog.at_level(logging.DEBUG):
        r_health = bearer_client.get("/health")
        r_no_auth = bearer_client.post(mcp_path, json={})
        r_wrong = bearer_client.post(
            mcp_path, json={},
            headers={"Authorization": "Bearer wrong-token"}
        )
        r_correct = bearer_client.post(
            mcp_path, json={},
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"}
        )

    # Token must not appear in any log record
    for record in caplog.records:
        assert _TEST_TOKEN not in record.getMessage(), (
            f"Token leaked in log record: {record.getMessage()!r}"
        )

    # Token must not appear in 401 response bodies
    for resp in (r_no_auth, r_wrong):
        assert _TEST_TOKEN not in resp.text, (
            f"Token leaked in 401 response body: {resp.text!r}"
        )


# ---------------------------------------------------------------------------
# (e) F5 websocket rejection: RuntimeError on non-http non-lifespan scope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bearer_websocket_rejected():
    """WebSocket scopes must raise RuntimeError — never auth-exempt."""
    calls = []

    async def _inner_app(scope, receive, send):
        calls.append(scope)

    mw = BearerASGIMiddleware(_inner_app, _TEST_TOKEN)
    ws_scope = {"type": "websocket", "path": "/mcp"}

    async def _recv():
        pass

    async def _send(msg):
        pass

    with pytest.raises(RuntimeError):
        await mw(ws_scope, _recv, _send)

    assert calls == [], "Inner app must not be called for websocket scope"


# ---------------------------------------------------------------------------
# Task 3: subprocess fail-closed + serve_open tests (M2-pinned)
# ---------------------------------------------------------------------------

import socket
import subprocess
import time
import urllib.error
import urllib.request


def _free_port() -> int:
    """Bind ephemeral port 0, read the assigned port, close immediately."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listening(port: int, deadline_s: float) -> bool:
    """Poll until TCP port is accepting connections or deadline expires."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _base_env(tmp_path) -> dict:
    """Build a child-process env with kube pins and scrubbed LUMINO vars.

    Carries the test_main_entrypoint.py:54-61 import-hygiene pins so that
    server-mcp.py loaded in a child process does not hang on cluster I/O.
    """
    kubeconfig = tmp_path / "kube-config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    env = dict(os.environ)
    env["KUBECONFIG"] = str(kubeconfig)
    env["KUBEARCHIVE_ENABLED"] = "false"
    env["LUMINO_DISABLE_TELEMETRY"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env.pop("LUMINO_PROFILE", None)
    env.pop("LUMINO_CONFIG", None)
    env.pop("LUMINO_HTTP_TOKEN", None)
    # R2 hygiene: transport vars from the developer's shell must not leak into
    # child constructions (the "absent" stateless pin would inherit them).
    for _v in ("LUMINO_TRANSPORT", "LUMINO_BIND_HOST",
               "LUMINO_BIND_PORT", "LUMINO_STATELESS_HTTP"):
        env.pop(_v, None)
    # Phase 5: OTLP receiver vars — scrubbed so no token or bind address leaks
    # from the developer's shell into child processes (env hygiene, 2f F1).
    for _v in ("LUMINO_OTLP_TOKEN", "LUMINO_OTLP_BIND_HOST", "LUMINO_OTLP_BIND_PORT"):
        env.pop(_v, None)
    return env


def test_main_http_fail_closed(tmp_path):
    """Refuse to start: non-localhost bind + no token → sys.exit(1), no socket.

    M2-pinned: if resolve_http_serving's refuse branch returned 'serve_open'
    instead, this test would FAIL (server would bind and the returncode would
    be 0 or the socket would be accepting).
    """
    port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_TRANSPORT"] = "streamable-http"
    env["LUMINO_BIND_HOST"] = "0.0.0.0"
    env["LUMINO_BIND_PORT"] = str(port)

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit (fail-closed), got 0.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "refus" in combined.lower(), (
        f"Expected 'refus' in output (refuse log line); got:\n{combined}"
    )
    assert not _wait_listening(port, 1), (
        f"Port {port} is accepting connections — server should have refused to bind"
    )


def test_main_http_serve_open(tmp_path):
    """serve_open path: localhost + no token → server binds, /health returns 200.

    Uses a real socket. The process is terminated in the finally block.
    """
    port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_TRANSPORT"] = "streamable-http"
    env["LUMINO_BIND_HOST"] = "127.0.0.1"
    env["LUMINO_BIND_PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(port, 30), (
            f"Server never bound on port {port} within 30s"
        )
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=5
        ) as resp:
            assert resp.status == 200, f"Expected 200, got {resp.status}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_main_http_serve_authed(tmp_path):
    """serve_authed path: localhost + token → BearerASGIMiddleware wired by main.py.

    Probes three invariants over a real TCP socket:
    (1) GET /health with no auth → 200 (exemption holds even under authed serving).
    (2) POST /mcp with no Authorization header → 401 (middleware gates correctly).
    (3) POST /mcp with correct Bearer token → status != 401 (auth passes through).

    Mutation-verify target: if serve_authed serves the plain (unwrapped) app,
    assertion (2) fails — no-auth POST reaches the inner app and returns non-401.
    """
    port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_TRANSPORT"] = "streamable-http"
    env["LUMINO_BIND_HOST"] = "127.0.0.1"
    env["LUMINO_BIND_PORT"] = str(port)
    env["LUMINO_HTTP_TOKEN"] = "authed-wiring-token"

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(port, 30), (
            f"Server never bound on port {port} within 30s"
        )

        # (1) /health with no auth must be exempt → 200
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=5
        ) as resp:
            assert resp.status == 200, f"/health expected 200, got {resp.status}"

        # (2) POST /mcp with no Authorization → 401 (middleware short-circuits)
        mcp_url = f"http://127.0.0.1:{port}/mcp"
        req_no_auth = urllib.request.Request(
            mcp_url, data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req_no_auth, timeout=5):
                pass
            assert False, "Expected 401 for no-auth POST /mcp, got non-error response"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, (
                f"Expected 401 for no-auth POST /mcp, got {exc.code}"
            )

        # (3) POST /mcp with correct Bearer → auth passes (status != 401)
        req_authed = urllib.request.Request(
            mcp_url, data=b"{}", method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer authed-wiring-token",
            },
        )
        try:
            with urllib.request.urlopen(req_authed, timeout=5) as resp:
                authed_status = resp.status
        except urllib.error.HTTPError as exc:
            authed_status = exc.code
        assert authed_status != 401, (
            f"Expected non-401 for correctly-authed POST /mcp, got {authed_status}"
        )

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# D7 stateless_http subprocess pin (phase 2f Task 4)
#
# Uses the SUBPROCESS pattern to avoid in-process re-registration of
# server_mcp in sys.modules, which could leak a stateless-constructed module
# into other fixtures (round-1 F6).
# ---------------------------------------------------------------------------

_STATELESS_SNIPPET = (
    "import importlib.util, sys, pathlib;"
    "src = pathlib.Path('src');"
    "sys.path.insert(0, str(src));"
    "spec = importlib.util.spec_from_file_location('server_mcp', src / 'server-mcp.py');"
    "mod = importlib.util.module_from_spec(spec);"
    "sys.modules['server_mcp'] = mod;"
    "spec.loader.exec_module(mod);"
    "print(mod.mcp.settings.stateless_http)"
)


def test_stateless_http_env_true(tmp_path):
    """LUMINO_STATELESS_HTTP=true → mcp.settings.stateless_http is True.

    D7-pinned: if the construction branch does not read LUMINO_STATELESS_HTTP,
    the child prints False and the assertion fails.
    """
    env = _base_env(tmp_path)
    env["LUMINO_STATELESS_HTTP"] = "true"
    result = subprocess.run(
        [sys.executable, "-c", _STATELESS_SNIPPET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Child process failed.\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.strip() == "True", (
        f"Expected stateless_http=True, got {result.stdout.strip()!r}\n"
        f"stderr={result.stderr}"
    )


def test_stateless_http_env_absent(tmp_path):
    """No LUMINO_STATELESS_HTTP → mcp.settings.stateless_http is False (default).

    D7-pinned (construction-neutrality): the no-env path must not accidentally
    set stateless_http=True (would break today's SDK default behaviour).
    """
    env = _base_env(tmp_path)
    # _base_env does NOT set LUMINO_STATELESS_HTTP, so it is absent here.
    result = subprocess.run(
        [sys.executable, "-c", _STATELESS_SNIPPET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Child process failed.\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.strip() == "False", (
        f"Expected stateless_http=False, got {result.stdout.strip()!r}\n"
        f"stderr={result.stderr}"
    )


# ---------------------------------------------------------------------------
# Phase 5 Task 4: OTLP receiver subprocess tests (a)-(h)
# Per task-4-brief.md: fail-closed (M2), serve_open, D1 stdio+otlp, F2 hygiene,
# M7/F8 port-conflict, config errors, two-source refuse, V7 disabled-source.
# ---------------------------------------------------------------------------

import json as _json  # local alias — used for building OTLP request bodies


def _write_otlp_config(
    tmp_path,
    *,
    source_name: str = "my_otlp",
    enabled: bool = True,
    ring_capacity: int = 10,
    max_body_bytes: int = 1_048_576,
) -> Path:
    """Write a minimal lumino.yaml with one OTLP source and return its path."""
    cfg = tmp_path / "lumino.yaml"
    enabled_str = "true" if enabled else "false"
    cfg.write_text(
        f"sources:\n"
        f"  {source_name}:\n"
        f"    adapter: otlp\n"
        f"    enabled: {enabled_str}\n"
        f"    ring_capacity: {ring_capacity}\n"
        f"    max_body_bytes: {max_body_bytes}\n"
    )
    return cfg


_OTLP_MINIMAL_BODY = _json.dumps({"resourceLogs": []}).encode()


@pytest.mark.slow
def test_otlp_fail_closed_non_localhost(tmp_path):
    """(a) M2-pinned: non-localhost bind without token → exit ≠0, 'refus', port never listens.

    Mutation: if resolve_http_serving refuse→serve_open, the receiver binds and
    returncode is 0 → assertion FAILS.
    """
    cfg = _write_otlp_config(tmp_path)
    otlp_port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)
    env["LUMINO_OTLP_BIND_HOST"] = "0.0.0.0"
    env["LUMINO_OTLP_BIND_PORT"] = str(otlp_port)
    # No LUMINO_OTLP_TOKEN → resolve_http_serving("0.0.0.0", None) → "refuse"

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit (fail-closed M2), got 0.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "refus" in combined.lower(), (
        f"Expected 'refus' in output; got:\n{combined}"
    )
    assert not _wait_listening(otlp_port, 1), (
        f"Port {otlp_port} is accepting connections — receiver should have refused to bind"
    )


@pytest.mark.slow
def test_otlp_serve_open_localhost(tmp_path):
    """(b) serve_open: localhost + no token → OTLP receiver up (POST→200) + MCP /health up."""
    cfg = _write_otlp_config(tmp_path)
    otlp_port = _free_port()
    mcp_port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)
    env["LUMINO_TRANSPORT"] = "streamable-http"
    env["LUMINO_BIND_HOST"] = "127.0.0.1"
    env["LUMINO_BIND_PORT"] = str(mcp_port)
    env["LUMINO_OTLP_BIND_PORT"] = str(otlp_port)
    # LUMINO_OTLP_BIND_HOST absent → defaults to "127.0.0.1" (serve_open)

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(otlp_port, 30), (
            f"OTLP receiver never bound on port {otlp_port} within 30s"
        )
        # POST to OTLP /v1/logs → 200
        req = urllib.request.Request(
            f"http://127.0.0.1:{otlp_port}/v1/logs",
            data=_OTLP_MINIMAL_BODY,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200, f"OTLP POST expected 200, got {resp.status}"

        # MCP /health also up under streamable-http
        assert _wait_listening(mcp_port, 30), (
            f"MCP never bound on port {mcp_port} within 30s"
        )
        with urllib.request.urlopen(
            f"http://127.0.0.1:{mcp_port}/health", timeout=5
        ) as resp:
            assert resp.status == 200, f"/health expected 200, got {resp.status}"

        # MCP port must NOT route OTLP /v1/logs (rejected mount design).
        req_vlogs = urllib.request.Request(
            f"http://127.0.0.1:{mcp_port}/v1/logs",
            data=_OTLP_MINIMAL_BODY,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req_vlogs, timeout=5) as r:
                assert False, f"MCP port /v1/logs expected 404, got {r.status}"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, (
                f"MCP port /v1/logs expected 404 (rejected mount), got {exc.code}"
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.slow
def test_otlp_listens_under_stdio(tmp_path):
    """(c) D1: OTLP ingest port listens even under stdio transport."""
    cfg = _write_otlp_config(tmp_path)
    otlp_port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)
    env["LUMINO_OTLP_BIND_PORT"] = str(otlp_port)
    # No LUMINO_TRANSPORT → stdio (default)

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(otlp_port, 30), (
            f"OTLP port {otlp_port} never listened under stdio transport (D1 broken)"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.slow
def test_otlp_port_conflict_exits(tmp_path):
    """(d) M7/F8-pinned: pre-bound OTLP port → thread dies → exit 1 within deadline.

    Mutation: removing the thread.is_alive() check from the poll means the poll
    waits the full 10 s deadline before detecting failure, but still exits 1.
    The real M7 mutation is removing the poll entirely, which would let main()
    proceed and then deadlock (stdio) or start (http) — port-conflict is undetected.
    """
    cfg = _write_otlp_config(tmp_path)
    otlp_port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)
    env["LUMINO_OTLP_BIND_PORT"] = str(otlp_port)

    # Hold the port so uvicorn cannot bind; thread exits and poll detects it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", otlp_port))
        blocker.listen(1)

        _t0 = time.monotonic()
        result = subprocess.run(
            [sys.executable, "main.py"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        _elapsed = time.monotonic() - _t0

    assert result.returncode != 0, (
        f"Expected non-zero exit (port conflict / M7), got 0.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    # F8-timing pin: thread-death detection must be fast (<5s; measured ~2s).
    # Mutation: remove the ``if not otlp_thread.is_alive(): break`` line →
    # poll runs its full 10 s deadline → elapsed > 5 s → FAILS.
    assert _elapsed < 5.0, (
        f"Expected fast exit (<5 s; thread-death detection proven at ~2 s); "
        f"took {_elapsed:.2f} s"
    )


@pytest.mark.slow
def test_otlp_missing_mandatory_key_exits(tmp_path):
    """(e) Missing mandatory OTLP option → exit 1 naming the key."""
    # ring_capacity is mandatory (no default); omitting it must name it.
    cfg = tmp_path / "lumino.yaml"
    cfg.write_text(
        "sources:\n"
        "  my_otlp:\n"
        "    adapter: otlp\n"
        "    max_body_bytes: 1048576\n"
    )
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit (missing mandatory key), got 0.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "ring_capacity" in combined, (
        f"Expected 'ring_capacity' named in output; got:\n{combined}"
    )


@pytest.mark.slow
def test_otlp_two_sources_exits(tmp_path):
    """(f) Two enabled OTLP sources → exit 1."""
    cfg = tmp_path / "lumino.yaml"
    cfg.write_text(
        "sources:\n"
        "  otlp_a:\n"
        "    adapter: otlp\n"
        "    ring_capacity: 10\n"
        "    max_body_bytes: 1048576\n"
        "  otlp_b:\n"
        "    adapter: otlp\n"
        "    ring_capacity: 10\n"
        "    max_body_bytes: 1048576\n"
    )
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit (two OTLP sources), got 0.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.slow
def test_otlp_stdio_stdout_hygiene(tmp_path):
    """(g) F2-pinned: stdio + OTLP + one successful POST → child stdout clean.

    Child stdout is the JSON-RPC channel under stdio transport.  Uvicorn's
    access log handler writes to sys.stdout when access_log=True — that would
    corrupt the MCP stream.  With log_config=None and access_log=False the
    access log is disabled entirely.

    Mutation: drop access_log=False → "POST /v1/logs" appears in child stdout
    → assertion FAILS.
    """
    cfg = _write_otlp_config(tmp_path)
    otlp_port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)
    env["LUMINO_OTLP_BIND_PORT"] = str(otlp_port)
    # No LUMINO_TRANSPORT → stdio; stdout IS the JSON-RPC channel

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(otlp_port, 30), (
            f"OTLP port {otlp_port} never listened"
        )
        # One successful POST — triggers an access log entry if access_log=True
        req = urllib.request.Request(
            f"http://127.0.0.1:{otlp_port}/v1/logs",
            data=_OTLP_MINIMAL_BODY,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()

    stdout = out.decode(errors="replace")
    assert "POST /v1/logs" not in stdout, (
        f"Uvicorn access log leaked into child stdout (F2 broken); stdout:\n{stdout!r}"
    )
    # Non-discriminating defense: "Uvicorn running" goes to uvicorn.error →
    # stderr always (even with log_config=None), so this assertion is a guard
    # against mis-routing but not the primary F2 mutation killer.  The
    # "POST /v1/logs" assertion above is the discriminating one.
    assert "Uvicorn running" not in stdout, (
        f"Uvicorn startup message leaked into child stdout (F2 broken); stdout:\n{stdout!r}"
    )


@pytest.mark.slow
def test_otlp_disabled_source_no_listener(tmp_path):
    """(h) V7: otlp source with enabled: false → process starts cleanly, port never listens."""
    cfg = _write_otlp_config(tmp_path, enabled=False)
    otlp_port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)
    env["LUMINO_OTLP_BIND_PORT"] = str(otlp_port)
    # No LUMINO_TRANSPORT → stdio

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Allow startup time; verify process is alive (no error) and port is silent.
        time.sleep(2)
        assert proc.poll() is None, (
            f"Process exited unexpectedly (returncode={proc.returncode}); "
            f"disabled source must not cause exit"
        )
        assert not _wait_listening(otlp_port, 0.5), (
            f"OTLP port {otlp_port} is accepting connections — "
            f"disabled source (enabled: false) must not start a receiver"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# (MINOR-7) LUMINO_OTLP_BIND_PORT invalid value → exit 1 naming the var
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_otlp_invalid_bind_port(tmp_path):
    """(MINOR-7) LUMINO_OTLP_BIND_PORT=not-a-port → exit 1, message names the var.

    Mutation: drop the try/except ValueError around int(LUMINO_OTLP_BIND_PORT) →
    ValueError propagates, caught by main()'s outer except → exit 1 but without
    the env-var name in the message → 'LUMINO_OTLP_BIND_PORT' assertion FAILS.
    """
    cfg = _write_otlp_config(tmp_path)
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)
    env["LUMINO_OTLP_BIND_PORT"] = "not-a-port"

    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit (invalid port), got 0.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "LUMINO_OTLP_BIND_PORT" in combined, (
        f"Expected 'LUMINO_OTLP_BIND_PORT' named in output; got:\n{combined}"
    )


# ---------------------------------------------------------------------------
# (i) IMPORTANT-1: authed ingest wiring — token→None mutation kills this
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_otlp_authed_ingest_wiring(tmp_path):
    """(i) Authed ingest: LUMINO_OTLP_TOKEN wired by _start_otlp_receiver.

    Probes OTLP receiver auth over a real TCP socket:
    (1) POST /v1/logs with no Authorization → 401, WWW-Authenticate: Bearer.
    (2) POST /v1/logs with correct Bearer token → 200.

    MUTATION: replace otlp_token with None in _start_otlp_receiver (pass None
    to build_receiver_app) → bare app, no auth → (1) returns 200 instead of
    401 → FAILS on the 401 assertion.
    """
    cfg = _write_otlp_config(tmp_path)
    otlp_port = _free_port()
    env = _base_env(tmp_path)
    env["LUMINO_CONFIG"] = str(cfg)
    env["LUMINO_OTLP_BIND_HOST"] = "127.0.0.1"
    env["LUMINO_OTLP_BIND_PORT"] = str(otlp_port)
    env["LUMINO_OTLP_TOKEN"] = "otlp-wiring-token"
    env["LUMINO_TRANSPORT"] = "stdio"

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_listening(otlp_port, 30), (
            f"OTLP receiver never bound on port {otlp_port} within 30s"
        )

        # (1) POST /v1/logs with no Authorization → 401, WWW-Authenticate: Bearer
        req_no_auth = urllib.request.Request(
            f"http://127.0.0.1:{otlp_port}/v1/logs",
            data=_OTLP_MINIMAL_BODY,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req_no_auth, timeout=5):
                pass
            assert False, "Expected 401 for no-auth POST, got non-error response"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, f"Expected 401 for no-auth POST, got {exc.code}"
            www_auth = exc.headers.get("WWW-Authenticate", "")
            assert www_auth == "Bearer", (
                f"Expected WWW-Authenticate: Bearer, got {www_auth!r}"
            )

        # (2) POST /v1/logs with correct Bearer token → 200
        req_authed = urllib.request.Request(
            f"http://127.0.0.1:{otlp_port}/v1/logs",
            data=_OTLP_MINIMAL_BODY,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer otlp-wiring-token",
            },
            method="POST",
        )
        with urllib.request.urlopen(req_authed, timeout=5) as resp:
            assert resp.status == 200, f"Expected 200 for authed POST, got {resp.status}"

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# IMPORTANT-2: push→fetch ring-identity join (in-process, real uvicorn thread)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_otlp_push_to_fetch_ring_identity(tmp_path):
    """In-process: POST over real OTLP socket → fetch via OtlpLogSource → ring identity.

    Starts a real uvicorn receiver thread via _start_otlp_receiver (imported
    from main.py), posts a distinctive marker record, then fetches via the
    adapter built by ADAPTER_FACTORIES["otlp"] and asserts:

    (1) adapter._ring IS mod._otlp_rings["my_otlp"] (same object, not just equal).
    (2) The marker body appears in the fetched batch.

    MUTATION: change _start_otlp_receiver to build a LOCAL LogRing (not
    stored via setdefault into mod._otlp_rings). Under that mutant the
    identity assert (1) still PASSES — the factory's own setdefault registers
    a matching ring while the RECEIVER writes into the orphan — so the marker
    fetch (2) is the ONLY discriminating kill (verified in review; recorded
    honestly here so future maintainers don't trust (1) alone).
    """
    import asyncio
    import importlib.util as _ilu

    cfg_path = _write_otlp_config(tmp_path)
    otlp_port = _free_port()
    kube_cfg = tmp_path / "kube-config-ring"
    kube_cfg.write_text(_FAKE_KUBECONFIG)

    # Save all env keys we will touch.
    _env_keys = [
        "LUMINO_CONFIG", "LUMINO_OTLP_BIND_HOST", "LUMINO_OTLP_BIND_PORT",
        "LUMINO_OTLP_TOKEN", "KUBECONFIG", "KUBEARCHIVE_ENABLED",
        "LUMINO_DISABLE_TELEMETRY",
    ]
    _saved = {k: os.environ.get(k) for k in _env_keys}

    # Set env for both module loading (reads LUMINO_CONFIG at import time)
    # and for _start_otlp_receiver (reads OTLP vars at call time).
    os.environ["LUMINO_CONFIG"] = str(cfg_path)
    os.environ["KUBECONFIG"] = str(kube_cfg)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ["LUMINO_DISABLE_TELEMETRY"] = "1"
    os.environ["LUMINO_OTLP_BIND_HOST"] = "127.0.0.1"
    os.environ["LUMINO_OTLP_BIND_PORT"] = str(otlp_port)
    os.environ.pop("LUMINO_OTLP_TOKEN", None)

    # Use unique sys.modules keys to avoid collision with the module fixture.
    _srv_key = f"server_mcp_ring_id_{otlp_port}"
    _main_key = f"lumino_main_{otlp_port}"
    otlp_server = None

    try:
        # Load a fresh server module with the OTLP config.
        srv_spec = _ilu.spec_from_file_location(_srv_key, SRC / "server-mcp.py")
        mod = _ilu.module_from_spec(srv_spec)
        sys.modules[_srv_key] = mod
        srv_spec.loader.exec_module(mod)

        # Load _start_otlp_receiver from main.py.
        main_spec = _ilu.spec_from_file_location(_main_key, REPO_ROOT / "main.py")
        main_mod = _ilu.module_from_spec(main_spec)
        main_spec.loader.exec_module(main_mod)

        # Start the OTLP receiver (real uvicorn daemon thread).
        otlp_server = main_mod._start_otlp_receiver(mod)
        assert otlp_server is not None, (
            "_start_otlp_receiver must return a Server when OTLP source is configured"
        )
        assert otlp_server.started, "OTLP server should be started after _start_otlp_receiver"

        # POST a distinctive marker record over the real socket.
        _marker = "push-to-fetch-ring-identity-marker-abc123"
        body = _json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": [
            {"body": {"stringValue": _marker}}
        ]}]}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{otlp_port}/v1/logs",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200, f"OTLP POST expected 200, got {resp.status}"

        # Build the adapter via ADAPTER_FACTORIES (mirrors _get_adapter_instance).
        sc = mod._lumino_config.sources["my_otlp"]
        adapter = mod.ADAPTER_FACTORIES["otlp"]("my_otlp", sc)

        from core.selector import Entity

        batch = asyncio.run(adapter.fetch_logs(Entity("*"), None, None))

        # (1) Ring identity: adapter must share the same ring object as the receiver.
        assert adapter._ring is mod._otlp_rings["my_otlp"], (
            "adapter._ring must be the same object as mod._otlp_rings['my_otlp'] "
            "(push-to-fetch ring identity broken)"
        )

        # (2) Marker record must be fetchable via the shared ring.
        bodies = [r.body for r in batch.records]
        assert _marker in bodies, (
            f"Marker {_marker!r} not found in fetched batch; "
            f"got bodies: {bodies!r}"
        )

    finally:
        if otlp_server is not None:
            otlp_server.should_exit = True
        sys.modules.pop(_srv_key, None)
        sys.modules.pop(_main_key, None)
        # Restore env.
        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
