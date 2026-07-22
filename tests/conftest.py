"""Shared test setup: make src/ importable. Tests must be fully offline —
no Slack API, no live T3, no live pps, no daemon state (.state/)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
