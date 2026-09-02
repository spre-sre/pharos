"""TDD tests pinning OtlpLogSource's full semantic surface (phase 5, Task 2).

Mutants targeted (reviewer-identified survivors):
  M-tail   head-N swap: records[:N] instead of records[-N:]
  M-unb    "unbounded"→"BOGUS" in requested_window[0]
  M-end    iso_z(now) dropped from requested_window[1]
  M-nat    Native selector branch deleted → no SelectorNotSupported
  M-udn    undated note omitted
  M-trunc  ring truncation note omitted
  M-skew   skew note omitted
  M-wfilt  window filtering disabled (include=True always)
  M-match  Matchers filtering deleted (all records pass)

Also pins:
  covered_window verbatim from ring.covered_window()
  Entity fnmatch globbing (pattern, not equality)
  Provenance.truncated set True on tail-N cut (MINOR 3 fix)
"""
from __future__ import annotations

import asyncio
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from adapters.otlp.logs import OtlpLogSource  # noqa: E402
from adapters.otlp.rings import LogRing, iso_z  # noqa: E402
from core.selector import Entity, Limit, Matchers, Native, TimeWindow  # noqa: E402
from core.selector import SelectorNotSupported  # noqa: E402
from core.signals import LogRecord  # noqa: E402


# ─── helpers ─────────────────────────────────────────────────────────────────

def _run(coro):
    """Run an async coroutine synchronously (uses a fresh event loop each call)."""
    return asyncio.run(coro)


