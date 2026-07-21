"""Resolve shared paths + the Slack token, independent of the current working
directory. The daemon runs from the slack project root, but the CLI tools may be
invoked by a Claude session running in *another* project, so they can't rely on
relative paths or an exported token. Everything resolves from the installed
package location instead."""

from __future__ import annotations

import os
from pathlib import Path

# .../projects/slack/src/slackcc/paths.py -> .../projects/slack
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("SLACKCC_STATE_DIR", str(PROJECT_ROOT / ".state")))
DOTENV = Path(os.environ.get("SLACKCC_DOTENV", str(PROJECT_ROOT / ".env")))


def claims_path() -> Path:
    return STATE_DIR / "claimed.json"


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def resolve_token(name: str = "SLACK_BOT_TOKEN") -> str | None:
    """Env var wins; otherwise fall back to the slack project's .env so the
    agent doesn't have to manage the token."""
    return os.environ.get(name) or _parse_dotenv(DOTENV).get(name)
