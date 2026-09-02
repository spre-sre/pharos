"""
tests/test_rbac_manifest.py

Drift defence for deploy/rbac-readonly.yaml.  Four assertion groups:

  (a) Verb closure: file parses; every verbs list ⊆ {get, list, watch} per spec
      §4.7:221; and today every verbs list ⊆ {get, list} (watch deliberately
      withheld until an informer lands — reviewer-ratified deviation recorded in
      the manifest header).

  (b) Secrets separation: `secrets` appears in NO aggregate-labelled ClusterRole;
      it may only appear in pharos-readonly-secrets (cluster-wide, non-aggregated)
      and the namespace-scoped Role variant.

  (c) Living inventory: a dict maps every (apiGroup, resource) the code touches to
      (expected_verbs: frozenset[str], evidence: str).  Three assertions enforce this:
        Direction 1 — every (apiGroup, resource) in ANY ClusterRole or Role doc is
          either in INVENTORY or in SECRETS_TIER_ALLOWED.
        Direction 2 — every INVENTORY key appears in at least one aggregate-tier rule.
        Verb check — the union of verbs for each (apiGroup, resource) across ALL
          Role/ClusterRole docs exactly equals the frozenset in INVENTORY (for INVENTORY
          pairs) or in SECRETS_TIER_ALLOWED (for secrets-tier pairs).  Using all roles
          (not just aggregate) prevents a known pair from being smuggled into the
          non-aggregate secrets tier with wrong verbs.
      (nonResourceURLs are not (apiGroup, resource) pairs and are excluded.)

  (d) Naming discipline: every metadata.name starts with "pharos"; binding subjects
      live in namespace "pharos"; roleRef names start with "pharos"; the
      app.kubernetes.io/name label == "pharos".

Mutations:
  M2:  add `create` to any verbs list → (a) fails.
  M3a: add the aggregate label to pharos-readonly-secrets, or move `secrets`
       into pharos-readonly-core → (b) fails.
  M3b: delete `pods/log` from the manifest → (c) direction-2 fails
       ("inventory entries missing from manifest").
  M3c: widen any resource's verbs in the manifest without updating INVENTORY →
       (c) verb-set check fails.
  M3d: add a bogus rule (e.g. rbac clusterroles get,list) to pharos-readonly-secrets →
       (c) direction-1 fails (pair not in INVENTORY and not in SECRETS_TIER_ALLOWED).
  M3e: add a known INVENTORY pair with wrong verbs to the non-aggregate secrets tier
       (e.g. serviceaccounts get,list) → (c) verb-set check fails (all-role union
       exposes the smuggled verbs).
"""
from __future__ import annotations

import pathlib
import yaml

REPO = pathlib.Path(__file__).parent.parent
MANIFEST = REPO / "deploy" / "rbac-readonly.yaml"

# ──────────────────────────────────────────────────────────────────────────────
# Verb constants
# ──────────────────────────────────────────────────────────────────────────────

# Spec §4.7:221 boundary — the set the manifest is allowed to contain.
_SPEC_VERBS: set[str] = {"get", "list", "watch"}

# Today's grant — watch is withheld (nothing in the code opens a watch stream).
# Reviewer-ratified deviation; see manifest header for rationale.
_TODAY_VERBS: set[str] = {"get", "list"}

# ──────────────────────────────────────────────────────────────────────────────
# Aggregate-label key / value
# ──────────────────────────────────────────────────────────────────────────────
_AGG_LABEL = "rbac.authorization.k8s.io/aggregate-to-pharos-readonly"
_AGG_VALUE = "true"

# ──────────────────────────────────────────────────────────────────────────────
# Inventory shorthand verb sets
# ──────────────────────────────────────────────────────────────────────────────
_GL = frozenset({"get", "list"})  # get + list
_G  = frozenset({"get"})          # get only
_L  = frozenset({"list"})         # list only


def _load_docs() -> list[dict]:
    """Parse all YAML documents from the manifest."""
    return list(yaml.safe_load_all(MANIFEST.read_text()))


def _is_aggregate_labelled(doc: dict) -> bool:
    labels = (doc.get("metadata") or {}).get("labels") or {}
    return labels.get(_AGG_LABEL) == _AGG_VALUE


def _is_clusterrole(doc: dict) -> bool:
    return (doc or {}).get("kind") == "ClusterRole"


def _is_role(doc: dict) -> bool:
    return (doc or {}).get("kind") == "Role"