def _iso_z(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt(ts: float) -> datetime:
    """POSIX float → tz-aware datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _rec(body: str, *, ts: Optional[str] = None, entity: str = "pod-a", **attrs) -> LogRecord:
    return LogRecord(
        timestamp=ts,
        body=body,
        severity=None,
        attributes={"entity": entity, **attrs},
    )


# Fixed clock for deterministic tests.
_NOW_TS = 2_000.0       # 1970-01-01T00:33:20Z
_START_TS = 1_000.0     # 1970-01-01T00:16:40Z

# Five recv timestamps spaced 10 s apart (oldest first).
_RECV = [1_100.0 + i * 10.0 for i in range(5)]

# Five sender timestamps: T+0 … T+4 seconds from a "sender epoch" far from now.
_SENDER_BASE = 1_500.0  # 1970-01-01T00:25:00Z (inside [start, now])
_SENDER_TS = [_iso_z(_SENDER_BASE + i) for i in range(5)]


def _make_ring_5(*, capacity: int = 100) -> LogRing:
    """Ring pre-seeded with 5 records (fixed sender ts, ascending recv_ts)."""
    ring = LogRing(capacity=capacity, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
    for i in range(5):
        ring.append(_RECV[i], _rec(f"msg-{i}", ts=_SENDER_TS[i]))
    return ring


def _make_source(ring: LogRing, *, now_ts: float = _NOW_TS) -> OtlpLogSource:
    return OtlpLogSource(ring, {}, now_fn=lambda: now_ts)


# ─── M-tail: tail-N, not head-N ──────────────────────────────────────────────

class TestTailN:
    """5 records, limit 3 → NEWEST 3 in ascending sender-ts order."""

    def test_tail_not_head_returns_newest_three(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(
            Entity("pod-a"), None, Limit(max_records=3)
        ))
        assert len(batch.records) == 3
        # Ascending order preserved — but these are the NEWEST 3 (indices 2,3,4).
        assert batch.records[0].body == "msg-2"
        assert batch.records[1].body == "msg-3"
        assert batch.records[2].body == "msg-4"

    def test_head_mutant_would_return_oldest(self):
        """Whitebox: if head-N were used, the first record would be msg-0."""
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(
            Entity("pod-a"), None, Limit(max_records=3)
        ))
        # Under the head-N mutant records[0].body would be "msg-0" — verify
        # the real implementation does NOT return msg-0 first.
        assert batch.records[0].body != "msg-0", (
            "tail-N bug: got oldest record first (head-N behaviour)"
        )

    def test_no_limit_returns_all_five(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        assert len(batch.records) == 5

    def test_limit_equal_to_count_returns_all(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(
            Entity("pod-a"), None, Limit(max_records=5)
        ))
        assert len(batch.records) == 5

    def test_provenance_truncated_true_when_tail_drops(self):
        """MINOR 3: Provenance.truncated must be True when tail-N drops records."""
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(
            Entity("pod-a"), None, Limit(max_records=3)
        ))
        assert batch.provenance.truncated is True, (
            "Provenance.truncated must be True when tail-N dropped 2 records"
        )

    def test_provenance_truncated_false_when_no_cut(self):
        """Provenance.truncated stays False when limit >= record count."""
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(
            Entity("pod-a"), None, Limit(max_records=10)
        ))
        assert batch.provenance.truncated is False


# ─── M-unb: "unbounded" in requested_window[0] ───────────────────────────────

class TestUnboundedWindow:
    """No time params → requested_window[0] == "unbounded" EXACTLY."""

    def test_no_window_yields_unbounded_start(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        assert batch.provenance.requested_window[0] == "unbounded", (
            f"got {batch.provenance.requested_window[0]!r}, expected 'unbounded'"
        )

    def test_empty_window_obj_yields_unbounded_start(self):
        """TimeWindow() with no bounds is equivalent to no window."""
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(), None))
        assert batch.provenance.requested_window[0] == "unbounded"

    def test_window_with_only_start_no_longer_unbounded(self):
        """If start is provided, requested_window[0] must NOT be 'unbounded'."""
        ring = _make_ring_5()
        src = _make_source(ring)
        w_start = _dt(_START_TS)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(start=w_start), None))
        assert batch.provenance.requested_window[0] != "unbounded"
        assert batch.provenance.requested_window[0] == _iso_z(_START_TS)


# ─── M-end: iso_z(now) in requested_window[1] ────────────────────────────────

class TestRequestedWindowEnd:
    """No end param → requested_window[1] == iso_z(injected now) EXACTLY."""

    def test_no_end_uses_now_fn(self):
        ring = _make_ring_5()
        src = _make_source(ring, now_ts=_NOW_TS)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        expected_end = _iso_z(_NOW_TS)
        assert batch.provenance.requested_window[1] == expected_end, (
            f"got {batch.provenance.requested_window[1]!r}, expected {expected_end!r}"
        )

    def test_explicit_end_uses_that_end(self):
        ring = _make_ring_5()
        src = _make_source(ring, now_ts=_NOW_TS)
        end_ts = 1_800.0
        w_end = _dt(end_ts)
        batch = _run(src.fetch_logs(
            Entity("pod-a"), TimeWindow(end=w_end), None
        ))
        assert batch.provenance.requested_window[1] == _iso_z(end_ts)

    def test_injected_clock_determines_end(self):
        """End rendering must use the injected now_fn, not wall time."""
        ring = _make_ring_5()
        # Set now_ts to a clearly non-wall-time value.
        fixed_now = 42_000.0
        src = _make_source(ring, now_ts=fixed_now)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        expected = _iso_z(fixed_now)
        assert batch.provenance.requested_window[1] == expected, (
            f"expected injected clock {expected!r}, got {batch.provenance.requested_window[1]!r}"
        )


# ─── M-nat: Native selector → SelectorNotSupported ───────────────────────────

class TestNativeSelector:
    def test_native_raises_selector_not_supported(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        with pytest.raises(SelectorNotSupported):
            _run(src.fetch_logs(Native("some PromQL"), None, None))

    def test_native_error_names_entity_and_matchers(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        with pytest.raises(SelectorNotSupported) as exc_info:
            _run(src.fetch_logs(Native("q"), None, None))
        msg = str(exc_info.value)
        assert "Entity" in msg
        assert "Matchers" in msg


# ─── M-udn: undated note ─────────────────────────────────────────────────────

class TestUndatedNote:
    """timestamp=None record → undated note must appear (active window only)."""

    def _active_window(self) -> TimeWindow:
        return TimeWindow(start=_dt(_START_TS))

    def test_timestamp_none_adds_note_in_active_window(self):
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        ring.append(_RECV[0], _rec("null-ts-msg", ts=None))
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), self._active_window(), None))
        notes = " ".join(batch.provenance.notes)
        assert "undated records kept without time filtering" in notes, (
            f"undated note missing; got notes={batch.provenance.notes!r}"
        )

    def test_undated_note_only_once_for_multiple_null_ts_records(self):
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        for i in range(3):
            ring.append(_RECV[i], _rec(f"null-{i}", ts=None))
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), self._active_window(), None))
        note_count = sum(
            1 for n in batch.provenance.notes
            if "undated records kept without time filtering" in n
        )
        assert note_count == 1

    def test_no_undated_note_without_active_window(self):
        """With no time bounds, undated records pass silently — no note added."""
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        ring.append(_RECV[0], _rec("null-ts", ts=None))
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        notes_str = " ".join(batch.provenance.notes)
        assert "undated" not in notes_str

    def test_unparseable_timestamp_kept_and_noted_in_active_window(self):
        """timestamp='not-a-date' in active window → record KEPT + undated note."""
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        ring.append(_RECV[0], _rec("unparseable-ts-msg", ts="not-a-date"))
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), self._active_window(), None))
        assert len(batch.records) == 1, "unparseable-timestamp record must be KEPT"
        notes = " ".join(batch.provenance.notes)
        assert "undated records kept without time filtering" in notes, (
            f"undated note missing for unparseable timestamp; notes={batch.provenance.notes!r}"
        )

    def test_unparseable_timestamp_kept_no_note_without_window(self):
        """timestamp='not-a-date' without active window → kept silently (no note)."""
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        ring.append(_RECV[0], _rec("unparseable-no-window", ts="not-a-date"))
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        assert len(batch.records) == 1, "unparseable-timestamp record must be KEPT"
        notes_str = " ".join(batch.provenance.notes)
        assert "undated" not in notes_str


# ─── M-trunc: ring truncation note ───────────────────────────────────────────

class TestTruncatedNote:
    """ring.note_truncated(3) → note with '3 record(s) truncated since receiver start'."""

    def test_note_truncated_appears(self):
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        ring.append(_RECV[0], _rec("msg", ts=_SENDER_TS[0]))
        ring.note_truncated(3)
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        notes_str = " ".join(batch.provenance.notes)
        assert "3 record(s) truncated since receiver start" in notes_str, (
            f"truncation note missing; got notes={batch.provenance.notes!r}"
        )

    def test_no_truncation_note_when_zero(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        notes_str = " ".join(batch.provenance.notes)
        assert "truncated since receiver start" not in notes_str

    def test_truncation_count_is_exact(self):
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        ring.append(_RECV[0], _rec("msg", ts=_SENDER_TS[0]))
        ring.note_truncated(7)
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        notes_str = " ".join(batch.provenance.notes)
        assert "7 record(s) truncated since receiver start" in notes_str


# ─── M-skew: skew note ───────────────────────────────────────────────────────

class TestSkewNote:
    """Record inside requested window but outside [covered.start, now] → skew note."""

    def test_skew_note_on_future_sender_ts(self):
        # Ring: start_ts=_START_TS, now=_NOW_TS.
        # A record whose sender ts is _NOW_TS + 100 (future → outside [covered.start, now]).
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        future_ts = _iso_z(_NOW_TS + 100)
        ring.append(_RECV[0], _rec("future-msg", ts=future_ts))
        src = _make_source(ring)
        # Explicitly request a wide window that includes the future_ts,
        # so the record is NOT window-filtered out (it passes window but triggers skew).
        w_start = _dt(_START_TS)
        w_end = _dt(_NOW_TS + 200)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(start=w_start, end=w_end), None))
        notes_str = " ".join(batch.provenance.notes)
        assert "skew" in notes_str, (
            f"skew note missing; got notes={batch.provenance.notes!r}"
        )

    def test_skew_note_contains_only_timestamps(self):
        """Skew note must not embed any attacker-controlled record content."""
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        future_ts = _iso_z(_NOW_TS + 100)
        ring.append(_RECV[0], _rec("ATTACKER_BODY", ts=future_ts))
        src = _make_source(ring)
        w_start = _dt(_START_TS)
        w_end = _dt(_NOW_TS + 200)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(start=w_start, end=w_end), None))
        for note in batch.provenance.notes:
            if "skew" in note:
                assert "ATTACKER_BODY" not in note, (
                    "skew note embeds attacker-controlled record body"
                )

    def test_skew_note_only_once_for_multiple_skewed_records(self):
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        for i in range(3):
            future_ts = _iso_z(_NOW_TS + 100 + i)
            ring.append(_RECV[i], _rec(f"future-{i}", ts=future_ts))
        src = _make_source(ring)
        w_start = _dt(_START_TS)
        w_end = _dt(_NOW_TS + 200)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(start=w_start, end=w_end), None))
        skew_notes = [n for n in batch.provenance.notes if "skew" in n]
        assert len(skew_notes) == 1, f"expected exactly 1 skew note, got {skew_notes!r}"

    def test_no_skew_note_without_active_window(self):
        """Skew detection requires an active window; unbounded fetch → no skew note."""
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        future_ts = _iso_z(_NOW_TS + 100)
        ring.append(_RECV[0], _rec("future-msg", ts=future_ts))
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        notes_str = " ".join(batch.provenance.notes)
        assert "skew" not in notes_str


# ─── M-wfilt: window filtering ───────────────────────────────────────────────

class TestWindowFiltering:
    """Explicit window excluding 2 of 5 records → only 3 returned."""

    def test_window_excludes_records_outside_range(self):
        # 5 records with sender timestamps _SENDER_BASE+0 through _SENDER_BASE+4.
        # Window: [_SENDER_BASE+2, _SENDER_BASE+4] → includes records 2,3,4 only.
        ring = _make_ring_5()
        src = _make_source(ring)
        w_start = _dt(_SENDER_BASE + 2)
        w_end = _dt(_SENDER_BASE + 4)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(start=w_start, end=w_end), None))
        assert len(batch.records) == 3, (
            f"window filtering failed: got {len(batch.records)} records, expected 3; "
            f"mutant (filtering disabled) would return 5"
        )
        assert batch.records[0].body == "msg-2"
        assert batch.records[1].body == "msg-3"
        assert batch.records[2].body == "msg-4"

    def test_window_start_excludes_older_records(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        # Only include records 3 and 4 (sender ts _SENDER_BASE+3 and +4).
        w_start = _dt(_SENDER_BASE + 3)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(start=w_start), None))
        assert len(batch.records) == 2
        assert all(r.body in ("msg-3", "msg-4") for r in batch.records)

    def test_window_end_excludes_newer_records(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        w_end = _dt(_SENDER_BASE + 1)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(end=w_end), None))
        assert len(batch.records) == 2
        assert all(r.body in ("msg-0", "msg-1") for r in batch.records)

    def test_mutant_detection_all_five_is_wrong(self):
        """Sanity: if filtering is off, all 5 pass — this test's assertion would fail."""
        ring = _make_ring_5()
        src = _make_source(ring)
        w_start = _dt(_SENDER_BASE + 2)
        w_end = _dt(_SENDER_BASE + 4)
        batch = _run(src.fetch_logs(Entity("pod-a"), TimeWindow(start=w_start, end=w_end), None))
        # 5 would mean window filtering is disabled (the mutant).
        assert len(batch.records) != 5, (
            "window filtering is disabled (mutant detected)"
        )


# ─── M-match: Matchers filtering ─────────────────────────────────────────────

class TestMatchersFiltering:
    def test_matchers_equality_filters_records(self):
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        for i in range(3):
            ring.append(_RECV[i], LogRecord(
                timestamp=_SENDER_TS[i], body=f"msg-{i}", severity=None,
                attributes={"entity": "pod-a", "env": "prod" if i < 2 else "staging"},
            ))
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Matchers({"env": "prod"}), None, None))
        assert len(batch.records) == 2, (
            f"Matchers filter returned {len(batch.records)} records; expected 2 "
            f"(mutant: dropping the filter returns 3)"
        )
        assert all(r.attributes.get("env") == "prod" for r in batch.records)

    def test_matchers_all_terms_must_match(self):
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        ring.append(_RECV[0], LogRecord(
            timestamp=_SENDER_TS[0], body="both", severity=None,
            attributes={"entity": "pod-a", "env": "prod", "tier": "web"},
        ))
        ring.append(_RECV[1], LogRecord(
            timestamp=_SENDER_TS[1], body="one", severity=None,
            attributes={"entity": "pod-a", "env": "prod", "tier": "db"},
        ))
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Matchers({"env": "prod", "tier": "web"}), None, None))
        assert len(batch.records) == 1
        assert batch.records[0].body == "both"

    def test_matchers_no_match_returns_empty(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Matchers({"env": "nonexistent"}), None, None))
        assert len(batch.records) == 0


