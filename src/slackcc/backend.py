"""Agent backend: drives Claude Code via the Claude Agent SDK.

Previously this shelled out to `claude -p --output-format json`. It now uses the
in-process `claude_agent_sdk.query()` generator instead. Behavior is unchanged:
each turn is one-shot and resumes the thread's session by id (`resume=`), so the
Slack layer (`app.py`) and the `TurnResult` contract are untouched. The win is
that we're now on the SDK seam -- streaming deltas, tool-use events, model
choice and `interrupt()` -- which the phone bridge (Step 2) builds on.

The SDK spawns the same bundled Claude Code engine and reads/writes the same
`~/.claude/projects/<proj>/*.jsonl` transcripts as `claude -p --resume`, so
session continuity and Dancode chat-viewing keep working unchanged.

`run_turn` stays synchronous (the Slack Bolt handler is sync); it drives the
async generator via `anyio.run` with a per-turn timeout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    TextBlock,
    query,
)

log = logging.getLogger(__name__)


@dataclass
class TurnResult:
    ok: bool
    text: str
    session_id: str | None = None
    error: str | None = None


def _build_options(
    *,
    cwd: str,
    claude_bin: str,
    append_system_prompt: str | None,
    allowed_tools: list[str] | None,
    permission_mode: str | None,
    resume: str | None,
    model: str | None,
) -> ClaudeAgentOptions:
    kwargs: dict = {
        "cwd": cwd,
        # Match `claude -p` semantics: load CLAUDE.md + project/local settings.
        # The SDK loads NO filesystem settings by default; the daemon relies on
        # each project's CLAUDE.md (e.g. the gphotos Slack-reviewer instructions).
        "setting_sources": ["user", "project", "local"],
    }
    # `claude_bin` defaults to "claude" (found on PATH); only override if custom.
    if claude_bin and claude_bin != "claude":
        kwargs["cli_path"] = claude_bin
    if append_system_prompt:
        # Preset-append == CLI `--append-system-prompt` (a bare string would
        # REPLACE Claude Code's default prompt and change behavior).
        kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": append_system_prompt,
        }
    if allowed_tools:
        kwargs["allowed_tools"] = list(allowed_tools)
    if permission_mode:
        kwargs["permission_mode"] = permission_mode
    if resume:
        kwargs["resume"] = resume
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


async def _run_turn_async(*, prompt: str, options: ClaudeAgentOptions) -> TurnResult:
    """Consume the SDK message stream and collapse it into a TurnResult.

    The terminal `ResultMessage` carries the final text + session_id (the exact
    analog of the old `--output-format json` payload). We accumulate streamed
    assistant text only as a fallback for the rare case where no ResultMessage
    arrives.
    """
    texts: list[str] = []
    result_msg: ResultMessage | None = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
        elif isinstance(msg, ResultMessage):
            result_msg = msg

    if result_msg is None:
        joined = "\n".join(texts).strip()
        return TurnResult(
            ok=bool(joined),
            text=joined,
            error=None if joined else "claude produced no result",
        )

    text = (result_msg.result or "\n".join(texts)).strip()
    if result_msg.is_error:
        err = (
            "; ".join(result_msg.errors or [])
            or result_msg.subtype
            or "claude reported an error"
        )
        return TurnResult(ok=False, text=text, session_id=result_msg.session_id, error=err)
    return TurnResult(ok=True, text=text, session_id=result_msg.session_id)


def run_turn(
    *,
    prompt: str,
    cwd: str,
    claude_bin: str = "claude",
    append_system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
    permission_mode: str | None = None,
    resume: str | None = None,
    timeout: int = 300,
    model: str | None = None,
) -> TurnResult:
    """Run one Claude Code turn via the SDK and return its result.

    `model` is optional and unused by the Slack path today; it's the seam the
    phone bridge uses to run a fast model (e.g. Haiku) for live turns.
    """
    options = _build_options(
        cwd=cwd,
        claude_bin=claude_bin,
        append_system_prompt=append_system_prompt,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        resume=resume,
        model=model,
    )
    log.info(
        "claude turn (sdk): cwd=%s resume=%s model=%s tools=%s",
        cwd, bool(resume), model or "default", allowed_tools,
    )

    async def _main() -> TurnResult:
        with anyio.fail_after(timeout):
            return await _run_turn_async(prompt=prompt, options=options)

    try:
        return anyio.run(_main)
    except TimeoutError:
        return TurnResult(ok=False, text="", error=f"claude timed out after {timeout}s")
    except CLINotFoundError as exc:
        return TurnResult(ok=False, text="", error=f"claude CLI not found: {exc}")
    except (ProcessError, ClaudeSDKError) as exc:
        return TurnResult(ok=False, text="", error=f"claude error: {str(exc)[:500]}")
