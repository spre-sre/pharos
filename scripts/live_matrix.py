"""live_matrix.py - Live tool-sweep harness for MCP tool parity checking.

Runs a configurable catalog of MCP tool calls against one or more server
configurations, persists results under ``live-matrix-runs/``, and computes a
structured diff when two runs are compared.

This module must be importable cheaply (the ``mcp`` SDK adds ~0.3 s at import
time; all ``mcp`` imports are therefore function-local, never at module level).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LATENCY_THRESHOLD = 0.50  # relative change that triggers a latency flag
SIZE_THRESHOLD = 0.50  # relative change that triggers a size flag


def _len_bucket(n: int) -> str:
    """Map a list length to one of the five canonical bucket strings."""
    if n == 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 10:
        return "2-10"
    if n <= 100:
        return "11-100"
    return "100+"


def _shape_to_str(shape: Any) -> str:
    """Render a shape compactly for use in finding detail strings.

    Examples: ``"str"``, ``"{a,b}"``, ``"[int×2-10]"``.
    """
    if shape is None:
        return "null"
    if isinstance(shape, str):
        return shape  # scalars like "str", "int", "null", "empty"
    if isinstance(shape, dict):
        t = shape.get("type")
        if t == "dict":
            keys = ",".join(sorted(shape.get("keys", {})))
            return f"{{{keys}}}"
        if t == "list":
            elem_str = _shape_to_str(shape.get("elem", "empty"))
            bucket = shape.get("len_bucket", "?")
            return f"[{elem_str}×{bucket}]"
    return repr(shape)


# ---------------------------------------------------------------------------
# extract_shape
# ---------------------------------------------------------------------------


def extract_shape(value: Any) -> "dict | str":
    """Return a structural fingerprint of *value*, safe to serialise to JSON.

    Scalars become the strings ``"str"``, ``"int"``, ``"float"``, ``"bool"``,
    or ``"null"``.  ``bool`` is tested before ``int`` because Python bools are
    int subclasses.

    Lists become ``{"type":"list","elem":<shape-of-first>|"empty",
    "len_bucket":<bucket>}``.

    Dicts become ``{"type":"dict","keys":{k: <shape>}}`` with keys sorted.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):  # must precede int check
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        n = len(value)
        elem: Any = extract_shape(value[0]) if n > 0 else "empty"
        return {"type": "list", "elem": elem, "len_bucket": _len_bucket(n)}
    if isinstance(value, dict):
        return {
            "type": "dict",
            "keys": {k: extract_shape(v) for k, v in sorted(value.items())},
        }
    # Fallback for types not covered above (e.g. bytes, custom classes)
    return type(value).__name__


# ---------------------------------------------------------------------------
# Internal shape differ
# ---------------------------------------------------------------------------

# Each element is a (detail_string, severity) tuple where severity is
# "fail" or "flag".  Callers unpack accordingly.
_ShapeDiff = tuple[str, str]


def _diff_shapes(shape_a: Any, shape_b: Any, path: str) -> list[_ShapeDiff]:
    """Recursively diff two shape trees.

    Returns a list of ``(detail, severity)`` tuples where *severity* is
    ``"fail"`` or ``"flag"``.  The *detail* string has the form
    ``"<path>: <compact-a> -> <compact-b>"``.

    List-emptiness transitions (one side empty / zero-bucket, the other
    populated) are ``"flag"``; all other shape changes are ``"fail"``.
    """
    if shape_a == shape_b:
        return []

    if isinstance(shape_a, dict) and isinstance(shape_b, dict):
        ta = shape_a.get("type")
        tb = shape_b.get("type")

        if ta == "dict" == tb:
            diffs: list[_ShapeDiff] = []
            all_keys = sorted(set(shape_a["keys"]) | set(shape_b["keys"]))
            for k in all_keys:
                ka = shape_a["keys"].get(k)
                kb = shape_b["keys"].get(k)
                child_path = f"{path}.{k}"
                if ka is None:
                    diffs.append((f"{child_path}: (absent) -> {_shape_to_str(kb)}", "fail"))
                elif kb is None:
                    diffs.append((f"{child_path}: {_shape_to_str(ka)} -> (absent)", "fail"))
                else:
                    diffs.extend(_diff_shapes(ka, kb, child_path))
            return diffs

        if ta == "list" == tb:
            ea = shape_a.get("elem")
            eb = shape_b.get("elem")
            ba = shape_a.get("len_bucket", "0")
            bb = shape_b.get("len_bucket", "0")

            a_empty = ea == "empty" or ba == "0"
            b_empty = eb == "empty" or bb == "0"

            if a_empty != b_empty:
                # One side empty, the other populated → flag (not fail)
                detail = (
                    f"{path}: {_shape_to_str(shape_a)} -> {_shape_to_str(shape_b)}"
                    " (list emptiness)"
                )
                return [(detail, "flag")]

            # Both non-empty: compare elem types and bucket separately
            list_diffs: list[_ShapeDiff] = []
            list_diffs.extend(_diff_shapes(ea, eb, f"{path}[]"))
            if ba != bb:
                list_diffs.append((f"{path}: bucket {ba} -> {bb}", "fail"))
            return list_diffs

    # Different structural types, or one side is a scalar string
    return [(f"{path}: {_shape_to_str(shape_a)} -> {_shape_to_str(shape_b)}", "fail")]


# ---------------------------------------------------------------------------
# diff_tool_records
# ---------------------------------------------------------------------------


def diff_tool_records(name: str, a: "dict | None", b: "dict | None") -> list[dict]:
    """Compare two tool-call records and return a list of Finding dicts.

    Each Finding has the shape::

        {"tool": str, "kind": str, "severity": "fail"|"flag", "detail": str}

    *kind* is one of: ``"status"``, ``"error_type"``, ``"shape"``,
    ``"latency"``, ``"size"``, ``"presence"``.

    A ``None`` record means the tool was absent from that run; this produces a
    single ``presence`` fail finding.

    Records are expected to carry the keys produced by the Task-3 runner:
    ``status``, ``shape`` (pre-computed), ``latency_ms``, ``response_bytes``,
    and optionally ``error_type``.
    """
    if a is None or b is None:
        which = "run-a" if a is None else "run-b"
        return [
            {
                "tool": name,
                "kind": "presence",
                "severity": "fail",
                "detail": f"tool absent from {which}",
            }
        ]

    findings: list[dict] = []

    # --- status ---
    if a.get("status") != b.get("status"):
        findings.append(
            {
                "tool": name,
                "kind": "status",
                "severity": "fail",
                "detail": f"{a.get('status')} -> {b.get('status')}",
            }
        )

    # --- error_type ---
    et_a = a.get("error_type")
    et_b = b.get("error_type")
    if et_a != et_b:
        findings.append(
            {
                "tool": name,
                "kind": "error_type",
                "severity": "fail",
                "detail": f"{et_a} -> {et_b}",
            }
        )

    # --- shape (pre-computed by the runner, stored under the "shape" key) ---
    for diff_str, sev in _diff_shapes(a["shape"], b["shape"], "result"):
        findings.append({"tool": name, "kind": "shape", "severity": sev, "detail": diff_str})

    # --- latency (±50 % threshold; guard only the divisor) ---
    lat_a = a.get("latency_ms")
    lat_b = b.get("latency_ms")
    if lat_a and lat_b is not None and abs(lat_b - lat_a) / lat_a > LATENCY_THRESHOLD:
        findings.append(
            {
                "tool": name,
                "kind": "latency",
                "severity": "flag",
                "detail": f"{lat_a}ms -> {lat_b}ms",
            }
        )

    # --- size (±50 % threshold; guard only the divisor) ---
    sz_a = a.get("response_bytes")
    sz_b = b.get("response_bytes")
    if sz_a and sz_b is not None and abs(sz_b - sz_a) / sz_a > SIZE_THRESHOLD:
        findings.append(
            {
                "tool": name,
                "kind": "size",
                "severity": "flag",
                "detail": f"{sz_a}B -> {sz_b}B",
            }
        )

    return findings


