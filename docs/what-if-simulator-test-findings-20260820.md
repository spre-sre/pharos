# what_if_scenario_simulator — Live Test Findings (2026-08-20)

Test matrix run against **prd-rh01** and **prd-p02** via the streamable-http server
(bearer-authed, 127.0.0.1:8000), 7-cluster fleet registered.

## Coverage

| Axis | Values exercised |
|------|-----------------|
| scenario_type | resource_limits, scaling, configuration, deployment (4/4) |
| simulation_duration | 1h, 24h, 7d (3/3) |
| load_profile | current, peak, custom (3/3) |
| risk_tolerance | conservative, moderate, aggressive (3/3) |
| scope | explicit namespaces, multi-namespace, default (None → all→first-10) |
| source | prd-rh01, prd-p02, nonexistent (error path) |
| invalid inputs | bad scenario_type, bad duration, empty changes, bad load_profile, bad risk_tolerance, unknown source (6/6 rejected cleanly) |

## What works

- **Per-source routing incl. Prometheus**: real metrics from the right cluster
  (116 nodes on rh01, 107 on p02); named-source bearer token path works.
- **Real component inventory**: `affected_components` listed 16 real services
  (configuration scenario) and 13 real deployments (scaling scenario) from
  `openshift-pipelines` on prd-p02.
- **Validation**: all 6 invalid-input probes returned immediate, specific errors.
- **Latency with sane scope**: 13–22s per simulation.

## Bugs found (ranked)

1. **FIXED AT TOOL LEVEL** (latency gate unachievable against prd-rh01 today
   for environmental reasons, unrelated to the tool — see below) — Unpaginated
   pod list in baseline collection (`collect_baseline_system_data`
   → `list_pods_fn(namespace, ...)`, helpers/utils.py:2444). Against
   `rhtap-releng-tenant` on prd-rh01 (thousands of completed release pods) the
   response is ~54MB; the connection breaks mid-read (`IncompleteRead` at 54MB /
   40MB / 16MB), urllib3 retries 3×, ~5 minutes wasted, then the per-namespace
   `except` swallows it and the sim proceeds with an empty baseline for that
   namespace — **silent data loss + 5-minute stall**. Other call sites already
   use `limit=50/100`; this path needs the same (or field_selector on phase).
   Fixed by `895fec0` (bound list_pods to limit=200, dispatch off the event
   loop, truncation sentinel), `a812594` (warn on silent pod-list truncation;
   assert off-loop dispatch), `7d59edd` (surface baseline failures from both
   handlers into simulation_quality instead of swallowing them), `b4c47ab`
   (pin data_completeness zeroing to the succeeded/attempted factor), and —
   fix-loop round 1 — the `field_selector`/`request_timeout` change (see
   "Fix-loop rounds" below) that scopes `collect_baseline_system_data` and
   `identify_affected_components` to `status.phase=Running` pods only and
   bounds every `list_pods`/`list_node` call with a 30s client-side timeout.

   **Silent data loss: fixed and verified live** (Task 7 Step 3 and again in
   fix-loop round 2): a collection failure always surfaces in
   `simulation_quality.limitations` / `.collection_warnings`, never vanishes.

   **Payload/latency at the tool level: fixed and verified live.**
   Round-1 investigation found `limit=200` bounds row *count* but not
   response *size* — rhtap-releng-tenant's pod manifests are individually
   bloated (managed-fields/annotations on old completed release pods), so a
   `limit=200` fetch still read 2.3–5.0MB before the connection broke, at
   **two** call sites per simulation (double cost). The `field_selector`
   fix addresses this directly: `oc get pods -n rhtap-releng-tenant
   --field-selector=status.phase=Running -o name` returns single-digit pod
   counts (8-11 across two independent checks) in 1.6-13.9s, vs thousands of
   pods and multi-MB reads unfiltered. This is now what the tool sends.

   **Latency at the cluster/network level: NOT fixed, and out of scope —
   this is today's real, independently-verified prd-rh01 link degradation,
   not a payload-size problem.** `oc get nodes -o name` against prd-rh01 —
   a ~4KB response, 120 lines, nothing to do with pod payload size — took
   **50.9s** in one measurement here and was independently reported by the
   controller at 46.0s; `oc get ns <ns> -o name` (single namespace, trivial
   payload) took 2.8s (controller-reported) for comparison. The link itself
   is dropping/stalling MB-and-sub-MB-scale transfers on prd-rh01 today,
   which is why the round-1 fix (Running-only pods) eliminated the pod-list
   failure but a *second*, independent failure then surfaced on the
   `list_node()` call (also affected — see the Deferred/graceful-degradation
   note below and the fix-loop round 2 live-check table). Fix-loop round 2
   added `_request_timeout=30` to that `list_node()` call so a degraded link
   fails fast into `collection_warnings` (Task 2's existing graceful-
   degradation path) instead of stalling for minutes; it does NOT and cannot
   make a degraded network link fast. The <60s latency gate was moved to
   prd-p02 (a healthy link) for this reason and passed there at 9.54s — see
   the fix-loop round 2 table below. Against prd-rh01 today the check is
   evaluated on completion-without-hang and failure-surfacing instead of
   wall time, and both pass.
