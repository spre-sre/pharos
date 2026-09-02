"""TDD tests for src/adapters/otlp/parse.py (Task 2, Step 1 RED).

parse_export_logs_request semantics under test:
  (a)  Simple body: one record, correct fields
  (b)  timeUnixNano as str → ISO-Z
  (c)  timeUnixNano as int → ISO-Z
  (d)  Clamping: "9"*30, "-1", "abc", 2**63, 0 (valid)
  (e)  AnyValue variants: string, int, double, bool, bytes, array, kvlist
  (f)  Entity precedence: k8s.pod.name > service.name > "otlp"
  (g)  F5 truncation: oversized record → budget enforced, count=1
  (h)  Malformed shapes → ValueError, NO payload content in message (F3/M6b)
  (i)  Multiple resourceLogs / scopeLogs / logRecords
  (j)  No-HTTP-client import lock (F13i)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from adapters.otlp.parse import parse_export_logs_request  # noqa: E402
from core.signals import LogRecord  # noqa: E402


# ─── helpers ──────────────────────────────────────────────────────────────────

def _parse(body, max_record_bytes=65536):
    """Convenience wrapper."""
    return parse_export_logs_request(body, max_record_bytes=max_record_bytes)


def _single_record_body(
    time_unix_nano=None,
    body_str="log message",
    severity_text=None,
    record_attrs=None,
    resource_attrs=None,
) -> dict:
    """Build a minimal valid OTLP ExportLogsServiceRequest with one record."""
    lr: dict = {"body": {"stringValue": body_str}}
    if time_unix_nano is not None:
        lr["timeUnixNano"] = time_unix_nano
    if severity_text is not None:
        lr["severityText"] = severity_text
    if record_attrs:
        lr["attributes"] = [
            {"key": k, "value": v}
            for k, v in record_attrs.items()
        ]

    resource: dict = {}
    if resource_attrs:
        resource = {
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}}
                for k, v in resource_attrs.items()
            ]
        }

    return {
        "resourceLogs": [
            {
                "resource": resource,
                "scopeLogs": [{"logRecords": [lr]}],
            }
        ]
    }


# ─── (a) simple body ──────────────────────────────────────────────────────────

def test_parse_simple_body_returns_one_record():
    """One record body → list of one LogRecord, count=0."""
    body = _single_record_body(
        time_unix_nano="1767225600000000000",  # 2026-01-01T00:00:00Z
        body_str="hello world",
        severity_text="INFO",
    )
    records, count = _parse(body)
    assert count == 0
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, LogRecord)
    assert rec.body == "hello world"
    assert rec.severity == "INFO"
    assert rec.timestamp is not None


# ─── (b/c) timeUnixNano str/int ───────────────────────────────────────────────

def test_nano_timestamp_as_string():
    """timeUnixNano as str → exact ISO-Z timestamp."""
    body = _single_record_body(time_unix_nano="1767225600000000000")  # 2026-01-01T00:00:00Z
    records, _ = _parse(body)
    assert records[0].timestamp == "2026-01-01T00:00:00Z"


def test_nano_timestamp_as_int():
    """timeUnixNano as int → same ISO-Z timestamp."""
    body = _single_record_body(time_unix_nano=1767225600000000000)  # 2026-01-01T00:00:00Z
    records, _ = _parse(body)
    assert records[0].timestamp == "2026-01-01T00:00:00Z"


# ─── (d) timestamp clamping ───────────────────────────────────────────────────

def test_nano_clamp_too_large_string():
    """timeUnixNano = '9'*30 (beyond 2**63) → timestamp=None, no raise (F3)."""
    body = _single_record_body(time_unix_nano="9" * 30)
    records, count = _parse(body)
    assert records[0].timestamp is None
    assert count == 0  # undated is not truncation


def test_nano_clamp_negative_string():
    """timeUnixNano = '-1' → timestamp=None, no raise (F3)."""
    body = _single_record_body(time_unix_nano="-1")
    records, _ = _parse(body)
    assert records[0].timestamp is None


def test_nano_clamp_non_parseable():
    """timeUnixNano = 'abc' → timestamp=None, no raise (F3)."""
    body = _single_record_body(time_unix_nano="abc")
    records, _ = _parse(body)
    assert records[0].timestamp is None


def test_nano_zero_is_valid():
    """timeUnixNano = 0 is within [0, 2**63) → valid timestamp (epoch)."""
    body = _single_record_body(time_unix_nano=0)
    records, _ = _parse(body)
    assert records[0].timestamp == "1970-01-01T00:00:00Z"


def test_nano_zero_string_is_valid():
    """timeUnixNano = '0' → valid timestamp."""
    body = _single_record_body(time_unix_nano="0")
    records, _ = _parse(body)
    assert records[0].timestamp == "1970-01-01T00:00:00Z"


def test_nano_exclusive_upper_bound():
    """timeUnixNano = 2**63 (exclusive upper bound) → timestamp=None."""
    body = _single_record_body(time_unix_nano=2**63)
    records, _ = _parse(body)
    assert records[0].timestamp is None


def test_nano_max_valid():
    """timeUnixNano = 2**63 - 1 (last valid value) → valid timestamp."""
    body = _single_record_body(time_unix_nano=2**63 - 1)
    records, _ = _parse(body)
    assert records[0].timestamp is not None
    assert records[0].timestamp.endswith("Z")


def test_nano_negative_int():
    """timeUnixNano = -1 (int, not str) → timestamp=None."""
    body = _single_record_body(time_unix_nano=-1)
    records, _ = _parse(body)
    assert records[0].timestamp is None


# ─── (e) AnyValue variants ────────────────────────────────────────────────────

def test_anyvalue_string():
    """stringValue → str attribute."""
    body = _single_record_body(
        record_attrs={"mykey": {"stringValue": "hello"}}
    )
    records, _ = _parse(body)
    assert records[0].attributes["mykey"] == "hello"
    assert isinstance(records[0].attributes["mykey"], str)


def test_anyvalue_int():
    """intValue (as string in JSON) → int attribute."""
    body = _single_record_body(
        record_attrs={"count": {"intValue": "42"}}
    )
    records, _ = _parse(body)
    assert records[0].attributes["count"] == 42
    assert isinstance(records[0].attributes["count"], int)


def test_anyvalue_double():
    """doubleValue → float attribute."""
    body = _single_record_body(
        record_attrs={"ratio": {"doubleValue": 3.14}}
    )
    records, _ = _parse(body)
    assert abs(records[0].attributes["ratio"] - 3.14) < 1e-9


def test_anyvalue_bool_true():
    """boolValue = True → bool attribute."""
    body = _single_record_body(
        record_attrs={"flag": {"boolValue": True}}
    )
    records, _ = _parse(body)
    assert records[0].attributes["flag"] is True


def test_anyvalue_bool_false():
    """boolValue = False → bool False (not 0)."""
    body = _single_record_body(
        record_attrs={"flag": {"boolValue": False}}
    )
    records, _ = _parse(body)
    assert records[0].attributes["flag"] is False


def test_anyvalue_bytes_kept_as_string():
    """bytesValue → base64 string kept as-is (no decode)."""
    body = _single_record_body(
        record_attrs={"blob": {"bytesValue": "aGVsbG8="}}
    )
    records, _ = _parse(body)
    assert records[0].attributes["blob"] == "aGVsbG8="


def test_anyvalue_array():
    """arrayValue → list of resolved values."""
    body = _single_record_body(
        record_attrs={
            "tags": {
                "arrayValue": {
                    "values": [
                        {"stringValue": "alpha"},
                        {"stringValue": "beta"},
                    ]
                }
            }
        }
    )
    records, _ = _parse(body)
    assert records[0].attributes["tags"] == ["alpha", "beta"]


def test_anyvalue_kvlist():
    """kvlistValue → dict of resolved key-value pairs."""
    body = _single_record_body(
        record_attrs={
            "meta": {
                "kvlistValue": {
                    "values": [
                        {"key": "x", "value": {"intValue": "1"}},
                        {"key": "y", "value": {"stringValue": "two"}},
                    ]
                }
            }
        }
    )
    records, _ = _parse(body)
    assert records[0].attributes["meta"] == {"x": 1, "y": "two"}


# ─── (f) entity precedence ────────────────────────────────────────────────────

def test_entity_pod_name_wins():
    """k8s.pod.name beats service.name."""
    body = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "k8s.pod.name", "value": {"stringValue": "my-pod"}},
                    {"key": "service.name", "value": {"stringValue": "my-svc"}},
                ]
            },
            "scopeLogs": [{"logRecords": [{"body": {"stringValue": "msg"}}]}],
        }]
    }
    records, _ = _parse(body)
    assert records[0].attributes["entity"] == "my-pod"


def test_entity_service_name_fallback():
    """service.name used when k8s.pod.name absent."""
    body = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "my-svc"}},
                ]
            },
            "scopeLogs": [{"logRecords": [{"body": {"stringValue": "msg"}}]}],
        }]
    }
    records, _ = _parse(body)
    assert records[0].attributes["entity"] == "my-svc"


def test_entity_default_otlp():
    """'otlp' fallback when neither pod name nor service name present."""
    body = _single_record_body()
    records, _ = _parse(body)
    assert records[0].attributes["entity"] == "otlp"


# ─── (g) F5 truncation ────────────────────────────────────────────────────────

def test_f5_truncation_large_attribute():
    """Attribute set that exceeds budget → attributes dropped, count=1."""
    max_bytes = 100
    large_val = "x" * 500  # well over 100 bytes
    body = _single_record_body(
        body_str="short",
        record_attrs={"big": {"stringValue": large_val}},
    )
    records, count = _parse(body, max_record_bytes=max_bytes)
    assert count == 1
    assert len(records) == 1
    rec = records[0]
    retained = len(rec.body) + sum(
        len(k) + len(str(v)) for k, v in rec.attributes.items()
    )
    assert retained <= max_bytes, (
        f"retained size {retained} > max_record_bytes {max_bytes}"
    )


def test_f5_body_truncated_when_oversized():
    """Body alone exceeds max_record_bytes → body truncated, count=1."""
    max_bytes = 10
    body = _single_record_body(body_str="x" * 100)
    records, count = _parse(body, max_record_bytes=max_bytes)
    assert count == 1
    assert len(records[0].body) <= max_bytes


def test_f5_no_truncation_when_fits():
    """Record well within budget → count=0, body unchanged."""
    body = _single_record_body(body_str="short message")
    records, count = _parse(body, max_record_bytes=65536)
    assert count == 0
    assert records[0].body == "short message"


def test_f5_count_accumulates_across_records():
    """Two oversized records → count=2."""
    max_bytes = 5
    big_body = _single_record_body(body_str="x" * 100)
    body = {
        "resourceLogs": (
            big_body["resourceLogs"] + big_body["resourceLogs"]
        )
    }
    _, count = _parse(body, max_record_bytes=max_bytes)
    assert count == 2


def test_f5_mixed_some_truncated():
    """One fits, one oversized → count=1."""
    max_bytes = 50
    fits_body = _single_record_body(body_str="hi")
    big_body = _single_record_body(body_str="x" * 100)
    body = {
        "resourceLogs": (
            fits_body["resourceLogs"] + big_body["resourceLogs"]
        )
    }
    records, count = _parse(body, max_record_bytes=max_bytes)
    assert count == 1
    assert len(records) == 2
    # First record body unchanged (it fits)
    assert records[0].body == "hi"


def test_entity_preserved_on_over_budget_record():
    """Over-budget record (huge body) → entity retained, truncated_count=1.

    Regression guard: entity is budget-EXEMPT.  Without the exemption, entity
    is the LAST key added to record_attrs (insertion order) and is the FIRST
    attribute dropped when the body alone exhausts the budget.  Entity selectors
    then return "No logs found" for a pod that HAS logs — the §4.2.1 honesty
    failure described in the phase-5 pre-merge review.
    """
    max_bytes = 16  # tiny budget — body alone will fill it
    body = _single_record_body(
        body_str="x" * 10000,
        resource_attrs={"k8s.pod.name": "victim-pod"},
    )
    records, count = _parse(body, max_record_bytes=max_bytes)
    assert count == 1, "oversized record must be counted as truncated"
    assert len(records) == 1
    assert records[0].attributes.get("entity") == "victim-pod", (
        "entity must survive budget enforcement on truncated records"
    )
    assert len(records[0].body) <= max_bytes, (
        "body must be trimmed to fit within max_record_bytes"
    )


def test_entity_capped_at_512_chars():
    """k8s.pod.name with 10 KB value → entity stored as exactly 512 chars.

    The budget exemption must not become a bypass: a huge source attribute
    is capped at _ENTITY_MAX_CHARS (512) before being stored.
    """
    huge_pod_name = "p" * 10000
    body = _single_record_body(
        body_str="log message",
        resource_attrs={"k8s.pod.name": huge_pod_name},
    )
    records, _ = _parse(body)
    entity = records[0].attributes.get("entity", "")
    assert len(entity) == 512, (
        f"entity must be capped at 512 chars; got len={len(entity)}"
    )
    assert entity == huge_pod_name[:512], (
        "entity must be the first 512 characters of k8s.pod.name"
    )


# ─── (h) malformed shapes → ValueError, no payload content ───────────────────

def test_malformed_body_not_dict_raises():
    """Non-dict body → ValueError."""
    with pytest.raises(ValueError):
        _parse("not a dict")


def test_malformed_body_not_dict_no_payload_in_message():
    """ValueError message must NOT contain the payload content (F3/M6b)."""
    payload = "ATTACKER_CONTROLLED_INPUT_12345"
    with pytest.raises(ValueError) as exc_info:
        _parse(payload)
    assert "ATTACKER_CONTROLLED_INPUT_12345" not in str(exc_info.value)


def test_malformed_resource_logs_not_list():
    """resourceLogs that is not a list → ValueError."""
    with pytest.raises(ValueError):
        _parse({"resourceLogs": "NOT_A_LIST"})


def test_malformed_resource_logs_not_list_no_payload():
    """ValueError from bad resourceLogs must not embed payload (F3/M6b)."""
    attacker_data = "INJECTED_DATA_ABCDEF"
    with pytest.raises(ValueError) as exc_info:
        _parse({"resourceLogs": attacker_data})
    assert attacker_data not in str(exc_info.value)


def test_malformed_scope_logs_not_list():
    """scopeLogs that is not a list → ValueError."""
    with pytest.raises(ValueError):
        _parse({
            "resourceLogs": [
                {"scopeLogs": "NOT_A_LIST"}
            ]
        })


def test_malformed_log_records_not_list():
    """logRecords that is not a list → ValueError."""
    with pytest.raises(ValueError):
        _parse({
            "resourceLogs": [
                {"scopeLogs": [{"logRecords": "NOT_A_LIST"}]}
            ]
        })


# ─── (i) multiple nesting structures ─────────────────────────────────────────

def test_empty_resource_logs():
    """Empty resourceLogs → empty list, count=0."""
    records, count = _parse({"resourceLogs": []})
    assert records == []
    assert count == 0


def test_missing_resource_logs_key():
    """Missing resourceLogs key → treated as empty."""
    records, count = _parse({})
    assert records == []
    assert count == 0


def test_multiple_scope_logs_in_one_resource_log():
    """Two scopeLogs → 2 records total."""
    body = {
        "resourceLogs": [{
            "scopeLogs": [
                {"logRecords": [{"body": {"stringValue": "line1"}}]},
                {"logRecords": [{"body": {"stringValue": "line2"}}]},
            ]
        }]
    }
    records, _ = _parse(body)
    assert [r.body for r in records] == ["line1", "line2"]


def test_multiple_resource_logs():
    """Two resourceLogs → all records collected."""
    body = {
        "resourceLogs": [
            {"scopeLogs": [{"logRecords": [{"body": {"stringValue": "r1"}}]}]},
            {"scopeLogs": [{"logRecords": [{"body": {"stringValue": "r2"}}]}]},
        ]
    }
    records, _ = _parse(body)
    assert len(records) == 2


def test_record_with_no_body_field():
    """logRecord with no body field → body is a str (empty or default)."""
    body = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{}]}]}]}
    records, _ = _parse(body)
    assert len(records) == 1
    assert isinstance(records[0].body, str)


def test_resource_attrs_appear_in_record():
    """Resource attributes are merged into record attributes."""
    body = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "host.name", "value": {"stringValue": "node-1"}},
                ]
            },
            "scopeLogs": [{"logRecords": [{"body": {"stringValue": "msg"}}]}],
        }]
    }
    records, _ = _parse(body)
    assert records[0].attributes.get("host.name") == "node-1"


def test_record_attrs_override_resource_attrs():
    """Record-level attributes override resource attributes for same key."""
    body = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "env", "value": {"stringValue": "prod"}},
                ]
            },
            "scopeLogs": [{
                "logRecords": [{
                    "body": {"stringValue": "msg"},
                    "attributes": [
                        {"key": "env", "value": {"stringValue": "staging"}},
                    ],
                }]
            }],
        }]
    }
    records, _ = _parse(body)
    assert records[0].attributes["env"] == "staging"


# ─── (j) no-HTTP-client import lock (F13i) ───────────────────────────────────

def test_otlp_parse_no_http_client_imports():
    """parse.py must NOT import any outbound HTTP client library (F13i).

    The parser is pure in-process logic; importing requests/httpx/aiohttp/
    urllib would widen the attack surface unnecessarily.
    """
    src_path = (
        Path(__file__).resolve().parents[2]
        / "src" / "adapters" / "otlp" / "parse.py"
    )
    tree = ast.parse(src_path.read_text())
    forbidden_roots = {"requests", "httpx", "aiohttp", "urllib", "kubernetes"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden_roots, (
                    f"Forbidden HTTP client import in parse.py: {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            assert root not in forbidden_roots, (
                f"Forbidden HTTP client import in parse.py: {module!r}"
            )


# ─── (k) budget key-counting: long key + short value ─────────────────────────
# IMPORTANT 1: Two mutant-killing tests proving len(k) is charged at both the
# estimate site (parse_export_logs_request, line ~295) and the enforce_budget
# site (_enforce_budget, line ~179).  A len(k)-deletion mutant at either site
# would incorrectly let the record pass without truncation.

def test_long_key_charges_estimate_site():
    """Attribute with a LONG KEY + short value → budget check fires (estimate site).

    Mutant: deleting len(k) from the estimate sum → under-counts size → record
    passes without truncation (count stays 0).  This test fails under that mutant.
    """
    # Budget just large enough for the body but NOT for body + long_key + short_value.
    # body = "x" (1 byte), key = "k" * 200 (200 bytes), value = "v" (1 byte).
    # True size = 1 + 200 + 1 = 202.
    # With len(k) deleted from estimate: estimate = 1 + len("v") = 2 → passes budget.
    max_bytes = 100
    long_key = "k" * 200
    body = _single_record_body(
        body_str="x",
        record_attrs={long_key: {"stringValue": "v"}},
    )
    records, count = _parse(body, max_record_bytes=max_bytes)
    assert count == 1, (
        f"long key not charged at estimate site; count={count} "
        f"(mutant: len(k) deleted from estimate sum)"
    )


def test_long_key_charges_enforce_budget_site():
    """Attribute with a LONG KEY + short value → key len counted inside _enforce_budget.

    Mutant: deleting len(k) from attr_size in _enforce_budget → over-permits
    attributes that should be dropped, meaning 'kept' contains the long-key attr.
    This test fails under that mutant.
    """
    # body = "x" (1 byte), key = "k" * 200, value = "v" (1 byte).
    # Budget: 10 bytes — enough for body (1) but NOT for body + long_key + value.
    # With len(k) deleted from attr_size inside _enforce_budget:
    #   attr_size = len("v") = 1 → fits in remaining 9 bytes → kept incorrectly.
    max_bytes = 10
    long_key = "k" * 200
    body = _single_record_body(
        body_str="x",
        record_attrs={long_key: {"stringValue": "v"}},
    )
    records, count = _parse(body, max_record_bytes=max_bytes)
    # The long-key attribute must NOT appear in the kept attrs.
    assert long_key not in records[0].attributes, (
        f"long key kept despite budget exhaustion at _enforce_budget site "
        f"(mutant: len(k) deleted from attr_size)"
    )
