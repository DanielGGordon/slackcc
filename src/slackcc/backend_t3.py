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
    timeout: int = 300,
    runtime_mode: str = "full-access",
    on_progress: Callable[[str], None] | None = None,
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
            "attachments": [],
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

    deadline = time.monotonic() + timeout
    last_progress = ""
    last_progress_at = 0.0
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECS)
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
            # Mid-turn: surface what the agent is doing right now. The polls
            # already carry it; emit only on change, at a bounded rate.
            if on_progress is not None:
                summary = _progress_summary(thread, turn.get("turnId"))
                now = time.monotonic()
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