# ---------------------------------------------------------------------------
# diff_runs
# ---------------------------------------------------------------------------


def diff_runs(
    run_a: "dict[str, dict]", run_b: "dict[str, dict]"
) -> list[dict]:
    """Compare two full run snapshots; return a sorted list of Finding dicts.

    The union of tool names across both runs is covered (tools present in only
    one run produce ``presence`` findings).  Output is sorted by
    (severity=fail first, then tool name).
    """
    all_tools = sorted(set(run_a) | set(run_b))
    findings: list[dict] = []
    for tool_name in all_tools:
        findings.extend(
            diff_tool_records(tool_name, run_a.get(tool_name), run_b.get(tool_name))
        )

    # fail sorts before flag; within severity, sort by tool name
    findings.sort(key=lambda f: (0 if f["severity"] == "fail" else 1, f["tool"]))
    return findings


# ---------------------------------------------------------------------------
# Input catalog
# ---------------------------------------------------------------------------
#
# CATALOG maps each of the 48 registered tool names to a descriptor dict:
#
#   {
#       "args":         dict   — call arguments, may contain {placeholder} strings
#       "expectation":  "ok" | "error_ok"
#       "note":         str    — human-readable reason when not "" (esp. for error_ok)
#       "accepts_source": bool — True iff "source" is in the tool's input_schema
#   }
#
# Placeholder strings (replaced by render_args at call time):
#   {namespace}     — target namespace for most tools
#   {pod}           — a pod name within that namespace
#   {pipelinerun}   — a Tekton PipelineRun name
#   {pipelinerun_ns}— namespace that owns the PipelineRun
#
# Bounded-parameter rules (per plan spec):
#   limit: 10  |  tail_lines: 100  |  max_context_tokens: 5000  |  time_period: "1h"
#   max_namespaces: 3
# Applied wherever the tool schema declares that parameter; prevents the
# matrix sweep from issuing expensive or unbounded queries.
#
# Source of truth: tests/characterization/parity_reference.json
# → tools[<name>].input_schema  (NOT server docstrings; 8 of 48 tools have
#   no def in src/server-mcp.py).
# accepts_source = "source" in input_schema.properties  (41/48 tools).

