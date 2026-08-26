"""Download files a user attached in Slack so the agent can read them.

Slack file URLs (`url_private`) require the bot token as a Bearer header and the
`files:read` scope. Downloaded files land in the project's `.slack-incoming/`
so the agent can Read them (Claude Code can read images and PDFs directly;
audio needs a separate transcription step). Both inbound legs use these
helpers: the daemon (`app.py`) for a message it dispatches, and
`slack-wait-reply` for a reply that lands in a thread an agent has claimed --
otherwise an attachment sent mid-loop would reach the agent as text only.

`build_t3_attachment` additionally packages a downloaded image for T3's
`thread.turn.start` `message.attachments`, so it shows up as a real inline
attachment in the T3 GUI (and is handed to the model as image content) instead
of just a file path in the prompt text. T3 has no separate upload endpoint --
the whole image rides along as a base64 data URL in the dispatch payload, so
only formats/sizes its Claude adapter accepts are worth encoding."""

from __future__ import annotations

import base64
import mimetypes
import os
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

# Mirrors T3's PROVIDER_SEND_TURN_MAX_IMAGE_BYTES and the mime types its
# ClaudeAdapter actually forwards to the model (packages/contracts/src/
# orchestration.ts, apps/server/src/provider/Layers/ClaudeAdapter.ts).
MAX_T3_IMAGE_BYTES = 10 * 1024 * 1024
T3_SUPPORTED_IMAGE_MIME_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}


class SlackFileError(RuntimeError):
    """Slack served something that isn't the file."""


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
        served = (resp.headers.get_content_type() or "").lower()
        data = resp.read()
    # A token without `files:read` (or one that can't see this file) doesn't get
    # a 4xx -- Slack answers 200 with its HTML sign-in page. Writing that to
    # disk as `photo.png` would fail much later and much more confusingly.
    if served == "text/html" and (file_obj.get("mimetype") or "").lower() != "text/html":
        raise SlackFileError(
            f"Slack returned an HTML page instead of {dest.name!r} -- "
            "the bot token is probably missing the `files:read` scope"
        )
    dest.write_bytes(data)
    return dest


def incoming_dir(project_dir: Path | str, thread_ts: str) -> Path:
    """Where one thread's inbound attachments live. The daemon and
    `slack-wait-reply` both route through here so files from the same thread
    land in the same place no matter which leg downloaded them."""
    return Path(project_dir) / ".slack-incoming" / str(thread_ts).replace(".", "_")


def download_files(
    file_objs: Iterable[dict],
    dest_dir: Path,
    token: str,
    on_error: Callable[[dict, Exception], None] | None = None,
) -> list[Path]:
    """Download every attachment on a message, skipping the ones that fail --
    one bad file must not cost the agent the rest of the message."""
    out: list[Path] = []
    for fo in file_objs:
        try:
            path = download_slack_file(fo, dest_dir, token)
        except Exception as e:  # noqa: BLE001 - a bad file shouldn't kill the turn
            if on_error is not None:
                on_error(fo, e)
            continue
        if path is not None:
            out.append(path)
    return out


def build_t3_attachment(path: Path) -> dict | None:
    """A downloaded file, packaged as a T3 `message.attachments` entry -- or
    None if it's not an image type/size T3's Claude adapter will accept (it
    still reaches the agent via the local-path note in the prompt text)."""
    mime_type = mimetypes.guess_type(path.name)[0]
    if mime_type not in T3_SUPPORTED_IMAGE_MIME_TYPES:
        return None
    data = path.read_bytes()
    if not data or len(data) > MAX_T3_IMAGE_BYTES:
        return None
    return {
        "type": "image",
        "name": path.name,
        "mimeType": mime_type,
        "sizeBytes": len(data),
        "dataUrl": f"data:{mime_type};base64,{base64.b64encode(data).decode()}",
    }