def _aggregate_clusterroles(docs: list[dict]) -> list[dict]:
    """Return the subset of ClusterRole docs that carry the aggregate label."""
    return [d for d in docs if _is_clusterrole(d) and _is_aggregate_labelled(d)]


def _all_verbs_in_doc(doc: dict) -> list[list[str]]:
    """Collect every verbs list from a single manifest document."""
    result = []
    for rule in (doc.get("rules") or []):
        v = rule.get("verbs")
        if v:
            result.append(list(v))
    return result


def _resource_pairs_from_doc(doc: dict) -> set[tuple[str, str]]:
    """
    Extract (apiGroup, resource) pairs from a ClusterRole/Role document.

    nonResourceURLs rules are excluded — they are not (apiGroup, resource) pairs
    and are handled by a separate manifest rule.
    """
    pairs: set[tuple[str, str]] = set()
    for rule in (doc.get("rules") or []):
        if "nonResourceURLs" in rule:
            continue
        groups = rule.get("apiGroups") or [""]
        resources = rule.get("resources") or []
        for g in groups:
            for r in resources:
                pairs.add((g, r))
    return pairs


def _resource_verb_map_from_docs(docs: list[dict]) -> dict[tuple[str, str], frozenset[str]]:
    """
    Build a map from (apiGroup, resource) to the union of granted verbs across all
    rules in the given list of ClusterRole/Role documents.

    Used to assert verb-set equality between the manifest and INVENTORY.
    """
    acc: dict[tuple[str, str], set[str]] = {}
    for doc in docs:
        for rule in (doc.get("rules") or []):
            if "nonResourceURLs" in rule:
                continue
            groups = rule.get("apiGroups") or [""]
            resources = rule.get("resources") or []
            verbs = set(rule.get("verbs") or [])
            for g in groups:
                for r in resources:
                    acc.setdefault((g, r), set()).update(verbs)
    return {k: frozenset(v) for k, v in acc.items()}


# ──────────────────────────────────────────────────────────────────────────────
# (b) SECRETS EXEMPTION
#
# Pairs that may appear in non-aggregate Role/ClusterRole documents without a
# corresponding INVENTORY entry, mapped to their EXPECTED verb set.
# Currently only secrets — the cluster-wide pharos-readonly-secrets ClusterRole
# (get,list) and the namespace-scoped Role (get with resourceNames) are both
# deliberately outside the aggregate contract.
#
# Carrying the expected verb set here (not just the pair) lets the verb-equality
# check span ALL roles: any known INVENTORY pair smuggled into the secrets tier
# with wrong verbs is caught, and the secrets grant itself is pinned.
# ──────────────────────────────────────────────────────────────────────────────

SECRETS_TIER_ALLOWED: dict[tuple[str, str], frozenset[str]] = {
    # ClusterRole grants get+list; Role adds resourceNames restriction but same verbs
    # union: {"get","list"}
    ("", "secrets"): frozenset({"get", "list"}),
}


# ──────────────────────────────────────────────────────────────────────────────
# (c) LIVING INVENTORY
#
# Every (apiGroup, resource) pair maps to:
#   (expected_verbs: frozenset[str], evidence: str)
#
# expected_verbs is the exact set granted in the aggregate tiers.  evidence is a
# file:line proof that the code actually touches that API.  Three assertions:
#   Direction 1: every pair in ANY ClusterRole/Role doc is either in INVENTORY
#     or in SECRETS_TIER_ALLOWED.
#   Direction 2: every INVENTORY key appears in at least one aggregate-tier rule.
#   Verb check: the union of verbs for each pair across aggregate tiers == frozenset.
#
# Secrets are excluded: they are in SECRETS_TIER_ALLOWED, never in INVENTORY.
# nonResourceURLs pairs are excluded from all three assertions.
# ──────────────────────────────────────────────────────────────────────────────

