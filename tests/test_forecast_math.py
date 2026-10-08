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
import math
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
    # 30d horizon: the true trend reaches 90 % in about 7 days
    out = await rf._analyze_node_resources_new("7d", "30d", _Log(), query_fn=_node_query_fn(seen), core_api=None)
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

    def flaky_trend(metric, now, *args):
        if metric["metric"]["instance"] == "n2":
            raise OverflowError("date value out of range")
        return real_trend(metric, now, *args)

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


def test_one_hot_sample_is_not_exhaustion():
    now = datetime.now(timezone.utc)
    metric = {"values": [[1000, "60"], [1300, "62"], [1600, "61"], [1900, "60"], [2200, "95"]]}
    trend = rf._usage_trend(metric, now)
    assert trend["predicted_exhaustion"] is None
    assert "spike" in trend["exhaustion_note"]


@pytest.mark.parametrize("values", [
    [60.0] * 49 + [95.0],                              # flat, then one spike
    [60.0, 60.01, 60.02, 60.03, 95.0],                 # few points, then one spike
    [60 + (2 if i % 2 else -2) for i in range(49)] + [95.0],  # noisy flat, then one spike
])
def test_one_spike_creates_no_forecast(values):
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend({"values": [[1000 + 300 * i, str(v)] for i, v in enumerate(values)]}, now)
    assert trend["predicted_exhaustion"] is None
    assert "spike" in trend["exhaustion_note"]


def test_levelling_off_curve_is_not_already_exhausted():
    """A straight line through a curve that levels off at 85 % is above the
    data at the newest sample; that alone must not read as exhausted."""
    now = datetime.now(timezone.utc)
    values = [[1000 + 300 * i, str(85 - 45 * math.exp(-i / 6))] for i in range(50)]
    trend = rf._usage_trend({"values": values}, now, timedelta(hours=24), "24h")
    assert trend["predicted_exhaustion"] is None
    assert "levelling off" in trend["exhaustion_note"]


def test_drop_at_the_end_is_not_exhausted_or_projected():
    """A ramp to 95 % that drops to 40 %: the trend is above the data, so
    neither "exhausted now" nor a forecast from the trend."""
    now = datetime.now(timezone.utc)
    values = [[1000 + 300 * i, str(50 + 5 * i)] for i in range(10)] + [[4000, "40"]]
    trend = rf._usage_trend({"values": values}, now)
    assert trend["predicted_exhaustion"] is None
    assert "far below the trend" in trend["exhaustion_note"]


def test_two_points_cannot_be_exhausted_on_one_hot_sample():
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend({"values": [[1000, "50"], [1300, "95"]]}, now)
    assert trend["predicted_exhaustion"] is None
    assert "spike" in trend["exhaustion_note"]


def test_spike_on_a_falling_trend_has_a_spike_note():
    now = datetime.now(timezone.utc)
    values = [[1000 + 300 * i, str(95 - 5 * i)] for i in range(10)] + [[4000, "91"]]  # 95 % falling to 50 %
    metric = {"values": values}
    trend = rf._usage_trend(metric, now)
    assert trend["predicted_exhaustion"] is None
    assert "spike" in trend["exhaustion_note"]


def test_exhausted_series_has_a_note():
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend({"values": [[1000, "92"], [1300, "93"], [1600, "94"]]}, now)
    assert "already exhausted" in trend["exhaustion_note"]


@pytest.mark.parametrize("values,note", [
    ([[1000, "50"], [1300, "49"], [1600, "48"]], "not growing"),
    ([[1000, "10"], [1300, "10.01"], [1600, "10.02"]], "in about 27.8 days, beyond the 24h horizon"),
    ([[1000, "10"], [1300, "10.000001"], [1600, "10.000002"]], "not reached within 365 days (projection limit)"),
    ([[1000, "85"], [1300, "85.01"], [1600, "85.02"]], "in about 41.5 hours, beyond the 24h horizon"),
])
def test_unprojected_exhaustion_has_a_note(values, note):
    trend = rf._usage_trend({"values": values}, datetime.now(timezone.utc), timedelta(days=1))
    assert trend["predicted_exhaustion"] is None
    assert note in trend["exhaustion_note"]


