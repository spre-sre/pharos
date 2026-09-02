"""Bounded, off-loop node listing for the remaining raw list_node() call sites.

Finding 2026-08-21: six agents generating fleet reports froze the server —
`_get_fallback_cluster_health` (and friends) call `list_node()` synchronously
on the event loop with no client-side timeout. Against a degraded apiserver
(prd-rh01/rh02: stream killed mid-response, urllib3 retries for minutes) one
stuck call starves every other request, including /health.

`collect_baseline_system_data` (helpers/utils.py) now calls the same
`list_nodes_bounded` helper as every other site — consolidated below.

Bug 7 (memory: pharos-tool-bugs-live-testing, accepted risk twice): the
caller-bound `wait_for` in `list_nodes_bounded` protects the CALLER, but the
abandoned worker thread lingers in the shared default `asyncio.to_thread`
executor (min(32, cpu+4)) until its own urllib3 retries exhaust — up to ~2
minutes. Node listing now runs on a small DEDICATED executor
(`_NODE_LISTING_EXECUTOR`) so a wedged apiserver under heavy fan-out can only
exhaust its own small pool, never the shared one every other tool call
(log fetches, pod lists, ...) draws from.
"""
import asyncio
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from helpers.utils import (  # noqa: E402
    _get_active_node_names,
    _get_fallback_cluster_health,
    _NODE_LISTING_EXECUTOR,
    _NODE_LISTING_EXECUTOR_MAX_WORKERS,
    list_nodes_bounded,
)
from helpers.resource_forecasting import (  # noqa: E402
    _analyze_cluster_capacity_new,
    _analyze_node_resources_new,
)

logger = logging.getLogger("test")


def _fake_node(name="n1", ready="True"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={}),
        status=SimpleNamespace(
            capacity={"cpu": "4", "memory": "16384Ki"},
            conditions=[SimpleNamespace(type="Ready", status=ready)],
            addresses=[SimpleNamespace(type="InternalIP", address="10.0.0.1")],
        ),
        spec=SimpleNamespace(unschedulable=None),
    )


class _RecordingCore:
    """Fake CoreV1Api recording kwargs and calling thread for list_node.

    Also answers the other read calls made by the functions under test with
    empty results so each function runs to its node-listing section.
    """

    def __init__(self, n_nodes=2):
        self.node_calls = []
        self.node_call_threads = []
        self._n = n_nodes
        self.api_client = SimpleNamespace(
            configuration=SimpleNamespace(host="https://fake:6443")
        )

    def list_node(self, **kwargs):
        self.node_calls.append(kwargs)
        self.node_call_threads.append(threading.current_thread())
        return SimpleNamespace(
            items=[_fake_node(f"n{i}") for i in range(self._n)],
            metadata=SimpleNamespace(_continue=None),
        )

    def list_namespace(self, **kwargs):
        return SimpleNamespace(items=[], metadata=SimpleNamespace(_continue=None))

    def list_namespaced_pod(self, namespace, **kwargs):
        return SimpleNamespace(items=[], metadata=SimpleNamespace(_continue=None))


# ── shared helper ──────────────────────────────────────────────────────────────

def test_list_nodes_bounded_sets_client_timeout():
    core = _RecordingCore()
    asyncio.run(list_nodes_bounded(core))
    assert core.node_calls and core.node_calls[0].get("_request_timeout") == 30


def test_list_nodes_bounded_runs_off_the_event_loop():
    core = _RecordingCore()
    asyncio.run(list_nodes_bounded(core))
    assert core.node_call_threads[0] is not threading.main_thread()


def test_list_nodes_bounded_returns_items():
    core = _RecordingCore(n_nodes=3)
    nodes = asyncio.run(list_nodes_bounded(core))
    assert len(nodes.items) == 3


def test_list_nodes_bounded_bounds_the_caller():
    """Review round 1 MAJOR: _request_timeout alone does not bound the caller —
    urllib3 retries read timeouts (Retry.total=3), worst case ~4x the timeout.
    The awaitable itself must be wrapped in asyncio.wait_for so a degraded
    link cannot hold the caller for minutes."""
    import time

    class _StuckCore:
        def list_node(self, **kwargs):
            time.sleep(3)
            return SimpleNamespace(items=[], metadata=SimpleNamespace(_continue=None))

    async def _measure():
        # measure inside the running loop: asyncio.run teardown joins the
        # abandoned worker thread, which is not what bounds the caller
        start = time.monotonic()
        try:
            await list_nodes_bounded(_StuckCore(), request_timeout=0.05)
            return time.monotonic() - start, False
        except (TimeoutError, asyncio.TimeoutError):
            return time.monotonic() - start, True

    elapsed, raised = asyncio.run(_measure())
    assert raised, "a stuck node list must raise a timeout to the caller"
    assert elapsed < 2.5, f"caller was held {elapsed:.1f}s despite request_timeout=0.05"


def test_get_active_node_names_bounded_caller_times_out_to_none():
    """Re-review MAJOR-3: the async dispatch of _get_active_node_names must
    bound the caller like list_nodes_bounded does — a wedged apiserver
    degrades to the None (filter-disabled) path instead of holding the
    forecaster ~2 minutes."""
    import time
    from helpers.resource_forecasting import get_active_node_names_bounded

    class _StuckCore:
        api_client = SimpleNamespace(configuration=SimpleNamespace(host="h"))

        def list_node(self, **kwargs):
            time.sleep(3)
            return SimpleNamespace(items=[], metadata=SimpleNamespace(_continue=None))

    async def _measure():
        start = time.monotonic()
        result = await get_active_node_names_bounded(_StuckCore(), request_timeout=0.05)
        return time.monotonic() - start, result

    elapsed, result = asyncio.run(_measure())
    assert result is None, "timeout must degrade to None (filter disabled)"
    assert elapsed < 2.5, f"caller was held {elapsed:.1f}s despite request_timeout=0.05"


