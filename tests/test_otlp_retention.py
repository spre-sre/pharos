"""Unit tests for Task-5 OTLP retention surfacing and ingest-key in list_sources.

Covers (in order per the brief):
  - CRITICAL 1: list_sources real-call with OTLP source — ring-absent and ring-present
    ingest sub-dict shape (V5 shape exact check, listening=False)
  - Ingest-key shape: ring-absent (V5) and ring-present paths
  - _otlp_listening default False (V5)
  - F12 retention message variants pinned via REAL tool calls (not tautologies)
  - V1 predicate variants: outside-retention fires / does NOT fire
  - Negative case: unbounded window + evicted==0 + empty → honest "No logs found"
  - IMPORTANT 2: requested_window uses injected clock (not wall clock)
  - V6 requested_window tuple rendering rule
  - MINOR 9: skew note NOT emitted for unbounded fetch (behavioral, no inspect.getsource)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"

_FAKE_KUBECONFIG = """\
apiVersion: v1
clusters:
- cluster:
    server: https://127.0.0.1:6443
  name: fake
contexts:
- context:
    cluster: fake
    user: fake
  name: fake
current-context: fake
kind: Config
users:
- name: fake
  user: {}
"""

_MOD_NAME = "server_mcp_otlp_retention_test"


@pytest.fixture(scope="module")
def srv() -> ModuleType:
    """Load server-mcp.py once for the module; yields the module object."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as kf:
        kf.write(_FAKE_KUBECONFIG.encode())
        kube_path = kf.name

    saved = {k: os.environ.get(k) for k in (
        "KUBECONFIG", "KUBEARCHIVE_ENABLED", "LUMINO_DISABLE_TELEMETRY",
        "LUMINO_CONFIG", "LUMINO_PROFILE")}
    os.environ["KUBECONFIG"] = kube_path
    os.environ["KUBEARCHIVE_ENABLED"] = "false"
    os.environ.setdefault("LUMINO_DISABLE_TELEMETRY", "1")
    os.environ.pop("LUMINO_CONFIG", None)
    os.environ.pop("LUMINO_PROFILE", None)

    sys.path.insert(0, str(_SRC))
    spec = importlib.util.spec_from_file_location(_MOD_NAME, str(_SRC / "server-mcp.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)

    yield mod

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    sys.modules.pop(_MOD_NAME, None)
    try:
        sys.path.remove(str(_SRC))
    except ValueError:
        pass
    try:
        os.unlink(kube_path)
    except OSError:
        pass


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_ring(capacity=10, start_ts=1_000_000.0, now_ts=1_000_100.0):
    from adapters.otlp.rings import LogRing
    return LogRing(capacity=capacity, now_fn=lambda: now_ts, start_ts=start_ts)


def _push_record(ring, entity="test-app", body="ERROR log line", recv_ts=1_000_010.0):
    from core.signals import LogRecord
    from adapters.otlp.rings import iso_z
    rec = LogRecord(
        timestamp=iso_z(recv_ts),
        body=body,
        attributes={"entity": entity},
    )
    ring.append(recv_ts, rec)
    return rec


def _make_otlp_config(source_name="otlp-test", capacity=10):
    from core.config_types import ResolvedConfig, SourceConfig
    return ResolvedConfig(
        profile="test",
        sources={
            source_name: SourceConfig(
                adapter="otlp",
                options={"ring_capacity": capacity, "max_body_bytes": 65536},
            )
        },
    )


def _make_otlp_adapter(ring, now_ts=1_000_100.0):
    from adapters.otlp.logs import OtlpLogSource
    opts = {"ring_capacity": ring._capacity, "max_body_bytes": 65536,
            "max_record_bytes": 65536, "signals": ["logs"]}
    return OtlpLogSource(ring, opts, now_fn=lambda: now_ts)


# ─── CRITICAL 1: list_sources real-call with OTLP source ─────────────────────

@pytest.mark.asyncio
class TestListSourcesIngest:
    """CRITICAL 1: list_sources() returns correct ingest sub-dict for OTLP sources.

    Tests both ring-absent and ring-present variants by patching _lumino_config,
    _source_registry, and _otlp_rings, then awaiting the REAL list_sources().
    Mutation: adapter-guard changed to never-match → ingest key absent → FAILS.
    """

    async def test_ring_absent_ingest_exact_v5_shape(self, srv):
        """Ring-absent: ingest sub-dict matches the V5 all-zero shape exactly."""
        from core.config_types import ResolvedConfig, SourceConfig
        from core.registry import build_registry

        source_name = "list-absent-src"
        cfg = ResolvedConfig(
            profile="test",
            sources={source_name: SourceConfig(
                adapter="otlp",
                options={"ring_capacity": 10, "max_body_bytes": 65536},
            )},
        )
        reg = build_registry(cfg)

        orig_cfg = srv._lumino_config
        orig_reg = srv._source_registry
        orig_rings = dict(srv._otlp_rings)
        orig_listening = srv._otlp_listening

        try:
            srv._lumino_config = cfg
            srv._source_registry = reg
            srv._otlp_rings.pop(source_name, None)   # ensure ring absent
            srv._otlp_listening = False
            result = await srv.list_sources()
        finally:
            srv._lumino_config = orig_cfg
            srv._source_registry = orig_reg
            srv._otlp_rings = orig_rings
            srv._otlp_listening = orig_listening

        sources = {s["name"]: s for s in result["sources"]}
        assert source_name in sources, f"source {source_name!r} not in result"
        assert sources[source_name]["ingest"] == {
            "buffered": 0,
            "dropped_oldest": 0,
            "truncated_records": 0,
            "covered_window": None,
            "listening": False,
            "signals": ["logs"],
        }, f"ingest mismatch: {sources[source_name]['ingest']!r}"

    async def test_ring_present_ingest_v5_shape(self, srv):
        """Ring present (seeded): ingest has covered_window list + correct counters."""
        from core.config_types import ResolvedConfig, SourceConfig
        from core.registry import build_registry

        source_name = "list-present-src"
        ring = _make_ring(capacity=10)
        _push_record(ring, body="sample-log")
        cfg = ResolvedConfig(
            profile="test",
            sources={source_name: SourceConfig(
                adapter="otlp",
                options={"ring_capacity": 10, "max_body_bytes": 65536},
            )},
        )
        reg = build_registry(cfg)

        orig_cfg = srv._lumino_config
        orig_reg = srv._source_registry
        orig_rings = dict(srv._otlp_rings)
        orig_listening = srv._otlp_listening

        try:
            srv._lumino_config = cfg
            srv._source_registry = reg
            srv._otlp_rings = {source_name: ring}
            srv._otlp_listening = False
            result = await srv.list_sources()
        finally:
            srv._lumino_config = orig_cfg
            srv._source_registry = orig_reg
            srv._otlp_rings = orig_rings
            srv._otlp_listening = orig_listening

        sources = {s["name"]: s for s in result["sources"]}
        assert source_name in sources
        ingest = sources[source_name]["ingest"]
        # V5 shape exact check (ring-present variant)
        assert ingest["buffered"] == 1
        assert ingest["dropped_oldest"] == 0
        assert ingest["truncated_records"] == 0
        assert isinstance(ingest["covered_window"], list)
        assert len(ingest["covered_window"]) == 2
        assert ingest["listening"] is False
        assert ingest["signals"] == ["logs"]

    async def test_listening_false_reflected_in_list_sources(self, srv):
        """_otlp_listening=False is mirrored into the ingest.listening field."""
        from core.config_types import ResolvedConfig, SourceConfig
        from core.registry import build_registry

        source_name = "list-listen-src"
        ring = _make_ring()
        cfg = ResolvedConfig(
            profile="test",
            sources={source_name: SourceConfig(
                adapter="otlp",
                options={"ring_capacity": 10, "max_body_bytes": 65536},
            )},
        )
        reg = build_registry(cfg)

        orig_cfg = srv._lumino_config
        orig_reg = srv._source_registry
        orig_rings = dict(srv._otlp_rings)
        orig_listening = srv._otlp_listening

        try:
            srv._lumino_config = cfg
            srv._source_registry = reg
            srv._otlp_rings = {source_name: ring}

            srv._otlp_listening = False
            result_off = await srv.list_sources()
            srv._otlp_listening = True
            result_on = await srv.list_sources()
        finally:
            srv._lumino_config = orig_cfg
            srv._source_registry = orig_reg
            srv._otlp_rings = orig_rings
            srv._otlp_listening = orig_listening

        def _ingest(result):
            return {s["name"]: s for s in result["sources"]}[source_name]["ingest"]

        assert _ingest(result_off)["listening"] is False
        assert _ingest(result_on)["listening"] is True


# ─── ingest-key shape tests ───────────────────────────────────────────────────

class TestOtlpIngestStats:
    """V5 ring-absent and ring-present ingest-key rendering."""

    def test_ring_absent_returns_zeros_and_none_window(self, srv):
        """V5: no ring in _otlp_rings → all zeros, covered_window=None, listening=False."""
        # Ensure the source name is NOT in _otlp_rings.
        srv._otlp_rings.pop("absent-src", None)
        result = srv._otlp_ingest_stats("absent-src")
        assert result == {
            "buffered": 0,
            "dropped_oldest": 0,
            "truncated_records": 0,
            "covered_window": None,
            "listening": False,
            "signals": ["logs"],
        }

    def test_ring_present_empty_no_eviction(self, srv):
        """Ring present + empty + no eviction → buffered=0, dropped=0, window=list."""
        ring = _make_ring()
        srv._otlp_rings["empty-src"] = ring
        orig_listening = srv._otlp_listening
        srv._otlp_listening = False
        try:
            result = srv._otlp_ingest_stats("empty-src")
        finally:
            srv._otlp_listening = orig_listening
            srv._otlp_rings.pop("empty-src", None)

        assert result["buffered"] == 0
        assert result["dropped_oldest"] == 0
        assert result["truncated_records"] == 0
        assert isinstance(result["covered_window"], list)
        assert len(result["covered_window"]) == 2
        assert result["listening"] is False
        assert result["signals"] == ["logs"]

    def test_ring_present_with_records_and_eviction(self, srv):
        """Ring present with records and eviction → correct counters, listening flag."""
        ring = _make_ring(capacity=2)
        _push_record(ring, body="record-1")
        _push_record(ring, body="record-2")
        _push_record(ring, body="record-3")  # evicts record-1

        srv._otlp_rings["evicted-src"] = ring
        orig_listening = srv._otlp_listening
        srv._otlp_listening = True
        try:
            result = srv._otlp_ingest_stats("evicted-src")
        finally:
            srv._otlp_listening = orig_listening
            srv._otlp_rings.pop("evicted-src", None)

        assert result["buffered"] == 2
        assert result["dropped_oldest"] == 1
        assert result["listening"] is True
        assert result["signals"] == ["logs"]
        assert isinstance(result["covered_window"], list)

    def test_listening_reflects_module_flag(self, srv):
        """_otlp_ingest_stats reads _otlp_listening from the module level."""
        ring = _make_ring()
        srv._otlp_rings["listen-test"] = ring
        orig = srv._otlp_listening
        try:
            srv._otlp_listening = True
            assert srv._otlp_ingest_stats("listen-test")["listening"] is True
            srv._otlp_listening = False
            assert srv._otlp_ingest_stats("listen-test")["listening"] is False
        finally:
            srv._otlp_listening = orig
            srv._otlp_rings.pop("listen-test", None)


# ─── V1 retention predicate via actual tool calls ────────────────────────────

@pytest.mark.asyncio
class TestRetentionPredicate:
    """V1 predicate tests against the actual smart_summarize_pod_logs / stream_analyze tools."""

    def _setup_otlp(self, srv, source_name="otlp-test",
                    capacity=10, records_to_push=0, eviction_records=0,
                    listening=True):
        """Return (registry, ring, adapter, original_otlp_rings, original_listening)."""
        from core.config_types import ResolvedConfig, SourceConfig
        from core.registry import build_registry

        ring = _make_ring(capacity=capacity)
        entity = "retained-app"

        for i in range(records_to_push):
            _push_record(ring, entity=entity, body=f"LOG line {i}")

        for i in range(eviction_records):
            _push_record(ring, entity=entity, body=f"EVICT trigger {i}")

        cfg = _make_otlp_config(source_name=source_name, capacity=capacity)
        registry = build_registry(cfg)
        adapter = _make_otlp_adapter(ring)

        orig_rings = dict(srv._otlp_rings)
        orig_listening = srv._otlp_listening
        orig_adapter_instances = dict(srv._adapter_instances)
        orig_config = srv._lumino_config
        orig_registry = srv._source_registry

        srv._otlp_rings = {source_name: ring}
        srv._otlp_listening = listening
        srv._adapter_instances = {source_name: adapter}
        srv._lumino_config = cfg
        srv._source_registry = registry

        return (ring, adapter, orig_rings, orig_listening,
                orig_adapter_instances, orig_config, orig_registry)

    def _teardown(self, srv, orig_rings, orig_listening,
                  orig_adapter_instances, orig_config, orig_registry):
        srv._otlp_rings = orig_rings
        srv._otlp_listening = orig_listening
        srv._adapter_instances = orig_adapter_instances
        srv._lumino_config = orig_config
        srv._source_registry = orig_registry

    async def test_unbounded_evicted_fires_smart_summarize(self, srv):
        """V1: unbounded window (no time params) + evicted>0 + empty → outside_retention."""
        (ring, _, orig_rings, orig_listening,
         orig_ai, orig_cfg, orig_reg) = self._setup_otlp(
            srv, capacity=1, records_to_push=1, eviction_records=1)
        try:
            result = await srv.smart_summarize_pod_logs(
                namespace="ns",
                pod_name="no-match-pod",  # won't match "retained-app"
                source="otlp-test",
            )
        finally:
            self._teardown(srv, orig_rings, orig_listening, orig_ai, orig_cfg, orig_reg)

        assert result.get("outside_retention") is True, (
            f"Expected outside_retention=True, got: {result!r}"
        )
        assert result["evicted"] == 1
        assert "record(s) evicted" in result["message"]
        assert isinstance(result["requested_window"], list)
        assert result["requested_window"][0] == "unbounded"
        assert isinstance(result["covered_window"], list)

    async def test_unbounded_evicted_fires_stream_analyze(self, srv):
        """V1: unbounded window + evicted>0 + empty → outside_retention in stream_analyze."""
        (ring, _, orig_rings, orig_listening,
         orig_ai, orig_cfg, orig_reg) = self._setup_otlp(
            srv, capacity=1, records_to_push=1, eviction_records=1)
        try:
            result = await srv.stream_analyze_pod_logs(
                namespace="ns",
                pod_name="no-match-pod",
                source="otlp-test",
            )
        finally:
            self._teardown(srv, orig_rings, orig_listening, orig_ai, orig_cfg, orig_reg)

        assert result.get("outside_retention") is True, (
            f"Expected outside_retention=True, got: {result!r}"
        )
        assert result["evicted"] == 1
        assert "record(s) evicted" in result["message"]

    async def test_negative_unbounded_no_eviction_no_outside_retention(self, srv):
        """V1 negative: unbounded window + evicted==0 + empty → honest 'No logs found'.

        An unbounded request against a ring with zero evictions is a COMPLETE
        buffer — an empty result is an honest 'no matches', not a retention miss.
        (consult D3 in the plan for the rationale.)
        """
        (ring, _, orig_rings, orig_listening,
         orig_ai, orig_cfg, orig_reg) = self._setup_otlp(
            srv, capacity=10, records_to_push=0, eviction_records=0)
        try:
            result = await srv.smart_summarize_pod_logs(
                namespace="ns",
                pod_name="no-match-pod",
                source="otlp-test",
            )
        finally:
            self._teardown(srv, orig_rings, orig_listening, orig_ai, orig_cfg, orig_reg)

        # Must NOT fire retention — empty ring with no eviction is honest
        assert "outside_retention" not in result, (
            f"retention fired spuriously for unbounded+0-eviction: {result!r}"
        )
        # Should fall through to "No logs found"
        error = result.get("error", "")
        assert "No logs found" in error or "no log" in error.lower(), (
            f"expected 'No logs found', got: {result!r}"
        )

    async def test_windowed_start_before_covered_start_fires(self, srv):
        """V1: explicit window.start < covered.start → fires evicted==0 variant."""
        from core.config_types import ResolvedConfig, SourceConfig
        from core.registry import build_registry
        from adapters.otlp.rings import LogRing, iso_z

        # Ring start = 2025-01-01T00:00:10Z (1_735_689_610).
        # covered_window start = iso_z(ceil(1_735_689_610)) = "2025-01-01T00:00:10Z".
        # We request start_time = "2024-12-31T23:59:59Z" which is BEFORE covered.start.
        _fixed_start = 1_735_689_610.0   # 2025-01-01T00:00:10Z
        _fixed_now = 1_735_689_710.0     # 100 seconds later
        ring = LogRing(capacity=10, now_fn=lambda: _fixed_now, start_ts=_fixed_start)
        # No records pushed → no eviction; empty ring with zero drops.

        cfg = _make_otlp_config(source_name="otlp-windowed")
        registry = build_registry(cfg)
        adapter = _make_otlp_adapter(ring, now_ts=_fixed_now)

        orig_rings = dict(srv._otlp_rings)
        orig_listening = srv._otlp_listening
        orig_ai = dict(srv._adapter_instances)
        orig_cfg = srv._lumino_config
        orig_reg = srv._source_registry

        srv._otlp_rings = {"otlp-windowed": ring}
        srv._otlp_listening = False
        srv._adapter_instances = {"otlp-windowed": adapter}
        srv._lumino_config = cfg
        srv._source_registry = registry
        try:
            # Request window starting BEFORE the ring's covered_window start.
            # covered.start = "2025-01-01T00:00:10Z"
            # We request start_time = "2024-12-31T23:59:59Z" < covered.start
            result = await srv.smart_summarize_pod_logs(
                namespace="ns",
                pod_name="any-pod",
                source="otlp-windowed",
                start_time="2024-12-31T23:59:59Z",
            )
        finally:
            self._teardown(srv, orig_rings, orig_listening, orig_ai, orig_cfg, orig_reg)

        assert result.get("outside_retention") is True, (
            f"Expected outside_retention=True for window before covered start, got: {result!r}"
        )
        # evicted==0 → receiver-start message variant
        assert result["evicted"] == 0
        assert "receiver started at" in result["message"], (
            f"Expected evicted==0 message variant, got: {result['message']!r}"
        )
        # requested_window start must be the supplied ISO string (not 'unbounded')
        assert result["requested_window"][0] != "unbounded"

    async def test_f12_evicted_zero_exact_message_string(self, srv):
        """IMPORTANT 3: F12 evicted==0 message pinned through a REAL tool call.

        Ring start = 2025-01-01T00:00:10Z (1_735_689_610).
        Request window starts at 2024-12-31T23:59:59Z (before covered.start).
        Zero evictions → receiver-start message variant.
        MUTATION: rewrite the message tail → this test FAILS.
        """
        from core.config_types import ResolvedConfig, SourceConfig
        from core.registry import build_registry
        from adapters.otlp.rings import LogRing

        _fixed_start = 1_735_689_610.0   # 2025-01-01T00:00:10Z
        _fixed_now = 1_735_689_710.0     # 100 seconds later
        ring = LogRing(capacity=10, now_fn=lambda: _fixed_now, start_ts=_fixed_start)
        # No records → zero evictions

        cfg = _make_otlp_config(source_name="f12-exact-src")
        registry = build_registry(cfg)
        adapter = _make_otlp_adapter(ring, now_ts=_fixed_now)

        orig_rings = dict(srv._otlp_rings)
        orig_listening = srv._otlp_listening
        orig_ai = dict(srv._adapter_instances)
        orig_cfg = srv._lumino_config
        orig_reg = srv._source_registry

        srv._otlp_rings = {"f12-exact-src": ring}
        srv._otlp_listening = False
        srv._adapter_instances = {"f12-exact-src": adapter}
        srv._lumino_config = cfg
        srv._source_registry = registry
        try:
            result = await srv.smart_summarize_pod_logs(
                namespace="ns",
                pod_name="any-pod",
                source="f12-exact-src",
                start_time="2024-12-31T23:59:59Z",
            )
        finally:
            self._teardown(srv, orig_rings, orig_listening, orig_ai, orig_cfg, orig_reg)

        assert result.get("outside_retention") is True
        assert result["evicted"] == 0
        # FULL exact string pinned — mutation to tail → FAILS
        assert result["message"] == (
            "outside retention: the receiver started at 2025-01-01T00:00:10Z; "
            "nothing was being buffered before that"
        ), f"F12 message mismatch: {result['message']!r}"

    async def test_requested_window_uses_injected_clock(self, srv):
        """IMPORTANT 2: requested_window in retention return uses _prov.requested_window
        (injected clock), NOT time.time().

        Ring now_fn is pinned to 1970-01-12T13:46:40Z (1_000_000.0 — far in the
        past relative to wall clock).  Under the bug, requested_window[1] would be
        the current wall clock (~2026-07-...); under the fix it is exactly
        '1970-01-12T13:46:40Z'.  The two differ by ~56 years.
        """
        from adapters.otlp.rings import LogRing
        from core.config_types import ResolvedConfig, SourceConfig
        from core.registry import build_registry

        _FIXED_NOW = 1_000_000.0   # 1970-01-12T13:46:40Z
        _FIXED_START = 500_000.0   # 1970-01-06T18:53:20Z
        ring = LogRing(capacity=1, now_fn=lambda: _FIXED_NOW, start_ts=_FIXED_START)

        # Push 2 records into capacity-1 ring → 1 eviction
        from core.signals import LogRecord
        from adapters.otlp.rings import iso_z as _iso_z
        rec = LogRecord(timestamp=_iso_z(_FIXED_START + 1), body="record-A",
                        attributes={"entity": "app"})
        ring.append(_FIXED_START + 1, rec)
        rec2 = LogRecord(timestamp=_iso_z(_FIXED_START + 2), body="record-B",
                         attributes={"entity": "app"})
        ring.append(_FIXED_START + 2, rec2)  # evicts rec

        cfg = _make_otlp_config(source_name="clock-src")
        registry = build_registry(cfg)
        adapter = _make_otlp_adapter(ring, now_ts=_FIXED_NOW)

        orig_rings = dict(srv._otlp_rings)
        orig_listening = srv._otlp_listening
        orig_ai = dict(srv._adapter_instances)
        orig_cfg = srv._lumino_config
        orig_reg = srv._source_registry

        srv._otlp_rings = {"clock-src": ring}
        srv._otlp_listening = False
        srv._adapter_instances = {"clock-src": adapter}
        srv._lumino_config = cfg
        srv._source_registry = registry
        try:
            result = await srv.smart_summarize_pod_logs(
                namespace="ns",
                pod_name="no-match",   # empty result → retention fires
                source="clock-src",
                # unbounded (no time args) → requested_window = ("unbounded", now)
            )
        finally:
            self._teardown(srv, orig_rings, orig_listening, orig_ai, orig_cfg, orig_reg)

        assert result.get("outside_retention") is True, (
            f"Expected retention to fire; got: {result!r}"
        )
        # Exact strings — injected clock, not wall clock
        assert result["requested_window"] == ["unbounded", "1970-01-12T13:46:40Z"], (
            f"requested_window mismatch (injected-clock R6 discipline): "
            f"{result['requested_window']!r}"
        )


# ─── V6 requested_window rendering rule ──────────────────────────────────────

class TestV6WindowRendering:
    """Requested_window 2-tuple rendering per V6 pinned rule."""

    def test_iso_z_renders_datetime(self):
        """iso_z on a tz-aware datetime returns ISO-Z string."""
        from adapters.otlp.rings import iso_z
        from datetime import datetime, timezone
        dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert iso_z(dt) == "2026-01-01T00:00:00Z"


# ─── Skew-note unreachability on unbounded windows ───────────────────────────

@pytest.mark.asyncio
async def test_skew_note_absent_on_unbounded_fetch():
    """MINOR 9: unbounded fetch over a ring containing a skewed record → NO skew note.

    Behavioral pin: creates a ring with a skewed record (sender timestamp far
    earlier than receiver timestamp), fetches with an unbounded window, and
    asserts provenance.notes is empty.  No inspect.getsource — the behaviour
    itself is verified.
    """
    from adapters.otlp.logs import OtlpLogSource
    from adapters.otlp.rings import LogRing, iso_z
    from core.signals import LogRecord
    from core.selector import TimeWindow

    _RECV_TS = 1_735_689_610.0          # 2025-01-01T00:00:10Z (receiver time)
    _SKEWED_SENDER_TS = _RECV_TS - 3700  # >1 h before receiver → skew if window-active

    ring = LogRing(capacity=10, now_fn=lambda: _RECV_TS, start_ts=_RECV_TS - 100)
    # Record whose timestamp is skewed relative to receiver
    rec = LogRecord(
        timestamp=iso_z(_SKEWED_SENDER_TS),
        body="skewed log line",
        attributes={"entity": "app"},
    )
    ring.append(_RECV_TS, rec)   # appended at recv time but sender claims early ts

    opts = {"ring_capacity": 10, "max_body_bytes": 65536,
            "max_record_bytes": 65536, "signals": ["logs"]}
    adapter = OtlpLogSource(ring, opts, now_fn=lambda: _RECV_TS)

    from core.selector import Entity, Limit
    batch = await adapter.fetch_logs(
        Entity(name_or_pattern="app"),
        window=None,           # unbounded → active_window is False
        limit=Limit(max_records=100, max_bytes=None),
    )

    # Skew note must NOT appear — unbounded window disables the skew check
    assert len(batch.provenance.notes) == 0, (
        f"Expected no skew note for unbounded fetch; got: {batch.provenance.notes!r}"
    )
    assert len(batch.records) == 1   # record IS returned (skew doesn't drop records)
