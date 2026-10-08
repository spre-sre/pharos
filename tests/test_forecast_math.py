"""resource_bottleneck_forecaster maths and health verdicts.

C11: cluster health tested ">80" before ">90", so it could never be
     "critical"; a failed Prometheus query left usage at 0 % and read
     "healthy" with an invented runway.
C14: node trends were fitted on sample index with each kept point taken as
     5 minutes, although prometheus_query downsamples to 50 points (growth
     overstated ~40x on 7 days); the newest sample was dropped; the query
     window was local time labelled as UTC.
H14: namespace CPU was multiplied by 100 and shown as "cores", summed the
     pod-level cgroup series twice, and interpolated the namespace into
     PromQL unescaped.
"""

import importlib.util
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers import resource_forecasting as rf  # noqa: E402
from helpers.prometheus import _format_as_json  # noqa: E402

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


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class _FakeCore:
    def list_node(self, **kwargs):
        node = SimpleNamespace(status=SimpleNamespace(capacity={"cpu": "4", "memory": "16Gi"}))
        return SimpleNamespace(items=[node])


def _instant(value):
    return {"status": "success",
            "data": [{"metric": {}, "value": value, "timestamp": "1753180800"}]}


def _capacity_query_fn(cpu, memory):
    """cpu/memory: a value string, an Exception to raise, or a dict to return."""
    async def query_fn(query, **kwargs):
        answer = cpu if "node_cpu" in query else memory
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict):
            return answer
        return _instant(answer)
    return query_fn


# ── C11: cluster health and runway ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("cpu,memory,expected", [
    ("97.0", "50.0", "critical"),
    ("50.0", "91.0", "critical"),
    ("85.0", "50.0", "degraded"),
    ("50.0", "60.0", "healthy"),
])
async def test_cluster_health_thresholds(cpu, memory, expected):
    out = await rf._analyze_cluster_capacity_new(_FakeCore(), _Log(), query_fn=_capacity_query_fn(cpu, memory))
    assert out["overall_health"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    RuntimeError("connection refused"),
    {"status": "error", "error": "Prometheus unreachable"},
    {"status": "success", "data": []},
])
async def test_failed_usage_query_is_unknown_not_healthy(failure):
    out = await rf._analyze_cluster_capacity_new(_FakeCore(), _Log(), query_fn=_capacity_query_fn(failure, failure))
    assert out["overall_health"] == "unknown"
    assert out["current_cpu_usage"] is None
    assert out["current_memory_usage"] is None
    assert out["data_source"] == {"nodes": "kubernetes", "cpu": "unavailable", "memory": "unavailable"}
    assert out["capacity_runway"]["cpu_runway_days"] is None
    assert out["capacity_runway"]["memory_runway_days"] is None


@pytest.mark.asyncio
async def test_one_failed_query_does_not_hide_a_critical_one():
    out = await rf._analyze_cluster_capacity_new(
        _FakeCore(), _Log(), query_fn=_capacity_query_fn("95.0", RuntimeError("down")))
    assert out["overall_health"] == "critical"
    assert out["current_cpu_usage"] == "95.0%"
    assert out["current_memory_usage"] is None
    assert out["data_source"] == {"nodes": "kubernetes", "cpu": "prometheus", "memory": "unavailable"}


@pytest.mark.asyncio
async def test_one_failed_query_with_low_other_is_unknown():
    out = await rf._analyze_cluster_capacity_new(
        _FakeCore(), _Log(), query_fn=_capacity_query_fn("40.0", RuntimeError("down")))
    assert out["overall_health"] == "unknown"


@pytest.mark.asyncio
async def test_runway_is_not_invented_from_a_single_reading():
    """(90-u)/(u/30) assumed linear growth from 0 % over 30 days."""
    out = await rf._analyze_cluster_capacity_new(_FakeCore(), _Log(), query_fn=_capacity_query_fn("45.0", "45.0"))
    runway = out["capacity_runway"]
    assert runway["cpu_runway_days"] is None
    assert runway["memory_runway_days"] is None
    assert "note" in runway


# ── C14: node trend maths ────────────────────────────────────────────────────

_STEP = 300                 # Prometheus step the forecaster requests
_SAMPLES = 7 * 24 * 12      # 7 days of 5-minute samples
_BASE = 10.0                # percent at the oldest sample
_SLOPE_PER_5MIN = 0.02      # true growth, percent per 5 minutes (newest: 50.3 %)


def _series(now_ts):
    start = now_ts - (_SAMPLES - 1) * _STEP
    return [[start + i * _STEP, f"{_BASE + _SLOPE_PER_5MIN * i:.4f}"] for i in range(_SAMPLES)]


def _node_query_fn(seen):
    async def query_fn(query, **kwargs):
        seen.append(kwargs)
        if "node_cpu" not in query:
            return {"status": "success", "data": []}
        raw = [{"metric": {"instance": "node-1"}, "values": _series(datetime.now(timezone.utc).timestamp())}]
        # What prometheus_query really returns: statistics + 50 downsampled points.
        return {"status": "success", "data": _format_as_json(raw, "matrix")}
    return query_fn