def test_get_active_node_names_returns_none_on_failure():
    """Review round 1: a timed-out node list must be distinguishable from
    'no active nodes' — returning set() silently disabled the active-node
    filter; None signals degradation to the caller."""
    class _FailingCore:
        api_client = SimpleNamespace(configuration=SimpleNamespace(host="h"))

        def list_node(self, **kwargs):
            raise RuntimeError("read timed out (test-injected)")

    assert _get_active_node_names(_FailingCore()) is None


# ── _get_fallback_cluster_health (froze the server 2026-08-21) ────────────────

def test_fallback_cluster_health_node_call_bounded_and_off_loop():
    core = _RecordingCore()
    asyncio.run(_get_fallback_cluster_health(core))
    assert core.node_calls, "fallback health never listed nodes"
    assert core.node_calls[0].get("_request_timeout") == 30
    assert core.node_call_threads[0] is not threading.main_thread()


# ── resource forecasting: cluster capacity ────────────────────────────────────

def test_capacity_analysis_node_call_bounded_and_off_loop():
    core = _RecordingCore()

    async def query_fn(*a, **k):
        return {"result": []}

    asyncio.run(_analyze_cluster_capacity_new(core, logger, query_fn=query_fn))
    assert core.node_calls, "capacity analysis never listed nodes"
    assert core.node_calls[0].get("_request_timeout") == 30
    assert core.node_call_threads[0] is not threading.main_thread()


# ── resource forecasting: active-node filter ──────────────────────────────────

def test_get_active_node_names_sets_client_timeout():
    core = _RecordingCore()
    names = _get_active_node_names(core)
    assert core.node_calls[0].get("_request_timeout") == 30
    assert "n0" in names


# ── bug 7: dedicated executor isolation ───────────────────────────────────────

def test_dedicated_executor_is_small_and_bounded():
    """The pool must be small — its whole purpose is to be exhaustible on
    its own without touching the shared default executor's capacity."""
    assert isinstance(_NODE_LISTING_EXECUTOR, ThreadPoolExecutor)
    assert 2 <= _NODE_LISTING_EXECUTOR_MAX_WORKERS <= 8


def test_list_nodes_bounded_runs_on_the_dedicated_executor():
    core = _RecordingCore()
    asyncio.run(list_nodes_bounded(core))
    name = core.node_call_threads[0].name
    assert "node-listing" in name, (
        f"expected the dedicated executor's thread naming, got {name!r}"
    )


def test_get_active_node_names_bounded_runs_on_the_dedicated_executor():
    from helpers.resource_forecasting import get_active_node_names_bounded

    core = _RecordingCore()
    asyncio.run(get_active_node_names_bounded(core))
    name = core.node_call_threads[0].name
    assert "node-listing" in name, (
        f"expected the dedicated executor's thread naming, got {name!r}"
    )


def test_saturated_node_pool_does_not_starve_unrelated_to_thread_work():
    """Bug 7's actual failure mode: a wedged apiserver strands every worker
    in the node-listing pool. Prove an UNRELATED asyncio.to_thread call
    (the shared default executor — every other tool call site: log fetches,
    pod lists, ...) is not delayed by that saturation.

    Review finding: on a 12-core host the real default executor has 16
    workers, so saturating only the 4 dedicated slots never touches it —
    this test passed even reverted to the pre-fix asyncio.to_thread
    dispatch. Fix: shrink the loop's default executor to ONE worker for the
    duration of the test. That makes the assertion deterministic regardless
    of host core count — if node listing regressed back onto to_thread
    (the default executor), a single stuck call would occupy that one slot
    and the unrelated call would queue behind it for the full release wait.
    """
    release = threading.Event()

    class _StuckCore:
        def list_node(self, **kwargs):
            release.wait(timeout=5)
            return SimpleNamespace(items=[], metadata=SimpleNamespace(_continue=None))

    async def _run():
        loop = asyncio.get_running_loop()
        shrunk_default = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(shrunk_default)
        try:
            # Saturate every slot in the DEDICATED pool with stuck calls.
            stuck = [asyncio.ensure_future(list_nodes_bounded(_StuckCore(), request_timeout=10))
                    for _ in range(_NODE_LISTING_EXECUTOR_MAX_WORKERS)]
            await asyncio.sleep(0.05)  # let them all actually start running

            start = time.monotonic()
            result = await asyncio.to_thread(lambda: "unrelated-work-done")
            elapsed = time.monotonic() - start

            release.set()
            for t in stuck:
                t.cancel()
            return result, elapsed
        finally:
            shrunk_default.shutdown(wait=False, cancel_futures=True)

    result, elapsed = asyncio.run(_run())
    assert result == "unrelated-work-done"
    assert elapsed < 1.0, (
        f"unrelated to_thread work waited {elapsed:.2f}s behind a saturated "
        f"node-listing pool — the pools are not actually isolated"
    )


def test_node_resources_analysis_lists_nodes_off_loop():
    core = _RecordingCore()

    async def query_fn(*a, **k):
        return {"result": []}

    asyncio.run(_analyze_node_resources_new(
        "24h", "7d", logger, query_fn=query_fn, core_api=core))
    assert core.node_calls, "node-resources analysis never listed nodes"
    assert core.node_call_threads[0] is not threading.main_thread()