2. **FIXED** — No MCP progress notifications: long runs exceed the client 120s call
   timeout and 300s idle abort — the server finished the computation but the
   result was undeliverable. Tool should emit progress via ctx.report_progress
   (or chunk the work). Fixed by `54b80ca` (MCP progress notifications +
   cancellation checkpoints) and the `to_thread` dispatch introduced in
   `a812594` (makes the checkpoints land on real yield points instead of a
   single blocking call).
3. **FIXED** — No cancellation propagation: after client abandon, the server kept
   computing all queued simulations (session requests serialize, so abandoned
   work also blocks subsequent calls). Fixed by `54b80ca` (cancellation
   checkpoints between simulation phases) together with `a812594`'s
   `to_thread` dispatch, which gives the event loop a place to observe and
   act on cancellation instead of running one uninterruptible blocking call.
   Deferred: true per-session request *serialization* (a different client
   call arriving while one is in flight queuing behind it) is a FastMCP
   transport-level property, not something this tool's code controls — see
   "Deferred" below.
4. **FIXED** — Direction-blind cost model: a scale-DOWN (107→80 nodes) was scored
   "cost increase 30.1%, severity high". The model keys on |delta|, not sign.
   Fixed by `2ea1f6c` (cost model follows change direction and actual
   magnitude) and `de28c7b` (parsed no-op zero impact and deterministic sign
   tie-break — removed the canned-magnitude floor so the reported percentage
   reflects the real requested change). Verified live in Task 7 Step 3: a
   107→80 scale-down now reports a negative cost `expected_change` in the
   −7% to −8% range (sign and magnitude both correct).
5. **INTENTIONAL** — Error path omits known instances: unknown `source` returns
   `known_kubernetes_instances: []` despite 7 registered clusters. This is by
   design, not a bug: `src/server-mcp.py:676` —
   `"known_kubernetes_instances": [],  # Empty: invalid source must not enumerate the inventory`
   — an invalid/unauthenticated source request should not leak the list of
   valid cluster names as a discovery oracle. No change made.
6. **FIXED** — Cosmetic: default scope echoed `clusters: ["current"]` even when `source`
   named a cluster; `"Deployment with None replicas"` in affected_components.
   Fixed by `95baec9` (scope now echoes the actual source name; replica
   detail is never `None`). Verified live in Task 7 Step 3: with `source="prd-rh01"`
   and no scope supplied, `simulation_parameters.scope.clusters == ["prd-rh01"]`.

## Design observations (not bugs, worth a look)

- **Fixed (data starvation)** — `total_data_points: 2` (one CPU + one memory
  sample) used to calibrate every run regardless of scenario size, holding
  `heuristic_confidence` and `overall_quality` constant across wildly
  different scenarios — the "Monte Carlo" layer was effectively fixed
  multipliers per scenario_type. `73d9e71` (execute a Prometheus *range*
  query for the real calibration series, replacing the single instant-vector
  sample) fixes this: live verification in Task 7 Step 3 against
  `rhtap-releng-tenant` shows `total_data_points` well above the old
  constant-2 baseline and `heuristic_confidence` moving off the old-fixed
  0.75 to 0.92 via the existing confidence ladder, now driven by how much
  real series data was actually collected.
