"""OTLP receiver DNS-rebinding protection (H22).

The OTLP port served without a token on loopback accepted any Host header, so a
web page that rebinds its own name to 127.0.0.1 could POST forged log records
(prompt injection into LLM summaries). The MCP port already rejects foreign
Host headers through FastMCP; the OTLP port had no such check.
"""
import asyncio
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.http_transport import LoopbackHostASGIMiddleware, _host_is_loopback  # noqa: E402


def _client():
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/v1/logs", ok, methods=["POST"])])
    return TestClient(LoopbackHostASGIMiddleware(app))


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:4318", "localhost", "localhost:4318", "LOCALHOST:4318",
    "[::1]", "[::1]:4318",
])
def test_loopback_hosts_pass(host):
    resp = _client().post("/v1/logs", headers={"Host": host})
    assert resp.status_code == 200, host


@pytest.mark.parametrize("host", [
    "attacker.example", "attacker.example:4318", "127.0.0.1.attacker.example",
    "localhost.attacker.example:4318", "10.0.0.5:4318", "[::2]:4318", "",
    "127.0.0.1:4318@attacker.example", "localhost.", "127.1",
])
def test_foreign_hosts_are_rejected(host):
    resp = _client().post("/v1/logs", headers={"Host": host})
    assert resp.status_code == 403, host
    assert resp.json() == {"error": "forbidden host"}


def test_missing_host_header_is_rejected():
    received = []

    async def app(scope, receive, send):
        received.append(scope)

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/v1/logs", "headers": []}
    asyncio.run(LoopbackHostASGIMiddleware(app)(scope, receive, send))
    assert received == []
    assert sent[0]["status"] == 403


def test_lifespan_passes_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(LoopbackHostASGIMiddleware(app)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_non_ascii_port_digits_are_rejected():
    assert not _host_is_loopback("localhost:\u00b2")       # superscript two
    assert not _host_is_loopback("127.0.0.1:\u0663")       # Arabic-Indic three
    assert _host_is_loopback("127.0.0.1:4318")
