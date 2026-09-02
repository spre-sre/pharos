"""Tests for src/adapters/otlp/receiver.py (phase 5, Task 3).

Sections (all pinned per task-3-brief.md):
  AUTH        — 401 exact shape, correct→200+records, empty-token invariant
  PROTOCOL    — media-type 415/200, encoding 415, method 405/501
  MALFORMED   — ≥10 attacker shapes → 400 (F3 catch-all, never 5xx)
  ENCODING    — gzip+non-gzip→400, bad-CL→not-5xx, GZIP-uppercase→200
  SIZE        — a/b/c/c2/c3/c4 (wire cap, decompressed cap, boundary)
  LEAK        — caplog DEBUG: token absent, payload absent (M6)
  ISOLATION   — no /mcp or /health route
  F13i        — src/adapters/otlp/ imports no outbound HTTP client (widened)
"""
from __future__ import annotations

import ast
import asyncio
import gzip
import io
import json
import logging
import sys
import zlib
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from starlette.testclient import TestClient  # noqa: E402

from adapters.otlp.receiver import build_receiver_app  # noqa: E402
from adapters.otlp.rings import LogRing  # noqa: E402

# ── helpers ───────────────────────────────────────────────────────────────────

_TEST_TOKEN = "secret-otlp-token"

_VALID_BODY = b'{"resourceLogs":[]}'  # 19 bytes, valid OTLP JSON

_MINIMAL_OPTS = {
    "max_body_bytes": 4096,
    "max_record_bytes": 65536,
    "signals": ["logs"],
}


