from dataclasses import dataclass, field

from kubernetes.client.exceptions import ApiException

from .k8s_fakes import NS, FakeApi, items_list, obj, POD, POD2C, EVENT, PIPELINERUN, TASKRUN


@dataclass
class ToolCase:
    name: str            # registered tool name AND module attribute name
    kwargs: dict = field(default_factory=dict)
    patches: dict = field(default_factory=dict)  # server attr -> replacement
    id: str = ""         # suffix when a tool has >1 case

    @property
    def case_id(self):
        return f"{self.name}{'-' + self.id if self.id else ''}"


CASES: list[ToolCase] = [
    ToolCase(
        name="list_namespaces",
        patches={"k8s_core_api": FakeApi(
            list_namespace=items_list([NS("default"), NS("team-a-tenant")]),
        )},
    ),
]

# ── Shared pod/event fixtures ────────────────────────────────────────────────
_PODS = items_list([
    POD("api-1", "team-a", restarts=0),
    POD("api-2", "team-a", phase="Pending", restarts=7, ready=False),
])

# Each EVENT() call captures datetime.now() independently.  BackOff is
# created first (→ slightly older timestamp) and FailedScheduling is
# created last (→ slightly newer timestamp).  Both land 2–3 hours ago
# so they survive the 6-hour adaptive time-window filter while being
# excluded from the 10-minute volume-estimation window.
_EVENTS = items_list([
    EVENT("BackOff", "Back-off restarting failed container", "team-a"),
    EVENT("FailedScheduling", "0/3 nodes are available", "team-a",
          kind="Pod", name="api-2"),
])

# Pod used as the canned return value for read_namespaced_pod in cases
# that need a detailed pod view (check_resource_constraints, log tools).
_POD_DETAIL = POD("api-2", "team-a", phase="Pending", restarts=7, ready=False)

CASES += [
    # ── 1. list_pods_in_namespace ────────────────────────────────────────────
    ToolCase("list_pods_in_namespace", {"namespace": "team-a"},
             {"k8s_core_api": FakeApi(list_namespaced_pod=_PODS)}),

    # ── 2. get_kubernetes_resource ───────────────────────────────────────────
    # resource_type is singular ("pod") per the tool's actual signature.
    ToolCase("get_kubernetes_resource",
             {"resource_type": "pod", "namespace": "team-a", "name": "api-1"},
             {"k8s_core_api": FakeApi(
                 read_namespaced_pod=POD("api-1", "team-a"),
                 list_namespaced_pod=_PODS)}),

    # ── 3. search_resources_by_labels ───────────────────────────────────────
    # The tool's actual signature uses resource_types (list) and
    # label_selectors (list of dicts), not the brief's label_selector/namespace
    # strings.  namespaces prevents list_namespace being called.
    ToolCase("search_resources_by_labels",
             {"resource_types": ["pods"],
              "label_selectors": [{"key": "app", "value": "api-1",
                                   "operator": "equals"}],
              "namespaces": ["team-a"]},
             {"k8s_core_api": FakeApi(list_namespaced_pod=_PODS)}),

    # ── 4. check_resource_constraints ───────────────────────────────────────
    # check_resource_constraints calls list_pods() helper (→ list_namespaced_pod),
    # list_namespaced_resource_quota, and read_namespaced_pod for every pod
    # with status in [Failed, Pending, Running].  All three must be canned.
    ToolCase("check_resource_constraints", {"namespace": "team-a"},
             {"k8s_core_api": FakeApi(
                 read_namespace=NS("team-a"),
                 list_namespaced_pod=_PODS,
                 list_namespaced_resource_quota=items_list([]),
                 list_namespaced_limit_range=items_list([]),
                 list_namespaced_event=_EVENTS,
                 read_namespaced_pod=_POD_DETAIL)}),

    # ── 5. conservative_namespace_overview ──────────────────────────────────
    # Calls list_pods_in_namespace (→ list_namespaced_pod) then
    # smart_summarize_pod_logs for each pod, which in turn calls
    # get_all_pod_logs → read_namespaced_pod + read_namespaced_pod_log.
    ToolCase("conservative_namespace_overview", {"namespace": "team-a"},
             {"k8s_core_api": FakeApi(
                 list_namespaced_pod=_PODS,
                 list_namespaced_event=_EVENTS,
                 read_namespaced_pod=POD("api-1", "team-a"),
                 read_namespaced_pod_log="ERROR: connection refused\nINFO ok\n")}),

    # ── 6. adaptive_namespace_investigation ──────────────────────────────────
    # Calls list_pods_in_namespace, smart_get_namespace_events
    # (→ list_namespaced_event), and smart_summarize_pod_logs
    # (→ read_namespaced_pod + read_namespaced_pod_log).
    ToolCase("adaptive_namespace_investigation", {"namespace": "team-a"},
             {"k8s_core_api": FakeApi(
                 list_namespaced_pod=_PODS,
                 list_namespaced_event=_EVENTS,
                 read_namespaced_pod=POD("api-1", "team-a"),
                 read_namespaced_pod_log="ERROR: connection refused\nINFO ok\n")}),

    # ── 7. detect_anomalies ──────────────────────────────────────────────────
    # Calls list_pipelineruns and list_taskruns, both of which use
    # k8s_custom_api.list_namespaced_custom_object.  With empty items lists
    # the tool returns the "no data to analyse" baseline golden.
    ToolCase("detect_anomalies", {"namespace": "team-a"},
             {"k8s_custom_api": FakeApi(
                 list_namespaced_custom_object={"items": []})}),

    # ── 8. smart_get_namespace_events ────────────────────────────────────────
    # Adaptive mode: estimates volume via _get_namespace_events_internal("10m")
    # (→ list_namespaced_event; our 2 events are 2-3 hours old so sample=0 →
    # selects 6h window) then fetches again with 6h window.  _PODS is included
    # so the FakeApi matches the brief spec; it is not called by this tool.
    ToolCase("smart_get_namespace_events", {"namespace": "team-a"},
             {"k8s_core_api": FakeApi(list_namespaced_event=_EVENTS,
                                      list_namespaced_pod=_PODS)}),

    # ── 9. progressive_event_analysis ────────────────────────────────────────
    # Delegates entirely to smart_get_namespace_events (→ list_namespaced_event)
    # then runs ProgressiveEventAnalyzer.get_overview() (pure Python).
    ToolCase("progressive_event_analysis", {"namespace": "team-a"},
             {"k8s_core_api": FakeApi(list_namespaced_event=_EVENTS,
                                      list_namespaced_pod=_PODS)}),

    # ── 10. advanced_event_analytics ─────────────────────────────────────────
    # Calls progressive_event_analysis (analysis_level="detailed" for default
    # "comprehensive" depth) → smart_get_namespace_events → list_namespaced_event.
    # LogMetricsIntegrator.correlate_with_logs / correlate_with_metrics are pure
    # Python; MLPatternDetector and RunbookSuggestionEngine likewise.
    ToolCase("advanced_event_analytics", {"namespace": "team-a"},
             {"k8s_core_api": FakeApi(list_namespaced_event=_EVENTS,
                                      list_namespaced_pod=_PODS)}),
]

