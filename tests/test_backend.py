"""Tests for slackcc.backend: _build_options, _run_turn_async, and run_turn.

Fully offline: claude_agent_sdk.query is monkeypatched with a fake async
generator in the _run_turn_async / run_turn tests, so no real Claude Code
process is ever spawned. No network, no Slack/T3/pps, no repo .state/.env.
"""

from __future__ import annotations

from slackcc import backend
from slackcc.backend import _build_options, _run_turn_async, run_turn


# ---------------------------------------------------------------------------
# _build_options
# ---------------------------------------------------------------------------


def test_build_options_minimal_sets_cwd_and_setting_sources():
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="claude",
        append_system_prompt=None,
        allowed_tools=None,
        permission_mode=None,
        resume=None,
        model=None,
    )
    assert opts.cwd == "/tmp/proj"
    # Always loads user/project/local settings to match `claude -p` semantics.
    assert opts.setting_sources == ["user", "project", "local"]
    # None of the optional knobs were provided, so they stay unset/default.
    assert opts.cli_path is None
    assert opts.system_prompt is None
    assert opts.allowed_tools == []
    assert opts.permission_mode is None
    assert opts.resume is None
    assert opts.model is None


def test_build_options_append_system_prompt_uses_preset_append_not_bare_string():
    """A bare string system_prompt would REPLACE Claude Code's default prompt
    (per the source's own comment); the daemon must always use the
    preset+append form so the default prompt is preserved and extended."""
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="claude",
        append_system_prompt="Be extra careful with prod.",
        allowed_tools=None,
        permission_mode=None,
        resume=None,
        model=None,
    )
    assert opts.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "Be extra careful with prod.",
    }


def test_build_options_empty_append_system_prompt_is_falsy_and_skipped():
    # Empty string is falsy, so the `if append_system_prompt:` guard skips it
    # entirely -- system_prompt stays None (default Claude Code prompt).
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="claude",
        append_system_prompt="",
        allowed_tools=None,
        permission_mode=None,
        resume=None,
        model=None,
    )
    assert opts.system_prompt is None


def test_build_options_permission_mode_passthrough():
    """app.py relies on this passthrough to demote guests to 'default'
    permission mode -- _build_options must not alter or default it."""
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="claude",
        append_system_prompt=None,
        allowed_tools=None,
        permission_mode="default",
        resume=None,
        model=None,
    )
    assert opts.permission_mode == "default"


def test_build_options_permission_mode_none_stays_unset():
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="claude",
        append_system_prompt=None,
        allowed_tools=None,
        permission_mode=None,
        resume=None,
        model=None,
    )
    assert opts.permission_mode is None


def test_build_options_resume_passthrough():
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="claude",
        append_system_prompt=None,
        allowed_tools=None,
        permission_mode=None,
        resume="session-abc-123",
        model=None,
    )
    assert opts.resume == "session-abc-123"


def test_build_options_allowed_tools_and_model_passthrough():
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="claude",
        append_system_prompt=None,
        allowed_tools=["Read", "Bash"],
        permission_mode=None,
        resume=None,
        model="claude-haiku-4-5",
    )
    assert opts.allowed_tools == ["Read", "Bash"]
    assert opts.model == "claude-haiku-4-5"


def test_build_options_claude_bin_default_does_not_set_cli_path():
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="claude",
        append_system_prompt=None,
        allowed_tools=None,
        permission_mode=None,
        resume=None,
        model=None,
    )
    assert opts.cli_path is None


def test_build_options_custom_claude_bin_sets_cli_path():
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="/usr/local/bin/claude-custom",
        append_system_prompt=None,
        allowed_tools=None,
        permission_mode=None,
        resume=None,
        model=None,
    )
    assert opts.cli_path == "/usr/local/bin/claude-custom"


def test_build_options_empty_claude_bin_does_not_set_cli_path():
    # Falsy (empty string) claude_bin should not set cli_path either, since
    # the guard is `if claude_bin and claude_bin != "claude"`.
    opts = _build_options(
        cwd="/tmp/proj",
        claude_bin="",
        append_system_prompt=None,
        allowed_tools=None,
        permission_mode=None,
        resume=None,
        model=None,
    )
    assert opts.cli_path is None


def test_build_options_setting_sources_always_present_regardless_of_other_args():
    opts = _build_options(
        cwd="/x",
        claude_bin="my-claude",
        append_system_prompt="extra",
        allowed_tools=["Read"],
        permission_mode="plan",
        resume="sid",
        model="opus",
    )
    assert opts.setting_sources == ["user", "project", "local"]


# ---------------------------------------------------------------------------
# _run_turn_async -- fake claude_agent_sdk.query() message streams
# ---------------------------------------------------------------------------


def _make_fake_query(messages):
    """Return a fake replacement for claude_agent_sdk.query that yields the
    given messages regardless of prompt/options passed in."""

    async def fake_query(*, prompt, options):
        for msg in messages:
            yield msg

    return fake_query


async def _run_with_fake_stream(monkeypatch, messages):
    monkeypatch.setattr(backend, "query", _make_fake_query(messages))
    # options content is irrelevant since fake_query ignores it.
    opts = _build_options(
        cwd="/tmp",
        claude_bin="claude",
        append_system_prompt=None,
        allowed_tools=None,
        permission_mode=None,
        resume=None,
        model=None,
    )
    return await _run_turn_async(prompt="hi", options=opts)


