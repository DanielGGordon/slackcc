"""Outbound mirror: keeps Slack threads in sync with their T3 threads.

Covers the GUI-side of the bidirectional flow: when the user types into a
Slack-originated thread in the T3 GUI, that turn should surface in Slack too —
the user's message as "*<owner> said to the agent:* ..." and the assistant's
reply as a normal bot post. Slack-originated messages never re-post: the turn
backend ledgers their ids in `MirrorStore` before/right after each turn.

Design constraints:
- Poll-only. T3 has no self-hosted webhook; WS subscribe is a later upgrade.
- Messages are only posted once `streaming` is false AND older than a short
  grace period, closing the race where the mirror sees a completed reply a
  beat before `backend_t3.run_turn` ledgers it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from slack_sdk import WebClient

from .outbound import scrub
from .t3 import MirrorStore, T3Client, T3Error

log = logging.getLogger(__name__)

_POLL_SECS = 5.0
_GRACE_SECS = 10.0
_SLACK_CHUNK = 3800  # Slack rejects messages over ~4k chars


def _age_secs(iso: str) -> float:
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _post(slack: WebClient, channel: str, thread_ts: str, text: str) -> None:
    for i in range(0, len(text), _SLACK_CHUNK):
        slack.chat_postMessage(channel=channel, thread_ts=thread_ts,
                               text=text[i:i + _SLACK_CHUNK])


def _sweep(t3: T3Client, slack: WebClient, mirror: MirrorStore, owner: str) -> None:
    for thread_id, entry in mirror.threads().items():
        try:
            thread = t3.thread_snapshot(thread_id).get("thread", {})
        except T3Error as exc:
            if "thread_not_found" in str(exc):
                # Deleted in the T3 GUI; stop mirroring it forever.
                log.info("mirror: thread %s gone from T3; unregistering", thread_id)
                mirror.remove(thread_id)
            else:
                log.warning("mirror: snapshot failed for %s", thread_id, exc_info=True)
            continue
        # A turn emits one assistant message per text segment between tool
        # calls; only the turn's FINAL message (latestTurn.assistantMessageId)
        # belongs in Slack — intermediate status notes are skipped, unledgered.
        latest = thread.get("latestTurn") or {}
        final_assistant_id = (
            latest.get("assistantMessageId") if latest.get("state") == "completed" else None
        )
        for msg in thread.get("messages", []):
            role = msg.get("role")
            mid = msg.get("id", "")
            if msg.get("streaming") or not mid:
                continue
            if role == "assistant" and mid != final_assistant_id:
                continue
            if role not in ("user", "assistant"):
                continue
            if _age_secs(msg.get("updatedAt", "")) < _GRACE_SECS:
                continue
            if mirror.is_posted(thread_id, mid):
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                # Leave unledgered: the projection may still be filling in.
                continue
            mirror.mark_posted(thread_id, [mid])
            text = scrub(text)[0]
            try:
                if role == "user":
                    _post(slack, entry["channel"], entry["thread_ts"],
                          f"_{owner} said to the agent:_ {text}")
                else:
                    _post(slack, entry["channel"], entry["thread_ts"], text)
            except Exception:  # noqa: BLE001 - one bad post shouldn't kill the loop
                log.warning("mirror: slack post failed for %s", thread_id, exc_info=True)


def start(t3: T3Client, slack: WebClient, mirror: MirrorStore, owner: str) -> threading.Thread:
    def loop() -> None:
        log.info("t3 mirror started (poll %.0fs)", _POLL_SECS)
        while True:
            try:
                _sweep(t3, slack, mirror, owner)
            except Exception:  # noqa: BLE001
                log.exception("mirror sweep crashed; continuing")
            time.sleep(_POLL_SECS)

    thread = threading.Thread(target=loop, name="t3-mirror", daemon=True)
    thread.start()
    return thread
