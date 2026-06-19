"""Shared pytest configuration.

Runs Qt headless and makes the repo root importable so `core`, `measurements`,
etc. resolve regardless of where pytest is invoked from.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