def test_run_turn_async_with_result_message_ok(monkeypatch):
    # AssistantMessage isinstance checks in the source use the real SDK
    # classes, so use real ones (they're plain dataclasses) rather than fakes.
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    messages = [
        AssistantMessage(content=[TextBlock(text="streamed chunk")], model="m"),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess-42",
            result="final answer",
        ),
    ]

    result = anyio_run_helper(_run_with_fake_stream, monkeypatch, messages)

    assert result.ok is True
    assert result.text == "final answer"
    assert result.session_id == "sess-42"
    assert result.error is None


def test_run_turn_async_result_message_is_error_uses_errors_list(monkeypatch):
    from claude_agent_sdk import ResultMessage

    messages = [
        ResultMessage(
            subtype="error_during_execution",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="sess-err",
            result="partial text",
            errors=["boom", "kaboom"],
        ),
    ]

    result = anyio_run_helper(_run_with_fake_stream, monkeypatch, messages)

    assert result.ok is False
    assert result.text == "partial text"
    assert result.session_id == "sess-err"
    assert result.error == "boom; kaboom"


def test_run_turn_async_result_message_is_error_falls_back_to_subtype(monkeypatch):
    from claude_agent_sdk import ResultMessage

    messages = [
        ResultMessage(
            subtype="error_max_turns",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="sess-err2",
            result=None,
            errors=None,
        ),
    ]

    result = anyio_run_helper(_run_with_fake_stream, monkeypatch, messages)

    assert result.ok is False
    assert result.error == "error_max_turns"


def test_run_turn_async_result_message_is_error_falls_back_to_generic_message(monkeypatch):
    from claude_agent_sdk import ResultMessage

    messages = [
        ResultMessage(
            subtype=None,
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="sess-err3",
            result=None,
            errors=None,
        ),
    ]

    result = anyio_run_helper(_run_with_fake_stream, monkeypatch, messages)

    assert result.ok is False
    assert result.error == "claude reported an error"


def test_run_turn_async_result_message_no_result_text_falls_back_to_streamed_text(monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    messages = [
        AssistantMessage(content=[TextBlock(text="streamed only")], model="m"),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess-nores",
            result=None,
        ),
    ]

    result = anyio_run_helper(_run_with_fake_stream, monkeypatch, messages)

    assert result.ok is True
    assert result.text == "streamed only"
    assert result.session_id == "sess-nores"


def test_run_turn_async_no_result_message_but_streamed_text_present(monkeypatch):
    from claude_agent_sdk import AssistantMessage, TextBlock

    messages = [
        AssistantMessage(content=[TextBlock(text="  partial stream  ")], model="m"),
    ]

    result = anyio_run_helper(_run_with_fake_stream, monkeypatch, messages)

    assert result.ok is True
    assert result.text == "partial stream"
    assert result.session_id is None
    assert result.error is None


def test_run_turn_async_no_result_message_and_no_text_is_failure(monkeypatch):
    messages = []

    result = anyio_run_helper(_run_with_fake_stream, monkeypatch, messages)

    assert result.ok is False
    assert result.text == ""
    assert result.session_id is None
    assert result.error == "claude produced no result"


def anyio_run_helper(coro_fn, *args, **kwargs):
    import anyio

    return anyio.run(lambda: coro_fn(*args, **kwargs))


# ---------------------------------------------------------------------------
# run_turn -- synchronous wrapper (timeout / CLINotFoundError / ProcessError)
# ---------------------------------------------------------------------------


def test_run_turn_happy_path(monkeypatch):
    from claude_agent_sdk import ResultMessage

    def fake_query(*, prompt, options):
        async def gen():
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="sess-happy",
                result="the answer",
            )

        return gen()

    monkeypatch.setattr(backend, "query", fake_query)

    result = run_turn(prompt="hello", cwd="/tmp", timeout=5)

    assert result.ok is True
    assert result.text == "the answer"
    assert result.session_id == "sess-happy"


def test_run_turn_times_out(monkeypatch):
    import anyio

    def fake_query(*, prompt, options):
        async def gen():
            await anyio.sleep(10)
            yield  # pragma: no cover - unreachable

        return gen()

    monkeypatch.setattr(backend, "query", fake_query)

    result = run_turn(prompt="hello", cwd="/tmp", timeout=0.05)

    assert result.ok is False
    assert result.text == ""
    assert "timed out" in result.error


def test_run_turn_cli_not_found_error(monkeypatch):
    from claude_agent_sdk import CLINotFoundError

    def fake_query(*, prompt, options):
        async def gen():
            raise CLINotFoundError("claude binary missing")
            yield  # pragma: no cover - unreachable

        return gen()

    monkeypatch.setattr(backend, "query", fake_query)

    result = run_turn(prompt="hello", cwd="/tmp", timeout=5)

    assert result.ok is False
    assert result.text == ""
    assert "claude CLI not found" in result.error


def test_run_turn_process_error(monkeypatch):
    from claude_agent_sdk import ProcessError

    def fake_query(*, prompt, options):
        async def gen():
            raise ProcessError("boom", exit_code=1, stderr="stderr output")
            yield  # pragma: no cover - unreachable

        return gen()

    monkeypatch.setattr(backend, "query", fake_query)

    result = run_turn(prompt="hello", cwd="/tmp", timeout=5)

    assert result.ok is False
    assert result.text == ""
    assert "claude error" in result.error