# ── SAMPLE_LOG (reused by Tasks 7-9) ─────────────────────────────────────────
SAMPLE_LOG = "\n".join(
    ["2026-07-20T10:00:01Z INFO starting server",
     "2026-07-20T10:00:02Z ERROR connection refused to db:5432",
     "2026-07-20T10:00:03Z WARN retrying in 5s",
     "2026-07-20T10:00:04Z ERROR connection refused to db:5432",
     "2026-07-20T10:00:05Z FATAL giving up after 2 retries"] * 10
)
# 5 lines × 10 reps = 50 lines: 10 INFO, 20 ERROR, 10 WARN, 10 FATAL

# ── Pod-log fake ──────────────────────────────────────────────────────────────
# read_namespaced_pod is needed first (get_all_pod_logs reads container names),
# then read_namespaced_pod_log is called per container.  list_namespaced_pod is
# included so list_pods_in_namespace (called by semantic_log_search) works too.
_LOG_POD_API = FakeApi(
    list_namespaced_pod=_PODS,
    read_namespaced_pod=POD("api-1", "team-a"),
    read_namespaced_pod_log=SAMPLE_LOG,
)

CASES += [
    # ── 11. analyze_logs ─────────────────────────────────────────────────────
    # Pure text analysis — no k8s API calls.
    ToolCase("analyze_logs", {"log_text": SAMPLE_LOG}),

    # ── 12. smart_summarize_pod_logs ─────────────────────────────────────────
    # Adaptive mode: _quick_volume_estimate → get_pod_logs (read_namespaced_pod
    # + read_namespaced_pod_log), then main log fetch with tail_lines.
    ToolCase("smart_summarize_pod_logs",
             {"namespace": "team-a", "pod_name": "api-1"},
             {"k8s_core_api": _LOG_POD_API}),

    # ── 13. stream_analyze_pod_logs ──────────────────────────────────────────
    # No user time constraints → defaults to tail_lines=2000 via safety guard.
    # Calls get_pod_logs → read_namespaced_pod + read_namespaced_pod_log.
    ToolCase("stream_analyze_pod_logs",
             {"namespace": "team-a", "pod_name": "api-1"},
             {"k8s_core_api": _LOG_POD_API}),

    # ── 14. analyze_pod_logs_hybrid ──────────────────────────────────────────
    # strategy=auto + request_type=investigation → selects SMART_SUMMARY.
    # Delegates to smart_summarize_pod_logs (same API surface as case 12).
    # Cache starts empty each process run → cache_hit=False in golden.
    ToolCase("analyze_pod_logs_hybrid",
             {"namespace": "team-a", "pod_name": "api-1"},
             {"k8s_core_api": _LOG_POD_API}),

    # ── 15. detect_log_anomalies ─────────────────────────────────────────────
    # Signature: detect_log_anomalies(logs: str, ...) — pure text, no k8s API.
    # Brief incorrectly shows namespace/pod kwargs; actual required param is
    # `logs` (string).  No patches needed.
    ToolCase("detect_log_anomalies", {"logs": SAMPLE_LOG}),

    # ── 16. semantic_log_search ──────────────────────────────────────────────
    # Signature: semantic_log_search(query, namespaces, ...) — not pod-scoped.
    # Passes namespaces=["team-a"] to skip list_namespace auto-detect.
    # Internally calls list_pods_in_namespace → list_namespaced_pod,
    # get_pod_logs → read_namespaced_pod + read_namespaced_pod_log, and
    # smart_get_namespace_events → list_namespaced_event.
    ToolCase("semantic_log_search",
             {"query": "database connection failure",
              "namespaces": ["team-a"]},
             {"k8s_core_api": FakeApi(
                 list_namespaced_pod=_PODS,
                 read_namespaced_pod=POD("api-1", "team-a"),
                 read_namespaced_pod_log=SAMPLE_LOG,
                 list_namespaced_event=_EVENTS)}),

    # ── 17. get_etcd_logs ────────────────────────────────────────────────────
    # Auto-detects cluster type: tries OpenShift first (openshift-etcd ns,
    # k8s-app=etcd label) → finds etcd-node-1 → fetches logs via
    # read_namespaced_pod_log (cleaned via clean_etcd_logs).
    # Note: read_namespaced_pod is NOT called here; only list_namespaced_pod
    # and read_namespaced_pod_log are needed.
    ToolCase("get_etcd_logs", {},
             {"k8s_core_api": FakeApi(
                 list_namespaced_pod=items_list(
                     [POD("etcd-node-1", "openshift-etcd")]),
                 read_namespaced_pod_log=SAMPLE_LOG)}),
    # Task 3 (phase 3.5): tiny-budget proof — default budget (50000) >> 833-token
    # fixture so the existing golden is byte-identical; this case triggers the cap.
    ToolCase("get_etcd_logs", {"max_context_tokens": 50},
             {"k8s_core_api": FakeApi(
                 list_namespaced_pod=items_list(
                     [POD("etcd-node-1", "openshift-etcd")]),
                 read_namespaced_pod_log=SAMPLE_LOG)},
             id="budget-capped"),
]