INVENTORY: dict[tuple[str, str], tuple[frozenset[str], str]] = {
    # ── Core group ──────────────────────────────────────────────────────────
    # pods: list_namespaced_pod for pod enumeration
    ("", "pods"): (_GL, "src/server-mcp.py:1978"),
    # pods/log: read_namespaced_pod_log inside get_all_pod_logs
    ("", "pods/log"): (_G, "src/helpers/utils.py:580"),
    # events: list_namespaced_event for smart_get_namespace_events
    ("", "events"): (_GL, "src/server-mcp.py:3018"),
    # namespaces: list_namespace in list_namespaces
    ("", "namespaces"): (_GL, "src/server-mcp.py:1537"),
    # nodes: list_node in check_resource_constraints and topology
    ("", "nodes"): (_GL, "src/server-mcp.py:10020"),
    # services: read/list_namespaced_service for Prometheus discovery
    ("", "services"): (_GL, "src/server-mcp.py:5306"),
    # endpoints: read_namespaced_endpoints in get_kubernetes_resource
    ("", "endpoints"): (_G, "src/server-mcp.py:2174"),
    # configmaps: list_namespaced_config_map in live_system_topology_mapper
    ("", "configmaps"): (_GL, "src/server-mcp.py:10635"),
    # persistentvolumes: list_persistent_volume via get_kubernetes_resource dispatch
    ("", "persistentvolumes"): (_GL, "src/helpers/utils.py:1624"),
    # persistentvolumeclaims: list_namespaced_persistent_volume_claim in topology
    ("", "persistentvolumeclaims"): (_GL, "src/server-mcp.py:10598"),
    # resourcequotas: list_namespaced_resource_quota in get_kubernetes_resource
    ("", "resourcequotas"): (_GL, "src/server-mcp.py:2617"),
    # limitranges: limitrange dispatch entry in get_kubernetes_resource
    ("", "limitranges"): (_G, "src/server-mcp.py:2089"),
    # serviceaccounts: serviceaccount dispatch entry in get_kubernetes_resource
    ("", "serviceaccounts"): (_G, "src/server-mcp.py:2083"),

    # ── apps ────────────────────────────────────────────────────────────────
    # deployments: list_namespaced_deployment via search_resources_by_labels dispatch
    ("apps", "deployments"): (_GL, "src/helpers/utils.py:1629"),
    # replicasets: list_namespaced_replica_set via search_resources_by_labels dispatch
    ("apps", "replicasets"): (_GL, "src/helpers/utils.py:1630"),
    # daemonsets: list_namespaced_daemon_set via search_resources_by_labels dispatch
    ("apps", "daemonsets"): (_GL, "src/helpers/utils.py:1631"),
    # statefulsets: list_namespaced_stateful_set via search_resources_by_labels dispatch
    ("apps", "statefulsets"): (_GL, "src/helpers/utils.py:1632"),

    # ── batch ────────────────────────────────────────────────────────────────
    # jobs: list_namespaced_job via search_resources_by_labels dispatch
    ("batch", "jobs"): (_GL, "src/helpers/utils.py:1635"),
    # cronjobs: list_namespaced_cron_job via search_resources_by_labels dispatch
    ("batch", "cronjobs"): (_GL, "src/helpers/utils.py:1636"),

    # ── storage.k8s.io ──────────────────────────────────────────────────────
    # storageclasses: read_storage_class by name in get_kubernetes_resource
    ("storage.k8s.io", "storageclasses"): (_G, "src/server-mcp.py:2109"),

    # ── autoscaling ──────────────────────────────────────────────────────────
    # horizontalpodautoscalers: hpa dispatch in get_kubernetes_resource
    ("autoscaling", "horizontalpodautoscalers"): (_G, "src/server-mcp.py:2114"),

    # ── networking.k8s.io ───────────────────────────────────────────────────
    # ingresses: get_namespaced_custom_object by name in get_kubernetes_resource
    ("networking.k8s.io", "ingresses"): (_G, "src/server-mcp.py:2222"),

    # ── admissionregistration.k8s.io ────────────────────────────────────────
    # validatingwebhookconfigurations: validatingadmissionwebhook dispatch entry
    ("admissionregistration.k8s.io", "validatingwebhookconfigurations"): (_G, "src/server-mcp.py:2140"),
    # mutatingwebhookconfigurations: mutatingadmissionwebhook dispatch entry
    ("admissionregistration.k8s.io", "mutatingwebhookconfigurations"): (_G, "src/server-mcp.py:2141"),

    # ── tekton.dev ───────────────────────────────────────────────────────────
    # pipelineruns: list_cluster_custom_object group=tekton.dev plural=pipelineruns
    ("tekton.dev", "pipelineruns"): (_GL, "src/server-mcp.py:3929"),
    # taskruns: list_namespaced_custom_object group=tekton.dev plural=taskruns
    ("tekton.dev", "taskruns"): (_GL, "src/server-mcp.py:4239"),
    # pipelines: list_namespaced_custom_object group=tekton.dev plural=pipelines
    ("tekton.dev", "pipelines"): (_GL, "src/server-mcp.py:10762"),
    # tasks: list_namespaced_custom_object group=tekton.dev plural=tasks
    ("tekton.dev", "tasks"): (_GL, "src/server-mcp.py:10870"),
    # clustertasks: clustertask dispatch entry in get_kubernetes_resource (deprecated, v1beta1)
    ("tekton.dev", "clustertasks"): (_G, "src/server-mcp.py:2123"),

    # ── triggers.tekton.dev ──────────────────────────────────────────────────
    # triggerbindings: search_resources_by_labels dispatch (list) + get-by-name dispatch
    ("triggers.tekton.dev", "triggerbindings"): (_GL, "src/helpers/utils.py:1654"),
    # triggertemplates: search_resources_by_labels dispatch (list) + get-by-name dispatch
    ("triggers.tekton.dev", "triggertemplates"): (_GL, "src/helpers/utils.py:1655"),
    # eventlisteners: eventlistener dispatch in get_kubernetes_resource + list dispatch
    ("triggers.tekton.dev", "eventlisteners"): (_GL, "src/server-mcp.py:2129"),
    # triggers: search_resources_by_labels dispatch map entry (list only; no get-by-name)
    ("triggers.tekton.dev", "triggers"): (_L, "src/helpers/utils.py:1653"),

    # ── pipelinesascode.tekton.dev ───────────────────────────────────────────
    # repositories: list_cluster_custom_object group=pipelinesascode.tekton.dev (list only)
    ("pipelinesascode.tekton.dev", "repositories"): (_L, "src/server-mcp.py:4262"),

    # ── route.openshift.io ───────────────────────────────────────────────────
    # routes: list_namespaced_custom_object group=route.openshift.io in prometheus discovery
    ("route.openshift.io", "routes"): (_GL, "src/server-mcp.py:5162"),

    # ── config.openshift.io ──────────────────────────────────────────────────
    # clusteroperators: list_cluster_custom_object group=config.openshift.io (list only)
    ("config.openshift.io", "clusteroperators"): (_L, "src/server-mcp.py:10172"),
    # clusterversions: list_cluster_custom_object group=config.openshift.io (list only)
    ("config.openshift.io", "clusterversions"): (_L, "src/server-mcp.py:10196"),

    # ── machineconfiguration.openshift.io ────────────────────────────────────
    # machineconfigpools: list_cluster_custom_object plural=machineconfigpools (list only)
    ("machineconfiguration.openshift.io", "machineconfigpools"): (_L, "src/server-mcp.py:9698"),
    # machineconfigs: list_cluster_custom_object plural=machineconfigs (list only)
    ("machineconfiguration.openshift.io", "machineconfigs"): (_L, "src/server-mcp.py:9748"),

    # ── monitoring.coreos.com ────────────────────────────────────────────────
    # prometheuses: list_cluster_custom_object plural=prometheuses — list only
    #   (no get-by-name path anywhere in the tree; grep confirms single call site)
    ("monitoring.coreos.com", "prometheuses"): (_L, "src/server-mcp.py:5231"),
    # podmonitors: get_namespaced_custom_object by name only (no list call)
    ("monitoring.coreos.com", "podmonitors"): (_G, "src/server-mcp.py:2133"),
    # servicemonitors: get_namespaced_custom_object by name only (no list call)
    ("monitoring.coreos.com", "servicemonitors"): (_G, "src/server-mcp.py:2134"),
    # prometheusrules: get_namespaced_custom_object by name only (no list call)
    ("monitoring.coreos.com", "prometheusrules"): (_G, "src/server-mcp.py:2135"),
    # alertmanagers: get_namespaced_custom_object by name only (no list call)
    ("monitoring.coreos.com", "alertmanagers"): (_G, "src/server-mcp.py:2136"),

    # ── build.openshift.io ───────────────────────────────────────────────────
    # buildconfigs: search_resources_by_labels dispatch map entry (list only)
    ("build.openshift.io", "buildconfigs"): (_L, "src/helpers/utils.py:1640"),
    # builds: search_resources_by_labels dispatch map entry (list only)
    ("build.openshift.io", "builds"): (_L, "src/helpers/utils.py:1641"),

    # ── image.openshift.io ───────────────────────────────────────────────────
    # imagestreams: search_resources_by_labels dispatch map entry (list only)
    ("image.openshift.io", "imagestreams"): (_L, "src/helpers/utils.py:1642"),

    # ── apps.openshift.io ────────────────────────────────────────────────────
    # deploymentconfigs: search_resources_by_labels dispatch map entry (list only)
    ("apps.openshift.io", "deploymentconfigs"): (_L, "src/helpers/utils.py:1643"),

    # ── appstudio.redhat.com (Konflux) ───────────────────────────────────────
    # applications: get-by-name dispatch entry only (no list call in code)
    ("appstudio.redhat.com", "applications"): (_G, "src/server-mcp.py:2145"),
    # components: get-by-name dispatch + list_namespaced_custom_object in lineage.py
    ("appstudio.redhat.com", "components"): (_GL, "src/extensions/konflux/lineage.py:539"),
    # snapshots: get-by-name dispatch entry only (no list call in code)
    ("appstudio.redhat.com", "snapshots"): (_G, "src/server-mcp.py:2147"),
    # releases: get-by-name dispatch + list_namespaced_custom_object in lineage.py
    ("appstudio.redhat.com", "releases"): (_GL, "src/extensions/konflux/lineage.py:745"),
    # releaseplans: get-by-name dispatch entry only (no list call in code)
    ("appstudio.redhat.com", "releaseplans"): (_G, "src/server-mcp.py:2149"),
    # releaseplanadmissions: get-by-name dispatch entry only (no list call in code)
    ("appstudio.redhat.com", "releaseplanadmissions"): (_G, "src/server-mcp.py:2150"),
    # integrationtestscenarios: v1beta2 dispatch entry in get_kubernetes_resource
    ("appstudio.redhat.com", "integrationtestscenarios"): (_G, "src/server-mcp.py:2151"),
}


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_manifest_parses():
    """(a) The manifest file exists and YAML parses without error."""
    assert MANIFEST.exists(), f"manifest not found: {MANIFEST}"
    docs = _load_docs()
    assert len(docs) > 0, "manifest produced zero documents"


