"""T3-backed turn runner: the `backend.run_turn` sibling that dispatches the
turn into T3 Code instead of driving the Claude Agent SDK directly.

Same `TurnResult` contract as `backend.py`. `session_id` carries the T3 thread
id (stored in the same SessionStore), so each Slack thread is one T3 thread —
live-visible and steerable in the T3 GUI. T3's ClaudeAdapter loads the target
project's CLAUDE.md via settingSources, so the channel persona lives there,
not in a system-prompt append (message.dispatch has no such field).

Completion detection: poll the thread projection for the run our message landed
in -- the message's own `runId`, so a message T3 steers into an already-active
run (one the owner started in the GUI) is followed there too -- until that run
reaches a terminal status, then read the run's final assistant message. On
timeout we dispatch `run.interrupt` so the run doesn't keep going headless.

While the run is still going, the same polls feed `on_progress` with a summary
of the in-flight work (newest narration segment + newest tool call), which the
caller can surface (e.g. by editing the Slack placeholder).

A run can still block on a human: T3 parks it on a pending runtime request
(`approval_request` / `user_input_request`) until someone acts in the GUI. The
bridge runs every turn full-access (trust is decided at the Slack/pps layer),
so tool-call approvals don't fire here; what remains is the agent explicitly
asking a question (`user_input`, e.g. AskUserQuestion). That wait is not the
agent's time, so the turn `timeout` clock pauses while a request is pending,
`on_approval_wait` fires once per new request (so the caller can page the
owner), and a separate `approval_timeout` bounds how long the bridge holds the
Slack thread. When that expires the run is left going -- NOT interrupted -- so
the owner can still answer later; the mirror delivers the eventual reply into
Slack (`TurnResult.awaiting_approval`).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from . import customer as customer_voice
from .backend import TurnResult
from .t3 import (TERMINAL_RUN_STATUSES, MirrorStore, T3Client, T3Error, final_reply,
                 reply_streaming)

log = logging.getLogger(__name__)

_POLL_SECS = 2.5
_PROGRESS_MIN_SECS = 5.0  # floor between on_progress emissions (Slack edit budget)
_NARRATION_CLIP = 600
_TOOL_CLIP = 200
# Fragment of T3's ContextHandoffBudgetError message.
_CONTEXT_FULL_MARKER = "Insufficient context allowance"

# Turn items that are the agent doing something (what the GUI shows as a tool row).
_TOOL_ITEM_TYPES = {"command_execution", "dynamic_tool", "file_change", "web_search",
                    "file_search"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _find_run(projection: dict, message_id: str) -> dict | None:
    """The run carrying our message: the one the message is attributed to
    (also covers a message steered into an already-active run), else the run
    it started (`userMessageId`, set while the run is still queued)."""
    run_id = next((m.get("runId") for m in projection.get("messages", [])
                   if m.get("id") == message_id), None)
    for run in projection.get("runs", []):
        if (run_id and run.get("id") == run_id) or run.get("userMessageId") == message_id:
            return run
    return None


def _tool_label(item: dict) -> str:
    label = item.get("title") or item.get("fileName") or item.get("input") or ""
    return label.strip() if isinstance(label, str) else ""


def _progress_summary(projection: dict, run_id: str) -> str:
    """What the T3 GUI shows live, flattened for a Slack placeholder edit:
    this run's newest narration segment plus its newest tool call."""
    narration = ""
    for msg in projection.get("messages", []):
        if msg.get("role") == "assistant" and msg.get("runId") == run_id:
            text = (msg.get("text") or "").strip()
            if text:
                narration = text
    tool = ""
    for item in projection.get("turnItems", []):
        if item.get("runId") == run_id and item.get("type") in _TOOL_ITEM_TYPES:
            label = _tool_label(item)
            if label:
                tool = label
    parts = []
    if narration:
        parts.append(_clip(narration, _NARRATION_CLIP))
    if tool:
        parts.append(f"`{_clip(tool, _TOOL_CLIP)}`")
    return "\n".join(parts)


def _customer_progress(projection: dict, run_id: str, elapsed: float) -> str:
    """The same live view for a customer channel: no tool label (it is file
    names and commands), the newest narration only if it passes the leak gate,
    and time + step count (the run's tool-type turn items) instead."""
    # The newest segment only: if that is the final in progress (it carries the
    # customer-block marker) progress_text falls back to its generic line
    # rather than reaching back to older narration.
    narration = ""
    for msg in projection.get("messages", []):
        if msg.get("role") == "assistant" and msg.get("runId") == run_id:
            narration = (msg.get("text") or "").strip() or narration
    steps = sum(1 for item in projection.get("turnItems", [])
                if item.get("runId") == run_id and item.get("type") in _TOOL_ITEM_TYPES)
    return customer_voice.progress_text(narration, elapsed, steps, clip=_NARRATION_CLIP)


