import json
import os
import re
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "golden"

# Volatile field names masked wherever they appear (case-insensitive substring).
# NOTE: "age" is intentionally absent — substring matching would wrongly mask
# "message", "image", "storage", "usage", "average"; use exact matching below.
VOLATILE_KEYS = (
    "timestamp", "time", "duration", "started", "completed",
    "created", "finished", "last_seen", "first_seen", "expiry", "expires",
    "elapsed", "uptime", "generated_at", "analysis_id", "report_id",
    "peak_hour",
)
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?")


# uuid-hex fragments (sim-<hex>-...). Requires >=1 a-f letter so pure decimal
# runs (epochs, byte counts) stay UNMASKED - those are exactly the values the
# phase-1 regression oracle must be able to see drift in.
HEX_ID_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b")


def _is_volatile_key(key: str) -> bool:
    k = key.lower()
    # k == "id" / *_id covers simulation_id, analysis_id, trace ids, etc.
    # (deliberately NOT a substring match: "incident", "provider" are stable)
    # k == "age" / *_age / age_* uses exact/prefix/suffix match, not substring,
    # so "message", "image", "storage", "usage", "average" stay unmasked.
    age_volatile = k == "age" or k.endswith("_age") or k.startswith("age_")
    return k == "id" or k.endswith("_id") or age_volatile or any(v in k for v in VOLATILE_KEYS)


def normalize(obj, key: str = ""):
    if isinstance(obj, dict):
        return {k: ("<VOLATILE>" if _is_volatile_key(k) else normalize(v, k))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize(v, key) for v in obj]
    if isinstance(obj, str):
        if ISO_RE.search(obj):
            obj = ISO_RE.sub("<TS>", obj)
        if HEX_ID_RE.search(obj):
            obj = HEX_ID_RE.sub("<HEX>", obj)
        return obj
    if isinstance(obj, float):
        return round(obj, 6)
    return obj


def assert_matches_golden(tool_name: str, payload) -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    path = GOLDEN_DIR / f"{tool_name}.json"
    normalized = normalize(json.loads(json.dumps(payload, default=str)))
    if os.environ.get("UPDATE_GOLDENS") == "1":
        path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
        return
    assert path.exists(), (
        f"golden missing for {tool_name}; run UPDATE_GOLDENS=1 pytest ... "
        f"then INSPECT the file before committing"
    )
    expected = json.loads(path.read_text())
    assert normalized == expected, f"behavior drift in {tool_name}"
