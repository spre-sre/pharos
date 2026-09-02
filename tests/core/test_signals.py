import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.signals import LogBatch, LogRecord, Provenance


def test_from_text_round_trip():
    batch = LogBatch.from_text("line1\nline2\n", adapter="kubernetes",
                               query={"namespace": "ns", "pod": "p"})
    assert [r.body for r in batch.records] == ["line1", "line2"]
    assert batch.text == "line1\nline2"
    assert batch.provenance.adapter == "kubernetes"
    assert batch.provenance.query["pod"] == "p"


def test_empty_text_yields_empty_batch():
    batch = LogBatch.from_text("", adapter="inline", query={})
    assert batch.records == []
    assert batch.text == ""


def test_provenance_defaults_and_truncation_flag():
    p = Provenance(adapter="file", query={"path": "/x"})
    assert p.requested_window is None and p.covered_window is None
    assert p.truncated is False and p.notes == ()


def test_log_record_attributes():
    r = LogRecord(timestamp=None, body="hello", attributes={"container": "main"})
    assert r.severity is None and r.attributes["container"] == "main"