def _pending_requests(projection: dict, run_id: str) -> list[dict]:
    """Human-gated requests this run is parked on: every pending runtime
    request, joined with its turn item for the human-readable detail. Ordered
    oldest first. Each entry: requestId, kind (the runtime request kind, e.g.
    "user_input" or "command"), and requestKind / prompt / questions from the
    item when present."""
    items = {item.get("requestId"): item for item in projection.get("turnItems", [])
             if item.get("type") in ("approval_request", "user_input_request")}
    pending = []
    for req in projection.get("runtimeRequests", []):
        if req.get("status") != "pending":
            continue
        item = items.get(req.get("id")) or {}
        if item.get("runId") not in (None, run_id):
            continue
        pending.append({
            "requestId": req.get("id") or "",
            "kind": req.get("kind") or "",
            "requestKind": item.get("requestKind") or req.get("kind") or "",
            "prompt": item.get("prompt") or item.get("title") or "",
            "questions": item.get("questions") or [],
        })
    return pending


def describe_request(req: dict) -> str:
    """One-line human label for a pending request (Slack-safe, clipped)."""
    if req.get("kind") == "user_input":
        questions = req.get("questions") or []
        first = ""
        if questions and isinstance(questions[0], dict):
            first = str(questions[0].get("question") or questions[0].get("header") or "")
        return "a question for you" + (f": {_clip(first, _TOOL_CLIP)}" if first else "")
    label = {
        "command": "run a command",
        "file-change": "change a file",
        "file-read": "read a file",
    }.get(req.get("requestKind") or "", "a tool call")
    detail = (req.get("prompt") or "").strip()
    return label + (f": `{_clip(detail, _TOOL_CLIP)}`" if detail else "")


def _interrupt(client: T3Client, thread_id: str, run_id: str | None) -> None:
    if not run_id:
        return
    try:
        client.dispatch({
            "type": "run.interrupt",
            "commandId": f"slack-int-{uuid.uuid4()}",
            "threadId": thread_id,
            "runId": run_id,
        })
    except T3Error:
        log.warning("could not interrupt T3 run %s on thread %s", run_id, thread_id,
                    exc_info=True)


def _upload_images(client: T3Client, images: list[dict]) -> list[dict]:
    """Stage each image with T3; a failed upload drops just that image (it
    still reaches the agent via the local-path note in the prompt text)."""
    staged = []
    for image in images:
        try:
            staged.append(client.upload_image(
                name=image["name"], mime_type=image["mimeType"], data=image["data"]))
        except T3Error:
            log.warning("T3 image upload failed for %s; sending without it",
                        image.get("name"), exc_info=True)
    return staged


def _context_full(projection: dict, run_id: str) -> bool:
    """Whether the run failed on T3's context-handoff budget: the session is too
    full to re-inject history, so every further turn fails until it is
    compacted. T3 files the failure as an `error` turn item."""
    for item in projection.get("turnItems", []):
        if item.get("runId") == run_id and item.get("type") == "error":
            message = (item.get("failure") or {}).get("message") or ""
            if _CONTEXT_FULL_MARKER in message:
                return True
    return False


def run_turn(**kwargs) -> TurnResult:
    """One Slack turn, with a safety net for a full session: if the run fails
    because the context window has no room left, compact the session once and
    retry the same prompt. A failed compaction or a second failure is returned
    as-is -- never a loop."""
    result = _run_once(**kwargs)
    if not result.context_full:
        return result
    thread_id = kwargs["thread_id"]
    log.warning("t3 context full on %s; compacting and retrying once", thread_id)
    on_progress = kwargs.get("on_progress")
    if on_progress is not None:
        try:
            on_progress(":broom: _the conversation is long -- tidying it up first_")
        except Exception:  # noqa: BLE001 - progress must not kill the turn
            log.warning("progress callback failed", exc_info=True)
    compact = _run_once(**{**kwargs, "prompt": "/compact", "is_new": False, "images": None,
                           "on_progress": None, "on_approval_wait": None})
    if not compact.ok:
        log.error("auto-compact failed on %s: %s", thread_id, compact.error)
        return result
    return _run_once(**{**kwargs, "is_new": False})


