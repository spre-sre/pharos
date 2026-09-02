"""Task 2a seam tests — direct-globals generic tools.

Covers: advanced_event_analytics, automated_triage_rca_report_generator,
        semantic_log_search.

Each test proves that source is threaded through to the correct helper seam.
Before conversion the tools lead with _gate_source, which rejects named
kubernetes sources with a phase-3 error and never reaches the seam.  After
conversion the seam is invoked and source is visible there.
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

_FAKE_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster: {server: "https://127.0.0.1:1"}
  name: fake
contexts:
- context: {cluster: fake, user: fake}
  name: fake
current-context: fake
users:
- name: fake
  user: {token: "fake-token"}
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Import server-mcp.py once with a fake kubeconfig (module-scoped)."""
    kubeconfig = tmp_path_factory.mktemp("kube_2a") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    _orig = {
        "KUBECONFIG": os.environ.get("KUBECONFIG"),
        "KUBEARCHIVE_ENABLED": os.environ.get("KUBEARCHIVE_ENABLED"),
        "LUMINO_DISABLE_TELEMETRY": os.environ.get("LUMINO_DISABLE_TELEMETRY"),
        "LUMINO_CONFIG": os.environ.get("LUMINO_CONFIG"),
        "LUMINO_PROFILE": os.environ.get("LUMINO_PROFILE"),
    }
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    _orig_kube_loc = None
    try:
        from kubernetes.config import kube_config as _k8s_kube_config
        _orig_kube_loc = _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION
        _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = str(kubeconfig)
    except Exception:
        pass

    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location(
        "server_mcp_2a", SRC / "server-mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_2a"] = mod
    spec.loader.exec_module(mod)

    yield mod

    for key, orig in _orig.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    if _orig_kube_loc is not None:
        try:
            from kubernetes.config import kube_config as _k8s_kube_config
            _k8s_kube_config.KUBE_CONFIG_DEFAULT_LOCATION = _orig_kube_loc
        except Exception:
            pass
    try:
        sys.path.remove(str(SRC))
    except ValueError:
        pass


def _register_fake_instance(server, name):
    """Register a named kubernetes instance and inject a sentinel K8sClientSet.

    Returns the injected K8sClientSet so callers can identity-check its fields.
    """
    from core.registry import SourceEntry, ADAPTER_CAPABILITIES

    entry = SourceEntry(
        name=name,
        adapter="kubernetes",
        capabilities=ADAPTER_CAPABILITIES["kubernetes"],
        state="configured",
        default=False,
    )
    if name not in server._source_registry._entries:
        server._source_registry.add_instance(entry)

    sentinel_core = object()
    sentinel_custom = object()
    fake_cs = server.K8sClientSet(
        core_api=sentinel_core,
        apps_api=object(),
        custom_api=sentinel_custom,
        batch_api=object(),
        storage_api=object(),
        networking_api=object(),
        autoscaling_api=object(),
        apis_api=object(),
    )
    server._k8s_instances[name] = fake_cs
    return fake_cs


# ---------------------------------------------------------------------------
# advanced_event_analytics: source threads to _progressive_event_analysis_core
# ---------------------------------------------------------------------------


def test_advanced_event_analytics_threads_source_to_core(server, monkeypatch):
    """advanced_event_analytics must pass source= to _progressive_event_analysis_core.

    RED: before conversion _gate_source returns a phase-3 error for named
    kubernetes sources; _progressive_event_analysis_core is never called so
    captured is empty and the assertion fails.

    GREEN: after conversion the full resolve-or-gate pattern is used; the core
    receives source='fake-k8s-aaa'.
    """
    _register_fake_instance(server, "fake-k8s-aaa")

    captured = {}

    async def fake_core(**kwargs):
        captured.update(kwargs)
        # Return minimal (base_result, classified_events) so the tool exits cleanly
        return {}, []

    monkeypatch.setattr(server, "_progressive_event_analysis_core", fake_core)

    asyncio.run(
        server.advanced_event_analytics(
            "my-namespace",
            source="fake-k8s-aaa",
            include_log_correlation=False,
            include_metrics_correlation=False,
            include_runbook_suggestions=False,
        )
    )

    assert captured.get("source") == "fake-k8s-aaa", (
        f"Expected source='fake-k8s-aaa' threaded to _progressive_event_analysis_core, "
        f"got captured={captured!r}. "
        "Tool must use resolve-or-gate pattern, not bare _gate_source."
    )


