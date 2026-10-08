"""
tests/test_time_handling.py

Time handling must not depend on the host time zone, and time filters must
never be dropped silently.

H01: running PipelineRun/TaskRun durations used naive local now() against UTC
     start times ("2.08 hours" for a 5-minute run at UTC+2, negative at UTC-4).
H09: get_all_pod_logs ignored a naive since_time (TypeError) and an invalid one
     (whole log read), and dropped tail_lines when a time filter was given.
H18: the etcd until_time filter kept every "[<timestamp>] ..." line (cleaned
     logs) and raised on a naive until_time.
H08: smart_summarize_pod_logs ignored end_time on the Kubernetes path and did
     not report the window it used.
"""
import asyncio
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
sys.path.insert(0, str(SRC))

from helpers.log_analysis import _filter_logs_by_time_range  # noqa: E402
from helpers.utils import calculate_duration, calculate_duration_seconds, get_all_pod_logs  # noqa: E402


@pytest.fixture(params=["Europe/Berlin", "America/New_York", "Asia/Tokyo"])
def host_tz(request, monkeypatch):
    """Run the test as if the host were in a non-UTC time zone."""
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


def _iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── H01 ──────────────────────────────────────────────────────────────────────


def test_running_duration_independent_of_host_tz(host_tz):
    start = _iso_utc(datetime.now(timezone.utc) - timedelta(minutes=5))

    seconds = calculate_duration_seconds(start, None, use_current_if_missing=True)
    text = calculate_duration(start, None, use_current_if_missing=True)

    assert 295 <= seconds <= 310, f"{host_tz}: {seconds}"
    assert text.endswith("minutes (running)") and 4.9 <= float(text.split()[0]) <= 5.2, text


def test_naive_start_is_treated_as_utc(host_tz):
    start = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None).isoformat()

    seconds = calculate_duration_seconds(start, None, use_current_if_missing=True)

    assert 595 <= seconds <= 610, f"{host_tz}: {seconds}"


def test_finished_duration_unchanged():
    assert calculate_duration_seconds("2026-01-01T10:00:00Z", "2026-01-01T10:05:00Z") == 300
    assert calculate_duration("2026-01-01T10:00:00Z", "2026-01-01T12:30:00Z") == "2.50 hours"


# ── H09 ──────────────────────────────────────────────────────────────────────


class _PodApi:
    def __init__(self):
        self.log_calls = []

    def read_namespaced_pod(self, name, namespace, **kw):
        return SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(name="main")]))

    def read_namespaced_pod_log(self, **kw):
        self.log_calls.append(kw)
        return "line\n"


def test_naive_since_time_is_utc_and_keeps_tail_lines(host_tz):
    api = _PodApi()
    since = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None).isoformat(timespec="seconds")

    asyncio.run(get_all_pod_logs("p", "ns", api, tail_lines=50, since_time=since))

    (kw,) = api.log_calls
    assert 595 <= kw["since_seconds"] <= 610, f"{host_tz}: {kw}"
    assert kw["tail_lines"] == 50


def test_invalid_since_time_is_reported_not_ignored():
    api = _PodApi()

    result = asyncio.run(get_all_pod_logs("p", "ns", api, since_time="yesterday"))

    assert api.log_calls == [], "logs were read without the requested time filter"
    assert "time_filter_error" in result and "yesterday" in result["time_filter_error"]


def test_tail_lines_with_since_seconds_kept():
    api = _PodApi()

    asyncio.run(get_all_pod_logs("p", "ns", api, tail_lines=20, since_seconds=60))

    (kw,) = api.log_calls
    assert kw["since_seconds"] == 60 and kw["tail_lines"] == 20


# ── H18 ──────────────────────────────────────────────────────────────────────

UNTIL = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("fmt", [
    "{t}Z first",                    # raw kubectl --timestamps
    "[{t}Z] [INFO] first",           # clean_logs output
    "{d} {h} first",                 # "date time" form (naive = UTC)
])
def test_until_time_filters_cleaned_and_raw_lines(fmt):
    def line(ts, text):
        return fmt.format(t=ts.strftime("%Y-%m-%dT%H:%M:%S"), d=ts.strftime("%Y-%m-%d"),
                          h=ts.strftime("%H:%M:%S")).replace("first", text)

    logs = "\n".join([line(UNTIL - timedelta(minutes=5), "before"),
                      line(UNTIL + timedelta(minutes=5), "after")])

    out = _filter_logs_by_time_range(logs, UNTIL)

    assert "before" in out and "after" not in out


