# Honesty Contract

Lumino MCP server tools that make claims to callers must substantiate those claims
with evidence. This document defines the four clauses of that contract and records
which tools comply.

## Clause A — Coverage of a scan

A tool that iterates a bounded population (pods, namespaces, certificates) and
reports population-level conclusions must disclose what it requested, what it
discovered, and what it actually processed. A clean result from a scan that read
zero items is NOT evidence of a healthy cluster; it is evidence of a failed scan.

**Implementation:** `build_coverage(unit, requested, discovered, scanned, denied=0, skipped=0, **extra)`
in `src/helpers/utils.py`. Every `coverage` block MUST carry `requested_mode` set to
`"all"` (tool scanned the whole discovered population) or `"explicit"` (caller requested
specific items), passed via `**extra` — it is validated by the per-adopter semantic test
in `ADOPTER_GOLDEN_TABLE` but is NOT enforced at construction. The `verdict` field is
computed by the function:
- `"none"` — discovered == 0 or scanned == 0
- `"partial"` — 0 < scanned < discovered, OR denied > 0, OR skipped > 0
- `"complete"` — scanned == discovered and denied == 0 and skipped == 0

**Key name:** The block MUST be emitted under the exact key `coverage`. Layer 1 of the
invariant tests keys on this exact name. `check_cluster_certificate_health` previously
emitted a `scan_coverage` key; Plan D replaced it with `coverage` under Clause A
adoption (D2, `grep -c '"scan_coverage"' src/server-mcp.py` → 0).

**Adopters as of Plan C:**
- `adaptive_namespace_investigation` — `adaptive_metadata.coverage` (pods)

**Adopters added in Plan D:**
- `check_cluster_certificate_health` — alongside F-06 fix (namespaces); replaces `scan_coverage`
- `investigate_tls_certificate_issues` — alongside F-23(i) fix (namespaces)

**Plan D adopter obligations (completed in D4):**

0. **Renamed `critical_events_found` → `high_or_critical_events_found` (naming debt from C4, done D4).**
   The field in `adaptive_namespace_investigation`'s `investigation_summary` counted HIGH **and**
   CRITICAL events under a name that said only "critical". It was not untruthful — the
   same response carries `namespace_events.summary.high_severity_events` and
   `severity_breakdown` so a consumer could reconcile the counts — and the sibling
   `summary.critical_events` (CRITICAL-only) is arithmetically correct and was
   deliberately left alone. But shipped side by side the two read as contradictory at a
   glance, and the parity freeze meant the field shipped with NO schema-level
   documentation. D4's single parity regen renamed it and documented it in the docstring.

1. **ValueError safety.** `build_coverage` raises `ValueError` for negative counts or
   `scanned > discovered`. The current sole call site (`server-mcp.py`, inside
   `adaptive_namespace_investigation`) sits inside a `try: … except Exception` block that
   discards the entire result and returns only an error string. The call is safe by
   construction today (`scanned = pods_analyzed` increments only over
   `prioritized_pods[:min(max_pods, total_pods)]`, so `scanned <= discovered` always
   holds). Plan D adopters MUST either (a) guarantee the same invariant by construction,
   or (b) wrap the `build_coverage(...)` call so that a programming error in the count
   arguments raises visibly rather than discarding an otherwise-complete investigation.

2. **`requested_mode` is un-enforced at construction.** It is passed as `**extra` and
   validated only in the per-adopter semantic case in `ADOPTER_GOLDEN_TABLE`. A Plan D
   adopter that forgets `requested_mode` will fail only its semantic test, not a
   `TypeError`. This is why every new adopter MUST have a complete entry in
   `ADOPTER_GOLDEN_TABLE` before the commit lands.

3. **Layer 3 does not detect module-scope calls.** `build_coverage(` calls that are not
   inside any `def`/`async def` are silently ignored by the drift guard. This is a
   pre-existing known limitation; all current and planned call sites are inside async def
   tool handlers, so the gap is theoretical.