def test_advanced_event_analytics_core_gate_is_noop_for_named_instance(server, monkeypatch):
    """M3 fix: the gate passed to _progressive_event_analysis_core must be a no-op
    for resolved named instances, not _gate_source.

    Before the M3 fix the core's internal _gate_source call would reject the named
    instance with a phase-3 error (wrong tool name, wrapped shape).  After the fix,
    gate_fn is a lambda that always returns None so the core proceeds.
    """
    _register_fake_instance(server, "fake-k8s-m3")

    gate_calls = []

    async def fake_core(**kwargs):
        gate_fn = kwargs.get("gate_fn")
        if gate_fn is not None:
            # Call the gate and record whether it blocked
            result = gate_fn("progressive_event_analysis", "fake-k8s-m3", ("Event",))
            gate_calls.append(result)
        return {}, []

    monkeypatch.setattr(server, "_progressive_event_analysis_core", fake_core)

    asyncio.run(
        server.advanced_event_analytics(
            "my-namespace",
            source="fake-k8s-m3",
            include_log_correlation=False,
            include_metrics_correlation=False,
            include_runbook_suggestions=False,
        )
    )

    assert gate_calls, "fake_core was not called — source did not reach the core"
    assert all(r is None for r in gate_calls), (
        f"gate_fn must return None (no-op) for resolved named instances, "
        f"got: {gate_calls!r}.  Tool must pass a no-op gate, not bare _gate_source."
    )


# ---------------------------------------------------------------------------
# automated_triage_rca_report_generator: instance clients reach identify_failure_context
# ---------------------------------------------------------------------------


def test_automated_triage_rca_report_generator_clients_from_instance(server, monkeypatch):
    """automated_triage_rca_report_generator must pass instance clients to identify_failure_context.

    RED: before conversion _gate_source returns a phase-3 error; the helper is
    never called and captured stays empty.

    GREEN: after conversion _clients.core_api and _clients.custom_api from
    the fake instance (sentinels) are what identify_failure_context receives.
    """
    fake_cs = _register_fake_instance(server, "fake-k8s-rca")

    captured = {}

    async def fake_identify(failure_identifier, detect_fn, custom_api, core_api, logger, namespace=None):
        captured["core_api"] = core_api
        captured["custom_api"] = custom_api
        return {"found": False, "search_note": "stub", "namespaces_searched": []}

    monkeypatch.setattr(server, "identify_failure_context", fake_identify)

    asyncio.run(
        server.automated_triage_rca_report_generator(
            "fake-pipelinerun",
            source="fake-k8s-rca",
        )
    )

    assert captured.get("core_api") is fake_cs.core_api, (
        f"core_api passed to identify_failure_context must be the fake instance's "
        f"core_api sentinel (id={id(fake_cs.core_api):#x}), "
        f"got: {captured.get('core_api')!r}. "
        "Tool must use _clients.core_api, not the module global."
    )
    assert captured.get("custom_api") is fake_cs.custom_api, (
        f"custom_api passed to identify_failure_context must be the fake instance's "
        f"custom_api sentinel (id={id(fake_cs.custom_api):#x}), "
        f"got: {captured.get('custom_api')!r}. "
        "Tool must use _clients.custom_api, not the module global."
    )


# ---------------------------------------------------------------------------
# semantic_log_search: source threads to list_pods_in_namespace
# ---------------------------------------------------------------------------


def test_semantic_log_search_threads_source_to_list_pods(server, monkeypatch):
    """semantic_log_search must pass source= to list_pods_in_namespace.

    RED: before conversion _gate_source returns a phase-3 error for named
    kubernetes sources; list_pods_in_namespace is never called.

    GREEN: after conversion list_pods_in_namespace receives source='fake-k8s-sls'.
    """
    _register_fake_instance(server, "fake-k8s-sls")

    captured = {}

    async def fake_list_pods(namespace, limit=200, source=""):
        captured["source"] = source
        captured["namespace"] = namespace
        return []

    monkeypatch.setattr(server, "list_pods_in_namespace", fake_list_pods)

    # Stub _get_target_namespaces so the loop executes exactly one namespace
    async def fake_get_namespaces(namespaces, identified_components, list_namespaces_fn, detect_fn):
        return ["test-ns"]

    monkeypatch.setattr(server, "_get_target_namespaces", fake_get_namespaces)

    # Stub event/tekton searches to avoid network calls
    async def fake_search_events(ns, qinterp, sparams, events_fn, relevance_fn, match_fn, meta_fn):
        return []

    monkeypatch.setattr(server, "_search_events_semantically", fake_search_events)

    asyncio.run(
        server.semantic_log_search("find errors", source="fake-k8s-sls")
    )

    assert captured.get("source") == "fake-k8s-sls", (
        f"Expected source='fake-k8s-sls' threaded to list_pods_in_namespace, "
        f"got captured={captured!r}. "
        "Tool must call list_pods_in_namespace(namespace, source=source)."
    )