@pytest.mark.asyncio
async def test_projection_is_limited_to_the_forecast_horizon(monkeypatch):
    async def no_active_filter(core_api, request_timeout=30.0):
        return None

    async def query_fn(query, **kwargs):
        if "node_cpu" not in query:
            return {"status": "success", "data": []}
        # +1 % per 5 minutes from 10 %: reaches 90 % in 80 * 5 min = 6 h 40 min
        return {"status": "success", "data": [
            {"metric": {"instance": "n1"}, "values": [[1000, "8"], [1300, "9"], [1600, "10"]]}]}

    monkeypatch.setattr(rf, "get_active_node_names_bounded", no_active_filter)
    (short,) = await rf._analyze_node_resources_new("7d", "1h", _Log(), query_fn=query_fn, core_api=None)
    (long,) = await rf._analyze_node_resources_new("7d", "24h", _Log(), query_fn=query_fn, core_api=None)
    assert short["predicted_exhaustion"] is None
    assert "beyond the 1h horizon" in short["exhaustion_note"]
    assert long["predicted_exhaustion"] is not None


@pytest.mark.asyncio
async def test_capacity_parses_all_quantity_units():
    class _Core:
        def list_node(self, **kwargs):
            caps = [{"cpu": "1500m", "memory": "1.5Gi"}, {"cpu": "2", "memory": "1Ti"}]
            return SimpleNamespace(items=[SimpleNamespace(status=SimpleNamespace(capacity=c)) for c in caps])

    out = await rf._analyze_cluster_capacity_new(_Core(), _Log(), query_fn=_capacity_query_fn("40.0", "40.0"))
    assert out["total_nodes"] == 2
    assert out["total_cpu_cores"] == pytest.approx(3.5)
    assert out["total_memory_gb"] == pytest.approx(1025.5)


@pytest.mark.asyncio
async def test_unparsable_capacity_keeps_node_count_and_reason():
    class _Core:
        def list_node(self, **kwargs):
            return SimpleNamespace(items=[SimpleNamespace(status=SimpleNamespace(capacity={"cpu": "lots"}))])

    out = await rf._analyze_cluster_capacity_new(_Core(), _Log(), query_fn=_capacity_query_fn("40.0", "40.0"))
    assert out["total_nodes"] == 1
    assert out["total_cpu_cores"] is None
    assert out["data_source"]["nodes"] == "partial"
    assert out["nodes_error"]


@pytest.mark.asyncio
async def test_invalid_forecast_horizon_is_an_error(server, monkeypatch):
    async def fake_prom(query, **kwargs):
        return {"status": "success", "data": []}

    monkeypatch.setattr(server, "prometheus_query", fake_prom)
    result = await server.resource_bottleneck_forecaster(forecast_horizon="1w")
    assert "Invalid forecast_horizon" in result["error"]


def test_steady_ramp_past_90_is_exhausted_not_a_spike():
    now = datetime.now(timezone.utc)
    values = [[1000 + 300 * i, str(50 + 4 * i)] for i in range(10)] + [[4000, "94"]]
    trend = rf._usage_trend({"values": values}, now)
    assert trend["predicted_exhaustion"] == now.isoformat()
    assert "already exhausted" in trend["exhaustion_note"]


@pytest.mark.asyncio
async def test_overflowing_forecast_horizon_is_an_error(server, monkeypatch):
    async def fake_prom(query, **kwargs):
        return {"status": "success", "data": []}

    monkeypatch.setattr(server, "prometheus_query", fake_prom)
    result = await server.resource_bottleneck_forecaster(forecast_horizon="9999999999d")
    assert "Invalid forecast_horizon" in result["error"]


def test_spike_below_the_threshold_creates_no_trend():
    """Least squares would turn one 85 % sample on a flat 60 % series into a
    rising trend and a forecast; the robust slope does not."""
    now = datetime.now(timezone.utc)
    values = [[1000 + 300 * i, "60"] for i in range(49)] + [[1000 + 300 * 49, "85"]]
    trend = rf._usage_trend({"values": values}, now)
    assert trend["predicted_exhaustion"] is None
    assert "not growing" in trend["exhaustion_note"]


def _ts_values(values, step=300):
    return {"values": [[1000 + step * i, str(v)] for i, v in enumerate(values)]}


def test_noisy_ramp_crossing_90_is_forecast_not_a_spike():
    """60 + 0.6 % per 5 min with noise; the newest sample just crossed 90 %."""
    noise = [0.5, -0.7, 0.2, -0.3, 0.6, -0.5, 0.1, -0.6, 0.4, -0.2]
    values = [60 + 0.6 * i + noise[i % 10] for i in range(49)] + [90.4]
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend(_ts_values(values), now, timedelta(hours=24), "24h")
    assert trend["predicted_exhaustion"] is not None
    assert "spike" not in (trend.get("exhaustion_note") or "")
    assert datetime.fromisoformat(trend["predicted_exhaustion"]) - now < timedelta(minutes=30)