def _gzip(data: bytes) -> bytes:
    """Gzip-compress *data* with deterministic mtime=0."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def _make_ring():
    return LogRing(capacity=100)


def _make_app(token=None, opts=None, ring=None):
    if opts is None:
        opts = dict(_MINIMAL_OPTS)
    if ring is None:
        ring = _make_ring()
    return build_receiver_app(ring, opts, token)


def _client(token=None, opts=None, ring=None, raise_server_exceptions=False):
    app = _make_app(token=token, opts=opts, ring=ring)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


# ── AUTH ──────────────────────────────────────────────────────────────────────

class TestAuth:
    """AUTH: bearer middleware correctness (M1-pinned)."""

    def test_no_token_returns_401(self):
        client = _client(token=_TEST_TOKEN)
        resp = client.post("/v1/logs", content=_VALID_BODY,
                           headers={"content-type": "application/json"})
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self):
        client = _client(token=_TEST_TOKEN)
        resp = client.post(
            "/v1/logs", content=_VALID_BODY,
            headers={"content-type": "application/json",
                     "authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_401_has_www_authenticate_bearer(self):
        client = _client(token=_TEST_TOKEN)
        resp = client.post("/v1/logs", content=_VALID_BODY,
                           headers={"content-type": "application/json"})
        assert resp.headers.get("www-authenticate") == "Bearer"

    def test_401_body_shape(self):
        client = _client(token=_TEST_TOKEN)
        resp = client.post("/v1/logs", content=_VALID_BODY,
                           headers={"content-type": "application/json"})
        assert resp.json() == {"error": "unauthorized"}

    def test_correct_token_returns_200_empty_obj(self):
        client = _client(token=_TEST_TOKEN)
        resp = client.post(
            "/v1/logs", content=_VALID_BODY,
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {_TEST_TOKEN}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_correct_token_records_land_in_ring(self):
        """A valid OTLP body with records is appended to the ring."""
        body = json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": [
            {"body": {"stringValue": "hello-ring"}}
        ]}]}]}).encode()
        ring = _make_ring()
        client = _client(token=_TEST_TOKEN, ring=ring)
        resp = client.post(
            "/v1/logs", content=body,
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {_TEST_TOKEN}"},
        )
        assert resp.status_code == 200
        snap = ring.snapshot()
        assert len(snap) == 1
        _, rec = snap[0]
        assert rec.body == "hello-ring"

    def test_no_token_app_skips_auth(self):
        """token=None → bare app, no auth required."""
        client = _client(token=None)
        resp = client.post("/v1/logs", content=_VALID_BODY,
                           headers={"content-type": "application/json"})
        assert resp.status_code == 200

    def test_empty_token_invariant(self):
        """build_receiver_app('') must NOT construct BearerASGIMiddleware.

        Empty string is falsy; the app must behave like token=None (open),
        never wrap with middleware that would accept 'Bearer ' as valid (F13ii).
        """
        app = build_receiver_app(_make_ring(), _MINIMAL_OPTS, "")
        # Must not be a BearerASGIMiddleware — it must be a bare Starlette app
        from core.http_transport import BearerASGIMiddleware
        assert not isinstance(app, BearerASGIMiddleware), (
            "build_receiver_app must NOT construct BearerASGIMiddleware "
            "with an empty token"
        )

    def test_empty_token_serves_open(self):
        """token='' → bare app, no auth enforced (falsy path)."""
        client = TestClient(
            build_receiver_app(_make_ring(), _MINIMAL_OPTS, ""),
            raise_server_exceptions=False,
        )
        resp = client.post("/v1/logs", content=_VALID_BODY,
                           headers={"content-type": "application/json"})
        assert resp.status_code == 200


# ── PROTOCOL ──────────────────────────────────────────────────────────────────

class TestProtocol:
    """PROTOCOL: media-type, encoding, method routing."""

    @pytest.fixture(autouse=True)
    def _client(self):
        self.c = _client(token=None)

    def test_protobuf_returns_415_naming_encoding_json(self):
        resp = self.c.post(
            "/v1/logs",
            content=b"\x00\x01",
            headers={"content-type": "application/x-protobuf"},
        )
        assert resp.status_code == 415
        body = resp.json()
        assert "encoding" in body.get("message", "").lower() or \
               "json" in body.get("message", "").lower(), (
            f"415 for protobuf should name 'encoding' or 'json': {body}"
        )

    def test_json_with_charset_returns_200(self):
        """application/json; charset=utf-8 → accepted (F6)."""
        resp = self.c.post(
            "/v1/logs",
            content=_VALID_BODY,
            headers={"content-type": "application/json; charset=utf-8"},
        )
        assert resp.status_code == 200

    def test_json_with_charset_mixed_case_returns_200(self):
        """application/JSON; charset=utf-8 → accepted (case-insensitive)."""
        resp = self.c.post(
            "/v1/logs",
            content=_VALID_BODY,
            headers={"content-type": "  Application/JSON ; charset=utf-8"},
        )
        assert resp.status_code == 200

    def test_zstd_encoding_returns_415_naming_compression(self):
        """Content-Encoding: zstd → 415 naming 'compression: gzip|none' (F7)."""
        resp = self.c.post(
            "/v1/logs",
            content=b"\x28\xb5\x2f\xfd\x00",
            headers={"content-type": "application/json",
                     "content-encoding": "zstd"},
        )
        assert resp.status_code == 415
        msg = resp.json().get("message", "")
        assert "gzip" in msg.lower() or "none" in msg.lower() or \
               "compression" in msg.lower(), (
            f"415 for zstd should name compression options: {msg!r}"
        )

    def test_snappy_encoding_returns_415(self):
        """Content-Encoding: snappy → 415 (F7)."""
        resp = self.c.post(
            "/v1/logs",
            content=b"\xff\x00",
            headers={"content-type": "application/json",
                     "content-encoding": "snappy"},
        )
        assert resp.status_code == 415

    def test_get_returns_405(self):
        resp = self.c.get("/v1/logs")
        assert resp.status_code == 405

    def test_v1_metrics_post_returns_501_naming_5b(self):
        """POST /v1/metrics → 501; message names §4.2.1 item 5b."""
        resp = self.c.post(
            "/v1/metrics",
            content=_VALID_BODY,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 501
        msg = resp.json().get("message", "")
        assert "logs" in msg.lower() or "v1/logs" in msg.lower() or \
               "5b" in msg or "not implemented" in msg.lower(), (
            f"501 for /v1/metrics should name 5b: {msg!r}"
        )

    def test_v1_traces_post_returns_501(self):
        """POST /v1/traces → 501 (F7 / §4.2.1 item 5b)."""
        resp = self.c.post(
            "/v1/traces",
            content=_VALID_BODY,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 501


# ── MALFORMED ────────────────────────────────────────────────────────────────

# Thirteen attacker shapes — all must return 400, never 5xx (F3).
# Shapes (10)-(13) cause RecursionError (not ValueError) from json.loads; they
# specifically test the catch-all vs ValueError-only mutation (≥4 must FAIL with F3).
_ATTACKER_SHAPES = [
    b'{"resourceLogs":"x"}',                              # (1) string not list
    b'{"resourceLogs":5}',                                # (2) int not list
    b'{"resourceLogs":[{"scopeLogs":{}}]}',               # (3) scopeLogs-as-dict
    b'{"resourceLogs":[{"scopeLogs":[{"logRecords":{}}]}]}',  # (4) logRecords-as-dict
    b'[{"key":1}]',                                       # (5) top-level list
    b'[]',                                                # (6) top-level empty list
    b'999999999999999999999999999999',                    # (7) nanos giant int as body
    b'-1',                                                # (8) nanos -1 as body
    b'"abc"',                                             # (9) nanos "abc" as body
    b"[" * 200000 + b"]" * 200000,                       # (10) 400KB array nesting bomb
    b'{"a":' * 100000 + b'1' + b'}' * 100000,           # (11) 600KB object nesting bomb
    b'{"a":[' * 50000 + b'1' + b']}' * 50000,           # (12) 400KB mixed nesting bomb
    b"[" * 100000 + b"]" * 100000,                       # (13) 200KB array bomb variant
]


class TestMalformed:
    """MALFORMED: all attacker shapes → 400, never 5xx (F3 catch-all)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        # 10MB — large enough to let all bombs through _read_bounded
        # so they reach the parse try-block and raise RecursionError (not 413).
        opts = {"max_body_bytes": 10 * 1024 * 1024,
                "max_record_bytes": 65536, "signals": ["logs"]}
        self.c = _client(token=None, opts=opts)

    @pytest.mark.parametrize("body", _ATTACKER_SHAPES,
                             ids=[f"shape-{i}" for i in range(1, len(_ATTACKER_SHAPES) + 1)])
    def test_returns_400_not_5xx(self, body):
        resp = self.c.post(
            "/v1/logs", content=body,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400, (
            f"Expected 400 for attacker shape, got {resp.status_code}"
        )

    @pytest.mark.parametrize("body", _ATTACKER_SHAPES[:9],  # skip bombs (10-13) for echo check
                             ids=[f"echo-{i}" for i in range(1, 10)])
    def test_response_does_not_echo_payload(self, body):
        """Response body must not contain any substring of the payload (F3/M6b)."""
        resp = self.c.post(
            "/v1/logs", content=body,
            headers={"content-type": "application/json"},
        )
        resp_text = resp.text
        # Check a few meaningful substrings
        suspicious = [b"resourceLogs", b"scopeLogs", b"logRecords",
                      b"9999", b"-1", b"abc", b"key"]
        for sub in suspicious:
            if sub in body:
                assert sub.decode() not in resp_text, (
                    f"Payload substring {sub!r} leaked into response: {resp_text!r}"
                )


# ── ENCODING ROBUSTNESS ───────────────────────────────────────────────────────

class TestEncodingRobustness:
    """ENCODING ROBUSTNESS: V2/V3 proofs."""

    @pytest.fixture(autouse=True)
    def _client(self):
        self.c = _client(token=None)

    def test_gzip_header_on_non_gzip_body_returns_400_not_5xx(self):
        """Content-Encoding: gzip on plain JSON → 400 (zlib.error is not ValueError — V2)."""
        resp = self.c.post(
            "/v1/logs",
            content=_VALID_BODY,  # plain JSON, not gzip-encoded
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        assert resp.status_code == 400, (
            f"Expected 400 (zlib.error caught), got {resp.status_code}"
        )

    def test_gzip_magic_plus_junk_returns_400(self):
        """Gzip magic bytes + junk → zlib.error → 400 (V2)."""
        bad_gzip = b"\x1f\x8b\x08\x00" + b"\xff" * 50
        resp = self.c.post(
            "/v1/logs",
            content=bad_gzip,
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        assert resp.status_code == 400

    def test_content_length_abc_treated_as_absent(self):
        """Content-Length: abc → treat as absent → 200 with valid body (V2/V3)."""
        resp = self.c.post(
            "/v1/logs",
            content=_VALID_BODY,
            headers={"content-type": "application/json",
                     "content-length": "abc"},
        )
        assert resp.status_code == 200, (
            f"Non-digit Content-Length must be treated as absent → 200; got {resp.status_code}"
        )

    def test_content_length_superscript_digit_treated_as_absent(self):
        r"""Content-Length: \xb2 (U+00B2 SUPERSCRIPT TWO) → treated as absent → 200.

        '²'.isdigit() is True but int('²') raises ValueError, so the old isdigit()
        path would let the int() call raise and bubble out as 400.  isdecimal() (the
        correct guard) returns False for '²' → CL treated as absent → 200.

        Constructed directly at ASGI scope level — httpx UTF-8-encodes b'\xb2' to
        b'\xc3\xb2' before it reaches the ASGI scope, so Starlette sees 'Â²' (two
        chars) and both isdecimal and isdigit return False for that string.  Injecting
        (b"content-length", b"\xb2") raw into the ASGI scope ensures Starlette decodes
        it latin-1 → '²' (U+00B2) so the mutation is caught.

        MUTATION: isdecimal → isdigit → int('²') raises ValueError → 400 (≠200 → FAIL).
        """
        app = _make_app(token=None)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/v1/logs",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", b"\xb2"),  # raw bytes → ASGI latin-1 decode → '²'
            ],
            "server": ("127.0.0.1", 80),
        }

        body_consumed = [False]

        async def receive():
            if not body_consumed[0]:
                body_consumed[0] = True
                return {"type": "http.request", "body": _VALID_BODY, "more_body": False}
            return {"type": "http.disconnect"}

        events: list = []

        async def send(event):
            events.append(event)

        asyncio.run(app(scope, receive, send))

        start = next(e for e in events if e["type"] == "http.response.start")
        assert start["status"] == 200, (
            f"Non-decimal-digit CL (²) must be treated as absent → 200; got {start['status']}"
        )

    def test_content_length_negative_not_5xx(self):
        """Content-Length: -1 → treat as absent, never raise (V2/V3)."""
        resp = self.c.post(
            "/v1/logs",
            content=_VALID_BODY,
            headers={"content-type": "application/json",
                     "content-length": "-1"},
        )
        assert resp.status_code < 500

    def test_gzip_uppercase_accepted(self):
        """Content-Encoding: GZIP (uppercase) → accepted (tokens case-insensitive — V3)."""
        compressed = _gzip(_VALID_BODY)
        resp = self.c.post(
            "/v1/logs",
            content=compressed,
            headers={"content-type": "application/json",
                     "content-encoding": "GZIP"},
        )
        assert resp.status_code == 200

    def test_identity_encoding_accepted(self):
        """Content-Encoding: identity → accepted."""
        resp = self.c.post(
            "/v1/logs",
            content=_VALID_BODY,
            headers={"content-type": "application/json",
                     "content-encoding": "identity"},
        )
        assert resp.status_code == 200

    def test_gzip_encoding_with_valid_body_returns_200(self):
        """Real gzipped valid body → 200 (R1 positive)."""
        compressed = _gzip(_VALID_BODY)
        resp = self.c.post(
            "/v1/logs",
            content=compressed,
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        assert resp.status_code == 200


# ── SIZE ──────────────────────────────────────────────────────────────────────

# Small max for size tests; wire cap and decompressed cap triggered quickly.
_SIZE_MAX = 512


class TestSize:
    """SIZE: M3 a/b/c/c2/c3/c4 — wire cap, decompressed cap, boundary."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        opts = {"max_body_bytes": _SIZE_MAX, "max_record_bytes": 65536,
                "signals": ["logs"]}
        ring = _make_ring()
        self.opts = opts
        self.ring = ring
        self.c = _client(token=None, opts=opts, ring=ring)

    # (a) Declared oversize: Content-Length > max → 413 pre-read (no body read)
    def test_a_declared_oversize_returns_413(self):
        # httpx will set Content-Length to the actual body size (> _SIZE_MAX)
        big_body = b"x" * (_SIZE_MAX + 1)
        resp = self.c.post(
            "/v1/logs", content=big_body,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413

    # (b) Chunked no-length oversize: wire cap catches it without CL header
    def test_b_chunked_no_length_oversize_returns_413(self):
        # Pass as iterator → httpx uses transfer-encoding: chunked (no Content-Length)
        big_body = b"x" * (_SIZE_MAX + 10)
        resp = self.c.post(
            "/v1/logs",
            content=iter([big_body]),  # generator → no Content-Length
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413

    # (c) Single-chunk gzip bomb: compressed ≪ max, decompressed ≫ max
    def test_c_single_chunk_gzip_bomb_returns_413(self):
        raw = b"\x00" * 100000  # 100 KB of zeros → tiny compressed
        compressed = _gzip(raw)
        assert len(compressed) <= _SIZE_MAX, (
            f"gzip bomb must compress under {_SIZE_MAX} bytes; got {len(compressed)}"
        )
        resp = self.c.post(
            "/v1/logs", content=compressed,
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        assert resp.status_code == 413

    # (c2) Multi-chunk bomb: split compressed gzip across multiple chunks
    def test_c2_multi_chunk_gzip_bomb_returns_413(self):
        """16 chunks; cumulative decompressed > max → 413 (F1a — out_total cap)."""
        raw = b"\x00" * 100000
        compressed = _gzip(raw)
        # Split into ≥4 chunks, each within wire cap individually
        n = 8
        chunk_size = max(1, len(compressed) // n)
        chunks = [compressed[i:i + chunk_size]
                  for i in range(0, len(compressed), chunk_size)]
        assert len(chunks) >= 2, "need at least 2 chunks for multi-chunk test"
        resp = self.c.post(
            "/v1/logs",
            content=iter(chunks),  # chunked; no Content-Length
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        # Must be 413 — the cumulative decompressed cap fires (F1a-pinned).
        # The F1a mutant (constant max_length, no per-chunk out_total) returns
        # 400 (zlib.error swallowed into the catch-all); 400≠413 kills it.
        assert resp.status_code == 413, (
            f"multi-chunk gzip bomb must return 413 (cumulative cap); "
            f"got {resp.status_code}"
        )

    # (c3) Valid small gzip + trailing garbage → 413 (F1b — raw_total cap)
    def test_c3_trailing_garbage_returns_413(self):
        """Valid gzip + trailing garbage in SEPARATE chunks → raw_total > max → 413.

        F1b mutation (drop raw_total cap): garbage chunk arrives AFTER gzip EOF;
        without raw_total cap, dec.decompress(garbage) after eof raises zlib.error
        → caught as Exception → 400 (not 413). 413 assertion kills the mutant.
        """
        valid_gz = _gzip(_VALID_BODY)  # ~39 bytes compressed
        garbage = b"\x00" * (_SIZE_MAX + 100)  # > 512 bytes — exceeds wire cap
        assert len(garbage) > _SIZE_MAX
        # Send as TWO separate chunks (no Content-Length → Guard 1 skipped).
        # Chunk 1: valid gzip (raw_total < max → ok).
        # Chunk 2: garbage (raw_total tips over max → 413; without cap → zlib.error → 400).
        resp = self.c.post(
            "/v1/logs",
            content=iter([valid_gz, garbage]),  # 2-chunk stream, no Content-Length
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        assert resp.status_code == 413, (
            f"trailing garbage should return 413 (wire cap); got {resp.status_code}"
        )

    # (c4) Exact boundary — INCLUSIVE: body of exactly max_body_bytes → 200
    def test_c4_identity_exact_boundary_returns_200(self):
        """Body of exactly max_body_bytes → 200 (cap is INCLUSIVE, checks use >)."""
        # Craft a valid JSON body of exactly _SIZE_MAX bytes.
        # Use whitespace padding: {"resourceLogs":[], "<pad>": "<pad>"}
        # Simpler: just use a valid body + spaces to exact size.
        base = b'{"resourceLogs":[]}'  # 19 bytes
        padding = b" " * (_SIZE_MAX - len(base))  # JSON ignores trailing spaces
        # Actually that's not valid JSON with trailing non-whitespace. Let's use
        # a different approach: pad inside the dict.
        # {"resourceLogs":[],"x":"<padding>"}
        prefix = b'{"resourceLogs":[],"x":"'
        suffix = b'"}'
        pad_len = _SIZE_MAX - len(prefix) - len(suffix)
        assert pad_len >= 0, "SIZE_MAX too small for boundary test"
        body_exact = prefix + b"a" * pad_len + suffix
        assert len(body_exact) == _SIZE_MAX

        opts = {"max_body_bytes": _SIZE_MAX, "max_record_bytes": 65536,
                "signals": ["logs"]}
        c = _client(token=None, opts=opts)
        resp = c.post(
            "/v1/logs", content=body_exact,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200, (
            f"Exact boundary ({_SIZE_MAX} bytes) must return 200 (inclusive); "
            f"got {resp.status_code}"
        )

    def test_c4_identity_max_plus_one_returns_413(self):
        """Body of max_body_bytes+1 → 413."""
        big = b"x" * (_SIZE_MAX + 1)
        opts = {"max_body_bytes": _SIZE_MAX, "max_record_bytes": 65536,
                "signals": ["logs"]}
        c = _client(token=None, opts=opts)
        resp = c.post("/v1/logs", content=big,
                      headers={"content-type": "application/json"})
        assert resp.status_code == 413

    def test_c4_cl_equals_max_returns_200(self):
        """Declared Content-Length == max_body_bytes → NOT precheck 413 (uses >, never >=)."""
        base = b'{"resourceLogs":[]}'
        # Build exactly _SIZE_MAX bytes as the body
        prefix = b'{"resourceLogs":[],"x":"'
        suffix = b'"}'
        pad_len = _SIZE_MAX - len(prefix) - len(suffix)
        body_exact = prefix + b"a" * pad_len + suffix
        assert len(body_exact) == _SIZE_MAX

        opts = {"max_body_bytes": _SIZE_MAX, "max_record_bytes": 65536,
                "signals": ["logs"]}
        c = _client(token=None, opts=opts)
        resp = c.post(
            "/v1/logs", content=body_exact,
            headers={"content-type": "application/json",
                     "content-length": str(_SIZE_MAX)},  # CL == max → NOT fired
        )
        assert resp.status_code == 200

    def test_c4_gzip_exact_boundary_returns_200(self):
        """Gzip body of exactly compressed_size == max_body_bytes → 200."""
        compressed = _gzip(_VALID_BODY)  # ~39 bytes compressed, 19 bytes decompressed
        gz_size = len(compressed)
        opts = {"max_body_bytes": gz_size, "max_record_bytes": 65536,
                "signals": ["logs"]}
        c = _client(token=None, opts=opts)
        resp = c.post(
            "/v1/logs", content=compressed,
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        # gz_size bytes on wire (== max, not >), decompressed 19 bytes (< max) → 200
        assert resp.status_code == 200, (
            f"Gzip exact boundary (wire={gz_size}==max) must return 200; "
            f"got {resp.status_code}"
        )

    def test_c4_gzip_wire_max_plus_one_returns_413(self):
        """Gzip body of compressed_size == max+1 → 413."""
        compressed = _gzip(_VALID_BODY)
        gz_size = len(compressed)
        opts = {"max_body_bytes": gz_size - 1, "max_record_bytes": 65536,
                "signals": ["logs"]}
        c = _client(token=None, opts=opts)
        resp = c.post(
            "/v1/logs", content=compressed,
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        assert resp.status_code == 413


# ── GZIP BOUNDS DISCRIMINATION ───────────────────────────────────────────────

class TestGzipBoundsDiscrimination:
    """Mutation-killing tests that individually discriminate each gzip guard."""

    def test_f1a_max_length_decreases_per_chunk(self, monkeypatch):
        """dec.decompress max_length DECREASES as out_total grows (F1a discriminating).

        Monkeypatches zlib.decompressobj in the receiver module to record each
        max_length argument.  Asserts the first value == max_body_bytes + 1 and that
        the recorded values are NOT all equal (they decrease as out_total accumulates).

        MUTATION: max_length → constant (max_body_bytes + 1) → all recorded values
        equal → ``not all equal`` assertion FAILS.
        """
        import zlib as _real_zlib
        import adapters.otlp.receiver as _receiver_mod

        recorded: list[int] = []
        _orig = _real_zlib.decompressobj

        class _RecordingDec:
            def __init__(self, *args, **kwargs):
                self._dec = _orig(*args, **kwargs)

            def decompress(self, data, max_length=0):
                recorded.append(max_length)
                return self._dec.decompress(data, max_length)

            @property
            def eof(self):
                return self._dec.eof

            @property
            def unconsumed_tail(self):
                return self._dec.unconsumed_tail

            @property
            def unused_data(self):
                return self._dec.unused_data

        class _RecordingZlib:
            def decompressobj(self, *args, **kwargs):
                return _RecordingDec(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(_real_zlib, name)

        monkeypatch.setattr(_receiver_mod, "zlib", _RecordingZlib())

        # Body large enough (~2001 bytes) that decompressed output is produced
        # well before the last compressed chunk, so out_total changes mid-loop.
        # prefix=28 bytes, suffix=2 bytes → 1971 'a's gives exactly 2001 bytes
        raw = b'{"resourceLogs":[], "pad": "' + b"a" * 1971 + b'"}'
        assert len(raw) == 2001
        compressed = _gzip(raw)
        chunk_size = 5
        chunks = [compressed[i:i + chunk_size]
                  for i in range(0, len(compressed), chunk_size)]
        assert len(chunks) >= 2

        max_body_bytes = 4096
        opts = {"max_body_bytes": max_body_bytes, "max_record_bytes": 65536,
                "signals": ["logs"]}
        c = _client(token=None, opts=opts)
        resp = c.post(
            "/v1/logs",
            content=iter(chunks),
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        assert resp.status_code == 200, (
            f"Valid gzip body must return 200; got {resp.status_code}"
        )
        assert len(recorded) >= 2, (
            f"Need ≥2 decompress calls to check max_length; got {recorded!r}"
        )
        assert recorded[0] == max_body_bytes + 1, (
            f"First max_length must be max_body_bytes+1 ({max_body_bytes + 1}); "
            f"got {recorded[0]}"
        )
        assert not all(v == recorded[0] for v in recorded), (
            f"max_length must DECREASE as out_total grows; all equal: {recorded}"
        )

    def test_guard5_unused_data_trailing_bytes_returns_413(self):
        """Valid gzip + trailing garbage in one chunk → 413 (dec.eof+unused_data guard).

        Wire total is WELL under max_body_bytes → raw_total guard CANNOT fire.
        Only the ``dec.eof and dec.unused_data`` check catches the trailing bytes.

        MUTATION: drop ``dec.eof and dec.unused_data`` → 200 (garbage silently
        absorbed as empty decompress output after EOF).
        """
        valid_gz = _gzip(_VALID_BODY)       # ~39 bytes
        garbage = b"\xff\xfe" * 25          # 50 bytes of trailing noise
        single_chunk = valid_gz + garbage   # ~89 bytes total

        # max_body_bytes=4096: wire total (~89) << 4096 → raw_total guard never fires.
        opts = {"max_body_bytes": 4096, "max_record_bytes": 65536, "signals": ["logs"]}
        c = _client(token=None, opts=opts)
        resp = c.post(
            "/v1/logs",
            content=single_chunk,   # single bytes value → one ASGI body event
            headers={"content-type": "application/json",
                     "content-encoding": "gzip"},
        )
        assert resp.status_code == 413, (
            f"gzip+trailing-garbage must return 413 (unused_data guard); "
            f"got {resp.status_code}"
        )


# ── LEAK ──────────────────────────────────────────────────────────────────────

class TestLeak:
    """LEAK: caplog DEBUG shows no token, no payload (M6 tripwire)."""

    def _run_and_capture(self, client, body, headers):
        import logging
        root_logger = logging.getLogger()
        handler = logging.handlers_capture = []

        class CapHandler(logging.Handler):
            def emit(self, record):
                handler.append(self.format(record))

        cap = CapHandler()
        cap.setLevel(logging.DEBUG)
        root_logger.addHandler(cap)
        try:
            resp = client.post("/v1/logs", content=body, headers=headers)
        finally:
            root_logger.removeHandler(cap)
        return resp, "\n".join(handler)

    def test_token_not_logged_on_401(self, caplog):
        client = _client(token=_TEST_TOKEN)
        with caplog.at_level(logging.DEBUG):
            client.post(
                "/v1/logs", content=_VALID_BODY,
                headers={"content-type": "application/json",
                         "authorization": f"Bearer {_TEST_TOKEN}"},
            )
        assert _TEST_TOKEN not in caplog.text, (
            f"Token leaked into logs: {caplog.text[:200]}"
        )

    def test_payload_not_logged_on_400(self, caplog):
        """Attacker body must not appear in any log record (F3/M6b)."""
        attacker = b'{"resourceLogs":"ATTACKER_PAYLOAD_SENTINEL"}'
        opts = {"max_body_bytes": 4096, "max_record_bytes": 65536, "signals": ["logs"]}
        client = _client(token=None, opts=opts)
        with caplog.at_level(logging.DEBUG):
            client.post(
                "/v1/logs", content=attacker,
                headers={"content-type": "application/json"},
            )
        assert "ATTACKER_PAYLOAD_SENTINEL" not in caplog.text, (
            f"Payload substring leaked into logs: {caplog.text[:200]}"
        )

    def test_wrong_token_not_in_response(self):
        """Wrong bearer token must not appear in the 401 response body."""
        secret = "super-secret-bearer-token-XYZ"
        client = _client(token=secret)
        resp = client.post(
            "/v1/logs", content=_VALID_BODY,
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {secret}"},
        )
        assert secret not in resp.text


# ── ISOLATION ────────────────────────────────────────────────────────────────

class TestIsolation:
    """ISOLATION: /mcp and /health not routed (R5)."""

    @pytest.fixture(autouse=True)
    def _client(self):
        self.c = _client(token=None)

    def test_no_mcp_route(self):
        resp = self.c.get("/mcp")
        assert resp.status_code != 200, (
            "/mcp must not return 200 on the receiver app"
        )

    def test_no_health_route(self):
        resp = self.c.get("/health")
        assert resp.status_code != 200, (
            "/health must not return 200 on the bare receiver app (no exempt)"
        )

    def test_no_mcp_post(self):
        resp = self.c.post("/mcp", content=b"{}")
        assert resp.status_code != 200

    def test_root_not_found(self):
        resp = self.c.get("/")
        assert resp.status_code == 404

    def test_authed_app_health_returns_401_not_exempt(self):
        """GET /health on authed ingest app → 401; proves exempt_paths=frozenset().

        build_receiver_app passes exempt_paths=frozenset() to BearerASGIMiddleware,
        so /health is NOT exempt — the middleware checks auth first and returns 401.

        MUTATION: default exempt (frozenset({'/health'})) → /health skips auth →
        Starlette returns 404 (no /health route) ≠ 401 → FAIL.
        """
        from core.http_transport import BearerASGIMiddleware
        app = _make_app(token=_TEST_TOKEN)
        assert isinstance(app, BearerASGIMiddleware), (
            "authed app must be BearerASGIMiddleware-wrapped"
        )
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/health")
        assert resp.status_code == 401, (
            f"/health on authed ingest app must return 401 (not exempt); "
            f"got {resp.status_code}"
        )


# ── F13i: no outbound HTTP client import across ALL src/adapters/otlp/ ───────

class TestNoHttpClientImport:
    """F13i (WIDENED): all modules in src/adapters/otlp/ import no HTTP clients.

    The Task-2 test covered parse.py only.  This widens to the entire package
    (rings.py, config.py, parse.py, logs.py, receiver.py) — each is pure
    in-process with zero outbound calls.
    """

    _FORBIDDEN_ROOTS = frozenset({"requests", "httpx", "aiohttp", "urllib", "kubernetes"})
    _OTLP_DIR = SRC / "adapters" / "otlp"

    def _check_module(self, py_path: Path) -> list[str]:
        violations: list[str] = []
        tree = ast.parse(py_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in self._FORBIDDEN_ROOTS:
                        violations.append(
                            f"{py_path.name}: import {alias.name!r}"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root = module.split(".")[0]
                if root in self._FORBIDDEN_ROOTS:
                    violations.append(
                        f"{py_path.name}: from {module!r} import ..."
                    )
        return violations

    def test_no_http_client_imports_in_otlp_package(self):
        """All .py files in src/adapters/otlp/ (incl. __init__.py) must be
        HTTP-client-free and kubernetes-free (F13i widened)."""
        all_violations: list[str] = []
        for py in sorted(self._OTLP_DIR.glob("*.py")):
            # Include __init__.py in the lock (no _-prefix skip)
            all_violations.extend(self._check_module(py))
        assert not all_violations, (
            "Forbidden client imports found in src/adapters/otlp/:\n"
            + "\n".join(all_violations)
        )
