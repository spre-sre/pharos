"""TDD tests for src/adapters/otlp/rings.py and src/adapters/otlp/config.py.

Step 1 (RED): All tests written before any implementation.

Test coverage:
  (a)  Counted eviction: capacity 3, push 5 → buffered=3, dropped=2, oldest-retained #3
  (b)  M4 seed: stats.dropped_oldest reflects the explicit counter (NOT deduced from capacity)
  (c)  covered_window exact ISO-Z strings under injected clock:
         - zero-drops: uses start_ts
         - post-eviction: uses oldest-retained recv_ts
         - empty-ring-zero-drops: returns (iso(start_ts), iso(now)), NOT None
  (d)  M5 seed: skewed sender timestamps never move covered_window (receiver clock only)
  (e)  Validator: mandatory key absence/zero/negative/non-int → AdapterError naming the key;
       signals:["metrics"] → AdapterError naming 5b; happy path incl. max_record_bytes default
  (f)  M9a reconciliation: 4 threads × 500 appends into capacity-100 + concurrent snapshots
       → no exception, len + dropped == 2000
  (g)  V4: note_truncated accumulates across calls; note_truncated(0) is a no-op
  (h)  Snapshot isolation: snapshot() returns a list; length is frozen after subsequent appends
  (i)  note_truncated negative guard: n <= 0 is a no-op (does not decrement the counter)
  (j)  Capacity validation: LogRing(0) and LogRing(-1) raise ValueError at construction
  (k)  start_ts edge cases: omitted → now_fn(); 0.0 → respected, not falsy-coerced
  (l)  covered_window ceil: fractional recv_ts → lower bound ceils, not floors
  (m)  iso_z public helper: float, tz-aware datetime, naive datetime rejection
"""
from __future__ import annotations

import math
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# These imports are the RED failures — modules don't exist yet.
from adapters.otlp.rings import LogRing, iso_z  # noqa: E402
from adapters.otlp.config import validate_otlp_options  # noqa: E402
from core.errors import AdapterError  # noqa: E402


# ─── helpers ─────────────────────────────────────────────────────────────────

