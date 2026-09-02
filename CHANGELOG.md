# Changelog

All notable changes to Pharos MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-09-02

First release of Pharos as an independent project at
[spre-sre/pharos](https://github.com/spre-sre/pharos) (fresh-history import;
derived from lumino-mcp-server, see NOTICE).

### Added
- Multi-cluster source dispatch: every tool accepts `source=<cluster>`; `connect_cluster` registers named clusters from kubeconfig contexts
- Streamable HTTP transport with bearer-token auth (fail-closed on 0.0.0.0)
- GitHub Actions CI: 1699-test suite gate, multi-arch (amd64/arm64) container build, dev images to quay.io/geored/pharos, post-push smoke test
- Live tool matrix runner (`scripts/live_matrix.py`): spawns the MCP server as a stdio subprocess, sweeps all 48 tools against a live cluster, persists JSON run records, and diffs any two runs to flag schema/status regressions
- Input catalog covering all 48 tools with a completeness gate — every registered tool has a declared call shape enforced by a coverage test

### Changed
- `server-mcp.py` refactored from ~13,700 lines into a `helpers/` module family (`prometheus.py`, `log_analysis.py`, `event_analysis.py`, `resource_topology.py`, `decorators.py`, `utils.py`, `core/credentials.py`) with function bodies moved verbatim apart from documented client/callable injection, and zero parity/golden diff

### Fixed
- `prometheus_query` bearer-token lookup now reads directly from the kubeconfig when `oc` is unavailable; tolerates malformed entries and tilde-prefixed paths
- `manage_prediction_training_data` terminal statuses centralised to a shared constant; timezone-aware `datetime.now(UTC)` replaces four deprecated `utcnow()` call sites; `exceeded quota` admission keyword broadened beyond Kueue-only usage

## [1.1.0] - 2026-07-28

Live validation of all 48 tools against a 3,562-namespace Konflux stage cluster produced 37 distinct findings (20 tag-blockers); 21 closed across five correctness waves (cleanup2a–2e), plus the pre-campaign cleanup1 wave.

### Fixed
- **cleanup1** — self-provisioning RBAC removed; `search_resources_by_labels` silent resource fabrication killed (per-iteration reset + structured rejection); `manage_prediction_training_data` capability gate applied uniformly; admission-webhook API plurals corrected
- **cleanup2a** — source-error messages no longer enumerate the full adapter inventory; `resource_bottleneck_forecaster` scalar extraction corrected (was reading the second character of already-unwrapped metric strings, e.g. `"47.3"` → 7.0) at three sites; `get_etcd_logs` `since_time` converted to `since_seconds` correctly with UTC coercion [F-01, F-08, F-17, F-03]
- **cleanup2b** — `search_resources_by_labels` label-selector `ValueError` guard added; `check_resource_constraints` returns a clear error on non-existent namespaces instead of a false `Healthy` verdict; `get_tekton_pipeline_runs_status` cluster-wide discovery corrected (probe was exhausting its cap on alphabetically-early dormant tenants); `analyze_failed_pipeline` no longer fabricates RCA for `Unknown`-status TaskRuns; `pipeline_tracer` three-way stage classification prevents simultaneous `in_progress`+`failed` [F-04, F-07, F-02, F-14, F-15]
- **cleanup2c** — Honesty contract (`docs/HONESTY-CONTRACT.md`, `build_coverage()` helper, three-layer gate: structural golden check + per-adopter semantic check + AST registry-drift guard) introduced; `advanced_event_analytics` now receives the full event list (was being fed 3 of 92 events because a display cap was reused as model input); Kueue quota-exhaustion classifies as RESOURCE/HIGH; HIGH-severity factor and `UNCHARACTERISED` signal added to the risk model; `critical_events_found` field added to `adaptive_namespace_investigation` response (commit e15bc5e) — note: this field was renamed `high_or_critical_events_found` in cleanup2d (Obligation 0) [F-12, F-13, F-11a/b, F-21]
- **cleanup2d** — `pipeline_tracer` terminal-reason set closed (seven reasons including cancelled, timed-out, and `CouldntGetPipeline` were reported as `in_progress`); fabricated ML precision/recall and hardcoded `model_accuracy` ladder removed from `predictive_log_analyzer`; certificate and TLS scans promoted to Clause A coverage adopters; RBAC-denied certificate scan no longer reads as clean; single parity regen (19 description entries) covering canonical-alias docstrings, semantic-search accuracy, and carry-forward annotation fixes; `critical_events_found` renamed `high_or_critical_events_found` in `adaptive_namespace_investigation` (Obligation 0) [F-38, F-05, F-06, F-23, F-16, F-27a/b, F-28]
- **cleanup2e** — `list_namespaces` no longer returns the source adapter inventory on error; `get_openshift_cluster_operator_status` health keyed on operator data rather than the namespace list [F-01 partial, F-27]