# ─── Entity fnmatch globbing ──────────────────────────────────────────────────

class TestEntityGlob:
    """Entity selector uses fnmatch — patterns must work, not just equality."""

    def test_glob_star_matches_all(self):
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-*"), None, None))
        assert len(batch.records) == 5

    def test_glob_question_mark(self):
        ring = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=_START_TS)
        ring.append(_RECV[0], _rec("msg-x", entity="pod-x", ts=_SENDER_TS[0]))
        ring.append(_RECV[1], _rec("msg-y", entity="pod-y", ts=_SENDER_TS[1]))
        ring.append(_RECV[2], _rec("other", entity="svc-z", ts=_SENDER_TS[2]))
        src = _make_source(ring)
        # "pod-?" matches "pod-x" and "pod-y" but not "svc-z".
        batch = _run(src.fetch_logs(Entity("pod-?"), None, None))
        assert len(batch.records) == 2, (
            f"glob 'pod-?' should match pod-x and pod-y only; got {len(batch.records)}"
        )

    def test_exact_entity_match(self):
        """Exact name still works (not just patterns)."""
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        assert len(batch.records) == 5

    def test_equality_mutant_would_miss_glob(self):
        """== mutant: if Entity used == instead of fnmatch, "pod-*" would return 0."""
        ring = _make_ring_5()
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-*"), None, None))
        # Under the == mutant, "pod-*" != "pod-a" → 0 records.
        assert len(batch.records) > 0, (
            "Entity selector uses == instead of fnmatch (mutant detected)"
        )


