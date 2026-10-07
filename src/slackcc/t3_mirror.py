"""Outbound mirror: keeps Slack threads in sync with their T3 threads.

Covers the GUI-side of the bidirectional flow: when the user types into a
Slack-originated thread in the T3 GUI, that turn should surface in Slack too —
the user's message as "*<owner> said to the agent:* ..." and the assistant's
reply as a normal bot post. Slack-originated messages never re-post: the turn
backend ledgers their ids in `MirrorStore` before/right after each turn.
Only a user-role message T3 marks as human-typed (`t3.typed_by_human`) is
attributed to the owner; T3 also files agent text under role "user" (subagent
reports, agent-to-agent sends), and those are skipped, never posted.

Design constraints:
- Poll-only. T3 has no self-hosted webhook; WS subscribe is a later upgrade.
- Messages are only posted once `streaming` is false AND older than a short
  grace period, closing the race where the mirror sees a completed reply a
  beat before `backend_t3.run_turn` ledgers it.
- A thread deleted in the T3 GUI is a soft delete (`thread.deletedAt`); a
  thread that never existed answers 404 `thread_not_found`. Either way the
  mirror forgets it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from slack_sdk import WebClient

from .outbound import scrub
from .t3 import MirrorStore, T3Client, T3Error, final_reply, typed_by_human

log = logging.getLogger(__name__)

_POLL_SECS = 5.0
_GRACE_SECS = 10.0
_SLACK_CHUNK = 3800  # Slack rejects messages over ~4k chars
_SETTLE_NOTICE_MAX_AGE_SECS = 3600.0
# Only a thread's newest messages are considered. Older ones were handled on
# earlier sweeps, and their ids may have aged out of the ledger (_POSTED_CAP
# in t3.py) -- rescanning them would repost long-delivered history.
_TAIL_MESSAGES = 200


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
            projection = t3.thread_projection(thread_id)
        except T3Error as exc:
            if "thread_not_found" in str(exc):
                log.info("mirror: thread %s gone from T3; unregistering", thread_id)
                mirror.remove(thread_id)
            else:
                log.warning("mirror: snapshot failed for %s", thread_id, exc_info=True)
            continue
        thread = projection.get("thread") or {}
        if thread.get("deletedAt"):
            # Deleted in the T3 GUI (soft delete); stop mirroring it forever.
            log.info("mirror: thread %s deleted in T3; unregistering", thread_id)
            mirror.remove(thread_id)
            continue
        # Only a completed run's final assistant message belongs in Slack;
        # intermediate narration between tool calls is skipped, unledgered.
        finals = set()
        for run in projection.get("runs", []):
            if run.get("status") == "completed":
                reply = final_reply(projection, run.get("id"))
                if reply is not None:
                    finals.add(reply["id"])
        for msg in projection.get("messages", [])[-_TAIL_MESSAGES:]:
            role = msg.get("role")
            mid = msg.get("id", "")
            if msg.get("streaming") or not mid:
                continue
            if role == "assistant" and mid not in finals:
                continue
            if role not in ("user", "assistant"):
                continue
            if _age_secs(msg.get("updatedAt", "")) < _GRACE_SECS:
                continue
            if mirror.is_posted(thread_id, mid):
                continue
            if role == "user" and not typed_by_human(msg):
                # Agent-authored (e.g. a subagent's report relayed into the
                # thread) or of unknown origin: never "<owner> said". Ledgered
                # so the skip is decided and logged once.
                log.info("mirror: skipping non-human user message %s on %s "
                         "(createdBy=%s source=%s)", mid, thread_id,
                         msg.get("createdBy"), msg.get("creationSource"))
                mirror.mark_posted(thread_id, [mid])
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

        # Settled is a T3-side lifecycle flag (see t3.py); surface the transition in
        # Slack so the thread doesn't just go quiet, and tell the user how to undo it.
        settled_at = (thread.get("settledAt") or "settled") \
            if thread.get("settledOverride") == "settled" else None
        previous = entry.get("settled_notice")
        if settled_at != previous:
            mirror.set_settled_notice(thread_id, settled_at)
            # Only the (not settled -> settled) edge is worth a post. A settledAt
            # that shifts while the thread stays settled -- T3 filling the field a
            # beat after the override -- updates the record silently, and so does
            # a settle that is long past (one that predates the mirror seeing it,
            # e.g. surfaced by T3's v1 -> v2 history import).
            if (settled_at and previous is None
                    and _age_secs(thread.get("settledAt") or "") < _SETTLE_NOTICE_MAX_AGE_SECS):
                try:
                    _post(slack, entry["channel"], entry["thread_ts"],
                          f"_{owner} settled this chat. To unsettle this chat, simply reply._")
                except Exception:  # noqa: BLE001 - one bad post shouldn't kill the loop
                    log.warning("mirror: settle notice failed for %s", thread_id, exc_info=True)


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