## [1.0.0] - 2026-07-27

Full spec (§4–§9) delivery. v1.0.0-alpha.1 (2026-07-26) cut after all pre-1.0 phases (0 through 2f plus 3, 3.5, and 4) reached 856 tests and 48 tools; this release adds phases 5–6 and live-session fixes.

### Added
- **Phases 0–2: abstraction scaffold** — 39-tool characterization/golden suite with frozen parity baseline (phase 0); `core/signals.py` (`LogRecord`/`LogBatch`/`Provenance`) and `ReadOnlyCoreV1`/`ReadOnlyK8sClient` classifying every client access read-only (phases 1a–1e); config loader with built-in profiles, `AdapterRegistry`, `source=` dispatch parameter with capability gating on all 16 generic tools, 6 canonical tool aliases (`smart_summarize_logs`, `stream_analyze_logs`, `analyze_logs_hybrid`, `get_events_smart`, `query_metrics`, `topology_mapper`) bringing tool count to 48, extension mechanism with knowledge packs and `konflux` extension module, `connect_cluster` meta-tool with scheme-allowlisted credential refs and per-instance extension detection, `kubeconfig_dir` dial-free multi-file discovery, streamable-HTTP transport with `BearerASGIMiddleware` fail-closed serving, `/health` route, and `scripts/smoke_container.sh` tier-2 smoke test (phases 2a–2f)
- **Phases 3 / 3.5: file adapter + time-window** — `FileLogSource` (format sniffing, Entity-glob fetch, window semantics); dispatch wired into `smart_summarize_pod_logs` and `stream_analyze_pod_logs`; `make_time_window` live; token-budget bounding on three context-killer tools (`get_pipelinerun_logs`, `get_etcd_logs`, `ci_cd_performance_baselining_tool`)
- **Phase 4: remote log adapters** — `LokiLogSource` (LogQL compilation, injection-protected) and `ESLogSource` (script-rejected native bodies); shared HTTP transport with timeout enforcement and env-ref auth
- **Phase 5: OTLP/HTTP ingest** — in-process OTLP receiver on port 4318, bounded `LogRing` under lock, dual wire+decompressed bounds with zip-bomb guards, `OtlpLogSource` with honest `covered_window`, separate `LUMINO_OTLP_TOKEN`; zero new runtime dependencies
- **Phase 6: hardening** — read-only tripwire widened to all 55 source files (3-site anchored allowlist); tiered RBAC ClusterRole manifest (`deploy/rbac-readonly.yaml`) with living-inventory drift test; remote-agnostic secret-gated CI with tripwire-first and golden-gate enforcement; Pharos identity at 8 user-visible surfaces; Logan optional-dependency removed; container image at `quay.io/geored/pharos`

### Fixed
- Production startup crash: `main.py` did not register the server module in `sys.modules` before `exec_module` (live-only; regression-tested)
- Pod-log bytes-repr mangling: kubernetes client 36.x + urllib3 2.x returns pod logs as `str(bytes)` with literal `\n`; `normalize_pod_log_text` added at all three fetch sites
- `pipeline_tracer` cross-tenant lineage resolved in the release origin namespace; managed/tenant/final PLRs classified as release stage; `Released=False`+`Progressing` is in-flight, not failed
- `live_system_topology_mapper` and `list_pods_in_namespace` output-bounded to prevent 1.32 MB and ~18 MB payloads
- Cross-cluster namespace-cache poisoning: `list_namespaces` global single-slot cache was not keyed by kubernetes instance; fixed with per-instance slot and rollback purge

## [0.9.3] - 2026-01-30

### Added
- ML model persistence for `predictive_log_analyzer`
- KubeArchive fallback instructions added to pipeline logs tools

### Fixed
- Security: secret files excluded from package builds
- `resource_bottleneck_forecaster` no longer returns metrics for inactive nodes
- `pipeline_tracer` PR matching corrected for Konflux/AppStudio PAC labels
- `get_openshift_cluster_operator_status` fallback mode representation corrected
- `semantic_log_search` log-level misclassification fixed

## [0.9.2] - 2026-01-24