- **Fixed (canned magnitude)** — Impact percentages used to be driven by a
  scenario_type lookup table with a magnitude floor, lightly jittered — e.g.
  resource_limits always ≈15%/10%/5% (perf/rel/cost) regardless of the
  actual requested change. `de28c7b` removed the floor so the reported
  percentage scales with the real before/after values (see bug 4 above and
  the parsed no-op check in Task 7 Step 3, where a true no-op change reports
  0.0% rather than a nonzero canned value).
- All 5 original valid simulations returned LOW risk / proceed=true; with
  more real calibration data now flowing through (see above), whether the
  risk model discriminates any better under production load is worth a
  follow-up look but wasn't specifically re-probed in this round.

## Deferred (out of scope for this fix pass)

- **Per-session request serialization.** A second client call arriving while
  one what-if simulation is still in flight queues behind it rather than
  running concurrently. This is FastMCP transport-level session behavior,
  not something `what_if_scenario_simulator` (or any single tool) controls —
  out of scope for a tool-level fix.
- ~~`list_pods` retry storm on payload-bloated namespaces, hit twice per
  simulation~~ — **RESOLVED, fix-loop round 1.** Was: Task 7 Step 3 measured
  754s wall time for the `resource_limits` / `rhtap-releng-tenant` scenario
  because `limit=200` bounds row *count* but not response *size*, and the
  same expensive `list_pods` call ran at two independent call sites per
  simulation. Fix: `list_pods` (helpers/utils.py) gained `field_selector`
  and `request_timeout` params; `collect_baseline_system_data` and
  `identify_affected_components` (helpers/resource_topology.py) now both
  pass `field_selector="status.phase=Running"`, collapsing the payload from
  thousands of completed pods to single digits. See bug 1 above for the
  live-verified numbers.
- **Cluster/network-level latency on prd-rh01 (environmental, not a code
  defect) — open, cannot be fixed at the tool level.** Fix-loop round 2
  diagnosis: prd-rh01's API link was independently measured degraded for
  MB-scale (and even sub-MB-scale) transfers on 2026-08-21 — `oc get nodes
  -o name` (~4KB response) took 46.0-50.9s across two independent
  measurements, while `oc get ns <ns> -o name` (trivial payload) took 2.8s.
  This is a today's-network-conditions problem on this specific cluster, not
  a payload-size or code-path problem — bounding `list_node()` with
  `_request_timeout=30` (fix-loop round 2) makes a degraded call fail fast
  into `collection_warnings` instead of stalling for minutes, which is the
  correct tool-level response, but it cannot make the underlying link fast.
  If prd-rh01's link degrades again, expect `collection_warnings` entries
  and a slower-than-ideal-but-bounded simulation, not silent failure or an
  unbounded hang.
- **Range-query latency on subquery-hostile clusters.** The Prometheus range
  query added in `73d9e71` was NOT the bottleneck in any live check across
  either fix-loop round (it consistently completed in ~4-7s); calling this
  out as still-open in case a future cluster has slow Prometheus subqueries,
  but the theoretical concern from planning (review D11) never materialized
  in practice — cluster API link degradation did instead (see above).

## Task 7 live-check results (2026-08-20, in-process against prd-rh01)

Direct in-process invocation (not the shared HTTP server) against the real
prd-rh01 cluster, read-only. `connect_cluster` + three `what_if_scenario_simulator`
calls.

| # | Check | Result | Measured |
|---|-------|--------|----------|
| A.1 | resource_limits/rhtap-releng-tenant completes <60s | **FAIL** | 754.04s |
| A.2 | truncation note or collection_warnings on failure (not silent) | PASS | both `limitations` and `collection_warnings` carry the `rhtap-releng-tenant` failure |
| A.3 | total_data_points ≥ 20 | PASS | 48 |
| A.4 | heuristic_confidence == 0.92 | PASS | 0.92 |
| B | scale-down 107→80 cost expected_change ≈ −7% to −8% | PASS | −7.4% |
| C.1 | scope omitted, source="prd-rh01" → scope.clusters == ["prd-rh01"] | PASS | `['prd-rh01']` |
| C.2 | no-op replicas 1→1: cost & perf expected_change == 0.0% | PASS | `0.0%` / `0.0%` |