# ─── covered_window verbatim from ring.covered_window() ──────────────────────

class TestCoveredWindow:
    """covered_window in Provenance must match ring.covered_window() exactly."""

    def test_covered_window_exact_match(self):
        ring = _make_ring_5()
        expected_cw = ring.covered_window()  # call BEFORE fetch to snapshot
        src = _make_source(ring)
        batch = _run(src.fetch_logs(Entity("pod-a"), None, None))
        # covered_window may advance by milliseconds, but both are from the
        # same injected clock (_NOW_TS is fixed), so they must be byte-identical.
        assert batch.provenance.covered_window == expected_cw, (
            f"covered_window mismatch: "
            f"got {batch.provenance.covered_window!r}, expected {expected_cw!r}"
        )

    def test_covered_window_uses_ring_not_hardcoded(self):
        """Different start_ts → different covered_window[0]."""
        ring_a = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=1_000.0)
        ring_b = LogRing(capacity=10, now_fn=lambda: _NOW_TS, start_ts=1_500.0)
        src_a = _make_source(ring_a)
        src_b = _make_source(ring_b)
        batch_a = _run(src_a.fetch_logs(Entity("*"), None, None))
        batch_b = _run(src_b.fetch_logs(Entity("*"), None, None))
        assert batch_a.provenance.covered_window[0] != batch_b.provenance.covered_window[0]