async def _node_forecast(monkeypatch, seen):
    async def no_active_filter(core_api, request_timeout=30.0):
        return None
    monkeypatch.setattr(rf, "get_active_node_names_bounded", no_active_filter)
    out = await rf._analyze_node_resources_new("7d", "24h", _Log(), query_fn=_node_query_fn(seen), core_api=None)
    (cpu,) = [f for f in out if f["resource_type"] == "cpu"]
    return cpu


@pytest.mark.asyncio
async def test_growth_rate_uses_real_sample_times(monkeypatch):
    cpu = await _node_forecast(monkeypatch, [])
    assert cpu["growth_rate"]["unit"] == "percent_per_5min"
    assert cpu["growth_rate"]["value"] == pytest.approx(_SLOPE_PER_5MIN, rel=0.01)


@pytest.mark.asyncio
async def test_current_usage_is_the_newest_sample(monkeypatch):
    cpu = await _node_forecast(monkeypatch, [])
    newest = _BASE + _SLOPE_PER_5MIN * (_SAMPLES - 1)
    assert cpu["current_usage"]["value"] == pytest.approx(newest, abs=1e-3)


@pytest.mark.asyncio
async def test_predicted_exhaustion_follows_the_true_trend(monkeypatch):
    cpu = await _node_forecast(monkeypatch, [])
    newest = _BASE + _SLOPE_PER_5MIN * (_SAMPLES - 1)
    exhaustion = datetime.fromisoformat(cpu["predicted_exhaustion"])
    assert exhaustion.tzinfo is not None
    expected = datetime.now(timezone.utc) + timedelta(minutes=5 * (90 - newest) / _SLOPE_PER_5MIN)
    assert abs((exhaustion - expected).total_seconds()) < 0.02 * (expected - datetime.now(timezone.utc)).total_seconds() + 120