def _run_once(
    *,
    prompt: str,
    thread_id: str,
    is_new: bool,
    project_id: str,
    model: dict,
    title: str,
    client: T3Client,
    mirror: MirrorStore,
    images: list[dict] | None = None,
    timeout: int = 300,
    runtime_mode: str = "full-access",
    on_progress: Callable[[str], None] | None = None,
    on_approval_wait: Callable[[list[dict]], None] | None = None,
    approval_timeout: int = 3600,
    owner_name: str = "the owner",
    customer: bool = False,
) -> TurnResult:
    message_id = f"slack-user-{uuid.uuid4()}"
    # Ledger the inbound message BEFORE dispatch so the mirror never echoes a
    # Slack-originated message back into Slack.
    mirror.mark_posted(thread_id, [message_id])

    log.info("t3 turn: thread=%s new=%s project=%s", thread_id, is_new, project_id)
    if is_new:
        try:
            client.dispatch({
                "type": "thread.create",
                "commandId": f"slack-mk-{uuid.uuid4()}",
                "createdBy": "user",
                "creationSource": "web",
                "threadId": thread_id,
                "projectId": project_id,
                "title": title,
                "modelSelection": model,
                "runtimeMode": runtime_mode,
                "interactionMode": "default",
                "branch": None,
                "worktreePath": None,
            })
        except T3Error:
            # Deterministic ids make creates retryable: if the thread already
            # exists (e.g. a crash between create and dispatch, or a stale
            # sessions.json), proceed — the message dispatch surfaces any real
            # problem.
            log.warning("thread.create failed for %s; assuming it exists", thread_id,
                        exc_info=True)

    try:
        client.dispatch({
            "type": "message.dispatch",
            "commandId": f"slack-cmd-{uuid.uuid4()}",
            "createdBy": "user",
            "creationSource": "web",
            "threadId": thread_id,
            "messageId": message_id,
            "text": prompt,
            "attachments": _upload_images(client, images or []),
            # Same as the GUI composer: T3 starts a run when the thread is
            # idle and resolves the delivery itself when one is active.
            "deliveryIntent": "auto",
            "dispatchMode": {"type": "start_immediately"},
        })
    except T3Error as exc:
        return TurnResult(ok=False, text="", error=str(exc))

    # Two clocks: `timeout` bounds the agent's own working time and pauses
    # while the run is parked on a human (approval / question in the T3 GUI);
    # `approval_timeout` bounds that parked time so a never-answered request
    # can't hold this Slack handler forever.
    started = time.monotonic()
    deadline = started + timeout
    approval_deadline: float | None = None
    last_tick = time.monotonic()
    last_progress = ""
    last_progress_at = 0.0
    seen_requests: set[str] = set()
    run_id: str | None = None
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECS)
        now = time.monotonic()
        elapsed, last_tick = now - last_tick, now
        try:
            projection = client.thread_projection(thread_id)
        except T3Error:
            log.warning("t3 projection poll failed for %s", thread_id, exc_info=True)
            continue
        run = _find_run(projection, message_id)
        if run is None:
            continue  # not picked up yet
        run_id = run.get("id")
        status = run.get("status")
        if status not in TERMINAL_RUN_STATUSES:
            pending = _pending_requests(projection, run_id)
            if pending:
                # Parked on a human: this poll's wall time is theirs, not the
                # agent's -- push the turn deadline out by it.
                deadline += elapsed
                if approval_deadline is None:
                    approval_deadline = now + approval_timeout
                fresh = [r for r in pending if r["requestId"] not in seen_requests]
                if fresh:
                    seen_requests.update(r["requestId"] for r in fresh)
                    log.info("t3 run parked on %d pending request(s): thread=%s",
                             len(pending), thread_id)
                    if on_approval_wait is not None:
                        try:
                            on_approval_wait(fresh)
                        except Exception:  # noqa: BLE001 - notification must not kill the turn
                            log.warning("approval-wait callback failed", exc_info=True)
                if now >= approval_deadline:
                    # Hand the wait off: leave the request open in T3 so the
                    # owner can still act; the mirror posts the eventual reply.
                    log.info("t3 run still parked after %ss; releasing Slack handler: "
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
            # Mid-run: surface what the agent is doing right now. The polls
            # already carry it; emit only on change, at a bounded rate.
            if on_progress is not None:
                if customer:
                    summary = _customer_progress(projection, run_id, now - started)
                    if pending:
                        summary += f"\n:raised_hand: _paused -- waiting for {owner_name}'s go-ahead_"
                else:
                    summary = _progress_summary(projection, run_id)
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

        if reply_streaming(projection, run_id):
            continue  # terminal, but the last segment hasn't landed yet
        reply = final_reply(projection, run_id)
        private = run_id in customer_voice.private_run_ids(
            projection.get("messages", []), projection.get("runs", []))
        text = ""
        if reply is not None:
            text = reply["text"].strip()
            # Keep the mirror from double-posting the reply we're about to post.
            mirror.mark_posted(thread_id, [reply["id"]])

        if status == "completed":
            return TurnResult(ok=True, text=text, session_id=thread_id,
                              message_id=reply["id"] if reply is not None else None,
                              private=private)
        return TurnResult(
            ok=False,
            text=text,
            session_id=thread_id,
            error=f"T3 run ended in state '{status}'",
            private=private,
            context_full=status == "failed" and _context_full(projection, run_id),
        )

    _interrupt(client, thread_id, run_id)
    return TurnResult(
        ok=False,
        text="",
        session_id=thread_id,
        error=f"T3 turn timed out after {timeout}s (interrupted)",
    )
