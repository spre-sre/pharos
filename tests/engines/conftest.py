"""
Ensure src/ is in sys.path before engine test modules are imported.

Without this, pytest resolves ``engines`` against tests/engines/ (the test package
directory) instead of src/engines/ (the source package), causing a
ModuleNotFoundError on collection.  The test files themselves also call
sys.path.insert, but the conftest runs first and this guarantees the right
package takes priority even in multi-pass collection.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