# ---------------------------------------------------------------------------
# M1 fix: automated_triage_rca_report_generator internal tools receive source=
# ---------------------------------------------------------------------------


def test_automated_triage_rca_report_generator_internal_tools_receive_source(server, monkeypatch):
    """M1 fix: analyze_failed_pipeline must receive source= via functools.partial.

    RED: before the M1 fix, analyze_failed_pipeline is passed bare (no source);
    the partial is never created so captured stays empty / source="".

    GREEN: after the fix, _analyze_failed = functools.partial(analyze_failed_pipeline,
    source=source) is created and passed to analyze_pipeline_failure; when
    analyze_pipeline_failure calls it as analyze_failed_pipeline_func(ns, id_),
    the partial expands to analyze_failed_pipeline(ns, id_, source=source).
    """
    _register_fake_instance(server, "fake-k8s-m1-internal")
    captured = {}

    async def fake_identify(failure_identifier, detect_fn, custom_api, core_api, logger, namespace=None):
        return {"found": True, "type": "pipelinerun", "namespace": "test-ns"}

    async def fake_analyze_failed(namespace, pipeline_run, source=""):
        captured["source"] = source
        return {"failed_tasks": [], "pipeline_status": "Failed", "probable_root_cause": "test"}

    async def fake_analyze_pipeline_failure(ns, id_, depth, analyze_fn, perf_fn, logs_fn, al_fn, la_fn, deps_fn, lg):
        # Invoke the passed analyze_fn to verify it carries source= via partial
        await analyze_fn(ns, id_)
        return {"logs_analyzed": {}}

    monkeypatch.setattr(server, "identify_failure_context", fake_identify)
    monkeypatch.setattr(server, "analyze_failed_pipeline", fake_analyze_failed)
    monkeypatch.setattr(server, "analyze_pipeline_failure", fake_analyze_pipeline_failure)

    asyncio.run(
        server.automated_triage_rca_report_generator(
            "fake-pipelinerun",
            source="fake-k8s-m1-internal",
        )
    )

    assert captured.get("source") == "fake-k8s-m1-internal", (
        f"analyze_failed_pipeline must receive source='fake-k8s-m1-internal' via "
        f"functools.partial, got: {captured!r}.  "
        "Tool must bind source to internal tool references before passing to helpers."
    )


# ---------------------------------------------------------------------------
# M2 fix: semantic_log_search internal tools receive source=
# ---------------------------------------------------------------------------


def test_semantic_log_search_internal_tools_receive_source(server, monkeypatch):
    """M2 fix: smart_get_namespace_events must receive source= via functools.partial.

    RED: before the M2 fix, smart_get_namespace_events is passed bare; the
    partial is not created, so captured source stays "".

    GREEN: after the fix, _smart_events = functools.partial(smart_get_namespace_events,
    source=source) is passed to _search_events_semantically; when the helper
    calls get_namespace_events_func(namespace), the partial expands to
    smart_get_namespace_events(namespace, source=source).
    """
    _register_fake_instance(server, "fake-k8s-m2-internal")
    captured = {}

    async def fake_smart_events(namespace, source="", **kwargs):
        captured["source"] = source
        return {"events": []}

    async def fake_search_events_semantically(ns, qinterp, sparams, events_fn, relevance_fn, match_fn, meta_fn):
        # Invoke the passed events_fn to verify it carries source= via partial
        await events_fn(ns)
        return []

    async def fake_get_namespaces(namespaces, identified_components, list_namespaces_fn, detect_fn):
        return ["test-ns"]

    monkeypatch.setattr(server, "smart_get_namespace_events", fake_smart_events)
    monkeypatch.setattr(server, "_search_events_semantically", fake_search_events_semantically)
    monkeypatch.setattr(server, "_get_target_namespaces", fake_get_namespaces)

    async def fake_list_pods(namespace, limit=200, source=""):
        return []

    monkeypatch.setattr(server, "list_pods_in_namespace", fake_list_pods)

    asyncio.run(
        server.semantic_log_search("find errors", source="fake-k8s-m2-internal")
    )

    assert captured.get("source") == "fake-k8s-m2-internal", (
        f"smart_get_namespace_events must receive source='fake-k8s-m2-internal' via "
        f"functools.partial, got: {captured!r}.  "
        "Tool must bind source to internal tool references before passing to helpers."
    )
