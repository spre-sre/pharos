# Migration Guide — Pharos (was lumino-mcp-server)

> All tool counts and schema diffs below are machine-verified.
> The additive-parity claim is enforced continuously by
> `tests/characterization/test_parity.py::test_parity_additive_only`.
> The profile counts and extension delta are pinned by
> `tests/test_profile_docs.py` (M5 drift defence).

This guide covers two audiences:

- **Upstream GitHub consumers** pinned to commit `b6c24f27`
  (`github.com/spre-sre/lumino-mcp-server`, 39 tools)
- **GitLab downstream consumers** pinned to snapshot `49022a8`
  (SPAI-610, 21-tool surface with `k8s/` kustomize overlays)

---

## Additive-parity proof

Every tool registered at `b6c24f27` is still registered in Pharos, with
byte-identical schemas except for additive parameters.

```
parity_baseline_phase0.json  (39 tools, immutable — the b6c24f27 surface)
    ⊂
parity_reference.json        (48 tools)
    =  9 added / 0 removed
```

The 9 additions:

| Addition | Kind |
|----------|------|
| `analyze_logs_hybrid` | canonical alias for `analyze_pod_logs_hybrid` |
| `get_events_smart` | canonical alias for `smart_get_namespace_events` |
| `query_metrics` | canonical alias for `prometheus_query` |
| `smart_summarize_logs` | canonical alias for `smart_summarize_pod_logs` |
| `stream_analyze_logs` | canonical alias for `stream_analyze_pod_logs` |
| `topology_mapper` | canonical alias for `live_system_topology_mapper` |
| `connect_cluster` | meta tool — register additional cluster at runtime |
| `list_sources` | meta tool — enumerate configured sources |
| `refresh_capabilities` | meta tool — re-detect active extensions |

