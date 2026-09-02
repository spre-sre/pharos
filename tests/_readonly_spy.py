"""Recording spy proving reads go THROUGH a ReadOnlyCoreV1 (round-1 finding 1).

type(fn.__self__) capture CANNOT work: ReadOnlyCoreV1.__getattr__ returns the
raw client's bound method, so __self__ is the raw api. This subclass records at
the wrapper boundary. Monkeypatch the module-local ReadOnlyCoreV1 name
(helpers.utils.ReadOnlyCoreV1 / server.ReadOnlyCoreV1) to make_spy(record)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from core.readonly_client import ReadOnlyCoreV1


def make_spy(record):
    class RecordingReadOnly(ReadOnlyCoreV1):
        # classmethod wrap() builds cls(...), so spy-of-spy stays a spy
        def __getattr__(self, name):
            attr = super().__getattr__(name)  # raises WriteOperationError on writes
            record.append(name)               # only read verbs reach here
            return attr
    return RecordingReadOnly