def test_verbs_subset_today():
    """(a) Every verbs list across all documents is a subset of {get, list}.

    watch is deliberately withheld today (reviewer-ratified deviation from
    spec §4.7:221).  This test pins that decision; add watch here and update
    the manifest header when an informer lands.

    Mutation M2: add `create` to any verbs list → this test fails.
    """
    docs = _load_docs()
    violations = []
    for doc in docs:
        if doc is None:
            continue
        kind = doc.get("kind", "")
        name = (doc.get("metadata") or {}).get("name", "<unnamed>")
        for rule in (doc.get("rules") or []):
            verbs = set(rule.get("verbs") or [])
            extra = verbs - _TODAY_VERBS
            if extra:
                violations.append(
                    f"{kind}/{name}: unexpected verbs {extra!r} in rule "
                    f"(resources={rule.get('resources') or rule.get('nonResourceURLs')})"
                )
    assert not violations, (
        "verbs outside {get,list} found (watch deliberately withheld; "
        "create/update/delete are forbidden):\n" + "\n".join(violations)
    )


def test_secrets_absent_from_aggregate_tiers():
    """(b) `secrets` appears in NO aggregate-labelled ClusterRole.

    It may only appear in pharos-readonly-secrets (non-aggregated) and the
    namespace-scoped Role.

    Mutation M3a: add the aggregate label to pharos-readonly-secrets, or move
    `secrets` into pharos-readonly-core → this test fails.
    """
    docs = _load_docs()
    agg_roles = _aggregate_clusterroles(docs)
    violations = []
    for doc in agg_roles:
        name = (doc.get("metadata") or {}).get("name", "<unnamed>")
        for rule in (doc.get("rules") or []):
            resources = rule.get("resources") or []
            if "secrets" in resources:
                violations.append(f"{name}: secrets must not appear in aggregate-labelled roles")
    assert not violations, "\n".join(violations)


