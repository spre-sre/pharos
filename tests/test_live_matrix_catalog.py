"""Tests for CATALOG and render_args in scripts/live_matrix.py.

TDD: these tests are written first and must fail until CATALOG and render_args
are implemented.  Import path follows the same spec_from_file_location pattern
as tests/test_live_matrix_core.py.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import types

# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).parent.parent
_LM_PATH = REPO_ROOT / "scripts" / "live_matrix.py"
_PARITY_PATH = REPO_ROOT / "tests" / "characterization" / "parity_reference.json"


def _load_live_matrix() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("live_matrix", _LM_PATH)
    assert spec is not None, f"spec_from_file_location returned None for {_LM_PATH}"
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_lm = _load_live_matrix()
CATALOG = _lm.CATALOG
render_args = _lm.render_args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parity_tools() -> set[str]:
    with _PARITY_PATH.open() as f:
        data = json.load(f)
    return set(data["tools"])


def _parity_schemas() -> dict[str, dict]:
    with _PARITY_PATH.open() as f:
        data = json.load(f)
    return {name: tool.get("input_schema", {}) for name, tool in data["tools"].items()}


# Loaded once; tests reference this directly
_SCHEMAS: dict[str, dict] = _parity_schemas()

_TARGETS = {
    "namespace": "test-ns",
    "pod": "test-pod",
    "pipelinerun": "test-pr",
    "pipelinerun_ns": "test-pr-ns",
}


# ---------------------------------------------------------------------------
# test_catalog_covers_exactly_the_registered_tools
# ---------------------------------------------------------------------------


class TestCatalogCompleteness:
    def test_catalog_covers_all_parity_tools(self):
        """Every tool in parity_reference.json must have a CATALOG entry."""
        parity = _parity_tools()
        missing = parity - set(CATALOG)
        assert not missing, f"Tools in parity but not in CATALOG: {sorted(missing)}"

    def test_no_extra_tools_in_catalog(self):
        """Every CATALOG entry must correspond to a tool in parity_reference.json."""
        parity = _parity_tools()
        extra = set(CATALOG) - parity
        assert not extra, f"Tools in CATALOG but not in parity: {sorted(extra)}"

    def test_catalog_entry_has_required_keys(self):
        """Every CATALOG entry must have args, expectation, note, accepts_source."""
        for name, entry in CATALOG.items():
            assert "args" in entry, f"{name}: missing 'args'"
            assert "expectation" in entry, f"{name}: missing 'expectation'"
            assert "note" in entry, f"{name}: missing 'note'"
            assert "accepts_source" in entry, f"{name}: missing 'accepts_source'"
            assert entry["expectation"] in ("ok", "error_ok"), (
                f"{name}: expectation must be 'ok' or 'error_ok', "
                f"got {entry['expectation']!r}"
            )

    def test_accepts_source_matches_schema(self):
        """CATALOG accepts_source must match 'source' in parity schema properties."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            expected = "source" in schema.get("properties", {})
            assert entry["accepts_source"] == expected, (
                f"{name}: accepts_source={entry['accepts_source']!r} but "
                f"schema {'has' if expected else 'lacks'} 'source' property"
            )

    def test_catalog_args_satisfy_required_schema_fields(self):
        """Every required schema field must be present in CATALOG args."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            required = schema.get("required", [])
            args = entry["args"]
            for field in required:
                assert field in args, (
                    f"{name}: required field '{field}' missing from CATALOG args"
                )


# ---------------------------------------------------------------------------
# test_render_args_fills_placeholders_and_source
# ---------------------------------------------------------------------------


class TestRenderArgs:
    def test_namespace_placeholder_filled(self):
        entry = {"args": {"namespace": "{namespace}"}, "accepts_source": False}
        result = render_args(entry, _TARGETS, source=None)
        assert result["namespace"] == "test-ns"

    def test_pod_placeholder_filled(self):
        entry = {"args": {"pod_name": "{pod}"}, "accepts_source": False}
        result = render_args(entry, _TARGETS, source=None)
        assert result["pod_name"] == "test-pod"

    def test_pipelinerun_placeholder_filled(self):
        entry = {"args": {"pipeline_run": "{pipelinerun}"}, "accepts_source": False}
        result = render_args(entry, _TARGETS, source=None)
        assert result["pipeline_run"] == "test-pr"

    def test_pipelinerun_ns_placeholder_filled(self):
        entry = {
            "args": {"namespace": "{pipelinerun_ns}"},
            "accepts_source": False,
        }
        result = render_args(entry, _TARGETS, source=None)
        assert result["namespace"] == "test-pr-ns"

    def test_source_injected_when_accepted_and_not_none(self):
        entry = {"args": {"namespace": "{namespace}"}, "accepts_source": True}
        result = render_args(entry, _TARGETS, source="test-source")
        assert result.get("source") == "test-source"

    def test_source_not_injected_when_not_accepted(self):
        entry = {"args": {"namespace": "{namespace}"}, "accepts_source": False}
        result = render_args(entry, _TARGETS, source="test-source")
        assert "source" not in result

    def test_source_not_injected_when_source_is_none(self):
        entry = {"args": {"namespace": "{namespace}"}, "accepts_source": True}
        result = render_args(entry, _TARGETS, source=None)
        assert "source" not in result

    def test_non_string_args_pass_through_unchanged(self):
        entry = {"args": {"limit": 10, "flag": True, "data": [1, 2]}, "accepts_source": False}
        result = render_args(entry, _TARGETS, source=None)
        assert result["limit"] == 10
        assert result["flag"] is True
        assert result["data"] == [1, 2]

    def _assert_no_braces(self, value: object, tool: str, path: str) -> None:
        """Recursively assert no ``{``/``}`` survive in any nested string."""
        if isinstance(value, str):
            assert "{" not in value and "}" not in value, (
                f"{tool}: unreplaced placeholder at {path}={value!r}"
            )
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self._assert_no_braces(item, tool, f"{path}[{i}]")
        elif isinstance(value, dict):
            for k, v in value.items():
                self._assert_no_braces(v, tool, f"{path}.{k}")

    def test_no_brace_survives_rendering_for_catalog_entry(self):
        """Every string value in a rendered CATALOG entry must be brace-free."""
        for name, entry in CATALOG.items():
            rendered = render_args(entry, _TARGETS, source="x")
            for k, v in rendered.items():
                self._assert_no_braces(v, name, k)

    def test_original_args_not_mutated(self):
        """render_args must not modify entry['args'] in place."""
        entry = {
            "args": {"namespace": "{namespace}", "limit": 10},
            "accepts_source": True,
        }
        original_namespace = entry["args"]["namespace"]
        render_args(entry, _TARGETS, source="s")
        assert entry["args"]["namespace"] == original_namespace


# ---------------------------------------------------------------------------
# test_catalog_bounded
# ---------------------------------------------------------------------------


class TestCatalogBounded:
    def test_limit_is_bounded(self):
        """Tools with 'limit' in schema must have limit <= 10 in CATALOG args."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            if "limit" not in schema.get("properties", {}):
                continue
            args = entry["args"]
            assert "limit" in args, (
                f"{name}: schema has 'limit' but CATALOG args is missing it"
            )
            assert isinstance(args["limit"], int), (
                f"{name}: limit must be int, got {type(args['limit'])}"
            )
            assert args["limit"] <= 10, (
                f"{name}: limit={args['limit']} exceeds bound of 10"
            )

    def test_tail_lines_is_bounded(self):
        """Tools with 'tail_lines' in schema must have tail_lines <= 100 in CATALOG args."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            if "tail_lines" not in schema.get("properties", {}):
                continue
            args = entry["args"]
            assert "tail_lines" in args, (
                f"{name}: schema has 'tail_lines' but CATALOG args is missing it"
            )
            assert isinstance(args["tail_lines"], int), (
                f"{name}: tail_lines must be int, got {type(args['tail_lines'])}"
            )
            assert args["tail_lines"] <= 100, (
                f"{name}: tail_lines={args['tail_lines']} exceeds bound of 100"
            )

    def test_max_context_tokens_is_bounded(self):
        """Tools with 'max_context_tokens' in schema must have the value bounded."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            if "max_context_tokens" not in schema.get("properties", {}):
                continue
            args = entry["args"]
            assert "max_context_tokens" in args, (
                f"{name}: schema has 'max_context_tokens' but CATALOG args is missing it"
            )
            assert isinstance(args["max_context_tokens"], int), (
                f"{name}: max_context_tokens must be int"
            )
            assert args["max_context_tokens"] <= 5000, (
                f"{name}: max_context_tokens={args['max_context_tokens']} exceeds bound"
            )

    def test_time_period_is_bounded(self):
        """Tools with 'time_period' in schema must have time_period == '1h' in CATALOG args."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            if "time_period" not in schema.get("properties", {}):
                continue
            args = entry["args"]
            assert "time_period" in args, (
                f"{name}: schema has 'time_period' but CATALOG args is missing it"
            )
            assert args["time_period"] == "1h", (
                f"{name}: time_period={args['time_period']!r} must be '1h'"
            )

    def test_max_namespaces_is_bounded(self):
        """Tools with 'max_namespaces' in schema must have max_namespaces <= 3 in CATALOG args."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            if "max_namespaces" not in schema.get("properties", {}):
                continue
            args = entry["args"]
            assert "max_namespaces" in args, (
                f"{name}: schema has 'max_namespaces' but CATALOG args is missing it"
            )
            assert isinstance(args["max_namespaces"], int), (
                f"{name}: max_namespaces must be int"
            )
            assert args["max_namespaces"] <= 3, (
                f"{name}: max_namespaces={args['max_namespaces']} exceeds bound of 3"
            )

    def test_connect_cluster_is_error_ok(self):
        """connect_cluster must be error_ok (guaranteed error, no real dial)."""
        assert CATALOG["connect_cluster"]["expectation"] == "error_ok"

    def test_query_kubearchive_is_error_ok(self):
        """query_kubearchive must be error_ok (KUBEARCHIVE_ENABLED=false in matrix)."""
        assert CATALOG["query_kubearchive"]["expectation"] == "error_ok"

    def test_manage_prediction_training_data_action_is_read_only(self):
        """manage_prediction_training_data must use only an allowlisted read-only action."""
        action = CATALOG["manage_prediction_training_data"]["args"].get("action")
        assert action is not None, "action arg must be set"
        read_only_actions = {"stats", "list_failures"}
        assert action in read_only_actions, (
            f"manage_prediction_training_data action {action!r} is not in "
            f"read-only allowlist {read_only_actions}"
        )

    def test_prometheus_query_and_alias_use_count_up(self):
        """prometheus_query and query_metrics must use query='count(up)'."""
        for tool in ("prometheus_query", "query_metrics"):
            args = CATALOG[tool]["args"]
            assert args.get("query") == "count(up)", (
                f"{tool}: query must be 'count(up)', got {args.get('query')!r}"
            )


