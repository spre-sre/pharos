"""Tests for the pure-function core of scripts/live_matrix.py.

Import via spec_from_file_location because scripts/ is not a package.
"""
from __future__ import annotations

import importlib.util
import pathlib
import types

import pytest

# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).parent.parent
_LM_PATH = REPO_ROOT / "scripts" / "live_matrix.py"


def _load_live_matrix() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("live_matrix", _LM_PATH)
    assert spec is not None, f"spec_from_file_location returned None for {_LM_PATH}"
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_lm = _load_live_matrix()
extract_shape = _lm.extract_shape
diff_tool_records = _lm.diff_tool_records
diff_runs = _lm.diff_runs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(
    *,
    status: str = "ok",
    error_type: str | None = None,
    shape: object = None,
    latency_ms: int = 100,
    response_bytes: int = 1000,
) -> dict:
    """Build a tool-call record using EXACTLY the Task-3 runner key names."""
    r: dict = {
        "status": status,
        "shape": shape,
        "latency_ms": latency_ms,
        "response_bytes": response_bytes,
    }
    if error_type is not None:
        r["error_type"] = error_type
    return r


def _findings_of_kind(findings: list[dict], kind: str) -> list[dict]:
    return [f for f in findings if f["kind"] == kind]


# ---------------------------------------------------------------------------
# extract_shape
# ---------------------------------------------------------------------------


class TestExtractShape:
    def test_dict_sorted_keys(self):
        # Keys must be sorted regardless of insertion order
        result = extract_shape({"b": 1, "a": "x"})
        assert result == {"type": "dict", "keys": {"a": "str", "b": "int"}}
        # Sorted-order guarantee: reversing sorted() must fail this assertion
        assert list(result["keys"]) == ["a", "b"]

    # --- list len_bucket ---

    def test_list_empty(self):
        result = extract_shape([])
        assert result == {"type": "list", "elem": "empty", "len_bucket": "0"}

    def test_list_one(self):
        result = extract_shape([42])
        assert result["len_bucket"] == "1"
        assert result["elem"] == "int"

    def test_list_five(self):
        result = extract_shape([1, 2, 3, 4, 5])
        assert result["len_bucket"] == "2-10"

    def test_list_fifty(self):
        result = extract_shape(list(range(50)))
        assert result["len_bucket"] == "11-100"

    def test_list_two_hundred(self):
        result = extract_shape(list(range(200)))
        assert result["len_bucket"] == "100+"

    # --- scalars ---

    def test_none_is_null(self):
        assert extract_shape(None) == "null"

    def test_true_is_bool_not_int(self):
        # Python bools are ints; must test bool before int
        assert extract_shape(True) == "bool"

    def test_false_is_bool(self):
        assert extract_shape(False) == "bool"

    def test_int(self):
        assert extract_shape(42) == "int"

    def test_float(self):
        assert extract_shape(3.14) == "float"

    def test_str(self):
        assert extract_shape("hello") == "str"

    # --- nesting ---

    def test_nested_dict_list_dict(self):
        result = extract_shape({"xs": [{"v": 1.5}]})
        assert result == {
            "type": "dict",
            "keys": {
                "xs": {
                    "type": "list",
                    "elem": {"type": "dict", "keys": {"v": "float"}},
                    "len_bucket": "1",
                }
            },
        }


# ---------------------------------------------------------------------------
# diff_tool_records
# ---------------------------------------------------------------------------


