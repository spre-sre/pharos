import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "src"))

from characterization.cases import SAMPLE_LOG
from characterization.golden_utils import normalize

from engines.pattern_scan import scan


def test_scan_matches_frozen_golden():
    golden = json.loads(
        (REPO / "tests/characterization/golden/analyze_logs.json").read_text())
    result = normalize(json.loads(json.dumps(scan(SAMPLE_LOG), default=str)))
    assert result == golden