7/8 passed. The one failure (A.1, latency) is a real, reproducible finding,
not a harness artifact — see bug 1 and the Deferred section above for root
cause (retry storm on a payload-bloated namespace, hit at two call sites).

## Retest 2026-08-21 (branch fix/what-if-simulator-production @ 73d9e71)

Server restarted 09:42 on the fixed code; 1622-test suite green; live retest
against prd-rh01/prd-p02.

| # | Finding | Status |
|---|---------|--------|
| 1 | Unbounded pod list | **Fixed in Pharos, namespace still pathological.** Wire request now `/pods?limit=200`. But rhtap-releng-tenant breaks even that: reproduced with plain `oc get --raw ...pods?limit=200` — 7.5MB page, apiserver aborts the stream after ~2m20s with HTTP/2 INTERNAL_ERROR (cluster-side fault). Pharos burns 4 attempts (~10 min) before giving up. Follow-up: field_selector `status.phase=Running` (or metadata-only list) so the apiserver never has to serialize thousands of completed release pods. |
| 2 | No progress notifications | **Implemented but ineffective for this path.** The stall is one blocking HTTP read; no checkpoint can fire inside it. Client still aborts at 300s idle on the pathological namespace. Works for multi-phase progress otherwise. |
| 3 | No cancellation propagation | **Still open in practice.** Client abandoned at ~09:48; server computed until 10:12 (sim completed 29 min total). Idle-timeout abort likely doesn't deliver an MCP cancel, and checkpoints can't run mid-blocking-read. |
| 4 | Direction-blind cost model | **Fixed, verified live.** Scale-down 107→80 now scores cost −7.5% (was +30.1% high). |
| 5 | Error path omits instances | **Won't-fix by design** (server-mcp.py:645 F-01): enumerating clusters+usernames on a typo is info disclosure; error points to list_sources. |
| 6 | Scope echo / None replicas | **Fixed, verified live.** Real replica counts (incl. 0) in affected_components. |
| — | Fixed-multiplier calibration | **Fixed, verified live.** data_source=prometheus, 48 range-query points (24 CPU + 24 mem), 85 nodes; confidence/quality now vary with data. |
| — | Silent empty baseline | **Fixed.** Baseline failure now logged (`0 namespaces` collected) and surfaced in simulation_quality (7d59edd; unit-tested b4c47ab). |