def test_until_time_naive_is_utc_and_does_not_raise():
    logs = "2026-01-01T11:55:00Z before\n2026-01-01T12:05:00Z after"

    out = _filter_logs_by_time_range(logs, UNTIL.replace(tzinfo=None))

    assert "before" in out and "after" not in out


def test_continuation_lines_kept_before_cutoff():
    logs = "2026-01-01T11:55:00Z start\n    at frame 1\n2026-01-01T12:05:00Z after"

    out = _filter_logs_by_time_range(logs, UNTIL)

    assert "at frame 1" in out and "after" not in out


# ── H08 ──────────────────────────────────────────────────────────────────────

FAKE_KUBECONFIG = """\
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
    saved = {k: os.environ.get(k) for k in ("KUBECONFIG", "KUBEARCHIVE_ENABLED")}
    kubeconfig = tmp_path_factory.mktemp("kube_time") / "config"
    kubeconfig.write_text(FAKE_KUBECONFIG)
    os.environ["KUBECONFIG"] = str(kubeconfig)
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)
    spec = importlib.util.spec_from_file_location("server_mcp_time", SRC / "server-mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_mcp_time"] = mod
    spec.loader.exec_module(mod)
    yield mod
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    sys.modules.pop("server_mcp_time", None)


@pytest.mark.asyncio
async def test_summarize_end_time_window_is_applied_and_reported(server, monkeypatch):
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(hours=26), now - timedelta(hours=24)
    captured = {}

    async def fake_get_pod_logs(namespace, pod_name, clients=None, **params):
        captured.update(params)
        lines = [
            f"{_iso_utc(end - timedelta(minutes=30))} ERROR inside window",
            f"{_iso_utc(end + timedelta(hours=3))} ERROR after window",
        ]
        return {"logs": {"main": "\n".join(lines)}}

    monkeypatch.setattr(server, "get_pod_logs", fake_get_pod_logs)

    result = await server.smart_summarize_pod_logs(
        pod_name="p", namespace="ns", start_time=_iso_utc(start), end_time=_iso_utc(end))

    assert "error" not in result, result
    assert result["metadata"]["processing_metrics"]["total_log_lines"] == 1
    window = result["metadata"]["requested_window"]
    assert window[0].startswith(_iso_utc(start)[:16]) and window[1].startswith(_iso_utc(end)[:16])


@pytest.mark.asyncio
async def test_summarize_end_time_alone_reads_the_hour_before(server, monkeypatch):
    end = datetime.now(timezone.utc) - timedelta(hours=24)
    captured = {}

    async def fake_get_pod_logs(namespace, pod_name, clients=None, **params):
        captured.update(params)
        return {"logs": {"main": f"{_iso_utc(end - timedelta(minutes=10))} INFO ok"}}

    monkeypatch.setattr(server, "get_pod_logs", fake_get_pod_logs)

    result = await server.smart_summarize_pod_logs(pod_name="p", namespace="ns", end_time=_iso_utc(end))

    assert "error" not in result, result
    # must reach back past end_time (25 h), not just the default last hour
    assert captured["since_seconds"] >= 24 * 3600
    window = result["metadata"]["requested_window"]
    assert window[1].startswith(_iso_utc(end)[:16])


# ── undated events reach the server tools as None, not now() ─────────────────


@pytest.mark.asyncio
async def test_smart_events_summary_tolerates_undated_event(server, monkeypatch):
    async def fake_internal(*a, **kw):
        return {
            "filtered_events_count": 2,
            "events": [
                "[2026-01-01T10:00:00+00:00] Warning: BackOff - restarting (Object: Pod/p1)",
                "Warning: FailedScheduling - 0/3 nodes are available (Object: Pod/p2)",
            ],
            "applied_filters": {},
            "errors": [],
        }

    monkeypatch.setattr(server, "_get_namespace_events_internal", fake_internal)

    result = await server.smart_get_namespace_events("ns", last_n_events=10, strategy="smart_summary")

    assert "error" not in result, result
    stamps = {e["event_string"][:12]: e["timestamp"] for e in result["events"]}
    assert None in stamps.values()
    assert any(v and v.startswith("2026-01-01T10:00:00") for v in stamps.values())