# ─── E2E entity-exempt regression: over-budget record stays fetchable ─────────

class TestEntityBudgetExemptE2E:
    """E2E regression for the phase-5 pre-merge MEDIUM finding.

    Without the entity-exempt fix, parse sets record_attrs["entity"] last and
    _enforce_budget drops it first (iteration order) on an over-budget record.
    OtlpLogSource then filters on record.attributes["entity"] → no match →
    the tool returns "No logs found" while the truncation note is silently
    discarded.  This is the §4.2.1 honesty failure.

    After the fix, entity is re-attached AFTER _enforce_budget (capped at 512)
    so the truncated record is always findable by its Entity selector.
    """

    def test_over_budget_record_fetchable_by_entity(self):
        """Seed ring with over-budget record → Entity("victim-pod") returns it."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from adapters.otlp.parse import parse_export_logs_request  # noqa: E402

        max_record_bytes = 32  # far smaller than body — forces budget enforcement

        # Build a minimal OTLP request with a huge body for "victim-pod".
        otlp_body = {
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "k8s.pod.name", "value": {"stringValue": "victim-pod"}},
                    ]
                },
                "scopeLogs": [{
                    "logRecords": [{
                        "body": {"stringValue": "x" * 70000},
                        "timeUnixNano": "1767225600000000000",
                    }]
                }],
            }]
        }

        records, truncated_count = parse_export_logs_request(
            otlp_body, max_record_bytes=max_record_bytes
        )
        assert truncated_count == 1, "huge body must trigger truncation"
        assert len(records) == 1
        # Entity must survive budget enforcement.
        assert records[0].attributes.get("entity") == "victim-pod", (
            "entity dropped by _enforce_budget — exemption not applied"
        )

        # Seed ring with the truncated record.
        now_ts = 2_000.0
        start_ts = 1_000.0
        ring = LogRing(capacity=100, now_fn=lambda: now_ts, start_ts=start_ts)
        ring.append(now_ts - 10, records[0])
        ring.note_truncated(truncated_count)

        # Fetch via OtlpLogSource using an Entity selector.
        src = OtlpLogSource(ring, {}, now_fn=lambda: now_ts)
        batch = _run(src.fetch_logs(Entity("victim-pod"), None, None))

        assert len(batch.records) == 1, (
            "over-budget record must be returned by Entity('victim-pod'); "
            "got 0 records — entity was dropped by budget enforcement "
            "(pre-merge MEDIUM regression)"
        )
        assert len(batch.records[0].body) <= max_record_bytes, (
            "body must be trimmed to max_record_bytes"
        )
        notes_str = " ".join(batch.provenance.notes)
        assert "truncated" in notes_str, (
            f"truncation note missing; got notes={batch.provenance.notes!r}"
        )