def test_noisy_flat_series_with_one_85_sample_is_not_moved_forward():
    """The projection starts from the trend level, not from one high sample."""
    base = [60 + (2 if i % 2 else -2) + 0.01 * i for i in range(49)]
    now = datetime.now(timezone.utc)
    quiet = rf._usage_trend(_ts_values(base + [62.49], step=72), now, timedelta(hours=24), "24h")
    hot = rf._usage_trend(_ts_values(base + [85.0], step=72), now, timedelta(hours=24), "24h")
    assert quiet["predicted_exhaustion"] is None
    assert hot["predicted_exhaustion"] is None


def test_small_jump_on_a_clean_ramp_is_not_a_spike():
    """With no noise, any gap is many noise units; a jump of a few points
    over a rising trend is still the trend crossing 90 %, not a spike."""
    values = [60 + 0.6 * i for i in range(49)] + [92.4]   # trend value at the end: 89.4 %
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend(_ts_values(values), now, timedelta(hours=24), "24h")
    assert "spike" not in (trend.get("exhaustion_note") or "")
    assert trend["predicted_exhaustion"] is not None
    assert datetime.fromisoformat(trend["predicted_exhaustion"]) - now < timedelta(minutes=30)


@pytest.mark.parametrize("offset", [0.0, 0.6, 1.2])
def test_noisy_ramp_reaching_90_is_never_dropped(offset):
    """The whole-window trend reaches 90 % one step before the samples do;
    the newest samples are still rising, so it is now or a forecast."""
    noise = [0.5, -0.7, 0.2, -0.3, 0.6, -0.5, 0.1, -0.6, 0.4, -0.2]
    values = [60 + offset + 0.6 * i + noise[i % 10] for i in range(50)]
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend(_ts_values(values), now, timedelta(hours=24), "24h")
    assert trend["predicted_exhaustion"] is not None, trend
    assert datetime.fromisoformat(trend["predicted_exhaustion"]) - now < timedelta(minutes=30)


def test_flat_series_hovering_at_90_is_exhausted_now():
    """Whole-window trend just above 90 %, newest samples flat and within
    their noise of 90 %: at the threshold now, not "no longer rising"."""
    values = [80 + 0.4 * i for i in range(25)] + [89.6 + (0.4 if i % 2 else -0.4) for i in range(25)]
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend(_ts_values(values), now, timedelta(hours=24), "24h")
    assert trend["predicted_exhaustion"] == now.isoformat(), trend


def test_falling_series_after_a_peak_is_not_at_90_now():
    """A daily peak at 96 % that has passed, now about 84 % and falling: the
    whole-window trend is above 90 %, but this is not "at 90 % now"."""
    noise = [4, -4, 2, -2]
    values = [70 + 22 * math.sin(0.8 * math.pi * i / 49) + noise[(i + 1) % 4] for i in range(50)]
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend(_ts_values(values), now, timedelta(hours=24), "24h")
    assert trend["predicted_exhaustion"] is None, trend


# Noisy samples of a daily peak that has passed (70 + 22 sin(...) + noise),
# found by search: each is "at 90 % now" without one of the hover guards.
_PEAK_PASSED_FALLING = [
    71.2, 76.5, 72.1, 71.7, 72.4, 71.8, 76.0, 74.9, 75.0, 77.1, 81.6, 81.8, 80.2, 83.6, 82.0, 81.4, 83.8,
    77.5, 87.3, 82.9, 80.4, 84.0, 91.7, 79.0, 92.0, 87.7, 92.7, 86.3, 88.2, 90.4, 94.9, 89.1, 86.4, 91.0,
    92.8, 89.1, 88.6, 86.1, 98.7, 93.4, 96.7, 93.3, 93.6, 87.1, 92.4, 89.7, 92.0, 88.5, 91.7, 88.4]
_PEAK_PASSED_NOISY = [
    70.1, 77.0, 66.5, 82.5, 84.8, 71.2, 74.3, 77.4, 77.7, 80.4, 80.3, 89.5, 84.0, 83.4, 84.9, 85.9, 84.4,
    94.7, 87.8, 94.1, 82.2, 89.4, 94.1, 84.7, 89.2, 83.5, 92.2, 90.9, 85.5, 85.0, 87.3, 87.9, 96.0, 87.2,
    92.4, 94.9, 84.8, 89.1, 97.7, 98.4, 86.3, 85.6, 91.2, 93.9, 89.6, 84.2, 86.1, 81.4, 92.4, 88.9]


@pytest.mark.parametrize("values", [_PEAK_PASSED_FALLING, _PEAK_PASSED_NOISY],
                         ids=["newest-samples-clearly-falling", "newest-samples-more-than-2-points-under"])
def test_hover_needs_flat_newest_samples_close_to_90(values):
    now = datetime.now(timezone.utc)
    trend = rf._usage_trend(_ts_values(values), now, timedelta(hours=24), "24h")
    assert trend["predicted_exhaustion"] != now.isoformat(), trend