def test_secrets_present_in_non_aggregate_roles():
    """(b) `secrets` IS present in pharos-readonly-secrets and the ns-scoped Role."""
    docs = _load_docs()
    secrets_found_in: list[str] = []
    for doc in docs:
        if doc is None:
            continue
        kind = doc.get("kind", "")
        if kind not in ("ClusterRole", "Role"):
            continue
        name = (doc.get("metadata") or {}).get("name", "<unnamed>")
        for rule in (doc.get("rules") or []):
            if "secrets" in (rule.get("resources") or []):
                secrets_found_in.append(f"{kind}/{name}")
                break
    # Must be present in at least one non-aggregate document
    assert secrets_found_in, "secrets not found in any ClusterRole or Role"
    # None of those documents should be aggregate-labelled
    agg_roles = {
        (doc.get("metadata") or {}).get("name", "")
        for doc in docs
        if _is_clusterrole(doc) and _is_aggregate_labelled(doc)
    }
    for loc in secrets_found_in:
        _, role_name = loc.split("/", 1)
        assert role_name not in agg_roles, (
            f"{loc} is aggregate-labelled but contains secrets"
        )


def test_living_inventory_coverage():
    """(c) Full drift coverage: key equality (both directions) + verb-set equality.

    Direction 1 — no manifest rule lacking inventory evidence:
      every (apiGroup, resource) in ANY ClusterRole or Role doc is either in
      INVENTORY or in SECRETS_TIER_ALLOWED.

    Direction 2 — no inventory entry missing from the manifest:
      every INVENTORY key appears in at least one aggregate-tier rule.

    Verb check — no verb-set drift:
      the union of verbs for each (apiGroup, resource) across the aggregate tiers
      exactly equals the frozenset in INVENTORY.

    Mutation M3b: delete `pods/log` from the manifest → direction-2 fails.
    Mutation M3c: widen storageclasses get→get,list without updating INVENTORY →
      verb-set check fails.
    Mutation M3d: add bogus rbac rule to pharos-readonly-secrets → direction-1
      fails (pair not in INVENTORY and not in SECRETS_TIER_ALLOWED).
    Mutation M3e: add serviceaccounts get,list to pharos-readonly-secrets →
      verb-set check fails (all-role union exposes the smuggled verbs).
    """
    docs = _load_docs()
    agg_roles = _aggregate_clusterroles(docs)
    all_roles = [d for d in docs if d and d.get("kind") in ("ClusterRole", "Role")]

    # Build verb maps: aggregate tiers only (directions 2) and all roles (verb check)
    agg_verb_map = _resource_verb_map_from_docs(agg_roles)
    all_verb_map = _resource_verb_map_from_docs(all_roles)
    manifest_agg_pairs = set(agg_verb_map.keys())

    # Collect all pairs from ALL ClusterRole/Role docs (extended direction 1)
    all_pairs: set[tuple[str, str]] = set()
    for doc in all_roles:
        all_pairs |= _resource_pairs_from_doc(doc)

    inventory_pairs = set(INVENTORY.keys())

    # Direction 1 (extended to all roles): pair not in INVENTORY → must be in SECRETS_TIER_ALLOWED
    uncovered = (all_pairs - inventory_pairs) - set(SECRETS_TIER_ALLOWED.keys())
    assert not uncovered, (
        "manifest rules lack inventory evidence — add entries to INVENTORY "
        "or to SECRETS_TIER_ALLOWED:\n"
        + "\n".join(f"  {g!r}, {r!r}" for g, r in sorted(uncovered))
    )

    # Direction 2: every INVENTORY key must appear in at least one aggregate-tier rule
    only_in_inventory = inventory_pairs - manifest_agg_pairs
    assert not only_in_inventory, (
        "inventory entries missing from manifest — add rules to the appropriate tier:\n"
        + "\n".join(
            f"  ({g!r}, {r!r}) → evidence: {INVENTORY[(g, r)][1]}"
            for g, r in sorted(only_in_inventory)
        )
    )

    # Verb-set equality across ALL roles: catches verbs smuggled into non-aggregate tiers.
    # INVENTORY pairs must match their frozenset; SECRETS_TIER_ALLOWED pairs must match
    # the expected verbs declared in that dict (not just be present in the manifest).
    violations = []
    for (g, r), (expected_verbs, evidence) in INVENTORY.items():
        actual_verbs = all_verb_map.get((g, r), frozenset())
        if actual_verbs != expected_verbs:
            violations.append(
                f"  ({g!r}, {r!r}): manifest grants {sorted(actual_verbs)!r} "
                f"but inventory expects {sorted(expected_verbs)!r}  [{evidence}]"
            )
    for (g, r), expected_verbs in SECRETS_TIER_ALLOWED.items():
        actual_verbs = all_verb_map.get((g, r), frozenset())
        if actual_verbs != expected_verbs:
            violations.append(
                f"  ({g!r}, {r!r}): secrets-tier grants {sorted(actual_verbs)!r} "
                f"but SECRETS_TIER_ALLOWED expects {sorted(expected_verbs)!r}"
            )
    assert not violations, (
        "verb-set mismatch — update INVENTORY/SECRETS_TIER_ALLOWED or the manifest (not both):\n"
        + "\n".join(violations)
    )