# ── Task 0: multi-container + log-error branch goldens ───────────────────────

def _per_container_logs(name=None, namespace=None, container=None, **kw):
    # matches the real invocation: get_all_pod_logs calls
    # read_namespaced_pod_log(name=..., namespace=..., container=..., ...)
    # (utils.py:544). Distinct text per container makes the join visible.
    return f"log-line-from-{container or 'main'}"


CASES += [
    ToolCase("smart_summarize_pod_logs",
             {"namespace": "team-a", "pod_name": "api-2c"},
             {"k8s_core_api": FakeApi(
                 read_namespaced_pod=POD2C("api-2c", "team-a"),
                 list_namespaced_pod=items_list([POD2C("api-2c", "team-a")]),
                 read_namespaced_pod_log=_per_container_logs)},
             id="two-containers"),
    ToolCase("stream_analyze_pod_logs",
             {"namespace": "team-a", "pod_name": "api-gone"},
             {"k8s_core_api": FakeApi(
                 read_namespaced_pod=ApiException(status=404, reason="Not Found"),
                 list_namespaced_pod=items_list([]),
                 read_namespaced_pod_log=ApiException(status=404, reason="Not Found"))},
             id="log-error"),
]

# ── ML/predictive fake ────────────────────────────────────────────────────────
# predictive_log_analyzer accepts namespaces (list), not namespace+pod_name.
# With namespaces=["team-a"] the auto-detect path (list_namespaces/detect_tekton)
# is skipped.  The tool calls:
#   1. list_namespaced_pod (to enumerate pods)
#   2. read_namespaced_pod_log (for each Running/Failed/Succeeded pod → api-1)
#   3. list_namespaced_event (via _get_namespace_events_as_dicts, inside a
#      per-namespace try/except — missing method would be silently caught, but
#      providing it avoids AttributeError propagation and surfaces richer output)
# read_namespaced_pod is NOT called (the tool only needs pod list + logs).
# manage_prediction_training_data action="stats" reads only the local model
# store and SQLite training DB — no k8s API calls at all; no patches needed.
# Both HOME-dependent stores resolve to tmp (autouse deterministic fixture).
_ML_API = FakeApi(
    list_namespaced_pod=_PODS,
    read_namespaced_pod_log=SAMPLE_LOG,
    list_namespaced_event=_EVENTS,
)

