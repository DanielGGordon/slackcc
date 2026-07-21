"""Download files a user attached in Slack so the agent can read them.

Slack file URLs (`url_private`) require the bot token as a Bearer header and the
`files:read` scope. Downloaded files land in the project's `.slack-incoming/`
so the agent can Read them (Claude Code can read images and PDFs directly;
audio needs a separate transcription step)."""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path


def _safe_name(file_obj: dict) -> str:
    name = file_obj.get("name") or file_obj.get("id") or "file"
    # basename guards against path traversal in the Slack-provided filename
    return os.path.basename(str(name)).replace("\x00", "") or "file"


def download_slack_file(file_obj: dict, dest_dir: Path, token: str) -> Path | None:
    url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not url:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_name(file_obj)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - trusted Slack URL
        dest.write_bytes(resp.read())
    return dest