### Added
- Published to [MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.geored/lumino`
- Added PyPI badge to README
- Added `mcp-name` comment to README for registry validation

### Changed
- Registry name changed to `io.github.geored/lumino` for publishing

## [0.9.1] - 2026-01-24

### Added
- MCP Registry support with `server.json` for registry submission
- `[tool.mcp]` section in pyproject.toml for registry integration
- Published to [PyPI](https://pypi.org/project/lumino-mcp-server/) with full README documentation

### Changed
- Updated package description to better reflect SRE observability capabilities
- Added keywords: `sre`, `observability`, `pipelines`

### Fixed
- `analyze_failed_pipeline` - Handle deleted pods gracefully with fallback to TaskRun step info
- `check_cluster_certificate_health` - Fix duplicate namespace entries and respect user namespace filter
- `check_resource_constraints` - Add lowercase 'k' suffix support for count quotas (e.g., "2k" = 2000)
- `detect_log_anomalies` - Fix pattern key extraction showing "?i)" instead of category names
- `ci_cd_performance_baselining` - Filter out "unknown" task entries from Prometheus metrics

## [0.9.0] - 2026-01-18

### Added

#### Core Infrastructure
- Initial release of Lumino MCP Server with **37 MCP tools**
- MCP (Model Context Protocol) integration for AI-powered Kubernetes operations
- Multi-cluster support with automatic context detection
- Prometheus integration for metrics queries
- Namespace caching with 1-day TTL for performance

#### Kubernetes Tools
- `list_namespaces` - List all namespaces in the cluster
- `list_pods_in_namespace` - List pods with status and placement info
- `get_kubernetes_resource` - Retrieve details about any Kubernetes resource
- `search_resources_by_labels` - Search resources across types and namespaces
- `check_resource_constraints` - Identify resource bottlenecks

#### Tekton Pipeline Tools
- `list_pipelineruns` - List PipelineRuns with status and timing
- `list_taskruns` - List TaskRuns, optionally filtered by PipelineRun
- `find_pipeline` - Find pipelines matching a pattern across namespaces
- `get_pipelinerun_logs` - Fetch logs from all pods in a PipelineRun
- `get_tekton_pipeline_runs_status` - Cluster-wide status summary
- `list_recent_pipeline_runs` - Recent PipelineRuns across all namespaces
- `analyze_failed_pipeline` - Root cause analysis for failed pipelines

#### Analysis & Diagnostics
- `analyze_logs` - Extract error patterns and insights from logs
- `detect_anomalies` - Statistical anomaly detection in PipelineRuns
- `detect_log_anomalies` - ML-powered log anomaly detection
- `smart_get_namespace_events` - Adaptive event analysis with auto-filtering
- `progressive_event_analysis` - Multi-level event correlation
- `advanced_event_analytics` - ML patterns with runbook suggestions

#### Log Analysis Tools
- `smart_summarize_pod_logs` - Adaptive pod log analysis
- `stream_analyze_pod_logs` - Streaming log analysis with pattern detection
- `analyze_pod_logs_hybrid` - Intelligent strategy selection for log analysis
- `semantic_log_search` - Natural language log search

#### Predictive & Forecasting
- `predictive_log_analyzer` - ML-based failure prediction
- `resource_bottleneck_forecaster` - Resource exhaustion forecasting
- `what_if_scenario_simulator` - Impact simulation for config changes

#### CI/CD Performance
- `ci_cd_performance_baselining_tool` - Performance baselines with statistical analysis
- `pipeline_tracer` - Trace operations through pipeline flows
- `automated_triage_rca_report_generator` - Automated root cause analysis reports

#### OpenShift Support
- `get_machine_config_pool_status` - MCP status and update monitoring
- `get_openshift_cluster_operator_status` - Cluster operator health checks
- `get_etcd_logs` - Retrieve etcd pod logs
- `check_cluster_certificate_health` - Certificate expiration scanning
- `investigate_tls_certificate_issues` - TLS issue investigation

#### Namespace Investigation
- `conservative_namespace_overview` - Quick namespace health check
- `adaptive_namespace_investigation` - Progressive namespace analysis
- `live_system_topology_mapper` - Real-time dependency graph generation

#### Prometheus Integration
- `prometheus_query` - Execute PromQL queries with automatic auth

### Performance Optimizations
- `find_pipeline` - Cluster-wide queries with API limits and optional TaskRun fetching
- `ci_cd_performance_baselining_tool` - Parallelized Prometheus queries
- `get_tekton_pipeline_runs_status` - Configurable limits to prevent timeouts on large clusters
- `pipeline_tracer` - Parallelization and namespace targeting
- `predictive_log_analyzer` - Namespace targeting and improved log collection

### Container Image
- Available at `quay.io/geored/pharos`
- Multi-architecture support (amd64, arm64)