**Net: nothing that was live at `b6c24f27` was removed.** See the
[Deletions](#deletions-nothing-live-was-removed) section for the tools
that appear to be missing but were never live at the pinned commit.

---

## Canonical name aliases (§4.4)

Six generic tools now have canonical (preferred) names. The original
pod/namespace-suffixed names remain **fully registered** and continue
to work with no consumer change required.

| Original name (still works) | Canonical name (preferred) |
|---|---|
| `analyze_pod_logs_hybrid` | `analyze_logs_hybrid` |
| `live_system_topology_mapper` | `topology_mapper` |
| `prometheus_query` | `query_metrics` |
| `smart_get_namespace_events` | `get_events_smart` |
| `smart_summarize_pod_logs` | `smart_summarize_logs` |
| `stream_analyze_pod_logs` | `stream_analyze_logs` |

Both names dispatch to the **same function object** (verified by
`tests/test_canonical_aliases.py::test_m1_shared_body`), producing
identical output with identical schemas. Use the canonical name in new
agents and runbooks; existing calls using the original name require no
change.

---

## Extension tool relocations

12 tools now register only when their extension is active. Under the
default `konflux` profile the surface is **unchanged** — all 12 are
present. Under the `kubernetes` or `standalone` profiles they are
**genuinely absent**.

This is the **one behavioral difference** a consumer can hit, and only
by explicitly choosing a non-default profile (`LUMINO_PROFILE=kubernetes`
or `LUMINO_PROFILE=standalone`).

The 12 extension tools:

```
analyze_failed_pipeline         ci_cd_performance_baselining_tool
find_pipeline                   get_etcd_logs
get_machine_config_pool_status  get_openshift_cluster_operator_status
get_pipelinerun_logs            get_tekton_pipeline_runs_status
list_pipelineruns               list_recent_pipeline_runs
list_taskruns                   pipeline_tracer
```

See [README.md Profiles section](../README.md#profiles) for the
full per-profile breakdown and source declarations.

---

## Deletions: nothing live was removed

The tools listed below appear to be absent but were **never live at the
`b6c24f27` pinned commit** and therefore do not appear in the 39-tool
baseline:

| Tool | Reason absent |
|------|---------------|
| `templatize_pod_logs` | Required the optional `logan` dependency (`git+https://...`), which was never installed on the deployed server. The `if _LOGAN_AVAILABLE:` guard at `server-mcp.py:13016` prevented registration. |
| `deep_analyze_pod_logs` | Same `logan` dependency; same guard. |
| `get_konflux_components_status` | Already commented out at commit `b6c24f27` (see `server-mcp.py:2735`); absent from the 39-tool baseline by definition. |
| `track_pipeline_across_namespaces` | Already commented out at commit `b6c24f27` (see `server-mcp.py:3252`); absent from the 39-tool baseline by definition. |

The `logan` optional-dependency entry (`pyproject.toml:37-38`) and the
`allow-direct-references = true` flag that existed solely for it have
been removed in Pharos. The `~214`-line `if _LOGAN_AVAILABLE:` code
block remains in `server-mcp.py` but is now unreachable by any
supported install path. Formal deletion is deferred to a future cleanup
pass; the tool surface is unaffected.

---

## Additive parameters

The following parameters were added to existing tools between the
`parity_baseline_phase0.json` snapshot and the current
`parity_reference.json`. All additions are **optional** with safe
defaults; no existing call is broken.

**Generation snippet** (run from repo root to reproduce this list):

```python
import json

with open("tests/characterization/parity_baseline_phase0.json") as f:
    baseline = json.load(f)["tools"]
with open("tests/characterization/parity_reference.json") as f:
    reference = json.load(f)["tools"]

for name in sorted(baseline):
    if name not in reference:
        continue
    b = set(baseline[name].get("input_schema", {}).get("properties", {}).keys())
    r = set(reference[name].get("input_schema", {}).get("properties", {}).keys())
    added = sorted(r - b)
    if added:
        print(f"{name}: {added}")
```

**Per-tool added parameters** (generated from the above script):

| Tool | Added parameters |
|------|-----------------|
| `adaptive_namespace_investigation` | `source` |
| `advanced_event_analytics` | `source` |
| `analyze_failed_pipeline` | `source` |
| `analyze_logs` | `source` |
| `analyze_pod_logs_hybrid` | `source` |
| `automated_triage_rca_report_generator` | `source` |
| `check_cluster_certificate_health` | `source` |
| `check_resource_constraints` | `source` |
| `ci_cd_performance_baselining_tool` | `max_context_tokens` |
| `conservative_namespace_overview` | `source` |
| `detect_anomalies` | `source` |
| `detect_log_anomalies` | `source` |
| `find_pipeline` | `source` |
| `get_etcd_logs` | `max_context_tokens`, `source` |
| `get_kubernetes_resource` | `source` |
| `get_machine_config_pool_status` | `source` |
| `get_openshift_cluster_operator_status` | `source` |
| `get_pipelinerun_logs` | `source` |
| `get_tekton_pipeline_runs_status` | `source` |
| `investigate_tls_certificate_issues` | `source` |
| `list_namespaces` | `source` |
| `list_pipelineruns` | `source` |
| `list_pods_in_namespace` | `limit`, `source` |
| `list_recent_pipeline_runs` | `source` |
| `list_taskruns` | `source` |
| `live_system_topology_mapper` | `max_context_tokens`, `source` |
| `manage_prediction_training_data` | `source` |
| `predictive_log_analyzer` | `source` |
| `progressive_event_analysis` | `source` |
| `prometheus_query` | `source` |
| `resource_bottleneck_forecaster` | `source` |
| `search_resources_by_labels` | `source` |
| `semantic_log_search` | `source` |
| `smart_get_namespace_events` | `source` |
| `smart_summarize_pod_logs` | `source` |
| `stream_analyze_pod_logs` | `source` |

`source`: selects which registered source instance handles the call
(default `""` = the default kubernetes instance). Pass the source name
from `list_sources` to target a specific cluster or adapter.

`max_context_tokens`: integer upper bound on log/output size returned
(tools that can produce very large outputs). Safe to omit; the tool
applies its own internal default limit.

`limit` on `list_pods_in_namespace`: integer cap on the number of pods
returned. Safe to omit.

---

## New surface

### Three meta tools

| Tool | Purpose |
|------|---------|
| `list_sources` | Enumerate all configured source instances with connection status. |
| `connect_cluster` | Register an additional cluster at runtime via a credential reference. |
| `refresh_capabilities` | Re-detect active extensions and update the tool list (for `auto`-mode extensions). |

### Environment variables

See the full env-var table in the [README.md Profiles section](../README.md#profiles)
and in `config.example.yaml`. The table includes the previously undocumented
`LUMINO_PROFILE`, `LUMINO_CONFIG`, `LUMINO_STRICT_MODEL_LOADING`,
`TOKENIZERS_PARALLELISM`, and the disclosure that
**`LUMINO_DISABLE_TELEMETRY` is a no-op** (not read by `src/` or `main.py`).

### Transport and authentication

| Item | Detail |
|------|--------|
| Default transport | **stdio** (unchanged) |
| Container transport | `streamable-http`, binds `0.0.0.0:8000` inside the container. |
| Fail-closed contract | Non-localhost bind without `LUMINO_HTTP_TOKEN` → `main.py` refuses startup (exits 1). A bare `podman run` on the container image **intentionally exits 1**. |
| `/health` route | New unauthenticated HTTP GET endpoint; returns `{"status":"ok"}`. Not a tool. |
| OTLP ingest | Optional second listener on port `4318` with its **own separate** bearer token (`LUMINO_OTLP_TOKEN`). |

See `config.example.yaml` for the full transport reference including the
TLS-at-edge requirement and single-replica `stateless_http` caveat.

---

## Identity changes

| Item | Was (`b6c24f27`) | Now (Pharos) |
|------|-----------------|-------------|
| Container image | `quay.io/geored/lumino-mcp-server` | `quay.io/geored/pharos` |
| MCP serverInfo `name` | `lumino-mcp-server` | `pharos` |
| Outbound `User-Agent` | `LUMINO-MCP/1.0` | `Pharos/1.0` |
| MCP registry id | `io.github.geored/lumino` | `io.github.geored/pharos` |

**Compatibility note (internal identifiers are unchanged):** the
`LUMINO_*` environment variable prefix, the `lumino-mcp` logger tree
(`getLogger("lumino-mcp")`), and the `~/.lumino/` on-disk data
directory (`~/.lumino/models/`, `~/.lumino/training_data/`) are
**retained for compatibility** with existing deployments. Renaming these
surfaces will happen in a future major release with a deprecation window.
Operators do not need to rename their Secrets, Deployments, or log
filter rules.

---

## GitLab downstream (`49022a8`, 21-tool surface)

The SPAI-610 GitLab downstream snapshot (`49022a8`) ships a 21-tool
subset. Migration to Pharos is **purely additive** — every tool in that
snapshot is present in Pharos with the same or superset schema.

One structural difference requires attention:

**`k8s/` kustomize overlays.** The downstream snapshot includes
`k8s/base/rbac.yaml` with kustomize base and `rbac/testing/openshift`
overlays. **Pharos does not carry these overlays forward.** Consumers
relying on those manifests must port them onto
`deploy/rbac-readonly.yaml` (the new tiered RBAC manifest, with
aggregation-rule composition, `pharos`-prefixed names, and a
living-inventory drift-defence test). The new manifest is more
conservative: it omits cluster-wide `secrets` from the default aggregate
(separated into an opt-in tier), removes API groups the code never
touches, and adds API groups that were missing (e.g. `nodes`,
`route.openshift.io`, `machineconfiguration.openshift.io`,
`monitoring.coreos.com`, `appstudio.redhat.com`).