def test_pharos_naming():
    """(d) Naming discipline for the Pharos identity.

    Checks:
    - metadata.name starts with 'pharos' (prefix check, not substring)
    - metadata.namespace contains 'pharos' (kubearchive exempted as target ns)
    - app.kubernetes.io/name label == 'pharos' when present
    - RoleBinding/ClusterRoleBinding roleRef.name starts with 'pharos'
    - RoleBinding/ClusterRoleBinding subjects[].namespace == 'pharos'

    Identity tripwire: a stray 'lumino-mcp-reader' or similar name makes this fail.
    """
    docs = _load_docs()
    violations = []
    for doc in docs:
        if doc is None:
            continue
        kind = doc.get("kind", "")
        meta = doc.get("metadata") or {}
        name = meta.get("name")
        ns = meta.get("namespace")
        labels = meta.get("labels") or {}

        # name must start with "pharos" (prefix — stronger than substring)
        if name and not name.startswith("pharos"):
            violations.append(
                f"{kind}: metadata.name={name!r} does not start with 'pharos'"
            )

        # namespace must contain "pharos" (kubearchive is the legitimate target ns)
        if ns and "pharos" not in ns and ns != "kubearchive":
            violations.append(
                f"{kind}: metadata.namespace={ns!r} lacks 'pharos'"
            )

        # app.kubernetes.io/name label value must equal "pharos" when present
        app_name = labels.get("app.kubernetes.io/name")
        if app_name is not None and app_name != "pharos":
            violations.append(
                f"{kind}/{name}: app.kubernetes.io/name={app_name!r} must equal 'pharos'"
            )

        # RoleBinding/ClusterRoleBinding structural identity checks
        if kind in ("RoleBinding", "ClusterRoleBinding"):
            role_ref = doc.get("roleRef") or {}
            ref_name = role_ref.get("name", "")
            if ref_name and not ref_name.startswith("pharos"):
                violations.append(
                    f"{kind}/{name}: roleRef.name={ref_name!r} does not start with 'pharos'"
                )
            for subj in (doc.get("subjects") or []):
                subj_ns = subj.get("namespace")
                if subj_ns and subj_ns != "pharos":
                    violations.append(
                        f"{kind}/{name}: subject {subj.get('name')!r} has "
                        f"namespace={subj_ns!r}, expected 'pharos'"
                    )

    assert not violations, "naming violations:\n" + "\n".join(violations)


