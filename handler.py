"""Gateway hook entrypoint for Hermes Claude CLI mode."""

from __future__ import annotations

import sys
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

from hermes_adapter.exports import _export_to

_export_to(globals())
