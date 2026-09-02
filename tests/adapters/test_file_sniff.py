"""Tests for src/adapters/file/sniff.py — format detection and line parsing.

TDD order: tests written first, implementation follows.
THE RELPATH INVARIANT: attributes["file"] must always be a relative path.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from adapters.file.sniff import detect_format, parse_line


RELPATH = "some/relative/file.log"


# ─── detect_format ────────────────────────────────────────────────────────────


def test_detect_format_plain():
    """Lines without recognisable structure are classified as 'plain'."""
    lines = [
        "hello world",
        "this is a plain log line",
        "no structure here at all",
        "just text",
    ] * 4
    assert detect_format(lines) == "plain"


def test_detect_format_jsonlines():
    """≥60% well-formed JSON-object lines classify as 'jsonlines'."""
    json_lines = ['{"ts": "2024-01-01T00:00:00Z", "message": "ok"}'] * 7
    # 3 plain lines: 7/(7+3) = 70% → above threshold
    mixed = json_lines + ["plain text", "more plain text", "even more plain"]
    assert detect_format(mixed) == "jsonlines"


def test_detect_format_klog():
    """≥60% klog-header lines classify as 'klog'."""
    klog_lines = [
        "I0101 12:00:00.000000 1234 main.go:10] starting server",
        "W0102 09:30:45.123456 5678 foo.go:99] warning occurred",
        "E0103 11:11:11.111111 9999 bar.go:42] error encountered",
        "I0104 00:00:00.000000 0001 run.go:1] running",
    ] * 2
    assert detect_format(klog_lines) == "klog"


def test_detect_format_mixed_50_50_is_plain():
    """Exactly 50% JSON (below 60% threshold) → 'plain'."""
    lines = ['{"msg": "ok"}'] * 5 + ["plain text"] * 5
    assert detect_format(lines) == "plain"


def test_detect_format_jsonlines_exactly_60_percent():
    """3-of-5 JSON-object lines (exactly 60 %) → 'jsonlines': threshold is >=, not >."""
    json_line = '{"ts": "2024-01-01T00:00:00Z", "message": "ok"}'
    lines = [json_line] * 3 + ["plain text"] * 2
    assert detect_format(lines) == "jsonlines"


def test_detect_format_klog_exactly_60_percent():
    """3-of-5 klog-header lines (exactly 60 %) → 'klog': threshold is >=, not >."""
    klog_line = "I0101 12:00:00.000000 1234 main.go:10] message"
    lines = [klog_line] * 3 + ["plain text"] * 2
    assert detect_format(lines) == "klog"


# ─── parse_line: plain ────────────────────────────────────────────────────────


def test_parse_line_plain_format():
    """Plain format: body=line, timestamp/severity=None, attributes has file=relpath."""
    result = parse_line("hello world", "plain", RELPATH)
    assert result["body"] == "hello world"
    assert result["timestamp"] is None
    assert result["severity"] is None
    assert result["attributes"]["file"] == RELPATH
    assert not Path(result["attributes"]["file"]).is_absolute()


# ─── parse_line: jsonlines ───────────────────────────────────────────────────


def test_parse_line_jsonlines_standard_keys_and_leftover():
    """JSON line: ts→timestamp, level→severity, message→body; leftover keys land in attributes."""
    line = json.dumps({
        "ts": "2024-01-01T12:00:00Z",
        "level": "info",
        "message": "server started",
        "host": "node-1",
        "pid": 42,
    })
    result = parse_line(line, "jsonlines", RELPATH)
    assert result["timestamp"] == "2024-01-01T12:00:00Z"
    assert result["severity"] == "info"
    assert result["body"] == "server started"
    # Leftover keys preserved in attributes
    assert result["attributes"]["host"] == "node-1"
    assert result["attributes"]["pid"] == 42
    # Known-mapping keys must NOT be duplicated in attributes
    assert "ts" not in result["attributes"]
    assert "level" not in result["attributes"]
    assert "message" not in result["attributes"]
    # Relpath invariant
    assert result["attributes"]["file"] == RELPATH


def test_parse_line_jsonlines_timestamp_key_priority():
    """ts > time > timestamp > @timestamp — highest-priority key wins."""
    # Only @timestamp present
    result_at = parse_line(json.dumps({"@timestamp": "2024-02-01", "msg": "x"}), "jsonlines", RELPATH)
    assert result_at["timestamp"] == "2024-02-01"

    # ts takes priority over all others
    result_ts = parse_line(
        json.dumps({"ts": "2024-01-01", "timestamp": "2099-01-01", "@timestamp": "2099-02-01", "msg": "x"}),
        "jsonlines",
        RELPATH,
    )
    assert result_ts["timestamp"] == "2024-01-01"

    # time takes priority over timestamp and @timestamp
    result_time = parse_line(
        json.dumps({"time": "2024-03-01", "timestamp": "2099-01-01", "msg": "x"}),
        "jsonlines",
        RELPATH,
    )
    assert result_time["timestamp"] == "2024-03-01"


def test_parse_line_json_failure_falls_back_to_plain():
    """Unparseable JSON in 'jsonlines' mode falls back to plain treatment."""
    result = parse_line("not json at all {broken", "jsonlines", RELPATH)
    assert result["body"] == "not json at all {broken"
    assert result["timestamp"] is None
    assert result["severity"] is None
    assert result["attributes"]["file"] == RELPATH


# ─── parse_line: klog ─────────────────────────────────────────────────────────


def test_parse_line_klog_severity_mapping():
    """klog severity letters I/W/E/F map to INFO/WARNING/ERROR/CRITICAL."""
    cases = [
        ("I0101 12:00:00.000000 1234 main.go:10] server started", "INFO", "server started"),
        ("W0202 08:00:00.000000 1 foo.go:1] watchout", "WARNING", "watchout"),
        ("E0303 08:00:00.000000 1 bar.go:1] oops", "ERROR", "oops"),
        ("F0404 08:00:00.000000 1 baz.go:1] fatal crash", "CRITICAL", "fatal crash"),
    ]
    for line, expected_severity, expected_body_fragment in cases:
        result = parse_line(line, "klog", RELPATH)
        assert result["severity"] == expected_severity, f"bad severity for {line!r}"
        assert expected_body_fragment in result["body"], f"body missing for {line!r}"
        assert result["timestamp"] is not None, "klog timestamp must not be None"
        assert result["attributes"]["file"] == RELPATH


# ─── RELPATH INVARIANT ────────────────────────────────────────────────────────


def test_relpath_invariant_across_all_formats():
    """attributes['file'] is never an absolute path in any format."""
    relpath = "logs/app.log"
    lines_and_fmts = [
        ("just text", "plain"),
        (json.dumps({"ts": "2024", "message": "hi", "meta": "/abs/should/not/leak"}), "jsonlines"),
        ("I0101 09:00:00.000000 1 x.go:1] msg", "klog"),
        ("not json {", "jsonlines"),  # fallback path
    ]
    for line, fmt in lines_and_fmts:
        result = parse_line(line, fmt, relpath)
        file_attr = result["attributes"]["file"]
        assert not Path(file_attr).is_absolute(), (
            f"absolute path found in attributes['file'] for fmt={fmt!r}: {file_attr!r}"
        )
        assert file_attr == relpath, (
            f"attributes['file'] must equal relpath; got {file_attr!r} for fmt={fmt!r}"
        )