CASES += [
    # ── 18. predictive_log_analyzer ──────────────────────────────────────────
    # NOTE spec §4.7: this tool writes ML models and training-data samples to
    # the filesystem (~/.lumino/models and ~/.lumino/training_data) via joblib
    # and SQLite.  In phase 0 the autouse HOME redirect makes these writes land
    # in the per-test tmp dir (harmless).  Phase 1 must decide whether to stub
    # ModelPersistenceManager / TrainingDataStore or accept the writes.
    ToolCase("predictive_log_analyzer",
             {"namespaces": ["team-a"]},
             {"k8s_core_api": _ML_API}),

    # ── 19. manage_prediction_training_data (stats) ──────────────────────────
    # action="stats" is a read-only view of the (empty) model store and
    # SQLite training DB; it is the clean-state baseline golden.
    ToolCase("manage_prediction_training_data", {"action": "stats"}),

    # ── 19b. manage_prediction_training_data (collect) ────────────────────────
    # action="collect" triggers failure collection from namespaces:
    #   1. _get_namespace_events_as_dicts → list_namespaced_event
    #   2. collect_from_pod_status → list_namespaced_pod (READ covered by phase-1b)
    #   3. list_pipelineruns → list_namespaced_custom_object (no failed PRs → 0)
    # collect_from_namespaces=["team-a"] bypasses list_namespaces auto-detection.
    # from_events: _EVENTS objects lack .involved_object.uid so event conversion
    # raises AttributeError (caught per-event); events_as_dicts = [] → 0.
    # from_pods: api-1 (Running 0 restarts) and api-2 (Pending 7 restarts) are
    # evaluated by collect_from_pod_status; count is deterministic given fixed inputs.
    # from_pipelines: custom_api returns empty items → 0.
    ToolCase("manage_prediction_training_data",
             {"action": "collect", "collect_from_namespaces": ["team-a"]},
             {"k8s_core_api": FakeApi(
                 list_namespaced_pod=_PODS,
                 list_namespaced_event=_EVENTS,
             ),
              "k8s_custom_api": FakeApi(
                  list_namespaced_custom_object=lambda *a, **kw: {"items": []},
              )},
             id="collect"),
]

# ── Tekton/Pipeline CR fixtures ───────────────────────────────────────────────
_PLR_OK = PIPELINERUN("build-run-1", "team-a")
_PLR_BAD = PIPELINERUN("build-run-2", "team-a", succeeded=False)
_TR_BAD = TASKRUN("build-run-2-build", "team-a", "build-run-2", succeeded=False)


def _custom_api(**extra):
    """FakeApi covering CustomObjects surfaces touched by Tekton tools.

    list_namespaced_custom_object / list_cluster_custom_object dispatch on
    (group, plural); unknown combos return empty lists so tools that probe
    additional CRD types (e.g. repositories) degrade gracefully.
    get_namespaced_custom_object dispatches on plural so that
    get_pipeline_details (plural="pipelineruns") gets _PLR_BAD and
    get_task_details (plural="taskruns") gets _TR_BAD — both needed for
    analyze_failed_pipeline to produce an RCA golden with exit code 1.
    """
    canned = {
        ("tekton.dev", "pipelineruns"): {"items": [_PLR_OK, _PLR_BAD]},
        ("tekton.dev", "taskruns"): {"items": [_TR_BAD]},
    }

    def list_namespaced_custom_object(group, version, namespace, plural,
                                      **kwargs):
        return canned.get((group, plural), {"items": []})

    def list_cluster_custom_object(group, version, plural, **kwargs):
        return canned.get((group, plural), {"items": []})

    def get_namespaced_custom_object(group, version, namespace, plural,
                                     name=None, **kwargs):
        if plural == "taskruns":
            return _TR_BAD
        return _PLR_BAD

    return FakeApi(
        list_namespaced_custom_object=list_namespaced_custom_object,
        list_cluster_custom_object=list_cluster_custom_object,
        get_namespaced_custom_object=get_namespaced_custom_object,
        **extra,
    )


# Core-API fake for Tekton cases.  read_namespaced_pod is required by
# get_all_pod_logs (called by get_pipelinerun_logs and analyze_failed_pipeline)
# and by _prioritize_pipeline_pods / _estimate_pod_log_tokens.
_TEKTON_CORE = FakeApi(
    list_namespace=items_list([NS("team-a")]),
    list_namespaced_pod=items_list([POD("build-run-2-build-pod", "team-a",
                                        phase="Failed", ready=False)]),
    read_namespaced_pod=POD("build-run-2-build-pod", "team-a",
                            phase="Failed", ready=False),
    read_namespaced_pod_log=SAMPLE_LOG,
    list_namespaced_event=_EVENTS,
)