4. **Adopter handlers must be top-level async def tools.** The drift guard attributes
   each call to the OUTERMOST enclosing function. A registered adopter defined inside a
   wrapper or factory (e.g. `def _make_tool(): async def real_tool(): build_coverage(...)`)
   would be attributed to `_make_tool`, not `real_tool`, and reported as
   `UNREGISTERED:_make_tool`. This fails loudly rather than silently, so it is safe —
   but a future factory/plugin-registration refactor of a tool handler would trip the
   guard for a non-reason and require an `ADOPTERS` update to match the new outer name.

## Clause B — Evidence for a claim

A tool must not assert a population-level verdict ("0 certificates, healthy") when
RBAC denied it access to a meaningful share of the population. The specific defects
are F-06 and F-23(i); fixes land in Plan D.

## Clause C — No rule fired ≠ normal

Recommendation generators that fire when no category or severity rule matched must
not substitute a false normality assertion. When HIGH or CRITICAL events are present
but no known rule applies, the generator must instead emit an explicit signal:

```
UNCHARACTERISED: N high-or-critical severity event(s) matched no known remediation rule
```

This is observable and actionable: the caller knows the events exist and knows that
the system has no specific remediation playbook for them. A false "continue monitoring"
is actively misleading in triage.

**Affected generators (Plan C, C4):**
- `ProgressiveEventAnalyzer._generate_detailed_recommendations` (event_analysis.py)
- `generate_string_events_recommendations` (event_analysis.py)
- `generate_strategic_recommendations` (event_analysis.py)

## Clause D — Two legitimate "we don't know" categories (Plan D controller ruling)

The campaign identified two distinct categories of unknown output, and they MUST NOT be
unified. Conflating them produces output that is either misleading or indistinguishable
from a missing field.

### Category (a): A MISSING VALUE → `None` + a sibling explaining why

When a number exists in principle but nobody computed it (e.g. a metric that requires
a data source that was unavailable), emit `None` for the field AND a sibling key that
explains the absence. Examples:

- F-05 pattern: `"accuracy": None, "note": "No log data available for analysis"` (actual shipped string, `src/server-mcp.py:11586`)
- F-28 pattern: `"cpu_usage": None, "data_source": "unavailable"`

The sibling key name diverges across adopters (`note` vs `data_source`). That divergence
is BACKLOG — do not rename shipped fields to unify them.
The `get_resource_metrics` category-(a) shape (`status: None, data_source: "unavailable"`) is guarded by F-28 mutation pin `test_get_resource_metrics_no_placeholder_status` in `tests/test_placeholder_metrics.py`.

### Category (b): A VERDICT THAT CANNOT BE REACHED → a NAMED ENUM MEMBER, never `None`

When an operator reads a health or verdict field, `None` is indistinguishable from "field
absent" and is less informative than an explicit named outcome. When the verdict cannot be
computed (e.g. zero operators to assess, zero items to scan), emit a named enum member:

- F-27 pattern: `"overall_health": "undetermined"` (zero ClusterOperators)
- Coverage contract: `"verdict": "none"` (discovered == 0 or scanned == 0)

`None` is forbidden in these positions. Use the named member even when the result is
"we cannot say" — that IS a verdict.

## Invariant tests

`tests/test_honesty_contract.py` enforces four invariants after every commit:
1. **Structural:** every JSON key named `coverage` in every golden is a dict.
2. **Semantic:** per named adopter in `ADOPTER_GOLDEN_TABLE`, the mandatory scalar keys are present and typed correctly; the table must stay in sync with `ADOPTERS`.
3. **Registry drift:** the set of `build_coverage(` call sites in `src/` equals the adopter table.
4. **Closure attribution:** `build_coverage(` calls inside closures of a registered adopter are attributed to the adopter, not the inner def (guards against bypass via factory patterns).