# ---------------------------------------------------------------------------
# T2 rider: schema-conformance
# ---------------------------------------------------------------------------


class TestSchemaConformance:
    """T2 rider: every CATALOG entry's args validate against parity input_schema.

    Four sub-checks per tool:
    1. **Known keys** — every arg key is in schema.properties (or "source").
    2. **Required present** — all required fields appear in args (reconfirms
       TestCatalogCompleteness.test_catalog_args_satisfy_required_schema_fields).
    3. **Primitive type match** — for simple (non-anyOf) properties, the catalog
       value's Python type must be compatible with the schema type.
    4. **Enum membership** — when schema.properties[key].enum exists, the arg
       value must be in the enum.
    """

    # JSON Schema type → acceptable Python type(s)
    _JSON_TO_PYTHON: "dict[str, type | tuple]" = {
        "string": str,
        "integer": int,
        "boolean": bool,
        "number": (int, float),
        "array": list,
        "object": dict,
        "null": type(None),
    }

    def _type_ok(self, value: object, schema_type: str) -> bool:
        """Return True when *value* is compatible with *schema_type*."""
        expected = self._JSON_TO_PYTHON.get(schema_type)
        return expected is None or isinstance(value, expected)

    def test_all_arg_keys_are_known_schema_properties(self):
        """Every arg key in CATALOG must appear in the tool's schema properties (T2)."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            # "source" is a synthetic injection key (not in schema); allow it.
            known_keys = set(schema.get("properties", {}).keys()) | {"source"}
            for key in entry["args"]:
                assert key in known_keys, (
                    f"{name}: arg key '{key}' is not in schema properties "
                    f"{sorted(known_keys)}"
                )

    def test_primitive_type_match(self):
        """CATALOG arg values must match their schema type for simple (non-anyOf) props (T2)."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            props = schema.get("properties", {})
            for key, value in entry["args"].items():
                if key not in props:
                    continue  # "source" or other injected key
                prop_schema = props[key]
                if "type" not in prop_schema:
                    continue  # anyOf or unconstrained — skip
                assert self._type_ok(value, prop_schema["type"]), (
                    f"{name}.{key}: value {value!r} ({type(value).__name__}) "
                    f"does not match schema type '{prop_schema['type']}'"
                )

    def test_enum_membership(self):
        """CATALOG arg values must be in schema enum when enum is declared (T2)."""
        for name, entry in CATALOG.items():
            schema = _SCHEMAS.get(name, {})
            props = schema.get("properties", {})
            for key, value in entry["args"].items():
                if key not in props:
                    continue
                enum = props[key].get("enum")
                if enum is None:
                    continue
                assert value in enum, (
                    f"{name}.{key}: value {value!r} not in declared enum {enum}"
                )

    def test_schema_conformance_covers_all_49_tools(self):
        """All 49 parity tools must be present in CATALOG for conformance to hold (T2)."""
        parity_count = len(_SCHEMAS)
        catalog_count = len(CATALOG)
        assert parity_count == 49, (
            f"Expected 49 parity tools, found {parity_count}"
        )
        assert catalog_count == 49, (
            f"Expected 49 CATALOG entries, found {catalog_count}"
        )
