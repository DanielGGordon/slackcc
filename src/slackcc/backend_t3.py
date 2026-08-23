"""T3-backed turn runner: the `backend.run_turn` sibling that dispatches the
turn into T3 Code instead of driving the Claude Agent SDK directly.

Same `TurnResult` contract as `backend.py`. `session_id` carries the T3 thread
id (stored in the same SessionStore), so each Slack thread is one T3 thread —
live-visible and steerable in the T3 GUI. T3's ClaudeAdapter loads the target
project's CLAUDE.md via settingSources, so the channel persona lives there,
not in a system-prompt append (thread.turn.start has no such field).

Completion detection: poll the thread snapshot until `latestTurn` (requested at
or after our dispatch) reaches a terminal state, then read the message named by
`latestTurn.assistantMessageId`. On timeout we dispatch `thread.turn.interrupt`
so the turn doesn't keep running headless.

While the turn is still running, the same polls feed `on_progress` with a
summary of the in-flight work (newest narration segment + newest tool call),
which the caller can surface (e.g. by editing the Slack placeholder).

Approval-required turns (guests) can block on a human: T3 parks the turn on a
`approval.requested` / `user-input.requested` activity until someone acts in
the GUI. That wait is not the agent's time, so the turn `timeout` clock pauses
while a request is pending, `on_approval_wait` fires once per new request (so
the caller can page the owner), and a separate `approval_timeout` bounds how
long the bridge holds the Slack thread. When that expires the turn is left
running -- NOT interrupted -- so the owner can still approve later; the mirror
delivers the eventual reply into Slack (`TurnResult.awaiting_approval`).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from .backend import TurnResult
from .t3 import MirrorStore, T3Client, T3Error

log = logging.getLogger(__name__)

_POLL_SECS = 2.5
_TERMINAL = {"completed", "interrupted", "error"}
_PROGRESS_MIN_SECS = 5.0  # floor between on_progress emissions (Slack edit budget)
_NARRATION_CLIP = 600
_TOOL_CLIP = 200


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _progress_summary(thread: dict, turn_id: str | None) -> str:
    """What the T3 GUI shows live, flattened for a Slack placeholder edit:
    this turn's newest narration segment plus its newest tool call."""
    if not turn_id:
        return ""
    narration = ""
    for msg in thread.get("messages", []):
        if msg.get("role") == "assistant" and msg.get("turnId") == turn_id:
            text = (msg.get("text") or "").strip()
            if text:
                narration = text
    tool = ""
    for act in thread.get("activities", []):
        if act.get("tone") == "tool" and act.get("turnId") == turn_id:
            summary = (act.get("summary") or "").strip()
            if summary:
                tool = summary
    parts = []
    if narration:
        parts.append(_clip(narration, _NARRATION_CLIP))
    if tool:
        parts.append(f"`{_clip(tool, _TOOL_CLIP)}`")
    return "\n".join(parts)


_BLOCKING_KINDS = {
    # activity kind -> the kind that clears it
    "approval.requested": "approval.resolved",
    "user-input.requested": "user-input.resolved",
}


def _pending_requests(thread: dict, turn_id: str | None) -> list[dict]:
    """Human-gated requests this turn is parked on: every approval / user-input
    request activity whose requestId has no matching resolution yet. Ordered
    oldest first. Each entry is the request activity's payload plus `kind`."""
    if not turn_id:
        return []
    opened: dict[str, dict] = {}
    for act in thread.get("activities", []):
        if act.get("turnId") != turn_id:
            continue
        kind = act.get("kind") or ""
        payload = act.get("payload") or {}
        request_id = payload.get("requestId") or act.get("id") or ""
        if kind in _BLOCKING_KINDS:
            opened[request_id] = {"kind": kind, "requestId": request_id, **payload}
        elif kind in _BLOCKING_KINDS.values():
            opened.pop(request_id, None)
    return list(opened.values())