@pytest.fixture
def non_utc_host(monkeypatch):
    """CI runners use UTC, where naive local time equals UTC; force a zone
    where the old "naive now + Z" window is visibly wrong."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.asyncio
async def test_query_window_is_utc(monkeypatch, non_utc_host):
    seen = []
    await _node_forecast(monkeypatch, seen)
    assert seen[0]["start_time"].endswith("+00:00")
    start = datetime.fromisoformat(seen[0]["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(seen[0]["end_time"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert end.tzinfo is not None and start.tzinfo is not None
    assert abs((end - now).total_seconds()) < 60
    assert end - start == timedelta(days=7)


def test_downsampling_keeps_the_newest_sample():
    raw = [{"metric": {}, "values": [[i, str(i)] for i in range(1000)]}]
    (out,) = _format_as_json(raw, "matrix")
    assert out["sampled_count"] == 50
    assert out["values"][0] == [0, "0"]
    assert out["values"][-1] == [999, "999"]


# ── H14: namespace CPU / memory ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def server(tmp_path_factory):
    kubeconfig = tmp_path_factory.mktemp("kube_forecast_math") / "config"
    kubeconfig.write_text(_FAKE_KUBECONFIG)
    keys = ("KUBECONFIG", "KUBEARCHIVE_ENABLED", "LUMINO_DISABLE_TELEMETRY", "LUMINO_CONFIG", "LUMINO_PROFILE")
    orig = {k: os.environ.get(k) for k in keys}
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)
    spec = importlib.util.spec_from_file_location("server_mcp_forecast_math", SRC / "server-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_forecast_math"] = mod
    spec.loader.exec_module(mod)
    yield mod
    for k, v in orig.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


async def _namespace_forecasts(server, monkeypatch, namespace):
    queries = []

    async def fake_prom(query, **kwargs):
        queries.append(query)
        if "container_cpu_usage_seconds_total" in query:
            return _instant("0.473")
        if "container_memory" in query:
            return _instant("2.5")
        return {"status": "success", "data": []}

    async def no_nodes(*a, **k):
        return []

    async def no_overview(*a, **k):
        return {"overall_health": "unknown"}

    monkeypatch.setattr(server, "prometheus_query", fake_prom)
    monkeypatch.setattr(server, "_analyze_node_resources_new", no_nodes)
    monkeypatch.setattr(server, "_analyze_cluster_capacity_new", no_overview)
    result = await server.resource_bottleneck_forecaster(namespaces=[namespace], resource_types=["cpu", "memory"])
    return result["forecasts"], [q for q in queries if "container_" in q]


@pytest.mark.asyncio
async def test_namespace_cpu_is_in_cores_not_times_100(server, monkeypatch):
    forecasts, queries = await _namespace_forecasts(server, monkeypatch, "team-a")
    (cpu,) = [f for f in forecasts if f["resource_type"] == "namespace_cpu"]
    assert cpu["current_usage"] == {"value": 0.473, "unit": "cores"}
    cpu_query = next(q for q in queries if "container_cpu" in q)
    assert "* 100" not in cpu_query and "*100" not in cpu_query


@pytest.mark.asyncio
async def test_namespace_queries_skip_pod_level_series(server, monkeypatch):
    _, queries = await _namespace_forecasts(server, monkeypatch, "team-a")
    assert queries
    for q in queries:
        assert 'container!=""' in q, q
        assert 'container!="POD"' in q, q


@pytest.mark.asyncio
async def test_namespace_growth_is_not_claimed_as_zero(server, monkeypatch):
    forecasts, _ = await _namespace_forecasts(server, monkeypatch, "team-a")
    for f in forecasts:
        assert f["growth_rate"]["value"] is None, f


@pytest.mark.asyncio
async def test_namespace_label_value_is_escaped(server, monkeypatch):
    evil = 'x"} or vector(1) or {a="'
    _, queries = await _namespace_forecasts(server, monkeypatch, evil)
    assert queries
    for q in queries:
        assert 'namespace="x\\"} or vector(1) or {a=\\""' in q, q


def test_nan_samples_are_skipped():
    now = datetime.now(timezone.utc)
    metric = {"values": [[1000, "10"], [1300, "11"], [1600, "12"], [1900, "NaN"]]}
    trend = rf._usage_trend(metric, now)
    assert trend["current"] == 12.0
    assert trend["growth_per_5min"] == pytest.approx(1.0)


# ── Review follow-ups ────────────────────────────────────────────────────────

def test_already_exhausted_series_is_reported_now():
    now = datetime.now(timezone.utc)
    metric = {"values": [[1000, "92"], [1300, "93"], [1600, "94"]]}
    trend = rf._usage_trend(metric, now)
    assert trend["predicted_exhaustion"] == now.isoformat()


def test_far_projection_is_not_reported_and_does_not_overflow():
    now = datetime.now(timezone.utc)
    # +1e-9 % per 5 minutes: exhaustion billions of years away
    metric = {"values": [[1000, "10.000000000"], [1300, "10.000000001"], [1600, "10.000000002"]]}
    trend = rf._usage_trend(metric, now)
    assert trend["predicted_exhaustion"] is None


def test_too_few_samples_gives_a_growth_note():
    trend = rf._usage_trend({"values": [[1000, "10"], [1300, "11"]]}, datetime.now(timezone.utc))
    assert trend["growth_per_5min"] is None
    assert "fewer than 3" in trend["growth_note"]


@pytest.mark.asyncio
async def test_one_bad_series_does_not_drop_the_others(monkeypatch):
    async def no_active_filter(core_api, request_timeout=30.0):
        return None

    async def query_fn(query, **kwargs):
        if "node_filesystem" not in query:
            return {"status": "success", "data": []}
        return {"status": "success", "data": [
            {"metric": {"instance": "n1", "mountpoint": "/"},
             "values": [[1000, "10"], [1300, "11"], [1600, "12"]]},
            {"metric": {"instance": "n2", "mountpoint": "/"}, "values": "not a list"},
            {"metric": {"instance": "n3", "mountpoint": "/"},
             "values": [[1000, "20"], [1300, "21"], [1600, "22"]]},
        ]}

    real_trend = rf._usage_trend

    def flaky_trend(metric, now):
        if metric["metric"]["instance"] == "n2":
            raise OverflowError("date value out of range")
        return real_trend(metric, now)

    monkeypatch.setattr(rf, "get_active_node_names_bounded", no_active_filter)
    monkeypatch.setattr(rf, "_usage_trend", flaky_trend)
    out = await rf._analyze_node_resources_new("7d", "24h", _Log(), query_fn=query_fn, core_api=None)
    assert sorted(f["resource_identifier"]["node"] for f in out) == ["n1", "n3"]


@pytest.mark.asyncio
async def test_node_listing_failure_still_reads_usage():
    class _NoNodes:
        def list_node(self, **kwargs):
            raise RuntimeError("nodes is forbidden")

    out = await rf._analyze_cluster_capacity_new(_NoNodes(), _Log(), query_fn=_capacity_query_fn("95.0", "40.0"))
    assert out["overall_health"] == "critical"
    assert out["total_nodes"] is None and out["total_cpu_cores"] is None
    assert out["data_source"]["nodes"] == "unavailable"
    assert out["current_cpu_usage"] == "95.0%"


@pytest.mark.asyncio
async def test_namespace_growth_has_a_note(server, monkeypatch):
    forecasts, _ = await _namespace_forecasts(server, monkeypatch, "team-a")
    for f in forecasts:
        assert f["growth_rate"]["note"], f


@pytest.mark.asyncio
async def test_namespace_nan_cpu_is_dropped(server, monkeypatch):
    async def fake_prom(query, **kwargs):
        if "container_cpu_usage_seconds_total" in query:
            return _instant("NaN")
        return {"status": "success", "data": []}

    async def nothing(*a, **k):
        return []

    async def no_overview(*a, **k):
        return {"overall_health": "unknown"}

    monkeypatch.setattr(server, "prometheus_query", fake_prom)
    monkeypatch.setattr(server, "_analyze_node_resources_new", nothing)
    monkeypatch.setattr(server, "_analyze_cluster_capacity_new", no_overview)
    result = await server.resource_bottleneck_forecaster(namespaces=["team-a"], resource_types=["cpu"])
    assert [f for f in result["forecasts"] if f["resource_type"] == "namespace_cpu"] == []
