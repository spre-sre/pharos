"""Fakes for module-level k8s client globals in server-mcp."""
import datetime
from types import SimpleNamespace


class _NS(SimpleNamespace):
    """SimpleNamespace that supports to_dict() so extract_resource_info (which
    expects plain dicts) can process our fake objects without modification."""

    def to_dict(self):
        def _deconv(v):
            if isinstance(v, _NS):
                return v.to_dict()
            if isinstance(v, list):
                return [_deconv(x) for x in v]
            return v
        return {k: _deconv(v) for k, v in vars(self).items()}


def obj(**kwargs):
    """Recursive _NS: obj(metadata=dict(name='x')) -> .metadata.name"""
    def conv(v):
        if isinstance(v, dict):
            return _NS(**{k: conv(x) for k, x in v.items()})
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return conv(kwargs)


def items_list(items):
    """Return a fake list-response with .items and .metadata._continue=None.

    The pagination loop in _get_namespace_events_internal reads
    event_list_response.metadata._continue to decide whether to fetch
    more pages; providing it here prevents AttributeError and stops
    pagination after the first (and only) page.
    """
    return SimpleNamespace(
        items=list(items),
        metadata=SimpleNamespace(_continue=None),
    )


class FakeApi:
    """FakeApi(list_namespace=items_list([...])) -> .list_namespace(**any) returns it.

    Pass an Exception instance to raise it. Pass a callable to delegate.
    Any un-configured method raises AttributeError -> surfaces exactly which
    API surface a tool touches (extend the case's fake, never guess).
    """
    def __init__(self, **methods):
        self._methods = methods

    def __getattr__(self, name):
        if name not in self._methods:
            raise AttributeError(
                f"FakeApi has no canned method {name!r} - add it to the case"
            )
        result = self._methods[name]
        if callable(result) and not isinstance(result, Exception):
            return result
        def _call(*args, **kwargs):
            if isinstance(result, Exception):
                raise result
            return result
        return _call


def NS(name, phase="Active"):
    ns = obj(
        metadata=dict(name=name, labels={}, creation_timestamp="2026-07-01T00:00:00Z"),
        status=dict(phase=phase),
    )
    ns.metadata.labels = {}  # keep as plain dict (obj() would convert to _NS)
    return ns


def POD(name, ns, phase="Running", restarts=0, ready=True,
        cpu_req="100m", mem_req="128Mi"):
    pod = obj(
        metadata=dict(name=name, namespace=ns, labels={"app": name},
                      creation_timestamp=datetime.datetime(2026, 7, 1, 0, 0, 0),
                      owner_references=[]),
        spec=dict(node_name="node-1", containers=[dict(
            name="main",
            resources=dict(requests={"cpu": cpu_req, "memory": mem_req},
                           limits={"cpu": "500m", "memory": "512Mi"}),
        )]),
        status=dict(
            phase=phase,
            pod_ip="10.0.0.1",
            start_time=datetime.datetime(2026, 7, 1, 0, 0, 0),
            init_container_statuses=None,
            container_statuses=[dict(
                name="main", restart_count=restarts, ready=ready,
                state=dict(running=dict(started_at="2026-07-01T00:00:00Z"),
                           waiting=None, terminated=None),
                last_state=dict(running=None, waiting=None, terminated=None),
            )],
            conditions=[dict(type="Ready", status="True" if ready else "False",
                             reason=None, message=None)],
        ),
    )
    pod.metadata.labels = {"app": name}  # keep as plain dict (obj() would convert to _NS)
    return pod


def POD2C(name, ns):
    """Two-container pod: exercises get_pod_logs' multi-container join."""
    p = POD(name, ns)
    second = dict(name="sidecar",
                  resources=dict(requests={"cpu": "50m", "memory": "64Mi"},
                                 limits={"cpu": "100m", "memory": "128Mi"}))
    p.spec.containers = [p.spec.containers[0], obj(**second)]
    return p


def PIPELINERUN(name, ns, succeeded=True):
    status = "True" if succeeded else "False"
    reason = "Succeeded" if succeeded else "Failed"
    return {
        "apiVersion": "tekton.dev/v1", "kind": "PipelineRun",
        "metadata": {
            "name": name, "namespace": ns,
            "labels": {"tekton.dev/pipeline": "build",
                       "pipelines.appstudio.openshift.io/type": "build"},
            "annotations": {},
            "creationTimestamp": "2026-07-20T09:00:00Z",
        },
        "status": {
            "conditions": [{"type": "Succeeded", "status": status,
                            "reason": reason,
                            "message": f"Tasks Completed: 3 ({reason})"}],
            "startTime": "2026-07-20T09:00:05Z",
            "completionTime": "2026-07-20T09:10:05Z",
            "childReferences": [{"name": f"{name}-build", "kind": "TaskRun"}],
        },
    }


def TASKRUN(name, ns, plr, succeeded=True):
    status = "True" if succeeded else "False"
    # tekton.dev/pipelineTask supplies a stable task name that list_taskruns
    # can extract via step 2 (label lookup) without relying on name-suffix
    # parsing (step 3), which requires a random suffix not present in our
    # controlled fake names.
    return {
        "apiVersion": "tekton.dev/v1", "kind": "TaskRun",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {"tekton.dev/pipelineRun": plr,
                                "tekton.dev/pipelineTask": "build"},
                     "creationTimestamp": "2026-07-20T09:00:06Z"},
        "status": {
            "conditions": [{"type": "Succeeded", "status": status,
                            "reason": "Succeeded" if succeeded else "Failed"}],
            "podName": f"{name}-pod",
            "startTime": "2026-07-20T09:00:07Z",
            "completionTime": "2026-07-20T09:04:00Z",
            "steps": [{"name": "step-build",
                       "terminated": {"exitCode": 0 if succeeded else 1,
                                      "reason": "Completed" if succeeded
                                      else "Error"}}],
        },
    }


def EVENT(reason, message, ns, kind="Pod", name="p", type_="Warning", count=1):
    """Build a fake Kubernetes Event object.

    Timestamps are set to 2-3 hours ago so they fall inside the 6-hour
    time window used by smart_get_namespace_events (which is the adaptive
    default when volume estimation returns 0 events for the 10-min sample
    window).  The exact values are normalised to <TS> by golden_utils so
    the golden is stable across runs.
    """
    # Strip microseconds so the formatted string "2026-07-22 09:30:05" is fully
    # covered by ISO_RE (\d{4}-...\d{2}:\d{2}:\d{2}) when normalize() runs.
    # Without this, the golden would contain unstable ".763146" suffixes.
    _now = datetime.datetime.now().replace(microsecond=0)
    return obj(
        metadata=dict(name=f"{name}.{reason.lower()}", namespace=ns),
        reason=reason, message=message, type=type_, count=count,
        involved_object=dict(kind=kind, name=name, namespace=ns),
        first_timestamp=_now - datetime.timedelta(hours=3),
        last_timestamp=_now - datetime.timedelta(hours=2),
        event_time=None,
        source=dict(component="kubelet"),
    )