CATALOG: dict[str, dict] = {
    "adaptive_namespace_investigation": {
        "args": {"namespace": "{namespace}"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "advanced_event_analytics": {
        "args": {"namespace": "{namespace}", "time_period": "1h"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "analyze_failed_pipeline": {
        "args": {"namespace": "{pipelinerun_ns}", "pipeline_run": "{pipelinerun}"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "analyze_logs": {
        "args": {"log_text": "INFO matrix-probe test log"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "analyze_logs_hybrid": {
        "args": {"namespace": "{namespace}", "pod_name": "{pod}"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "analyze_pod_logs_hybrid": {
        "args": {"namespace": "{namespace}", "pod_name": "{pod}"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "automated_triage_rca_report_generator": {
        "args": {"failure_identifier": "matrix-probe-noop"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "check_cluster_certificate_health": {
        "args": {},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "check_resource_constraints": {
        "args": {"namespace": "{namespace}"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "ci_cd_performance_baselining_tool": {
        "args": {"max_context_tokens": 5000},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "connect_cluster": {
        "args": {
            "name": "matrix-probe",
            "credential_ref": "env:MATRIX_NONEXISTENT_TOKEN_VAR",
        },
        "expectation": "error_ok",
        "note": "guaranteed structured error, no dial",
        "accepts_source": False,
    },
    "disconnect_cluster": {
        "args": {"name": "matrix-nonexistent-instance"},
        "expectation": "error_ok",
        "note": "guaranteed structured error (unknown_source), no registry mutation",
        "accepts_source": False,
    },
    "conservative_namespace_overview": {
        "args": {"namespace": "{namespace}"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "detect_anomalies": {
        "args": {"namespace": "{namespace}", "limit": 10},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "detect_log_anomalies": {
        "args": {"logs": "INFO matrix-probe test log"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "find_pipeline": {
        "args": {"pipeline_id_pattern": "matrix-probe-noop"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "get_etcd_logs": {
        "args": {"tail_lines": 100, "max_context_tokens": 5000},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "get_events_smart": {
        "args": {
            "namespace": "{namespace}",
            "max_context_tokens": 5000,
            "time_period": "1h",
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "get_kubernetes_resource": {
        "args": {
            "resource_type": "pod",
            "name": "{pod}",
            "namespace": "{namespace}",
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "get_machine_config_pool_status": {
        "args": {},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "get_openshift_cluster_operator_status": {
        "args": {},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "get_pipelinerun_logs": {
        "args": {
            "pipelinerun_name": "{pipelinerun}",
            "namespace": "{pipelinerun_ns}",
            "tail_lines": 100,
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "get_tekton_pipeline_runs_status": {
        "args": {"max_namespaces": 3},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "investigate_tls_certificate_issues": {
        "args": {"max_namespaces": 3},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "list_namespaces": {
        "args": {},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "list_pipelineruns": {
        "args": {"namespace": "{namespace}", "limit": 10},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "list_pods_in_namespace": {
        "args": {"namespace": "{namespace}", "limit": 10},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "list_recent_pipeline_runs": {
        "args": {"limit": 10},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "list_sources": {
        "args": {},
        "expectation": "ok",
        "note": "",
        "accepts_source": False,
    },
    "list_taskruns": {
        "args": {"namespace": "{namespace}"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "live_system_topology_mapper": {
        "args": {"max_context_tokens": 5000},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "manage_prediction_training_data": {
        "args": {"action": "stats", "max_namespaces": 3},
        "expectation": "ok",
        "note": "read-only action; 'stats' is the default and safe",
        "accepts_source": True,
    },
    "pipeline_tracer": {
        "args": {
            "trace_identifier": "matrix-probe-noop",
            "trace_type": "commit",
            "trace_depth": "shallow",
            "include_artifacts": False,
            "max_namespaces": 3,
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "predictive_log_analyzer": {
        "args": {"namespaces": ["{namespace}"], "max_namespaces": 3},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "progressive_event_analysis": {
        "args": {"namespace": "{namespace}", "time_period": "1h"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "prometheus_query": {
        "args": {"query": "count(up)", "limit": 5},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "query_kubearchive": {
        "args": {
            "resource_type": "Pod",
            "namespace": "{namespace}",
            "limit": 10,
        },
        "expectation": "error_ok",
        "note": "matrix forces KUBEARCHIVE_ENABLED=false",
        "accepts_source": True,
    },
    "query_metrics": {
        "args": {"query": "count(up)", "limit": 5},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "refresh_capabilities": {
        "args": {},
        "expectation": "ok",
        "note": "",
        "accepts_source": False,
    },
    "resource_bottleneck_forecaster": {
        "args": {},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "search_resources_by_labels": {
        "args": {
            "resource_types": ["pods"],
            "label_selectors": [{"key": "app", "value": "matrix-probe", "operator": "equals"}],
            "namespaces": ["{namespace}"],
            "limit_per_type": 10,
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "semantic_log_search": {
        "args": {"query": "error"},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "smart_get_namespace_events": {
        "args": {
            "namespace": "{namespace}",
            "max_context_tokens": 5000,
            "time_period": "1h",
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "smart_summarize_logs": {
        "args": {
            "namespace": "{namespace}",
            "pod_name": "{pod}",
            "tail_lines": 100,
            "max_context_tokens": 5000,
            "time_period": "1h",
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "smart_summarize_pod_logs": {
        "args": {
            "namespace": "{namespace}",
            "pod_name": "{pod}",
            "tail_lines": 100,
            "max_context_tokens": 5000,
            "time_period": "1h",
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "stream_analyze_logs": {
        "args": {
            "namespace": "{namespace}",
            "pod_name": "{pod}",
            "tail_lines": 100,
            "max_context_tokens": 5000,
            "time_period": "1h",
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "stream_analyze_pod_logs": {
        "args": {
            "namespace": "{namespace}",
            "pod_name": "{pod}",
            "tail_lines": 100,
            "max_context_tokens": 5000,
            "time_period": "1h",
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "topology_mapper": {
        "args": {"max_context_tokens": 5000},
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
    "what_if_scenario_simulator": {
        "args": {
            "scenario_type": "resource_limits",
            "changes": {"cpu_limit": "500m"},
            "scope": {"namespaces": ["{namespace}"]},
        },
        "expectation": "ok",
        "note": "",
        "accepts_source": True,
    },
}


def _fill_placeholders(value: Any, targets: dict) -> Any:
    """Recursively replace ``{key}`` placeholders in *value* using *targets*.

    Strings are replaced directly.  Lists and dicts are walked recursively so
    that placeholders nested inside compound args (e.g. ``namespaces: ["{namespace}"]``
    or ``scope: {"namespaces": ["{namespace}"]}`` ) are also resolved.
    All other value types are returned unchanged.
    """
    if isinstance(value, str):
        for placeholder, replacement in targets.items():
            value = value.replace(f"{{{placeholder}}}", replacement)
        return value
    if isinstance(value, list):
        return [_fill_placeholders(item, targets) for item in value]
    if isinstance(value, dict):
        return {k: _fill_placeholders(v, targets) for k, v in value.items()}
    return value


def render_args(entry: dict, targets: dict, source: "str | None") -> dict:
    """Resolve placeholder strings in an entry's args and optionally inject source.

    Replaces ``{namespace}``, ``{pod}``, ``{pipelinerun}``, and
    ``{pipelinerun_ns}`` placeholders anywhere in the args tree — top-level
    strings, strings inside lists, or strings inside nested dicts — with the
    corresponding value from *targets*.  Non-string values pass through
    unchanged.

    Injects ``"source": source`` into the result iff *source* is not ``None``
    **and** ``entry["accepts_source"]`` is truthy.

    The original ``entry["args"]`` dict is never mutated.
    """
    result: dict = {k: _fill_placeholders(v, targets) for k, v in entry["args"].items()}
    if source is not None and entry.get("accepts_source"):
        result["source"] = source
    return result


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")
_USAGE = (
    "Usage:\n"
    "  live_matrix.py run"
    " [--tools t1,t2,...] [--timeout SECS]"
    " [--namespace NS] [--pod NAME]"
    " [--pipelinerun NS/NAME] [--source NAME]"
    " [--connect NAME=CREDREF]\n"
    "  live_matrix.py diff A B [--json]\n"
    "  live_matrix.py diff --baseline RUN [--json]\n"
    "  live_matrix.py bless RUN\n"
    "  live_matrix.py list\n"
    "\n"
    "Flags:\n"
    "  --pipelinerun NS/NAME  PipelineRun in namespace NS with name NAME (slash-separated)\n"
    "  --source NAME          Source name forwarded to every tool that accepts it;\n"
    "                         use --connect to activate extensions for kubeconfig-dir\n"
    "                         discovered instances before the sweep\n"
    "  --connect NAME=CREDREF Call connect_cluster(name, credential_ref) after\n"
    "                         session.initialize() so extensions activate for the\n"
    "                         named source; may be repeated for multiple clusters\n"
    "                         (split on first '=': left=name, right=credential_ref)\n"
    "  --help / -h            Print this usage and exit 0\n"
)

# Parity seam: points at the single source of truth for registered tool names.
# Separated from CATALOG so tests can monkeypatch CATALOG (sweep set) without
# disabling the unconditional parity check, and vice-versa.
_PARITY_PATH = _REPO_ROOT / "tests" / "characterization" / "parity_reference.json"


def _parity_names() -> set[str]:
    """Return the set of tool names from ``parity_reference.json``.

    This is the authoritative source for parity checks — the server's
    advertised tools must exactly match this set.  Tests monkeypatch this
    function to simulate a diverged tool surface without modifying CATALOG.
    """
    with open(_PARITY_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    return set(data["tools"].keys())


class ToolSetMismatch(Exception):
    """Raised when the server's advertised tools do not match CATALOG.

    Caught at the synchronous boundary in ``run_matrix``; process exits 3.
    Raising a typed exception (rather than calling sys.exit inside an anyio
    task group) avoids BaseExceptionGroup wrapping that would make the process
    exit 1 with a traceback instead of exit 3.
    """

    def __init__(self, only_server: list[str], only_catalog: list[str]) -> None:
        self.only_server = only_server
        self.only_catalog = only_catalog
        super().__init__(
            f"server-only={only_server!r}, catalog-only={only_catalog!r}"
        )


def _sanitize_for_dirname(s: str) -> str:
    """Replace chars outside ``[A-Za-z0-9._-]`` with hyphens."""
    return _SANITIZE_RE.sub("-", s)


def _get_cluster_id() -> str:
    """Parse cluster_id from kubeconfig current-context.

    Uses the first path in ``$KUBECONFIG``, falling back to
    ``~/.kube/config``.  Returns ``"unknown"`` on any failure.
    """
    try:
        import yaml  # third-party; kept local to avoid slowing imports

        kubeconfig_env = os.environ.get("KUBECONFIG", "")
        kubeconfig_path = (
            kubeconfig_env.split(":")[0]
            if kubeconfig_env
            else str(Path.home() / ".kube" / "config")
        )
        with open(kubeconfig_path) as fh:
            kc = yaml.safe_load(fh)
        cluster_id = (kc or {}).get("current-context") or "unknown"
    except Exception:
        cluster_id = "unknown"
    return cluster_id


def _compute_fingerprint() -> dict:
    """Return ``{git_sha, dirty, python, cluster_id}``."""
    repo = str(_REPO_ROOT)
    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
    except Exception:
        git_sha = "unknown"

    try:
        dirty_out = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo, text=True
        )
        dirty = bool(dirty_out.strip())
    except Exception:
        dirty = False

    return {
        "git_sha": git_sha,
        "dirty": dirty,
        "python": sys.version,
        "cluster_id": _get_cluster_id(),
    }


def _make_run_name(started_utc: "datetime", cluster_id: str) -> str:
    """Build the run directory base name from *started_utc* and *cluster_id*."""
    ts = started_utc.strftime("%Y%m%d-%H%M%S")
    cluster_part = _sanitize_for_dirname(cluster_id)[:40]
    return f"{ts}-{cluster_part}"


def _make_unique_dirs(runs_root: Path, base_name: str) -> "tuple[Path, Path]":
    """Return ``(tmp_dir, final_dir)`` with a unique suffix when *base_name* is taken.

    The *final_dir* name is ``base_name`` if unclaimed, otherwise
    ``base_name-2``, ``base_name-3``, … up to 999.  The *tmp_dir* is
    ``final_dir`` + ``".tmp"`` — it is the working directory that the caller
    fills and then atomically renames to *final_dir* on success.
    """
    candidate = runs_root / base_name
    if not candidate.exists():
        return runs_root / f"{base_name}.tmp", candidate
    for n in range(2, 1000):
        candidate = runs_root / f"{base_name}-{n}"
        if not candidate.exists():
            return runs_root / f"{candidate.name}.tmp", candidate
    raise RuntimeError(
        f"Could not find a unique run-dir name under {runs_root}/{base_name}"
    )


def _write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* atomically via a sibling ``.tmp`` file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _extract_error_code(payload: Any) -> str:
    """Extract a stable error identifier from *payload* (F-9 ordering).

    Checks keys in priority order: ``code`` → ``error_type`` → first 80 chars
    of ``error``.  Stable codes (``code``) take precedence over free-text error
    messages so that wording changes do not break future diffs.
    """
    if isinstance(payload, dict):
        if payload.get("code"):
            return str(payload["code"])
        if payload.get("error_type"):
            return str(payload["error_type"])
        if payload.get("error"):
            return str(payload["error"])[:80]
    return "MCPError"


# ---------------------------------------------------------------------------
# Shared response-unwrapping helper (F7 unification)
# ---------------------------------------------------------------------------


def _parse_response(resp: Any, *, non_json_as_none: bool = True) -> Any:
    """Extract first text content block from an MCP response and JSON-parse it.

    Used by both the discovery phase and the sweep loop to decode call_tool
    responses.  The two callers differ only in how non-JSON content is handled:

    When non_json_as_none=True (default — discovery semantics):
        An absent, empty, or non-JSON response maps to ``None``.

    When non_json_as_none=False (sweep semantics):
        Non-JSON text is returned as the raw string so ``extract_shape`` can
        characterise a plain-text tool response.  Absent or empty content
        returns an empty string.
    """
    raw = ""
    if resp is not None and resp.content:
        first = resp.content[0]
        raw = getattr(first, "text", str(first))
    if not raw:
        return None if non_json_as_none else ""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None if non_json_as_none else raw


# ---------------------------------------------------------------------------
# Discovery helper (mcp imports are function-local per module constraint)
# ---------------------------------------------------------------------------


async def _discover_targets_async(
    session: Any,
    *,
    source: "str | None",
    timeout_secs: float,
) -> "dict[str, str | None]":
    """Discover live targets via MCP tool calls on an initialized session.

    Makes up to 1 + N + 1 calls (list_namespaces, N pod probes ≤ 5,
    list_recent_pipeline_runs).  Each call is bounded by *timeout_secs*.

    Namespace selection:
      Preferred candidates match ``konflux|tenant`` (case-insensitive);
      non-matching namespaces fill remaining probe slots so the budget is
      never exhausted on preferred-only namespaces.  Picks the first namespace
      with a ``Running`` pod; falls back to the first namespace with any pod.

    Pipeline run selection:
      ``list_recent_pipeline_runs`` → ``{ns: [run_dicts]}``; first run
      supplies ``pipelinerun_ns`` and ``pipelinerun``.

    *source* is forwarded on every call (all three tools accept the source
    parameter) so the sweep and discovery target the same cluster.

    Returns a dict with keys ``namespace``, ``pod``, ``pipelinerun``,
    ``pipelinerun_ns``; any value may be ``None`` when the probe fails.

    ``anyio.BrokenResourceError`` is NOT caught — callers handle it so the
    completeness guard can detect a broken-transport scenario.
    ``ExceptionGroup``-wrapped transport failures cannot arise here because
    these calls are plain ``await``s, not nested task groups; they propagate
    as bare ``BrokenResourceError`` which the per-call ``except … raise``
    guards below re-raise to the caller.
    All other exceptions produce ``None`` for the relevant target.
    """
    import anyio  # noqa: PLC0415

    _PREF_RE = re.compile(r"konflux|tenant", re.IGNORECASE)
    _MAX_PROBE = 5

    result: dict[str, Any] = {
        "namespace": None,
        "pod": None,
        "pipelinerun": None,
        "pipelinerun_ns": None,
    }

    # Discovery uses module-level _parse_response with default non_json_as_none=True
    # (absent/empty/non-JSON → None).

    def _build_args(base: dict) -> dict:
        """Inject ``source`` into *base* when provided."""
        if source is not None:
            return {**base, "source": source}
        return base

    # --- 1. list_namespaces -----------------------------------------------
    try:
        _ns_resp: Any = None
        with anyio.move_on_after(timeout_secs):
            _ns_resp = await session.call_tool("list_namespaces", _build_args({}))
        ns_payload = _parse_response(_ns_resp)
    except anyio.BrokenResourceError:
        raise
    except Exception:
        ns_payload = None

    if isinstance(ns_payload, list):
        namespaces: list[str] = [n for n in ns_payload if isinstance(n, str)]
        preferred = [n for n in namespaces if _PREF_RE.search(n)]
        # Preferred namespaces probed first; non-preferred fill remaining slots
        # so the budget is never wasted on an all-preferred list with no pods.
        candidates = (preferred + [n for n in namespaces if n not in preferred])[:_MAX_PROBE]

        chosen_ns: str | None = None
        chosen_pod: str | None = None
        fallback_ns: str | None = None
        fallback_pod: str | None = None

        for ns in candidates:
            try:
                _pods_resp: Any = None
                with anyio.move_on_after(timeout_secs):
                    _pods_resp = await session.call_tool(
                        "list_pods_in_namespace",
                        _build_args({"namespace": ns, "limit": 10}),
                    )
                pods_payload = _parse_response(_pods_resp)
            except anyio.BrokenResourceError:
                raise
            except Exception:
                continue

            if not isinstance(pods_payload, list):
                continue

            for pod in pods_payload:
                if not isinstance(pod, dict):
                    continue  # skips string sentinels like "_truncation"
                pod_name = pod.get("name")
                if not pod_name:
                    continue
                if fallback_ns is None:
                    fallback_ns, fallback_pod = ns, pod_name
                if pod.get("status") == "Running":
                    chosen_ns, chosen_pod = ns, pod_name
                    break
            if chosen_ns is not None:
                break

        result["namespace"] = chosen_ns or fallback_ns
        result["pod"] = chosen_pod or fallback_pod

    # --- 2. list_recent_pipeline_runs -------------------------------------
    try:
        _pr_resp: Any = None
        with anyio.move_on_after(timeout_secs):
            _pr_resp = await session.call_tool(
                "list_recent_pipeline_runs", _build_args({"limit": 10})
            )
        pr_payload = _parse_response(_pr_resp)
    except anyio.BrokenResourceError:
        raise
    except Exception:
        pr_payload = None

    if (
        isinstance(pr_payload, dict)
        and "error" not in pr_payload
        and "error_type" not in pr_payload
    ):
        for ns, runs in pr_payload.items():
            if isinstance(runs, list) and runs:
                first_run = runs[0]
                if isinstance(first_run, dict) and first_run.get("name"):
                    result["pipelinerun"] = first_run["name"]
                    result["pipelinerun_ns"] = first_run.get("namespace", ns)
                    break

    return result


# ---------------------------------------------------------------------------
# Async runner core (mcp imports are function-local per module constraint)
# ---------------------------------------------------------------------------


async def _run_sweep_async(
    *,
    tools: list[str] | None,
    partial: bool,
    timeout_secs: float,
    flags: list[str],
    namespace_override: "str | None" = None,
    pod_override: "str | None" = None,
    pipelinerun_override: "str | None" = None,
    pipelinerun_ns_override: "str | None" = None,
    source: "str | None" = None,
    connect_pairs: "list[tuple[str, str]] | None" = None,
) -> Path:
    """Spawn the MCP server, sweep the requested tools, persist results atomically.

    The run directory is created as ``<name>.tmp`` and renamed to its final
    name only on success.  An aborted run (parity mismatch, unhandled
    exception) leaves the ``.tmp`` directory in place so that
    ``list``/``diff`` commands do not enumerate incomplete runs.

    All ``mcp`` imports live inside this function so that importing the
    module remains cheap (< 50 ms measured).
    """
    # Function-local mcp imports (module constraint: never at module level)
    import anyio  # noqa: PLC0415 — anyio is the MCP SDK's async backend
    from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
    from mcp.client.stdio import stdio_client  # noqa: PLC0415

    started_utc = datetime.now(timezone.utc)

    # Determine and create runs root
    runs_root = _runs_root()
    runs_root.mkdir(parents=True, exist_ok=True)

    # Compute fingerprint (git + cluster)
    fingerprint = _compute_fingerprint()

    # Build a unique (tmp_dir, final_dir) pair — atomic rename on success (F-4)
    base_name = _make_run_name(started_utc, fingerprint["cluster_id"])
    run_dir_tmp, run_dir_final = _make_unique_dirs(runs_root, base_name)
    run_dir_tmp.mkdir(parents=True, exist_ok=True)
    (run_dir_tmp / "raw").mkdir(exist_ok=True)

    # Child-process env rule: pass full env + transport overrides so KUBECONFIG
    # reaches the subprocess (the MCP SDK default allowlist would drop it).
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["main.py"],
        env={**os.environ, "LUMINO_TRANSPORT": "stdio", "KUBEARCHIVE_ENABLED": "false"},
        cwd=str(_REPO_ROOT),
    )

    # targets holds the actual resolved values (null until discovery/overrides fill
    # them in).  resolved_targets is the sweep-ready copy: None → sentinel string.
    # Both are computed inside the ClientSession context after discovery.
    targets: dict = {
        "namespace": None,
        "pod": None,
        "pipelinerun": None,
        "pipelinerun_ns": None,
    }
    resolved_targets: dict = {}
    tool_seconds: dict[str, float] = {}

    # Lifted outside the try block so the completeness guard (N-1) can read
    # them after except* catches BrokenResourceError.  Empty lists mean the
    # session never reached the sweep phase (e.g. initialize() failed).
    sweep_names: list[str] = []

    # connect_cluster results: lifted so the manifest writer can always read it
    # even when BrokenResourceError fires during teardown.
    connected_sources: list[dict] = []

    # Parity-mismatch flag: set inside the MCP context, raised AFTER both
    # `async with` blocks exit.  Raising inside ClientSession's anyio task
    # group wraps the exception in BaseExceptionGroup (process exits 1 with
    # traceback) — raising outside avoids that wrapping entirely (F-1 fix).
    _parity_error: tuple[list[str], list[str]] | None = None

    # Transport-failure flag (F4): set when anyio.BrokenResourceError or
    # anyio.ClosedResourceError surfaces during a tool call (mid-sweep transport
    # death) or during teardown (caught by except*).  Manifested as
    # transport_failure=true; bless refuses such runs; diff warns.
    _transport_failure: bool = False

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # connect_cluster calls: run before parity check and discovery so
                # extensions activate for kubeconfig-dir-discovered sources.
                # Failures are logged as warnings and do not abort the sweep — some
                # tools will error, which is informative.
                for _cc_name, _cc_cred in (connect_pairs or []):
                    _cc_result_raw: Any = None
                    try:
                        with anyio.move_on_after(timeout_secs):
                            _cc_result_raw = await session.call_tool(
                                "connect_cluster",
                                {"name": _cc_name, "credential_ref": _cc_cred},
                            )
                        _cc_payload = _parse_response(_cc_result_raw)
                        _cc_ok = (
                            _cc_result_raw is not None
                            and not _cc_result_raw.isError
                            and isinstance(_cc_payload, dict)
                            and _cc_payload.get("connected")
                        )
                        _cc_record = {
                            "name": _cc_name,
                            "credential_ref": _cc_cred,
                            "connected": _cc_ok,
                            "result": _cc_payload,
                        }
                        if _cc_ok:
                            print(
                                f"connect_cluster: {_cc_name!r} connected",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"WARNING: connect_cluster: {_cc_name!r} failed"
                                f" — result: {_cc_payload!r}",
                                file=sys.stderr,
                            )
                    except Exception as _cc_exc:
                        _cc_record = {
                            "name": _cc_name,
                            "credential_ref": _cc_cred,
                            "connected": False,
                            "result": {"error": str(_cc_exc)},
                        }
                        print(
                            f"WARNING: connect_cluster: {_cc_name!r} raised"
                            f" {type(_cc_exc).__name__}: {_cc_exc}",
                            file=sys.stderr,
                        )
                    connected_sources.append(_cc_record)

                # Parity check: UNCONDITIONAL — applies to both explicit and full
                # sweeps.  Compares advertised tools against parity_reference.json
                # (not CATALOG) so tests can monkeypatch CATALOG to a smaller
                # sweep set without disabling the parity gate.  Tests that want to
                # simulate a parity mismatch monkeypatch _parity_names() instead.
                parity_tool_names = _parity_names()
                tools_list_result = await session.list_tools()
                advertised = {t.name for t in tools_list_result.tools}

                if advertised != parity_tool_names:
                    # Record mismatch; do NOT raise here (inside task group).
                    # The raise happens below, after both context managers exit.
                    _parity_error = (
                        sorted(advertised - parity_tool_names),
                        sorted(parity_tool_names - advertised),
                    )

                if not _parity_error:
                    # Determine sweep order (sorted, explicit subset or full catalog).
                    # CATALOG is read at call time so monkeypatching works.
                    sweep_names = (
                        sorted(tools) if tools is not None else sorted(CATALOG.keys())
                    )

                    # Discovery phase: fill targets from live cluster before sweep.
                    # General exceptions degrade to null targets (no cluster available).
                    # BrokenResourceError propagates to the outer except* (transport fail).
                    try:
                        discovered = await _discover_targets_async(
                            session, source=source, timeout_secs=timeout_secs
                        )
                        for k, v in discovered.items():
                            if v is not None:
                                targets[k] = v
                    except anyio.BrokenResourceError:
                        raise
                    except Exception:
                        pass  # degrade: keep all targets as None

                    # CLI overrides win over discovery (applied after).
                    if namespace_override is not None:
                        targets["namespace"] = namespace_override
                    if pod_override is not None:
                        targets["pod"] = pod_override
                    if pipelinerun_override is not None:
                        targets["pipelinerun"] = pipelinerun_override
                    if pipelinerun_ns_override is not None:
                        targets["pipelinerun_ns"] = pipelinerun_ns_override

                    # Resolve None → sentinel for arg rendering.
                    resolved_targets = {
                        k: v if v is not None else "matrix-missing-target"
                        for k, v in targets.items()
                    }

                    for name in sweep_names:
                        record: dict = {
                            "args": {},
                            "expectation": None,
                            "status": "error",
                            "error_type": None,
                            "latency_ms": None,
                            "response_bytes": None,
                            "shape": None,
                        }
                        raw_text = ""
                        elapsed_ms: float = 0.0

                        t0 = time.monotonic()
                        try:
                            # CATALOG lookup is inside try so an unknown name records
                            # status="error" and the sweep continues (F-7).
                            entry = CATALOG[name]
                            call_args = render_args(entry, resolved_targets, source=source)
                            record["args"] = call_args
                            record["expectation"] = entry["expectation"]

                            # Use anyio.move_on_after rather than asyncio.wait_for:
                            # asyncio.wait_for spawns a detached asyncio task whose
                            # cancellation disrupts anyio's internal memory streams
                            # inside stdio_client (BrokenResourceError).
                            # anyio.move_on_after uses the same cancel-scope mechanism
                            # the MCP SDK itself uses, so teardown is clean.
                            call_result: Any = None
                            with anyio.move_on_after(timeout_secs) as cancel_scope:
                                call_result = await session.call_tool(name, call_args)
                            elapsed_ms = (time.monotonic() - t0) * 1000

                            if cancel_scope.cancelled_caught:
                                record.update(
                                    {"status": "timeout", "latency_ms": elapsed_ms}
                                )
                            else:
                                # Extract raw text for file write and response_bytes
                                raw_text = ""
                                if call_result.content:
                                    first = call_result.content[0]
                                    raw_text = getattr(first, "text", str(first))

                                # Parse payload via shared helper (sweep mode:
                                # non-JSON → raw string so extract_shape captures it)
                                payload: Any = _parse_response(
                                    call_result, non_json_as_none=False
                                )

                                response_bytes = len(raw_text.encode("utf-8"))
                                shape = extract_shape(payload)

                                # Classify status: isError flag OR in-band error keys
                                if call_result.isError:
                                    status = "error"
                                    error_type: str | None = _extract_error_code(payload)
                                elif isinstance(payload, dict) and (
                                    "error" in payload or "error_type" in payload
                                ):
                                    status = "error"
                                    error_type = _extract_error_code(payload)
                                else:
                                    status = "ok"
                                    error_type = None

                                record.update(
                                    {
                                        "status": status,
                                        "error_type": error_type,
                                        "latency_ms": elapsed_ms,
                                        "response_bytes": response_bytes,
                                        "shape": shape,
                                    }
                                )

                        except Exception as exc:
                            elapsed_ms = (time.monotonic() - t0) * 1000
                            # F4: tag run as transport-failed when the MCP transport
                            # itself dies mid-sweep (BrokenResourceError or
                            # ClosedResourceError from anyio).
                            if isinstance(
                                exc,
                                (anyio.BrokenResourceError, anyio.ClosedResourceError),
                            ):
                                _transport_failure = True
                            record.update(
                                {
                                    "status": "error",
                                    "error_type": type(exc).__name__,
                                    "latency_ms": elapsed_ms,
                                }
                            )

                        tool_seconds[name] = elapsed_ms / 1000

                        # Sanitize tool name for use as a filename (F-D1).
                        # Adversarial --tools values (e.g. "../escaped") must not
                        # write records outside the run directory.  Valid CATALOG
                        # names (a-z, _, digits) are unchanged by this transform.
                        safe_name = _sanitize_for_dirname(name)

                        # Persist raw response and tool record atomically
                        _write_atomic(run_dir_tmp / "raw" / f"{safe_name}.txt", raw_text)
                        _write_atomic(
                            run_dir_tmp / f"{safe_name}.json", json.dumps(record, indent=2)
                        )

    except* anyio.BrokenResourceError:
        # When anyio.move_on_after fires while a tool call is in-flight, the
        # MCP SDK's stdio_client stdout_reader task may raise BrokenResourceError
        # during context teardown (the ClientSession receiver closes before the
        # subprocess can drain its response).  All tool records are written
        # inside the sweep loop BEFORE this teardown error, so suppressing it
        # here is safe — execution continues to write the manifest.
        #
        # F4: mark the run as transport-failed.  Teardown-phase BrokenResourceError
        # can be benign (a completed sweep where the subprocess drains slowly) but
        # the flag lets bless refuse and diff warn so operators are informed.
        #
        # N-1 COMPLETENESS GUARD (below) distinguishes this safe case from a
        # genuine early failure (initialize()/list_tools() broken), where
        # sweep_names would still be empty.
        _transport_failure = True

    # N-1 completeness guard: a BrokenResourceError during initialize(),
    # list_tools(), or the discovery phase leaves sweep_names empty and
    # tool_seconds empty, making len(tool_seconds)==len(sweep_names) trivially
    # true (0==0).  Guard against that false-success by aborting unless
    # sweep_names is non-empty AND every requested tool has a record.
    # Parity-mismatch runs also leave sweep_names empty, but those are handled
    # by ToolSetMismatch below, so check _parity_error first.
    if not _parity_error and (not sweep_names or len(tool_seconds) < len(sweep_names)):
        n_got = len(tool_seconds)
        n_want = len(sweep_names) if sweep_names else "unknown (session failed before sweep start)"
        print(
            f"Error: session failed before sweep completed "
            f"({n_got}/{n_want} tool records written); "
            f"partial run left at: {run_dir_tmp}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Raise parity error AFTER both context managers have exited cleanly.
    # This avoids BaseExceptionGroup wrapping by anyio's task group (F-1).
    if _parity_error is not None:
        raise ToolSetMismatch(*_parity_error)

    finished_utc = datetime.now(timezone.utc)
    total_seconds = (finished_utc - started_utc).total_seconds()

    manifest = {
        "fingerprint": fingerprint,
        "flags": flags,
        "targets": targets,
        "connected_sources": connected_sources,
        "tool_seconds": tool_seconds,
        "total_seconds": total_seconds,
        "partial": partial,
        "transport_failure": _transport_failure,
        "started_utc": started_utc.isoformat(),
        "finished_utc": finished_utc.isoformat(),
    }
    _write_atomic(run_dir_tmp / "manifest.json", json.dumps(manifest, indent=2))

    # Atomic rename: only on full success does the final-named dir appear (F-4)
    os.replace(run_dir_tmp, run_dir_final)
    return run_dir_final


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_matrix(argv: list[str]) -> Path:
    """Parse *argv* and run the matrix sweep; return the run directory.

    ``argv[0]`` must be ``"run"``.  Supported flags (both ``--key value``
    and ``--key=value`` forms are accepted):

    ``--tools t1,t2,...``
        Comma-separated tool names to sweep (sets ``partial=True``).
        If omitted, all CATALOG tools are swept (``partial=False``).

    ``--timeout SECS``
        Per-tool call timeout in seconds; accepts floats (default 300).

    ``--namespace NS``
        Override the discovered namespace target.

    ``--pod NAME``
        Override the discovered pod target.

    ``--pipelinerun NS/NAME``
        Override the discovered pipelinerun target.  The value must be in
        ``NS/NAME`` format; it sets both ``pipelinerun_ns`` and ``pipelinerun``
        in the manifest targets.

    ``--source NAME``
        Source name passed to every tool that ``accepts_source``; recorded in
        manifest flags.  Use ``--connect`` when the source is a kubeconfig-dir
        discovered instance that needs extension activation before the sweep.

    ``--connect NAME=CREDREF``
        Call ``connect_cluster(name=NAME, credential_ref=CREDREF)`` after
        ``session.initialize()`` but before discovery and the tool sweep.
        Split on the first ``=``; left side is the cluster name, right side is
        the credential_ref string.  May be repeated for multiple clusters.
        Connection failures are logged as warnings and do not abort the sweep.

    Unknown flags or missing values exit 2 with a usage message on stderr.
    A tool-set parity mismatch exits 3 with the two diff lists on stderr.
    """
    if not argv or argv[0] != "run":
        raise ValueError(f"Expected 'run' subcommand, got: {argv!r}")

    # --help / -h: print usage and exit 0 (checked before strict parsing)
    if "--help" in argv or "-h" in argv:
        print(_USAGE)
        sys.exit(0)

    tools: list[str] | None = None
    timeout_secs: float = 300.0
    namespace_override: str | None = None
    pod_override: str | None = None
    pipelinerun_override: str | None = None
    pipelinerun_ns_override: str | None = None
    source: str | None = None
    connect_pairs: list[tuple[str, str]] = []

    # Strict parser: supports --key value and --key=value; rejects unknown flags
    i = 1
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            print(f"Error: unexpected argument: {arg!r}", file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            sys.exit(2)

        if "=" in arg:
            key, _, val = arg.partition("=")
            i += 1
        else:
            key = arg
            if i + 1 >= len(argv):
                print(f"Error: {key} requires a value", file=sys.stderr)
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
            val = argv[i + 1]
            i += 2

        if key == "--tools":
            # Deduplicate names (preserve first occurrence order) and reject empty val.
            tools = list(dict.fromkeys(t.strip() for t in val.split(",") if t.strip()))
            if not tools:
                print("Error: --tools requires at least one tool name", file=sys.stderr)
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
        elif key == "--timeout":
            try:
                timeout_secs = float(val)
            except ValueError:
                print(
                    f"Error: --timeout must be a number, got {val!r}",
                    file=sys.stderr,
                )
                sys.exit(2)
        elif key == "--namespace":
            if not val:
                print("Error: --namespace requires a non-empty value", file=sys.stderr)
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
            namespace_override = val
        elif key == "--pod":
            if not val:
                print("Error: --pod requires a non-empty value", file=sys.stderr)
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
            pod_override = val
        elif key == "--pipelinerun":
            if val.count("/") != 1:
                print(
                    f"Error: --pipelinerun requires exactly one '/' separator"
                    f" (NS/NAME), got {val!r}",
                    file=sys.stderr,
                )
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
            ns_part, _, name_part = val.partition("/")
            if not ns_part or not name_part:
                print(
                    f"Error: --pipelinerun requires NS/NAME format, got {val!r}",
                    file=sys.stderr,
                )
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
            pipelinerun_ns_override = ns_part
            pipelinerun_override = name_part
        elif key == "--source":
            if not val:
                print("Error: --source requires a non-empty value", file=sys.stderr)
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
            source = val
        elif key == "--connect":
            if not val or "=" not in val:
                print(
                    "Error: --connect requires NAME=CREDREF format"
                    f" (split on first '='), got {val!r}",
                    file=sys.stderr,
                )
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
            _cc_name_part, _, _cc_cred_part = val.partition("=")
            if not _cc_name_part or not _cc_cred_part:
                print(
                    "Error: --connect NAME=CREDREF must have non-empty NAME"
                    f" and CREDREF, got {val!r}",
                    file=sys.stderr,
                )
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
            connect_pairs.append((_cc_name_part, _cc_cred_part))
        else:
            print(f"Error: unknown flag: {key!r}", file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            sys.exit(2)

    partial = tools is not None

    try:
        return asyncio.run(
            _run_sweep_async(
                tools=tools,
                partial=partial,
                timeout_secs=timeout_secs,
                flags=argv,
                namespace_override=namespace_override,
                pod_override=pod_override,
                pipelinerun_override=pipelinerun_override,
                pipelinerun_ns_override=pipelinerun_ns_override,
                source=source,
                connect_pairs=connect_pairs or None,
            )
        )
    except ToolSetMismatch as exc:
        if exc.only_server:
            print(
                f"Tools in server but not in parity reference: {exc.only_server}",
                file=sys.stderr,
            )
        if exc.only_catalog:
            print(
                f"Tools in parity reference but not in server: {exc.only_catalog}",
                file=sys.stderr,
            )
        sys.exit(3)


# ---------------------------------------------------------------------------
# Shared runs-root helper
# ---------------------------------------------------------------------------


def _runs_root() -> Path:
    """Return the run-storage root from env or the default repo-relative path."""
    return Path(
        os.environ.get("LIVE_MATRIX_ROOT", str(_REPO_ROOT / "live-matrix-runs"))
    )


def _reject_unsafe_run_name(name: str) -> None:
    """Exit 2 if *name* would traverse outside the runs root.

    A run name is safe iff it equals its own sanitized form — i.e. it contains
    only ``[A-Za-z0-9._-]`` and has no path separators.  Any name that differs
    after sanitization (e.g. ``../outside``, ``/etc/passwd``) is rejected with
    exit 2 rather than silently rewritten, so filesystem access stays inside
    ``runs_root``.
    """
    if not name or name in (".", "..") or _sanitize_for_dirname(name) != name:
        print(
            f"Error: run name contains disallowed characters: {name!r}",
            file=sys.stderr,
        )
        print(_USAGE, file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# diff subcommand helpers
# ---------------------------------------------------------------------------


def _load_run_dir(run_dir: Path) -> "tuple[dict, dict[str, dict]]":
    """Load manifest and all tool records from *run_dir*.

    Returns ``(manifest, tool_records)`` where *tool_records* maps each tool
    name to its persisted record dict.

    Exits 3 (structural) if:
    - *run_dir* does not exist or is not a directory
    - ``manifest.json`` cannot be read or parsed
    - any tool record cannot be read/parsed or is missing the ``"shape"`` key
      (the Task-1 differ subscripts ``a["shape"]`` bare; validation here prevents
      a bare KeyError traceback instead of the expected exit 3)
    """
    if not run_dir.is_dir():
        print(f"Error: run directory not found: {run_dir}", file=sys.stderr)
        sys.exit(3)

    manifest_path = run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Error: cannot read manifest {manifest_path}: {exc}", file=sys.stderr)
        sys.exit(3)

    tool_records: dict[str, dict] = {}
    for rec_path in sorted(run_dir.glob("*.json")):
        if rec_path.name == "manifest.json":
            continue
        tool_name = rec_path.stem
        try:
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(
                f"Error: cannot read tool record {rec_path}: {exc}", file=sys.stderr
            )
            sys.exit(3)
        if not isinstance(rec, dict) or "shape" not in rec:
            reason = (
                "not a JSON object"
                if not isinstance(rec, dict)
                else "missing required 'shape' key"
            )
            print(
                f"Error: tool record {rec_path.name} is invalid ({reason})"
                f" in run: {run_dir.name}",
                file=sys.stderr,
            )
            sys.exit(3)
        tool_records[tool_name] = rec

    return manifest, tool_records


def _fp_header(label: str, fp: dict) -> str:
    """Format a single fingerprint line for the diff header."""
    sha8 = fp.get("git_sha", "unknown")[:8]
    dirty_mark = " dirty" if fp.get("dirty") else ""
    cluster = fp.get("cluster_id", "unknown")
    return f"{label}: {sha8}{dirty_mark} {cluster}"


def _status_info_lines(records: "dict[str, dict]") -> "list[str]":
    """Compute info lines from run B's records (status-vs-expectation mismatches).

    These are NEVER findings and NEVER affect exit codes — they are purely
    informational annotations printed after the structural findings.
    """
    lines: list[str] = []
    for tool_name in sorted(records):
        rec = records[tool_name]
        status = rec.get("status")
        expectation = rec.get("expectation")
        if expectation == "ok" and status != "ok":
            lines.append(
                f"info: {tool_name}: expected ok, got {status}"
            )
        elif expectation == "error_ok" and status != "error":
            # timeout is NOT a structured error: it must trigger an info line
            lines.append(
                f"info: {tool_name}: expected error_ok, got {status}"
            )
    return lines


# ---------------------------------------------------------------------------
# diff subcommand
# ---------------------------------------------------------------------------


def diff_matrix(argv: "list[str]") -> None:
    """Handle ``diff A B`` and ``diff --baseline RUN`` subcommands.

    Exits 0 when there are no ``fail`` findings, 1 when any ``fail`` is found,
    2 for usage errors, and 3 for structural errors (missing run dir, unreadable
    manifest, tool record missing the ``"shape"`` key).
    """
    if not argv or argv[0] != "diff":
        raise ValueError(f"Expected 'diff' subcommand, got: {argv!r}")

    # --help / -h
    if "--help" in argv or "-h" in argv:
        print(_USAGE)
        sys.exit(0)

    baseline_run_b: "str | None" = None  # set when --baseline is used
    json_output: bool = False
    positionals: "list[str]" = []

    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            if "=" in arg:
                key, _, val = arg.partition("=")
                i += 1
            else:
                key = arg
                val = None
                i += 1

            if key == "--baseline":
                if val is None:
                    if i >= len(argv):
                        print("Error: --baseline requires a value", file=sys.stderr)
                        sys.exit(2)
                    val = argv[i]
                    i += 1
                baseline_run_b = val
            elif key == "--json":
                json_output = True
            else:
                print(f"Error: unknown flag: {key!r}", file=sys.stderr)
                print(_USAGE, file=sys.stderr)
                sys.exit(2)
        else:
            positionals.append(arg)
            i += 1

    runs_root = _runs_root()

    if baseline_run_b is not None:
        # --baseline mode: run A comes from the baseline file, run B is the arg
        if positionals:
            print(
                "Error: positional run names are not allowed with --baseline",
                file=sys.stderr,
            )
            print(_USAGE, file=sys.stderr)
            sys.exit(2)
        baseline_file = runs_root / "baseline"
        if not baseline_file.is_file():
            print(f"Error: no baseline file found at {baseline_file}", file=sys.stderr)
            sys.exit(3)
        run_a_name = baseline_file.read_text(encoding="utf-8").strip()
        run_b_name = baseline_run_b
    else:
        if len(positionals) != 2:
            print(
                "Error: diff requires exactly two run names (A B) or --baseline RUN",
                file=sys.stderr,
            )
            print(_USAGE, file=sys.stderr)
            sys.exit(2)
        run_a_name, run_b_name = positionals

    # Reject names that sanitize differently (path traversal guard)
    _reject_unsafe_run_name(run_a_name)
    _reject_unsafe_run_name(run_b_name)

    run_dir_a = runs_root / run_a_name
    run_dir_b = runs_root / run_b_name

    manifest_a, records_a = _load_run_dir(run_dir_a)
    manifest_b, records_b = _load_run_dir(run_dir_b)

    fp_a = manifest_a.get("fingerprint", {})
    fp_b = manifest_b.get("fingerprint", {})

    # Cross-cluster warning (before any output so it's always on stderr)
    if fp_a.get("cluster_id", "unknown") != fp_b.get("cluster_id", "unknown"):
        print("WARNING: cross-cluster diff", file=sys.stderr)

    # Transport-failure warning (F4: informational only, never affects exit code)
    _tf_a = manifest_a.get("transport_failure", False)
    _tf_b = manifest_b.get("transport_failure", False)
    if _tf_a or _tf_b:
        _tf_which = ", ".join(
            label for label, flag in (("run-a", _tf_a), ("run-b", _tf_b)) if flag
        )
        print(
            f"WARNING: transport failure recorded in {_tf_which} — "
            "results may be incomplete",
            file=sys.stderr,
        )

    findings = diff_runs(records_a, records_b)
    info_lines = _status_info_lines(records_b)

    if json_output:
        data = {
            "findings": findings,
            "info": info_lines,
            "fingerprints": {"a": fp_a, "b": fp_b},
        }
        print(json.dumps(data))
    else:
        # Header: fingerprints for both runs
        print(_fp_header("run-a", fp_a))
        print(_fp_header("run-b", fp_b))
        for f in findings:
            print(f"{f['severity']}: {f['tool']}: {f['kind']}: {f['detail']}")
        for line in info_lines:
            print(line)

    if any(f["severity"] == "fail" for f in findings):
        sys.exit(1)
    sys.exit(0)


# ---------------------------------------------------------------------------
# bless subcommand
# ---------------------------------------------------------------------------


def bless_matrix(argv: "list[str]") -> None:
    """Handle ``bless RUN`` subcommand.

    Writes the run's directory basename (single line) to ``<runs_root>/baseline``.
    Refuses partial runs (exit 2).  Exits 2 for unsafe run names (traversal guard).
    Exits 3 if the run directory does not exist, the manifest cannot be read, or
    any tool record is structurally invalid (same validation diff uses, so blessed
    runs are always diffable).
    """
    if not argv or argv[0] != "bless":
        raise ValueError(f"Expected 'bless' subcommand, got: {argv!r}")

    # --help / -h
    if "--help" in argv or "-h" in argv:
        print(_USAGE)
        sys.exit(0)

    if len(argv) != 2:
        print("Error: bless requires exactly one run name", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        sys.exit(2)

    run_name = argv[1]
    runs_root = _runs_root()

    # Reject traversal attempts before any filesystem access
    _reject_unsafe_run_name(run_name)

    run_dir = runs_root / run_name

    # Load + validate manifest and all records (exits 3 on any structural fault)
    manifest, _ = _load_run_dir(run_dir)

    if manifest.get("partial"):
        print(
            f"Error: cannot bless a partial run: {run_name!r} "
            f"(set partial=false by running without --tools)",
            file=sys.stderr,
        )
        sys.exit(2)

    if manifest.get("transport_failure", False):
        print(
            f"Error: cannot bless a run with transport failure: {run_name!r} "
            f"(the MCP transport died mid-sweep; results may be incomplete)",
            file=sys.stderr,
        )
        sys.exit(2)

    baseline_path = runs_root / "baseline"
    baseline_path.write_text(run_name + "\n", encoding="utf-8")
    print(f"Blessed: {run_name}")
    sys.exit(0)


# ---------------------------------------------------------------------------
# list subcommand
# ---------------------------------------------------------------------------


def list_matrix(argv: "list[str]") -> None:
    """Handle ``list`` subcommand.

    Prints all completed run directories newest-first.  Each line contains:
    dir, sha[:8], dirty, cluster_id, partial.

    Skips ``.tmp`` directories and the ``baseline`` file.
    """
    if not argv or argv[0] != "list":
        raise ValueError(f"Expected 'list' subcommand, got: {argv!r}")

    # --help / -h
    if "--help" in argv or "-h" in argv:
        print(_USAGE)
        sys.exit(0)

    # No flags defined; reject unknowns for consistency
    for arg in argv[1:]:
        if arg.startswith("--"):
            print(f"Error: unknown flag: {arg!r}", file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            sys.exit(2)
        else:
            print(f"Error: unexpected argument: {arg!r}", file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            sys.exit(2)

    runs_root = _runs_root()
    if not runs_root.is_dir():
        sys.exit(0)

    # Collect run directories (exclude .tmp dirs and non-dirs such as the baseline file)
    run_dirs = [
        entry
        for entry in runs_root.iterdir()
        if entry.is_dir() and not entry.name.endswith(".tmp")
    ]

    # Sort by mtime descending (newest first)
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for run_dir in run_dirs:
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            print(f"{run_dir.name}  (damaged: missing manifest.json)")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"{run_dir.name}  (damaged: {exc})")
            continue

        fp = manifest.get("fingerprint", {})
        sha8 = fp.get("git_sha", "unknown")[:8]
        dirty = "dirty" if fp.get("dirty") else "clean"
        cluster_id = fp.get("cluster_id", "unknown")
        partial = "partial" if manifest.get("partial") else "full"

        print(f"{run_dir.name}  {sha8}  {dirty}  {cluster_id}  {partial}")

    sys.exit(0)


# ---------------------------------------------------------------------------
# CLI stub  →  full dispatcher
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _argv = sys.argv[1:]
    if not _argv:
        print(_USAGE)
        sys.exit(2)

    _cmd = _argv[0]
    if _cmd in ("--help", "-h"):
        print(_USAGE)
        sys.exit(0)
    elif _cmd == "run":
        # F1: print the finalized run dir path to STDOUT as the last line of a
        # successful run so callers can capture it without parsing stderr.
        print(run_matrix(_argv))
    elif _cmd == "diff":
        diff_matrix(_argv)
    elif _cmd == "bless":
        bless_matrix(_argv)
    elif _cmd == "list":
        list_matrix(_argv)
    else:
        print(f"Error: unknown subcommand: {_cmd!r}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        sys.exit(2)