def test_umbrella_uses_aggregation_rule():
    """Structural: pharos-readonly must have an aggregationRule selecting the label."""
    docs = _load_docs()
    umbrella = next(
        (d for d in docs if d and d.get("kind") == "ClusterRole"
         and (d.get("metadata") or {}).get("name") == "pharos-readonly"),
        None,
    )
    assert umbrella is not None, "pharos-readonly ClusterRole not found"
    agg = umbrella.get("aggregationRule") or {}
    selectors = agg.get("clusterRoleSelectors") or []
    assert any(
        s.get("matchLabels", {}).get(_AGG_LABEL) == _AGG_VALUE
        for s in selectors
    ), "pharos-readonly aggregationRule does not select the aggregate label"


def test_clusterrolebinding_binds_umbrella_to_pharos_sa():
    """Structural: the ClusterRoleBinding must bind pharos-readonly to the pharos SA."""
    docs = _load_docs()
    crb = next(
        (d for d in docs if d and d.get("kind") == "ClusterRoleBinding"
         and (d.get("metadata") or {}).get("name") == "pharos-readonly"),
        None,
    )
    assert crb is not None, "pharos-readonly ClusterRoleBinding not found"
    ref = crb.get("roleRef") or {}
    assert ref.get("name") == "pharos-readonly", "ClusterRoleBinding roleRef.name != pharos-readonly"
    subjects = crb.get("subjects") or []
    assert any(
        s.get("kind") == "ServiceAccount"
        and s.get("name") == "pharos"
        and s.get("namespace") == "pharos"
        for s in subjects
    ), "ClusterRoleBinding must bind to ServiceAccount pharos in namespace pharos"