def describe_request(req: dict) -> str:
    """One-line human label for a pending request (Slack-safe, clipped)."""
    if req.get("kind") == "user-input.requested":
        questions = req.get("questions") or []
        first = ""
        if questions and isinstance(questions[0], dict):
            first = str(questions[0].get("question") or questions[0].get("prompt") or "")
        return "a question for you" + (f": {_clip(first, _TOOL_CLIP)}" if first else "")
    label = {
        "command": "run a command",
        "file-change": "change a file",
        "file-read": "read a file",
    }.get(req.get("requestKind") or "", "a tool call")
    detail = (req.get("detail") or "").strip()
    return label + (f": `{_clip(detail, _TOOL_CLIP)}`" if detail else "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _interrupt(client: T3Client, thread_id: str) -> None:
    try:
        client.dispatch({
            "type": "thread.turn.interrupt",
            "commandId": f"slack-int-{uuid.uuid4()}",
            "threadId": thread_id,
            "createdAt": _now_iso(),
        })
    except T3Error:
        log.warning("could not interrupt T3 turn on thread %s", thread_id, exc_info=True)


def run_turn(
    *,
    prompt: str,
    thread_id: str,
    is_new: bool,
    project_id: str,
    model: dict,
    title: str,
    client: T3Client,
    mirror: MirrorStore,
    attachments: list[dict] | None = None,
    timeout: int = 300,
    runtime_mode: str = "full-access",
    on_progress: Callable[[str], None] | None = None,
    on_approval_wait: Callable[[list[dict]], None] | None = None,
    approval_timeout: int = 3600,
    owner_name: str = "the owner",
) -> TurnResult:
    dispatched_at = _now_iso()
    message_id = f"slack-user-{uuid.uuid4()}"
    # Ledger the inbound message BEFORE dispatch so the mirror never echoes a
    # Slack-originated message back into Slack.
    mirror.mark_posted(thread_id, [message_id])

    command: dict = {
        "type": "thread.turn.start",
        "commandId": f"slack-cmd-{uuid.uuid4()}",
        "threadId": thread_id,
        "message": {
            "messageId": message_id,
            "role": "user",
            "text": prompt,
            "attachments": attachments or [],
        },
        # Owner gets full-access (bypass); guests get approval-required so
        # risky tool calls wait for approval in the T3 GUI (see senders.json).
        "runtimeMode": runtime_mode,
        "interactionMode": "default",
        "createdAt": dispatched_at,
    }

    log.info("t3 turn: thread=%s new=%s project=%s", thread_id, is_new, project_id)
    if is_new:
        # `bootstrap.createThread` is only expanded on the WS dispatch
        # path (ws.ts); over plain HTTP the thread must exist first.
        try:
            client.dispatch({
                "type": "thread.create",
                "commandId": f"slack-mk-{uuid.uuid4()}",
                "threadId": thread_id,
                "projectId": project_id,
                "title": title,
                "modelSelection": model,
                "runtimeMode": runtime_mode,
                "interactionMode": "default",
                "branch": None,
                "worktreePath": None,
                "createdAt": dispatched_at,
            })
        except T3Error:
            # Deterministic ids make creates retryable: if the thread already
            # exists (e.g. a crash between create and turn.start, or a stale
            # sessions.json), proceed — turn.start surfaces any real problem.
            log.warning("thread.create failed for %s; assuming it exists", thread_id,
                        exc_info=True)
    try:
        client.dispatch(command)
    except T3Error as exc:
        return TurnResult(ok=False, text="", error=str(exc))

    # Two clocks: `timeout` bounds the agent's own working time and pauses
    # while the turn is parked on a human (approval / question in the T3 GUI);
    # `approval_timeout` bounds that parked time so a never-answered request
    # can't hold this Slack handler forever.
    deadline = time.monotonic() + timeout
    approval_deadline: float | None = None
    last_tick = time.monotonic()
    last_progress = ""
    last_progress_at = 0.0
    seen_requests: set[str] = set()
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECS)
        now = time.monotonic()
        elapsed, last_tick = now - last_tick, now
        try:
            thread = client.thread_snapshot(thread_id).get("thread", {})
        except T3Error:
            log.warning("t3 snapshot poll failed for %s", thread_id, exc_info=True)
            continue
        turn = thread.get("latestTurn")
        # Ignore a terminal turn left over from before this dispatch (resume case).
        if not turn or turn.get("requestedAt", "") < dispatched_at:
            continue
        if turn.get("state") not in _TERMINAL:
            pending = _pending_requests(thread, turn.get("turnId"))
            if pending:
                # Parked on a human: this poll's wall time is theirs, not the
                # agent's -- push the turn deadline out by it.
                deadline += elapsed
                if approval_deadline is None:
                    approval_deadline = now + approval_timeout
                fresh = [r for r in pending if r["requestId"] not in seen_requests]
                if fresh:
                    seen_requests.update(r["requestId"] for r in fresh)
                    log.info("t3 turn parked on %d pending request(s): thread=%s",
                             len(pending), thread_id)
                    if on_approval_wait is not None:
                        try:
                            on_approval_wait(fresh)
                        except Exception:  # noqa: BLE001 - notification must not kill the turn
                            log.warning("approval-wait callback failed", exc_info=True)
                if now >= approval_deadline:
                    # Hand the wait off: leave the request open in T3 so the
                    # owner can still act; the mirror posts the eventual reply.
                    log.info("t3 turn still parked after %ss; releasing Slack handler: "
                             "thread=%s", approval_timeout, thread_id)
                    return TurnResult(
                        ok=False,
                        text="",
                        session_id=thread_id,
                        error=f"still waiting for approval after {approval_timeout}s",
                        awaiting_approval=True,
                    )
            else:
                approval_deadline = None
            # Mid-turn: surface what the agent is doing right now. The polls
            # already carry it; emit only on change, at a bounded rate.
            if on_progress is not None:
                summary = _progress_summary(thread, turn.get("turnId"))
                if pending:
                    summary = "\n".join(filter(None, [
                        summary,
                        f":raised_hand: _paused -- waiting for {owner_name} to approve "
                        f"{describe_request(pending[-1])} in T3_",
                    ]))
                if (summary and summary != last_progress
                        and now - last_progress_at >= _PROGRESS_MIN_SECS):
                    last_progress, last_progress_at = summary, now
                    try:
                        on_progress(summary)
                    except Exception:  # noqa: BLE001 - progress must not kill the turn
                        log.warning("progress callback failed", exc_info=True)
            continue

        assistant_id = turn.get("assistantMessageId")
        text = ""
        if assistant_id:
            for msg in thread.get("messages", []):
                if msg.get("id") == assistant_id and not msg.get("streaming"):
                    text = (msg.get("text") or "").strip()
                    break
            # Keep the mirror from double-posting the reply we're about to post.
            mirror.mark_posted(thread_id, [assistant_id])

        if turn["state"] == "completed":
            return TurnResult(ok=True, text=text, session_id=thread_id)
        return TurnResult(
            ok=False,
            text=text,
            session_id=thread_id,
            error=f"T3 turn ended in state '{turn['state']}'",
        )

    _interrupt(client, thread_id)
    return TurnResult(
        ok=False,
        text="",
        session_id=thread_id,
        error=f"T3 turn timed out after {timeout}s (interrupted)",
    )