# Schema-confirmed kwargs (checked against parity_reference.json
# input_schema.properties):
# • get_tekton_pipeline_runs_status: no "namespace" param (cluster-wide, all
#   defaults) — brief's {"namespace": "team-a"} corrected to {}.
# • pipeline_tracer: trace_type schema is plain string; valid runtime values
#   are "commit"/"pr"/"image"/"custom" (see server-mcp.py:9228).  Brief's
#   "pipeline_run" would return an error dict — corrected to "custom"
#   (matches by name substring).  namespaces=["team-a"] skips list_namespace.
# • ci_cd_performance_baselining_tool: no "namespace" param (Prometheus-only,
#   k8s API not called) — brief's {"namespace": "team-a"} corrected to {}.
for _name, _kwargs in [
    # ── 20. list_pipelineruns ─────────────────────────────────────────────────
    ("list_pipelineruns", {"namespace": "team-a"}),
    # ── 21. list_taskruns ────────────────────────────────────────────────────
    ("list_taskruns", {"namespace": "team-a"}),
    # ── 22. get_pipelinerun_logs ─────────────────────────────────────────────
    # pipelinerun_name is the correct param name (schema :get_pipelinerun_logs)
    ("get_pipelinerun_logs", {"namespace": "team-a",
                              "pipelinerun_name": "build-run-2"}),
    # ── 23. analyze_failed_pipeline ──────────────────────────────────────────
    # Heart of phase-1 RCA preservation: golden must contain the failed
    # TaskRun "build-run-2-build", exit code 1, and SAMPLE_LOG error lines.
    ("analyze_failed_pipeline", {"namespace": "team-a",
                                 "pipeline_run": "build-run-2"}),
    # ── 24. list_recent_pipeline_runs ────────────────────────────────────────
    # Cluster-wide: calls list_cluster_custom_object only (no list_namespace).
    ("list_recent_pipeline_runs", {}),
    # ── 25. find_pipeline ────────────────────────────────────────────────────
    # Cluster-wide search; pipeline_id_pattern is the correct param name.
    ("find_pipeline", {"pipeline_id_pattern": "build-run-2"}),
    # ── 26. get_tekton_pipeline_runs_status ───────────────────────────────────
    # Cluster-wide; list_namespace called with label_selector (ignored by fake).
    ("get_tekton_pipeline_runs_status", {}),
    # ── 27. pipeline_tracer ──────────────────────────────────────────────────
    # trace_type="custom" matches by name substring → finds _PLR_BAD.
    # namespaces=["team-a"] bypasses list_namespace auto-detection.
    ("pipeline_tracer", {"trace_identifier": "build-run-2",
                         "trace_type": "custom",
                         "namespaces": ["team-a"]}),
    # ── 28. ci_cd_performance_baselining_tool ────────────────────────────────
    # Uses Prometheus only; returns early (data_source=kubernetes_api_fallback)
    # when Prometheus endpoint is unavailable (no-cluster environment).
    ("ci_cd_performance_baselining_tool", {}),
]:
    CASES.append(ToolCase(_name, _kwargs,
                          {"k8s_custom_api": _custom_api(),
                           "k8s_core_api": _TEKTON_CORE}))

# Task 4 (phase 3.5): get_pipelinerun_logs budget-capped proof.
# budget 900 → effective 720 (80% safety buffer in AdaptiveLogProcessor).
# SAMPLE_LOG fixture: 2499 chars → 833 tokens (÷3 heuristic).
# 833 > 720, so truncation fires: pods_truncated=1 + TRUNCATED notice.
# Step 1 (brief): the EXACT fixture token count is 833 — confirmed above.
# The golden's token_budget_used field tracks the default max_token_budget
# (18000 since bug 2, 2026-08-21; regenerate the golden when it changes).
CASES.append(ToolCase(
    "get_pipelinerun_logs",
    {"namespace": "team-a", "pipelinerun_name": "build-run-2",
     "max_token_budget": 900},
    {"k8s_custom_api": _custom_api(), "k8s_core_api": _TEKTON_CORE},
    id="budget-capped",
))