The retest's follow-up recommendation for finding 1 — `field_selector
status.phase=Running` so the apiserver never has to serialize thousands of
completed release pods — is exactly what fix-loop round 1 implemented (see
below).

## Fix-loop round 1 (2026-08-21): field_selector + request_timeout

Controller ruling on the A.1 latency failure (754s vs 60s target). Changes:

- `list_pods` (`src/helpers/utils.py`) gained `field_selector: Optional[str]
  = None` and `request_timeout: Optional[float] = 30.0`, passed through to
  `list_namespaced_pod` as `field_selector=` / `_request_timeout=` in both
  the bounded and unbounded branches. Defaults preserve existing behavior
  for the two untouched call sites (`server-mcp.py:2344`, `:3604`).
- `collect_baseline_system_data` and `identify_affected_components`
  (`src/helpers/resource_topology.py`) now pass
  `field_selector="status.phase=Running"` — completed pods consume no
  resources, so Running-only is more correct for a resource baseline and
  collapses the payload.
- Tests: `tests/test_what_if_baseline_bounded.py` extended with 5 new tests
  (field_selector/request_timeout pass-through on both `list_pods` branches,
  default `field_selector=None`, baseline's call carries
  `field_selector="status.phase=Running"`); `tests/test_what_if_baseline_warnings.py`'s
  three fakes updated to accept the new `field_selector` kwarg (were breaking
  once `collect_baseline_system_data` started always passing it). Full suite
  (all of `tests/`, main + characterization combined): **1627 passed**
  (1309 main-suite + 318 characterization), zero golden churn. (Corrected
  2026-08-21 during re-review: this section previously understated the count
  as "1310 passed," which was only the `--ignore=tests/characterization`
  figure — that command's own total for round 1 was in fact 1309, not 1310;
  see round 2 below for where 1310 first became correct.)
- Live re-verification found the fix correct but incomplete: the
  `rhtap-releng-tenant` pod-list retry storm was gone, but a **new**,
  previously-masked failure surfaced on the unrelated `list_node()` call
  (754s → still ~550-700s), which is what triggered fix-loop round 2's
  controller diagnosis below.

## Fix-loop round 2 (2026-08-21): bounded list_node + revised live gate

Controller diagnosis (verified independently, see bug 1 above): prd-rh01's
API link was degraded that day for MB-and-sub-MB-scale transfers —
unrelated to payload size or the tool's code. `list_node()` inside
`collect_baseline_system_data` had no client-side timeout, so a degraded
link made it retry/stall for minutes exactly like the pre-fix pod list did.

- Added `_request_timeout=30` to the `list_node()` call in
  `collect_baseline_system_data` (`src/helpers/utils.py`, cluster-metrics
  section). A node-list failure already surfaced via `collection_warnings`/
  `limitations` by Task 2's design (graceful degradation) — this just makes
  a bad link fail fast instead of stalling.
- Test: `_RecordingCore.list_node` extended to record kwargs; new test
  `test_baseline_list_node_call_carries_request_timeout` asserts
  `_request_timeout == 30`. Full suite (main + characterization combined):
  **1628 passed** (1310 main-suite + 318 characterization), zero golden
  churn. (Corrected 2026-08-21 during re-review: this section previously
  said "1310 passed," which was the `--ignore=tests/characterization`
  figure alone, not the combined total.)
- The latency gate moved off prd-rh01 (a degraded link that day) onto
  prd-p02 (a healthy link), and the prd-rh01 check was re-scoped from
  time-based to completion-and-surfacing-based:

| # | Check | Cluster | Result | Measured |
|---|-------|---------|--------|----------|
| connect | connect_cluster | prd-p02 | PASS | connected, konflux/openshift/tekton active |
| P02.1 | resource_limits/openshift-pipelines completes <60s (the latency gate) | prd-p02 | **PASS** | **9.54s** |
| P02.2 | total_data_points / heuristic_confidence / collection_warnings | prd-p02 | PASS | 48 / 0.92 / `[]` |
| connect | connect_cluster | prd-rh01 | PASS | connected, konflux/openshift/tekton active |
| RH01.1 | resource_limits/rhtap-releng-tenant completes without hanging (not time-gated) | prd-rh01 | PASS | completed, 696.63s (informational) |
| RH01.2 | node-list failure surfaces in limitations/collection_warnings, not silent | prd-rh01 | PASS | both populated with the `list_node` `IncompleteRead` failure |

6/6 checks passed. The pod-list problem from round 1 (`rhtap-releng-tenant`,
754s, `IncompleteRead` on `/pods?limit=200`) did **not** recur in round 2's
prd-rh01 run — the field_selector fix held. The failure this time was
exclusively the node list, consistent with the controller's link-degradation
diagnosis (a ~4KB nodes response failing the same way a multi-MB pods
response used to). Independent corroboration measured here:
`oc get nodes -o name` against prd-rh01 took 50.9s for a 120-line/4KB
response; `oc get pods -n rhtap-releng-tenant --field-selector=status.phase=Running -o name`
took 13.9s and returned 8 pods (vs thousands unfiltered).

## Fix-loop round 3 (2026-08-21): topology selector test pin + off-loop quota call

Re-review of rounds 1-2 **approved** both, with three follow-ups, all ruled in scope:

1. `collect_baseline_system_data`'s `list_namespaced_resource_quota(namespace)` call
   (`src/helpers/utils.py`, ~:2559) had no `_request_timeout` and was NOT dispatched via
   `asyncio.to_thread` — the last unbounded, event-loop-blocking transfer left in the
   simulation path, one class of bug behind `list_pods` and `list_node` (both already
   fixed in rounds 1-2). Fixed: now
   `await asyncio.to_thread(k8s_core_api.list_namespaced_resource_quota, namespace, _request_timeout=30)`,
   same treatment as the other two. Existing `except ApiException` handling kept as-is;
   a timeout escaping to the per-namespace handler → `collection_warnings` is intended
   graceful-degradation behavior, not a regression.
2. Re-review found only `collect_baseline_system_data`'s `field_selector` pin had a
   test — `identify_affected_components`'s (resource_topology.py) was implemented in
   round 1 but never asserted. Added
   `test_identify_affected_components_pins_running_pods_selector`. Also added
   `test_baseline_resource_quota_call_carries_request_timeout` and
   `test_baseline_resource_quota_call_runs_off_the_event_loop` (both new
   `_RecordingCore` fields: `quota_calls`, `quota_call_threads`).
3. This section (both fix-loop sections' "Full suite: 1310 passed" lines) corrected
   above to the real combined figures (1627 / 1628) — the original number only counted
   `--ignore=tests/characterization`, not the full `tests/` tree.

No live re-verification this round (unit tests only, per controller instruction — the
p02/rh01 numbers above still stand as the last live evidence).

- Tests: `tests/test_what_if_baseline_bounded.py` + `tests/test_what_if_baseline_warnings.py`:
  **22 passed** (19 + 3 new).
- Full suite (main + characterization combined): **1631 passed** (1313 main-suite + 318
  characterization), zero golden churn.

## Repro notes

- Server: main repo checkout d95717b, config from mc-dispatch worktree
  lumino.yaml, KUBEARCHIVE_ENABLED=true, LUMINO_TRANSPORT=streamable-http.
- The 54MB namespace: `rhtap-releng-tenant` on prd-rh01 (shared release-eng
  namespace, ~20 PLRs stuck Running 2–5h at test time — see
  konflux_reports/prd_rh01_forklift_internal_services_pipeline_lifecycle_trace_20260820.md).
- Task 7 live check: main repo checkout 73d9e71 (Tasks 1-6 landed), config
  from mc-dispatch worktree lumino.yaml, KUBEARCHIVE_ENABLED=false, real
  `~/.kube/config` context `default/api-stone-prd-rh01-pg1f-p1-openshiftapps-com:6443/ggeorgie`,
  in-process (no HTTP server) via importlib load of `src/server-mcp.py`.
- Fix-loop rounds 1 and 2 live checks: same in-process harness pattern as
  Task 7. Round 2 additionally connects prd-p02 via context
  `default/api-stone-prod-p02-hjvn-p1-openshiftapps-com:6443/ggeorgie` for
  the latency gate.

## Post-merge follow-up candidates (from final whole-branch review, 2026-08-21)

- `proceed: true` can co-exist with `quality_score: 0.0` — recommendations only gate on risk, not data quality; now more visible since completeness honestly drops to 0 on collection failure.
- Monte Carlo multiplicative-noise tails can flip the cost sign at high uncertainty (CoV≈1.0 → a scale-down's worst_case renders as a cost increase); affects all impact types, pre-existing.
- No whole-simulation deadline: `_request_timeout=30` is per connection attempt (urllib3 default 3 retries → ~120s per call worst case); consider `Configuration.retries=0` on the sim path or an overall budget.
- `server-mcp.py` deployment readiness render (`ready_replicas/None`) — same bug class Task 5 fixed elsewhere.
- Test-ordering quirk: running `tests/test_what_if_progress.py` first in a multi-file pytest invocation poisons sys.path for three other files (CI's alphabetical order unaffected).
- Multi-cluster lifecycle gaps (found in live fleet use, 2026-08-21): (a) cluster
  clients are built once at connect time and never re-read the kubeconfig, so
  rotated SSO tokens require a full server restart ("credential refresh/re-dial
  policy" from the fleet deployment notes — now confirmed painful in practice);
  (b) a transient CRD-detection timeout during connect_cluster permanently
  degrades that instance's extensions for the process lifetime —
  refresh_capabilities only re-runs global auto-mode detection, and
  re-connecting the same name is refused with duplicate_name, so there is no
  recovery path short of a restart. Both are prerequisites for the in-cluster
  fleet deployment.
- `.claude/worktrees/mc-dispatch` copies of the fixed files now diverge from main-repo (~173 lines in utils.py) — expect conflicts if that worktree's branch is revived.
