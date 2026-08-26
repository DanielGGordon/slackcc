"""Resolve shared paths + the Slack token, independent of the current working
directory. The daemon runs from the slack project root, but the CLI tools may be
invoked by a Claude session running in *another* project, so they can't rely on
relative paths or an exported token. Everything resolves from the installed
package location instead."""

from __future__ import annotations

import json
import os
from pathlib import Path

# .../projects/slack/src/slackcc/paths.py -> .../projects/slack
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("SLACKCC_STATE_DIR", str(PROJECT_ROOT / ".state")))
DOTENV = Path(os.environ.get("SLACKCC_DOTENV", str(PROJECT_ROOT / ".env")))
# Same env var the daemon reads (config.load_settings), but anchored absolutely:
# a CLI is invoked from whatever project the agent is working in, so the
# daemon's "./config/channels.json" default would miss.
CHANNELS_CONFIG = Path(
    os.environ.get("SLACKCC_CONFIG", str(PROJECT_ROOT / "config" / "channels.json"))
)


def claims_path() -> Path:
    return STATE_DIR / "claimed.json"


def channel_cwd(channel_id: str) -> Path | None:
    """The project directory a channel routes to, read straight from the
    routing map -- or None if the channel isn't configured (or the file is
    unreadable). The CLIs need it to put inbound files exactly where the daemon
    would, and they have no Settings: no tokens, no env, no bolt app."""
    try:
        raw = json.loads(CHANNELS_CONFIG.read_text())
    except (OSError, ValueError):
        return None
    spec = (raw.get("channels") or {}).get(channel_id)
    if not isinstance(spec, dict) or not spec.get("cwd"):
        return None
    return Path(os.path.expanduser(str(spec["cwd"])))


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