# ── Task 9: Metrics / forecasting / certs / OpenShift / topology / RCA ───────
#
# Prometheus seam decision (server-mcp.py:4866-4921):
#   _execute_prometheus_query_internal returns PRE-PROCESSED data:
#     {"success": bool, "data": [raw result items], "endpoint_type": ..., "error": ...}
#   It extracts data.result from the raw HTTP response, so does NOT return raw
#   Prometheus JSON.  Per the brief: "if it returns processed results, patch one
#   level lower at the HTTP call instead."
#
#   • prometheus_query TOOL makes its own aiohttp call (does NOT call
#     _execute_prometheus_query_internal).  Seam = aiohttp.ClientSession +
#     _discover_prometheus_endpoint (patched at HTTP level).
#   • resource_bottleneck_forecaster calls prometheus_query() directly (module
#     attr), so we patch prometheus_query itself.
#   • what_if_scenario_simulator passes _execute_prometheus_query_internal as a
#     fn arg (server-mcp.py:12383) → patching the module attr works there.

import base64 as _b64


# ── Prometheus HTTP-level mock (for prometheus_query tool) ───────────────────
class _FakePrometheusResponse:
    status = 200

    async def json(self):
        return {
            "status": "success",
            "data": {"resultType": "vector", "result": [
                {"metric": {"__name__": "up", "job": "kubelet", "instance": "node-1"},
                 "value": [1753180800, "1"]},
                {"metric": {"__name__": "up", "job": "kubelet", "instance": "node-2"},
                 "value": [1753180800, "0"]},
            ]},
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _FakeAiohttpSession:
    def __init__(self, *a, **kw):
        pass

    def get(self, *a, **kw):
        return _FakePrometheusResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _fake_aiohttp:
    """Minimal aiohttp stand-in that returns a canned 200 Prometheus response."""
    class ClientSession(_FakeAiohttpSession):
        pass

    class ClientTimeout:
        def __init__(self, *a, **kw):
            pass


async def _fake_discover_endpoint(*a, **kw):
    """Make _discover_prometheus_endpoint return a fake URL instead of (None, None)."""
    return ("http://fake-prometheus:9090", "prometheus")


# ── _execute_prometheus_query_internal stand-in (for what_if_scenario_simulator) ─
# Contract matches the REAL function's pre-processed return:
#   {"success": bool, "data": [raw result items], "endpoint_type": ..., "error": ...}
async def _fake_prom_exec(*args, **kwargs):
    return {
        "success": True,
        "data": [
            {"metric": {"__name__": "up", "job": "kubelet", "instance": "node-1"},
             "value": [1753180800, "1"]},
            {"metric": {"__name__": "up", "job": "kubelet", "instance": "node-2"},
             "value": [1753180800, "0"]},
        ],
        "endpoint_type": "prometheus",
        "error": None,
    }


# ── prometheus_query direct-call stand-in (for resource_bottleneck_forecaster) ──
# resource_bottleneck_forecaster calls prometheus_query() directly (module attr).
# Range queries get values-list; instant queries get value-scalar.
async def _fake_prometheus_query(query="", query_type="instant", **kwargs):
    if query_type == "range":
        return {
            "status": "success",
            "data": [
                {"metric": {"instance": "node-1"},
                 "values": [[1753180800, "25.0"], [1753181100, "27.5"],
                             [1753181400, "30.0"]]},
            ],
            "result_count": 1,
        }
    # instant (including the "up" connectivity test)
    return {
        "status": "success",
        "data": [
            {"metric": {"__name__": "up", "instance": "node-1"},
             "value": [1753180800, "1"]},
        ],
        "result_count": 1,
    }


CASES += [
    # ── 29. prometheus_query ──────────────────────────────────────────────────
    # Seam: aiohttp.ClientSession (HTTP) + _discover_prometheus_endpoint.
    # This characterizes the RESULT-FORMATTING path (_process_prometheus_results)
    # with a vector containing up{node-1}=1 and up{node-2}=0.
    ToolCase("prometheus_query", {"query": "up"},
             {"_discover_prometheus_endpoint": _fake_discover_endpoint,
              "aiohttp": _fake_aiohttp}),
]

# ── Node / OpenShift / cert fixtures ─────────────────────────────────────────
_NODE = obj(
    metadata=dict(name="node-1",
                  labels={"node-role.kubernetes.io/worker": ""}),
    status=dict(
        capacity={"cpu": "8", "memory": "32Gi", "pods": "250"},
        allocatable={"cpu": "7500m", "memory": "30Gi", "pods": "250"},
        conditions=[dict(type="Ready", status="True")],
        node_info=dict(kubelet_version="v1.31.0"),
        addresses=None,
    ),
)

_CO = {
    "apiVersion": "config.openshift.io/v1",
    "kind": "ClusterOperator",
    "metadata": {"name": "kube-apiserver"},
    "status": {"conditions": [
        {"type": "Available", "status": "True", "reason": "AsExpected",
         "message": "all good",
         "lastTransitionTime": "2026-07-01T00:00:00Z"},
        {"type": "Degraded", "status": "False", "reason": "AsExpected",
         "message": "",
         "lastTransitionTime": "2026-07-01T00:00:00Z"},
    ]},
}

_MCP_CR = {
    "apiVersion": "machineconfiguration.openshift.io/v1",
    "kind": "MachineConfigPool",
    "metadata": {"name": "worker"},
    "status": {
        "machineCount": 3, "readyMachineCount": 3,
        "updatedMachineCount": 3, "degradedMachineCount": 0,
        "conditions": [{"type": "Updated", "status": "True"}],
    },
}


# _FAKE_CERT_SECRET: data must remain a plain dict (not converted to _NS by
# obj()) because check_cluster_certificate_health accesses it with dict indexing:
#   if key in secret.data         (requires __contains__ on a dict)
#   base64.b64decode(secret.data[key])  (requires __getitem__)
# _NS inherits from SimpleNamespace which is not iterable → "argument of type
# '_NS' is not iterable" error.  We build via obj() then assign data manually.
_FAKE_CERT_SECRET = obj(
    metadata=dict(name="api-tls", namespace="team-a"),
    type="kubernetes.io/tls",
)
# Assign data as a plain dict (b"not-a-real-cert" has no PEM header so
# parse_certificate is never called; tool skips it, total_certificates=0).
_FAKE_CERT_SECRET.data = {"tls.crt": _b64.b64encode(b"not-a-real-cert").decode()}

_INFRA_CORE = FakeApi(
    list_node=items_list([_NODE]),
    list_namespace=items_list([NS("team-a")]),
    list_namespaced_pod=_PODS,
    list_pod_for_all_namespaces=_PODS,
    list_namespaced_event=_EVENTS,
    list_namespaced_secret=items_list([_FAKE_CERT_SECRET]),
    list_secret_for_all_namespaces=items_list([_FAKE_CERT_SECRET]),
    list_namespaced_service=items_list([]),
    read_namespaced_pod_log=SAMPLE_LOG,
)


def _openshift_custom_api():
    """FakeApi covering OpenShift ClusterOperator and MachineConfigPool CRDs."""
    canned = {
        ("config.openshift.io", "clusteroperators"): {"items": [_CO]},
        ("machineconfiguration.openshift.io", "machineconfigpools"):
            {"items": [_MCP_CR]},
    }

    def list_cluster_custom_object(group, version, plural, **kwargs):
        return canned.get((group, plural), {"items": []})

    return FakeApi(list_cluster_custom_object=list_cluster_custom_object)


CASES += [
    # ── 30. resource_bottleneck_forecaster ────────────────────────────────────
    # Patches prometheus_query (not _execute_prometheus_query_internal) since
    # the forecaster calls prometheus_query("up") for connectivity test and
    # then via _analyze_node_resources_new for range queries.
    # The golden shows node-1 capacity math (CPU/memory trend from fake data).
    ToolCase("resource_bottleneck_forecaster", {},
             {"k8s_core_api": _INFRA_CORE,
              "prometheus_query": _fake_prometheus_query}),

    # ── 31. what_if_scenario_simulator ────────────────────────────────────────
    # scenario_type="scaling" (valid enum); brief had "scale_deployment" which
    # is not in valid_scenario_types (corrected per server-mcp.py:12331).
    # Patches _execute_prometheus_query_internal because it is passed as a
    # parameter to load_historical_performance_data (server-mcp.py:12383).
    # simulation_id contains uuid4().hex[:8] → masked by *_id rule.
    # Monte Carlo uses random.gauss → seeded at 0 by deterministic fixture.
    ToolCase("what_if_scenario_simulator",
             {"scenario_type": "scaling",
              "changes": {"namespace": "team-a", "deployment": "api",
                          "replicas": 5}},
             {"k8s_core_api": _INFRA_CORE,
              "k8s_apps_api": FakeApi(
                  list_namespaced_deployment=items_list([])),
              "_execute_prometheus_query_internal": _fake_prom_exec}),

    # ── 32. investigate_tls_certificate_issues ────────────────────────────────
    # No namespace param (confirmed :6437).  focus_on_system_namespaces=True
    # (default) filters "team-a" out of target_namespaces (no system pattern
    # match); detect_tekton_namespaces() returns all-empty categories.
    # Result: namespaces_searched=0, no TLS issues, no crash.
    ToolCase("investigate_tls_certificate_issues",
             {"time_range": "24h", "max_namespaces": 3},
             {"k8s_core_api": _INFRA_CORE}),

    # ── 33. check_cluster_certificate_health ─────────────────────────────────
    # Scans "team-a" + system cert namespaces.  _FAKE_CERT_SECRET data has no
    # PEM header so it is silently skipped; total_certificates=0 (not crash).
    ToolCase("check_cluster_certificate_health", {},
             {"k8s_core_api": _INFRA_CORE}),

    # ── 34. get_openshift_cluster_operator_status ─────────────────────────────
    # _openshift_custom_api() returns _CO (kube-apiserver, Available=True,
    # Degraded=False).  Golden must show healthy operator status.
    ToolCase("get_openshift_cluster_operator_status", {},
             {"k8s_custom_api": _openshift_custom_api()}),

    # ── 35. get_machine_config_pool_status ────────────────────────────────────
    # _openshift_custom_api() returns _MCP_CR (worker pool, 3/3 ready,
    # 0 degraded).  Golden must show healthy pool status.
    ToolCase("get_machine_config_pool_status", {},
             {"k8s_custom_api": _openshift_custom_api()}),

    # ── 36. live_system_topology_mapper ──────────────────────────────────────
    # namespace_filter="team-a" (regex) selects the "team-a" namespace.
    # Brief had namespace="team-a" which is not a valid parameter (corrected
    # per parity_reference.json schema).
    ToolCase("live_system_topology_mapper", {"namespace_filter": "team-a"},
             {"k8s_core_api": _INFRA_CORE,
              "k8s_apps_api": FakeApi(
                  list_namespaced_deployment=items_list([]),
                  list_namespaced_replica_set=items_list([]))}),

    # ── 37. automated_triage_rca_report_generator ────────────────────────────
    # failure_identifier is required (corrected — brief had only namespace).
    # Uses _TEKTON_CORE (has read_namespaced_pod) so analyze_failed_pipeline
    # can fetch pod details for the pipelinerun path.
    ToolCase("automated_triage_rca_report_generator",
             {"failure_identifier": "build-run-2", "namespace": "team-a"},
             {"k8s_core_api": _TEKTON_CORE,
              "k8s_custom_api": _custom_api()}),
]

# ── 38. query_kubearchive ─────────────────────────────────────────────────────
# KUBEARCHIVE_ENABLED=false at import time → discover_endpoint() returns None
# → check_kubearchive_availability returns available=False → tool returns the
# structured "disabled/unavailable" response.  No patches needed.
# Brief used "pipelineruns" (plural) but valid resource_types are singular
# (pipelinerun, taskrun, pod, release, snapshot — per server-mcp.py:12502);
# corrected to "pipelinerun" so the validation passes and we reach the
# disabled-path response.
CASES += [
    ToolCase("query_kubearchive",
             {"resource_type": "pipelinerun", "namespace": "team-a"}),
]

CASES += [
    ToolCase(name="list_sources"),  # no k8s patches: pure config/registry read
    ToolCase(name="refresh_capabilities"),  # no patches: pure state read (all 'on' → no-op)
]

# ── Phase 3 file-source cases ─────────────────────────────────────────────────
# Imported from cases_file.py to keep the file-adapter characterisation
# isolated.  No existing case ids are touched; the circular import is safe
# because ToolCase is fully defined above before this import runs.
from .cases_file import FILE_CASES  # noqa: E402

CASES += FILE_CASES

# ── Phase 4 loki-source cases ─────────────────────────────────────────────────
# Imported from cases_loki.py to keep the loki-adapter characterisation
# isolated.  No existing case ids are touched; same circular-import pattern.
from .cases_loki import LOKI_CASES  # noqa: E402

CASES += LOKI_CASES

# ── Phase 4 elasticsearch-source cases ───────────────────────────────────────
# Imported from cases_es.py to keep the ES-adapter characterisation
# isolated.  No existing case ids are touched; same circular-import pattern.
from .cases_es import ES_CASES  # noqa: E402

CASES += ES_CASES

# ── Phase 2e Task 5: connect_cluster golden case ──────────────────────────────
# A no-dial golden: kubeconfig ref with no credential_ref_roots configured
# (the harness builtin-konflux profile has none) → deterministic
# ref_outside_allowlist error.  No network I/O, no patches required.
# The hint field must be the STATIC scheme-grammar string (round-1 F10).
CASES += [
    ToolCase(
        name="connect_cluster",
        kwargs={"name": "golden-test", "credential_ref": "kubeconfig:/nonexistent#none"},
    ),
]

# ── disconnect_cluster golden case ────────────────────────────────────────────
# A no-dial golden: unknown instance name → deterministic unknown_source
# error. No network I/O, no patches required, no registry mutation.
CASES += [
    ToolCase(
        name="disconnect_cluster",
        kwargs={"name": "golden-nonexistent-instance"},
    ),
]

# ── Phase 5 OTLP-source cases ─────────────────────────────────────────────────
# Imported from cases_otlp.py to keep the OTLP-adapter characterisation
# isolated.  No existing case ids are touched; the circular-import pattern is
# identical to cases_file.py and cases_loki.py.
from .cases_otlp import OTLP_CASES  # noqa: E402

CASES += OTLP_CASES