def _iso_z(ts: float) -> str:
    """Reference ISO-Z renderer used to build expected strings in tests."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_record(label: str) -> dict:
    """Minimal stand-in for a LogRecord — any hashable is fine for the ring."""
    return {"body": label}


# Fixed timestamps for deterministic tests.
_T_START = 1_000.0   # 1970-01-01T00:16:40Z
_T_NOW   = 2_000.0   # 1970-01-01T00:33:20Z
_T_RECV  = [1_100.0 + i * 100.0 for i in range(10)]  # 1100, 1200, …, 2000


def _fixed_clock(ts: float):
    """Return a callable that always returns `ts` — injectable now_fn."""
    return lambda: ts


# ─── (a) counted eviction ────────────────────────────────────────────────────

class TestCountedEviction:
    def test_push_more_than_capacity_retains_newest(self):
        ring = LogRing(3, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        for i in range(5):
            ring.append(_T_RECV[i], _make_record(f"rec-{i}"))
        s = ring.stats()
        assert s["buffered"] == 3
        assert s["dropped_oldest"] == 2

    def test_oldest_retained_is_record_3(self):
        ring = LogRing(3, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        records = [_make_record(f"rec-{i}") for i in range(5)]
        for i in range(5):
            ring.append(_T_RECV[i], records[i])
        snap = ring.snapshot()
        assert len(snap) == 3
        # First element in snapshot is the oldest retained (record index 2 → rec-2)
        assert snap[0][1]["body"] == "rec-2"

    def test_capacity_one_always_retains_last(self):
        ring = LogRing(1, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        for i in range(3):
            ring.append(_T_RECV[i], _make_record(f"rec-{i}"))
        snap = ring.snapshot()
        assert len(snap) == 1
        assert snap[0][1]["body"] == "rec-2"
        assert ring.stats()["dropped_oldest"] == 2

    def test_no_eviction_within_capacity(self):
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        for i in range(5):
            ring.append(_T_RECV[i], _make_record(f"rec-{i}"))
        s = ring.stats()
        assert s["buffered"] == 5
        assert s["dropped_oldest"] == 0


# ─── (b) M4 seed — explicit counter ──────────────────────────────────────────

class TestM4Seed:
    def test_dropped_count_from_explicit_counter_not_capacity(self):
        """deque(maxlen=) would give the WRONG dropped count.
        With capacity=3 and 5 pushes, maxlen-deque silently evicts 2 without
        incrementing any counter.  The explicit while-popleft loop IS the
        counter.  This test MUST fail if you swap to deque(maxlen=)."""
        ring = LogRing(3, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        for i in range(5):
            ring.append(_T_RECV[i], _make_record(f"r{i}"))
        s = ring.stats()
        # The ONLY source of truth is the internal counter, not capacity math.
        assert s["dropped_oldest"] == 2, (
            "dropped_oldest must come from the explicit eviction counter; "
            "deque(maxlen=) gives 0 here (no counter), which would fail this."
        )

    def test_counter_increments_per_evicted_record(self):
        """Evict 10 records one at a time; counter must be exactly 10."""
        ring = LogRing(1, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        for i in range(11):
            ring.append(_T_RECV[i % len(_T_RECV)], _make_record(f"r{i}"))
        assert ring.stats()["dropped_oldest"] == 10


# ─── (c) covered_window exact strings ────────────────────────────────────────

class TestCoveredWindow:
    def test_zero_drops_uses_start_ts(self):
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        ring.append(_T_RECV[0], _make_record("r0"))
        cw = ring.covered_window()
        assert cw == (_iso_z(_T_START), _iso_z(_T_NOW))

    def test_post_eviction_uses_oldest_retained_recv_ts(self):
        ring = LogRing(3, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        # Push 5: after eviction, oldest retained is index 2 with _T_RECV[2]
        for i in range(5):
            ring.append(_T_RECV[i], _make_record(f"r{i}"))
        cw = ring.covered_window()
        oldest_recv_ts = _T_RECV[2]
        assert cw == (_iso_z(oldest_recv_ts), _iso_z(_T_NOW))

    def test_empty_ring_zero_drops_returns_start_ts_not_none(self):
        """Critical: an empty ring with no drops MUST return (iso(start_ts), iso(now))
        — NOT None.  The empty-ring-zero-drops case is a valid answer ('receiver
        started at X, nothing pushed yet') and the tools rely on this."""
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        cw = ring.covered_window()
        assert cw is not None
        assert cw == (_iso_z(_T_START), _iso_z(_T_NOW))

    def test_covered_window_exact_iso_format(self):
        """Verify the string format itself: YYYY-MM-DDTHH:MM:SSZ, no microseconds."""
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        cw = ring.covered_window()
        start_str, end_str = cw
        # Must match ISO-8601 basic with trailing Z
        import re
        pat = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        assert re.match(pat, start_str), f"bad format: {start_str!r}"
        assert re.match(pat, end_str), f"bad format: {end_str!r}"

    def test_stats_covered_window_matches_covered_window_method(self):
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        ring.append(_T_RECV[0], _make_record("r0"))
        s = ring.stats()
        cw = ring.covered_window()
        assert s["covered_window"] == cw


# ─── (d) M5 seed — receiver clock, not sender timestamps ─────────────────────

class TestM5Seed:
    def test_skewed_sender_ts_does_not_affect_covered_window(self):
        """covered_window MUST use recv_ts (the first positional arg to append),
        NOT any timestamp field inside the record (sender clock).

        If covered_window used the sender timestamp, injecting a far-future
        sender ts would shift the start; this test proves it doesn't.

        M5 mutation: changing lower from min(recv_ts) to min(sender_ts) makes
        this test FAIL because 'future_sender_ts' would become the window start."""
        future_sender_ts = 9_999_999.0  # far-future sender clock
        recv_ts = _T_RECV[0]           # 1100.0 — the receiver-side clock

        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        # Record carries a "far-future" sender timestamp inside it
        record = {"body": "msg", "sender_ts": future_sender_ts}
        ring.append(recv_ts, record)

        cw = ring.covered_window()
        # Start must be iso(start_ts) because dropped == 0
        assert cw[0] == _iso_z(_T_START)
        # Must NOT be influenced by the sender's future timestamp
        assert cw[0] != _iso_z(future_sender_ts)

    def test_skewed_sender_ts_post_eviction_uses_recv_ts(self):
        """After eviction, covered_window start = iso(oldest retained recv_ts),
        NOT iso(oldest sender ts in any record)."""
        ring = LogRing(2, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        # Push 3 records: recv_ts monotonically increasing, sender_ts wildly different
        recv_timestamps = [_T_RECV[0], _T_RECV[1], _T_RECV[2]]
        for i, recv_ts in enumerate(recv_timestamps):
            record = {"body": f"r{i}", "sender_ts": 99_999.0 - i}  # DECREASING sender ts
            ring.append(recv_ts, record)

        cw = ring.covered_window()
        # After 3 pushes into capacity-2: evicted 1, oldest retained has recv_ts = _T_RECV[1]
        assert cw[0] == _iso_z(_T_RECV[1])


# ─── (e) validator ────────────────────────────────────────────────────────────

class TestValidateOtlpOptions:
    def test_missing_ring_capacity_raises(self):
        with pytest.raises(AdapterError, match="ring_capacity"):
            validate_otlp_options("my-src", {"max_body_bytes": 1024})

    def test_missing_max_body_bytes_raises(self):
        with pytest.raises(AdapterError, match="max_body_bytes"):
            validate_otlp_options("my-src", {"ring_capacity": 100})

    def test_ring_capacity_zero_raises(self):
        with pytest.raises(AdapterError, match="ring_capacity"):
            validate_otlp_options("my-src", {"ring_capacity": 0, "max_body_bytes": 1024})

    def test_ring_capacity_negative_raises(self):
        with pytest.raises(AdapterError, match="ring_capacity"):
            validate_otlp_options("my-src", {"ring_capacity": -1, "max_body_bytes": 1024})

    def test_ring_capacity_non_int_raises(self):
        with pytest.raises(AdapterError, match="ring_capacity"):
            validate_otlp_options("my-src", {"ring_capacity": "100", "max_body_bytes": 1024})

    def test_ring_capacity_bool_raises(self):
        """bool is a subclass of int — must be rejected explicitly."""
        with pytest.raises(AdapterError, match="ring_capacity"):
            validate_otlp_options("my-src", {"ring_capacity": True, "max_body_bytes": 1024})

    def test_max_body_bytes_zero_raises(self):
        with pytest.raises(AdapterError, match="max_body_bytes"):
            validate_otlp_options("my-src", {"ring_capacity": 100, "max_body_bytes": 0})

    def test_max_body_bytes_negative_raises(self):
        with pytest.raises(AdapterError, match="max_body_bytes"):
            validate_otlp_options("my-src", {"ring_capacity": 100, "max_body_bytes": -1})

    def test_max_body_bytes_non_int_raises(self):
        with pytest.raises(AdapterError, match="max_body_bytes"):
            validate_otlp_options("my-src", {"ring_capacity": 100, "max_body_bytes": 1.5})

    def test_max_record_bytes_zero_raises(self):
        with pytest.raises(AdapterError, match="max_record_bytes"):
            validate_otlp_options(
                "my-src",
                {"ring_capacity": 100, "max_body_bytes": 1024, "max_record_bytes": 0},
            )

    def test_max_record_bytes_negative_raises(self):
        with pytest.raises(AdapterError, match="max_record_bytes"):
            validate_otlp_options(
                "my-src",
                {"ring_capacity": 100, "max_body_bytes": 1024, "max_record_bytes": -1},
            )

    def test_signals_metrics_raises_naming_5b(self):
        with pytest.raises(AdapterError, match="5b"):
            validate_otlp_options(
                "my-src",
                {"ring_capacity": 100, "max_body_bytes": 1024, "signals": ["metrics"]},
            )

    def test_signals_traces_raises_naming_5b(self):
        with pytest.raises(AdapterError, match="5b"):
            validate_otlp_options(
                "my-src",
                {"ring_capacity": 100, "max_body_bytes": 1024, "signals": ["traces"]},
            )

    def test_signals_mixed_raises_naming_5b(self):
        with pytest.raises(AdapterError, match="5b"):
            validate_otlp_options(
                "my-src",
                {"ring_capacity": 100, "max_body_bytes": 1024, "signals": ["logs", "metrics"]},
            )

    def test_happy_path_no_signals(self):
        result = validate_otlp_options(
            "my-src",
            {"ring_capacity": 100, "max_body_bytes": 1_048_576},
        )
        assert result["ring_capacity"] == 100
        assert result["max_body_bytes"] == 1_048_576
        assert result["max_record_bytes"] == 65_536  # default
        assert result["signals"] == ["logs"]

    def test_happy_path_explicit_logs_signal(self):
        result = validate_otlp_options(
            "my-src",
            {"ring_capacity": 50, "max_body_bytes": 512, "signals": ["logs"]},
        )
        assert result["signals"] == ["logs"]

    def test_happy_path_explicit_max_record_bytes(self):
        result = validate_otlp_options(
            "my-src",
            {"ring_capacity": 50, "max_body_bytes": 512, "max_record_bytes": 32_768},
        )
        assert result["max_record_bytes"] == 32_768

    def test_error_message_includes_source_name(self):
        with pytest.raises(AdapterError, match="special-source-name"):
            validate_otlp_options("special-source-name", {})


# ─── (f) M9a reconciliation (thread safety) ──────────────────────────────────

class TestM9aReconciliation:
    def test_concurrent_appends_no_exception(self):
        """4 threads × 500 appends into capacity-100 + concurrent snapshots
        → no exception, and len(snapshot) + dropped == 2000."""
        ring = LogRing(100, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        errors: list[Exception] = []

        def append_worker(thread_id: int) -> None:
            try:
                for i in range(500):
                    ring.append(_T_RECV[0], _make_record(f"t{thread_id}-{i}"))
            except Exception as exc:
                errors.append(exc)

        def snapshot_worker() -> None:
            try:
                for _ in range(100):
                    ring.snapshot()
            except Exception as exc:
                errors.append(exc)

        threads = []
        for t in range(4):
            threads.append(threading.Thread(target=append_worker, args=(t,)))
        threads.append(threading.Thread(target=snapshot_worker))

        for thr in threads:
            thr.start()
        for thr in threads:
            thr.join()

        assert not errors, f"thread safety errors: {errors}"

        s = ring.stats()
        total = s["buffered"] + s["dropped_oldest"]
        assert total == 2000, (
            f"Expected buffered + dropped == 2000; got buffered={s['buffered']}, "
            f"dropped={s['dropped_oldest']}, total={total}"
        )


# ─── (g) V4: note_truncated ──────────────────────────────────────────────────

class TestNoteTruncated:
    def test_accumulates_across_calls(self):
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        ring.note_truncated(3)
        ring.note_truncated(7)
        ring.note_truncated(1)
        assert ring.stats()["truncated_records"] == 11

    def test_zero_is_noop(self):
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        ring.note_truncated(5)
        ring.note_truncated(0)
        ring.note_truncated(0)
        assert ring.stats()["truncated_records"] == 5

    def test_initial_truncated_is_zero(self):
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        assert ring.stats()["truncated_records"] == 0

    def test_truncated_is_independent_of_dropped_oldest(self):
        """truncated_records and dropped_oldest are separate counters."""
        ring = LogRing(2, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        ring.append(_T_RECV[0], _make_record("r0"))
        ring.append(_T_RECV[1], _make_record("r1"))
        ring.append(_T_RECV[2], _make_record("r2"))  # evicts r0
        ring.note_truncated(4)
        s = ring.stats()
        assert s["dropped_oldest"] == 1
        assert s["truncated_records"] == 4


# ─── (h) snapshot isolation ───────────────────────────────────────────────────

class TestSnapshotIsolation:
    def test_snapshot_returns_list(self):
        """snapshot() must return a plain list, not a deque or other sequence.

        The list type is a binding contract: callers depend on slicing,
        json-serialisability, and the guarantee that the object is detached
        from the ring's internal buffer.
        """
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        ring.append(_T_RECV[0], _make_record("r0"))
        snap = ring.snapshot()
        assert isinstance(snap, list), (
            f"snapshot() must return list, got {type(snap).__name__}; "
            "returning self._buf (no copy) would give collections.deque"
        )

    def test_snapshot_is_isolated_from_subsequent_append(self):
        """A snapshot taken before an append must not change length after the append.

        Kill test for the 'return self._buf' (no-copy) mutant: a deque
        reference grows as the ring is appended to; a list copy does not.
        If this test fails with the 'return self._buf' mutant applied, the
        concurrent ``RuntimeError: deque mutated during iteration`` is the
        production failure mode being guarded against.
        """
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        ring.append(_T_RECV[0], _make_record("r0"))
        snap = ring.snapshot()
        length_before = len(snap)
        # Append AFTER taking the snapshot — must not affect the snapshot.
        ring.append(_T_RECV[1], _make_record("r1"))
        assert len(snap) == length_before, (
            f"snapshot length changed from {length_before} to {len(snap)} after "
            "a subsequent append; snapshot() must return an isolated copy"
        )


# ─── (i) note_truncated negative guard ───────────────────────────────────────

class TestNoteTruncatedNegativeGuard:
    def test_negative_n_is_noop(self):
        """note_truncated(-n) must not decrement the honesty counter.

        The guard was formerly 'if n == 0: return', which let negative values
        through and drove the counter to -94 in the review scenario.  The
        fix is 'if n <= 0: return'.
        """
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        ring.note_truncated(5)
        ring.note_truncated(-100)
        assert ring.stats()["truncated_records"] == 5, (
            "note_truncated(-100) changed the counter; n <= 0 must be a no-op"
        )


# ─── (j) capacity validation at construction ─────────────────────────────────

class TestCapacityValidation:
    def test_capacity_zero_raises_at_construction(self):
        """LogRing(0) must raise ValueError at construction.

        Without this guard the first append raises IndexError inside the
        eviction loop — an unhelpful error that T3's catch-all would
        misclassify as a client 400.
        """
        with pytest.raises(ValueError):
            LogRing(0, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)

    def test_capacity_negative_raises_at_construction(self):
        with pytest.raises(ValueError):
            LogRing(-1, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)


# ─── (k) start_ts construction edge cases ────────────────────────────────────

class TestStartTsConstruction:
    def test_start_ts_omitted_uses_now_fn(self):
        """When start_ts is not passed, the ring must use now_fn() at construction."""
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW))
        cw = ring.covered_window()
        # Empty ring, no drops: start = ceil(start_ts) = ceil(_T_NOW) = _T_NOW (whole sec).
        assert cw[0] == _iso_z(_T_NOW)

    def test_start_ts_zero_is_respected_not_falsy_coerced(self):
        """start_ts=0.0 (Unix epoch) must be stored as-is.

        A falsy coercion ('start_ts or now_fn()') would replace 0.0 with
        now_fn() — a silent bug.  The implementation uses 'is not None'.
        """
        ring = LogRing(10, now_fn=_fixed_clock(_T_NOW), start_ts=0.0)
        cw = ring.covered_window()
        # ceil(0.0) == 0 → "1970-01-01T00:00:00Z"
        assert cw[0] == _iso_z(0)


# ─── (l) covered_window ceil lower bound ─────────────────────────────────────

class TestCoveredWindowCeil:
    def test_fractional_recv_ts_ceils_lower_bound_post_eviction(self):
        """Post-eviction: lower bound must ceil to the next second, not floor.

        recv_ts=501.9 as oldest-retained → lower bound is "1970-01-01T00:08:22Z"
        (502 s), NOT "1970-01-01T00:08:21Z" (501 s, the old floor behaviour).

        Flooring overclaims coverage; ceiling is honest.
        """
        ring = LogRing(3, now_fn=_fixed_clock(_T_NOW), start_ts=_T_START)
        # Push 4: r0 gets evicted; r1 (recv_ts=501.9) becomes oldest retained.
        ring.append(499.0, _make_record("r0"))
        ring.append(501.9, _make_record("r1"))  # will be oldest retained
        ring.append(600.0, _make_record("r2"))
        ring.append(700.0, _make_record("r3"))

        cw = ring.covered_window()
        # math.ceil(501.9) == 502; datetime(502s UTC) == "1970-01-01T00:08:22Z"
        # floor would give "1970-01-01T00:08:21Z" — the old wrong answer
        expected_lower = "1970-01-01T00:08:22Z"
        assert cw[0] == expected_lower, (
            f"expected ceil'd lower bound {expected_lower!r}, got {cw[0]!r}; "
            "floor (old behaviour) would give '1970-01-01T00:08:21Z'"
        )


# ─── (m) iso_z public helper ─────────────────────────────────────────────────

class TestIsoZPublicHelper:
    def test_float_renders_utc_iso(self):
        """Basic float → whole-second ISO string."""
        assert iso_z(1_000.0) == "1970-01-01T00:16:40Z"

    def test_integer_timestamp_renders_correctly(self):
        """int (e.g. from math.ceil) is accepted the same as float."""
        assert iso_z(1_000) == "1970-01-01T00:16:40Z"

    def test_tz_aware_utc_datetime_is_accepted(self):
        dt = datetime(1970, 1, 1, 0, 16, 40, tzinfo=timezone.utc)
        assert iso_z(dt) == "1970-01-01T00:16:40Z"

    def test_tz_aware_non_utc_datetime_converts_to_utc(self):
        """A tz-aware datetime in a non-UTC zone must be converted to UTC before
        formatting — not just formatted in place."""
        from datetime import timedelta
        eastern = timezone(timedelta(hours=-5))
        # 1970-01-01T05:16:40-05:00 == 1970-01-01T10:16:40Z
        dt = datetime(1970, 1, 1, 5, 16, 40, tzinfo=eastern)
        assert iso_z(dt) == "1970-01-01T10:16:40Z"

    def test_naive_datetime_raises_value_error(self):
        """Naive datetime carries no timezone info; accepting it silently would
        be a silent UTC assumption bug."""
        dt = datetime(1970, 1, 1, 0, 16, 40)  # no tzinfo
        with pytest.raises(ValueError, match="naive datetime"):
            iso_z(dt)


class TestCoveredWindowClampAndCeilDiscipline:
    """Re-round pins: no inverted window on sub-second-old rings; ceil applies
    to the LOWER bound only (a ceil'd upper bound overstates coverage end)."""

    def test_subsecond_old_ring_never_inverts(self):
        from adapters.otlp.rings import LogRing
        # ring 0.22s old: ceil(start)=1001 would land AFTER floor(now)=1000
        ring = LogRing(capacity=4, now_fn=lambda: 1000.95, start_ts=1000.73)
        start, end = ring.covered_window()
        assert start <= end, f"inverted window: {start} > {end}"
        assert start == "1970-01-01T00:16:40Z"  # clamped to floor(now)=1000
        stats = ring.stats()
        assert stats["covered_window"][0] <= stats["covered_window"][1]

    def test_ceil_applies_to_lower_bound_only(self):
        from adapters.otlp.rings import LogRing
        # fractional now: a ceil'd UPPER bound would render 1001, floor renders 1000
        ring = LogRing(capacity=4, now_fn=lambda: 1000.9, start_ts=500.0)
        _start, end = ring.covered_window()
        assert end == "1970-01-01T00:16:40Z"  # floor(1000.9) — NOT 00:16:41Z