class TestDiffToolRecords:
    def test_identical_records_no_findings(self):
        rec = _rec()
        assert diff_tool_records("t", rec, rec) == []

    def test_status_ok_to_error_is_fail(self):
        a = _rec(status="ok")
        b = _rec(status="error")
        findings = diff_tool_records("t", a, b)
        status_f = _findings_of_kind(findings, "status")
        assert len(status_f) == 1
        assert status_f[0]["severity"] == "fail"
        assert status_f[0]["tool"] == "t"

    def test_added_result_key_is_shape_fail(self):
        # shape is pre-computed and stored directly in the record
        a = _rec(shape=extract_shape({"x": 1}))
        b = _rec(shape=extract_shape({"x": 1, "y": "new"}))
        findings = diff_tool_records("t", a, b)
        shape_f = _findings_of_kind(findings, "shape")
        assert len(shape_f) >= 1
        assert shape_f[0]["severity"] == "fail"
        # Detail must name the path including "result"
        assert "result" in shape_f[0]["detail"]

    def test_latency_100_to_200_is_flag(self):
        a = _rec(latency_ms=100)
        b = _rec(latency_ms=200)
        findings = diff_tool_records("t", a, b)
        lat_f = _findings_of_kind(findings, "latency")
        assert len(lat_f) == 1
        assert lat_f[0]["severity"] == "flag"

    def test_latency_100_to_120_no_finding(self):
        a = _rec(latency_ms=100)
        b = _rec(latency_ms=120)
        findings = diff_tool_records("t", a, b)
        assert _findings_of_kind(findings, "latency") == []

    def test_response_bytes_1000_to_5000_is_size_flag(self):
        a = _rec(response_bytes=1000)
        b = _rec(response_bytes=5000)
        findings = diff_tool_records("t", a, b)
        size_f = _findings_of_kind(findings, "size")
        assert len(size_f) == 1
        assert size_f[0]["severity"] == "flag"

    def test_response_bytes_collapse_to_zero_is_size_flag(self):
        """bytes 1000→0: collapse to zero must trigger flag (b is falsy but not None)."""
        a = _rec(response_bytes=1000)
        b = _rec(response_bytes=0)
        findings = diff_tool_records("t", a, b)
        size_f = _findings_of_kind(findings, "size")
        assert len(size_f) == 1
        assert size_f[0]["severity"] == "flag"

    def test_error_type_change_is_fail(self):
        a = _rec(status="error", error_type="TimeoutError")
        b = _rec(status="error", error_type="NetworkError")
        findings = diff_tool_records("t", a, b)
        et_f = _findings_of_kind(findings, "error_type")
        assert len(et_f) == 1
        assert et_f[0]["severity"] == "fail"

    def test_presence_when_b_is_none(self):
        rec = _rec()
        findings = diff_tool_records("t", rec, None)
        assert len(findings) == 1
        assert findings[0]["kind"] == "presence"
        assert findings[0]["severity"] == "fail"
        assert findings[0]["tool"] == "t"

    def test_presence_when_a_is_none(self):
        rec = _rec()
        findings = diff_tool_records("t", None, rec)
        assert len(findings) == 1
        assert findings[0]["kind"] == "presence"
        assert findings[0]["severity"] == "fail"

    def test_finding_keys_are_complete(self):
        """Every finding must have the four required keys."""
        a = _rec(latency_ms=100)
        b = _rec(latency_ms=200)
        for finding in diff_tool_records("t", a, b):
            assert set(finding.keys()) >= {"tool", "kind", "severity", "detail"}

    def test_deep_shape_path_naming_exact_string(self):
        """Shape diff must produce the exact path string result.data[].value: str -> float."""
        a = _rec(shape=extract_shape({"data": [{"value": "x"}]}))
        b = _rec(shape=extract_shape({"data": [{"value": 1.5}]}))
        findings = diff_tool_records("t", a, b)
        shape_f = _findings_of_kind(findings, "shape")
        assert shape_f, "Expected at least one shape finding"
        assert any(
            f["detail"] == "result.data[].value: str -> float" for f in shape_f
        ), f"Exact path not found; got: {[f['detail'] for f in shape_f]}"

    def test_spec_key_names_produce_shape_and_size_findings(self):
        """Regression: records using EXACTLY Task-3 spec keys must yield findings.

        The old code read a.get('result') and a.get('bytes'); with those keys
        absent the record returned zero findings even when shape changed and
        size grew 9×.
        """
        a = {
            "status": "ok",
            "shape": extract_shape({"value": "hello"}),
            "latency_ms": 100,
            "response_bytes": 1000,
        }
        b = {
            "status": "ok",
            "shape": extract_shape({"value": 3.14}),  # str -> float
            "latency_ms": 100,
            "response_bytes": 9000,  # 9× → flag
        }
        findings = diff_tool_records("spec_tool", a, b)
        shape_f = _findings_of_kind(findings, "shape")
        size_f = _findings_of_kind(findings, "size")
        assert shape_f, "Expected shape finding with spec key 'shape'"
        assert size_f, "Expected size finding with spec key 'response_bytes'"


