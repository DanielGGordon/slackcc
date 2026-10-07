"""Outbound mirror: keeps Slack threads in sync with their T3 threads.

Covers the GUI-side of the bidirectional flow: when the user types into a
Slack-originated thread in the T3 GUI, that turn should surface in Slack too —
the user's message as "*<owner> said to the agent:* ..." and the assistant's
reply as a normal bot post. Slack-originated messages never re-post: the turn
backend ledgers their ids in `MirrorStore` before/right after each turn.
Only a user-role message T3 marks as human-typed (`t3.typed_by_human`) is
attributed to the owner; T3 also files agent text under role "user" (subagent
reports, agent-to-agent sends), and those are skipped, never posted.

Customer-audience channels (`audience: "customer"`, see customer.py) differ:
Dan's GUI-typed messages are forwarded verbatim as "*Dan:* ..." unless they
start with `#agent` (private: not forwarded, and the run they trigger posts
nothing), and a run's final reply is reduced to its marked customer block.

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

from . import customer
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


def _post(slack: WebClient, channel: str, thread_ts: str, text: str) -> str | None:
    """Post (chunked); returns the Slack ts of the first chunk."""
    first = None
    for i in range(0, len(text), _SLACK_CHUNK):
        resp = slack.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                      text=text[i:i + _SLACK_CHUNK])
        first = first or (resp or {}).get("ts")
    return first


def _customer_delivery(slack: WebClient, mirror: MirrorStore, thread_id: str, entry: dict,
                       msg: dict, text: str, *, owner: str, settings, ledger,
                       private_runs: set[str]) -> None:
    """One eligible message of a customer-audience thread (already checked:
    settled, aged, not yet posted, and user-role ones are human-typed)."""
    mid, chan, ts = msg["id"], entry["channel"], entry["thread_ts"]
    key = customer.thread_key(chan, ts)
    mirror.mark_posted(thread_id, [mid])
    if msg.get("role") == "user":
        if customer.is_private(text):
            log.info("mirror: #agent message %s on %s is private; not forwarded", mid, thread_id)
            return
        text = scrub(text)[0]
        sent = _post(slack, chan, ts, f"*{owner}:* {text}")
        ledger.append(key, "dan_forward", text, slack_ts=sent, t3_message_id=mid)
        return
    if msg.get("runId") in private_runs:
        log.info("mirror: reply %s on %s answers a #agent message; not posted", mid, thread_id)
        return
    outcome = customer.resolve_final(text)
    if outcome.kind == "block":
        block = scrub(outcome.text)[0]
        sent = _post(slack, chan, ts, block)
        ledger.append(key, "block", block, slack_ts=sent, t3_message_id=mid)
        return
    # No usable block. A reply that merely lacks one stays silent in Slack (the
    # customer wasn't waiting on a Slack turn); a technical-looking block gets
    # the same neutral line as a Slack turn. Either way the owners are told once.
    if outcome.kind == "gated":
        sent = _post(slack, chan, ts, customer.HOLDING_LINE)
        ledger.append(key, "holding", customer.HOLDING_LINE, slack_ts=sent)
    customer.alert_owners(slack, settings, channel=chan, thread_ts=ts,
                          reason=outcome.why, raw=text)


_warned_unconfigured: set[str] = set()  # log once per thread, not every sweep


def _sweep(t3: T3Client, slack: WebClient, mirror: MirrorStore, owner: str, *,
           live=None, ledger: customer.Ledger | None = None) -> None:
    """`live` (LiveSettings) says which channels are customer-audience; without
    it every thread keeps the technical behavior."""
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
        settings = live.current if live is not None else None
        cfg = settings.channel(entry["channel"]) if settings is not None else None
        if settings is not None and cfg is None:
            # Live config no longer knows this channel (removed, or reloaded
            # away from customer): we can't tell who reads it, so say nothing.
            # Messages stay unledgered and go out if the channel comes back.
            if thread_id not in _warned_unconfigured:
                _warned_unconfigured.add(thread_id)
                log.warning("mirror: channel %s of thread %s is not in the live config; "
                            "delivering nothing for it", entry["channel"], thread_id)
            continue
        voice = cfg is not None and cfg.audience == "customer" and ledger is not None
        tail = projection.get("messages", [])[-_TAIL_MESSAGES:]
        # Whole projection, not the tail: only delivery is windowed.
        private_runs = (customer.private_run_ids(projection.get("messages", []),
                                                 projection.get("runs", []))
                        if voice else set())
        # Only a completed run's final assistant message belongs in Slack;
        # intermediate narration between tool calls is skipped, unledgered.
        finals = set()
        for run in projection.get("runs", []):
            if run.get("status") == "completed":
                reply = final_reply(projection, run.get("id"))
                if reply is not None:
                    finals.add(reply["id"])
        for msg in tail:
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
            if voice:
                try:
                    _customer_delivery(slack, mirror, thread_id, entry, msg, text,
                                       owner=owner, settings=settings, ledger=ledger,
                                       private_runs=private_runs)
                except Exception:  # noqa: BLE001 - one bad post shouldn't kill the loop
                    log.warning("mirror: customer delivery failed for %s", thread_id,
                                exc_info=True)
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


def start(t3: T3Client, slack: WebClient, mirror: MirrorStore, owner: str, *,
          live=None, ledger: customer.Ledger | None = None) -> threading.Thread:
    def loop() -> None:
        log.info("t3 mirror started (poll %.0fs)", _POLL_SECS)
        while True:
            try:
                _sweep(t3, slack, mirror, owner, live=live, ledger=ledger)
            except Exception:  # noqa: BLE001
                log.exception("mirror sweep crashed; continuing")
            time.sleep(_POLL_SECS)

    thread = threading.Thread(target=loop, name="t3-mirror", daemon=True)
    thread.start()
    return thread