# ---------------------------------------------------------------------------
# List-emptiness transition rules (NEW RULING)
# ---------------------------------------------------------------------------


class TestListEmptinessTransition:
    """Emptiness transitions are FLAG; all other list diffs stay FAIL."""

    def _list_rec(self, list_shape: dict) -> dict:
        return _rec(shape={"type": "dict", "keys": {"data": list_shape}})

    def test_empty_to_populated_is_shape_flag(self):
        a = self._list_rec({"type": "list", "elem": "empty", "len_bucket": "0"})
        b = self._list_rec({"type": "list", "elem": "int", "len_bucket": "2-10"})
        findings = diff_tool_records("t", a, b)
        shape_f = _findings_of_kind(findings, "shape")
        assert len(shape_f) == 1
        assert shape_f[0]["severity"] == "flag"
        assert "list emptiness" in shape_f[0]["detail"]

    def test_populated_to_empty_is_shape_flag(self):
        a = self._list_rec({"type": "list", "elem": "str", "len_bucket": "2-10"})
        b = self._list_rec({"type": "list", "elem": "empty", "len_bucket": "0"})
        findings = diff_tool_records("t", a, b)
        shape_f = _findings_of_kind(findings, "shape")
        assert len(shape_f) == 1
        assert shape_f[0]["severity"] == "flag"
        assert "list emptiness" in shape_f[0]["detail"]

    def test_populated_elem_type_change_is_fail(self):
        """Both lists non-empty; elem type changes str → int → FAIL."""
        a = self._list_rec({"type": "list", "elem": "str", "len_bucket": "2-10"})
        b = self._list_rec({"type": "list", "elem": "int", "len_bucket": "2-10"})
        findings = diff_tool_records("t", a, b)
        shape_f = _findings_of_kind(findings, "shape")
        assert shape_f, "Expected at least one shape finding"
        assert all(f["severity"] == "fail" for f in shape_f)

    def test_bucket_change_between_nonempty_is_fail(self):
        """Both lists non-empty; bucket '1'→'2-10' same elem → FAIL."""
        a = self._list_rec({"type": "list", "elem": "int", "len_bucket": "1"})
        b = self._list_rec({"type": "list", "elem": "int", "len_bucket": "2-10"})
        findings = diff_tool_records("t", a, b)
        shape_f = _findings_of_kind(findings, "shape")
        assert shape_f, "Expected at least one shape finding for bucket change"
        assert all(f["severity"] == "fail" for f in shape_f)


# ---------------------------------------------------------------------------
# diff_runs
# ---------------------------------------------------------------------------


class TestDiffRuns:
    def test_missing_tool_in_run_b_is_presence_fail(self):
        run_a = {
            "tool_a": _rec(),
            "tool_b": _rec(),
        }
        run_b = {
            "tool_a": _rec(),
            # tool_b absent
        }
        findings = diff_runs(run_a, run_b)
        presence_f = _findings_of_kind(findings, "presence")
        assert any(f["tool"] == "tool_b" for f in presence_f), (
            "Expected a presence finding for tool_b"
        )

    def test_fail_findings_sort_before_flags(self):
        # tool_x: latency spike (flag); tool_y: status change (fail)
        run_a = {
            "tool_x": _rec(latency_ms=100),
            "tool_y": _rec(status="ok"),
        }
        run_b = {
            "tool_x": _rec(latency_ms=200),  # flag
            "tool_y": _rec(status="error"),  # fail
        }
        findings = diff_runs(run_a, run_b)
        severities = [f["severity"] for f in findings]
        # All fails must appear before any flag
        seen_flag = False
        for sev in severities:
            if sev == "flag":
                seen_flag = True
            if seen_flag and sev == "fail":
                pytest.fail(f"Found 'fail' after 'flag' in sorted findings: {severities}")

    def test_diff_runs_covers_union_of_tools(self):
        run_a = {"tool_only_in_a": _rec()}
        run_b = {"tool_only_in_b": _rec()}
        findings = diff_runs(run_a, run_b)
        tools_in_findings = {f["tool"] for f in findings}
        assert "tool_only_in_a" in tools_in_findings
        assert "tool_only_in_b" in tools_in_findings

    def test_diff_runs_empty_when_identical(self):
        run = {"tool_a": _rec(), "tool_b": _rec()}
        assert diff_runs(run, run) == []
