"""Tests for backend_t3.run_turn: the T3-backed turn runner.

Fully offline: FakeT3Client below never touches the network; MirrorStore is a
real store backed by a tmp_path file.
"""

from __future__ import annotations

import pytest

from slackcc import backend_t3
from slackcc.backend_t3 import run_turn
from slackcc.t3 import MirrorStore, T3Error

STALE_TS = "2000-01-01T00:00:00Z"
FRESH_TS = "2999-01-01T00:00:00Z"


class FakeT3Client:
    """Scripted T3Client double: records every call, can raise on dispatch by
    command type, and can hand back a scripted sequence of thread snapshots
    (the last one repeats if polled more times than scripted)."""

    def __init__(self, snapshots=None, dispatch_errors=None, dispatch_hook=None):
        self.calls: list[tuple[str, dict]] = []
        self._snapshots = list(snapshots or [])
        self.dispatch_errors = dict(dispatch_errors or {})
        self.dispatch_hook = dispatch_hook

    def dispatch(self, command: dict) -> dict:
        self.calls.append(("dispatch", command))
        if self.dispatch_hook is not None:
            self.dispatch_hook(command)
        err = self.dispatch_errors.get(command["type"])
        if err is not None:
            raise err
        return {}

    def thread_snapshot(self, thread_id: str) -> dict:
        self.calls.append(("snapshot", {"threadId": thread_id}))
        if not self._snapshots:
            return {"thread": {}}
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]

    def dispatch_types(self) -> list[str]:
        return [cmd["type"] for kind, cmd in self.calls if kind == "dispatch"]


def make_mirror(tmp_path, thread_id, channel="C123", thread_ts="111.222"):
    mirror = MirrorStore(tmp_path / "mirror.json")
    mirror.register(thread_id, channel, thread_ts)
    return mirror


def completed_snapshot(assistant_id="assist-1", requested_at=FRESH_TS, messages=None):
    return {
        "thread": {
            "latestTurn": {
                "state": "completed",
                "requestedAt": requested_at,
                "assistantMessageId": assistant_id,
            },
            "messages": messages if messages is not None else [
                {"id": assistant_id, "streaming": False, "text": "hello from t3"},
            ],
        }
    }


def fast_poll(monkeypatch):
    monkeypatch.setattr(backend_t3, "_POLL_SECS", 0.01)
    monkeypatch.setattr(backend_t3, "_PROGRESS_MIN_SECS", 0.0)


def running_snapshot(turn_id="turn-1", narration=None, tool=None):
    """A mid-turn snapshot: latestTurn still running, with optional narration
    segments (assistant messages) and tool activities attributed to the turn."""
    messages = [{"id": "user-1", "role": "user", "turnId": turn_id,
                 "streaming": False, "text": "the inbound prompt"}]
    for i, text in enumerate(narration or []):
        messages.append({"id": f"assist-seg-{i}", "role": "assistant",
                         "turnId": turn_id, "streaming": True, "text": text})
    activities = [
        {"id": f"act-{i}", "tone": "tool", "turnId": turn_id, "summary": s}
        for i, s in enumerate(tool or [])
    ]
    return {
        "thread": {
            "latestTurn": {
                "turnId": turn_id,
                "state": "running",
                "requestedAt": FRESH_TS,
                "assistantMessageId": None,
            },
            "messages": messages,
            "activities": activities,
        }
    }


def test_user_message_is_ledgered_before_any_dispatch(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-ledger"
    mirror = make_mirror(tmp_path, thread_id)

    def assert_already_ledgered(command):
        posted = mirror.threads()[thread_id]["posted"]
        assert any(mid.startswith("slack-user-") for mid in posted), (
            "the inbound user message must be ledgered in the mirror before "
            "the very first dispatch call"
        )
        raise T3Error("boom - abort turn early, we already asserted what we need")

    client = FakeT3Client(dispatch_hook=assert_already_ledgered)

    result = run_turn(
        prompt="hi",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={"provider": "anthropic"},
        title="t",
        client=client,
        mirror=mirror,
    )

    assert result.ok is False
    # sanity: the hook did in fact run (dispatch was called at least once)
    assert client.calls


def test_new_thread_dispatches_create_before_turn_start_with_expected_fields(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-new"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(snapshots=[completed_snapshot()])

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=True,
        project_id="proj-42",
        model={"provider": "anthropic", "model": "claude"},
        title="My Thread",
        client=client,
        mirror=mirror,
        runtime_mode="approval-required",
    )

    assert result.ok is True
    types = client.dispatch_types()
    assert types.index("thread.create") < types.index("thread.turn.start")

    create_cmd = next(c for k, c in client.calls if k == "dispatch" and c["type"] == "thread.create")
    assert create_cmd["projectId"] == "proj-42"
    assert create_cmd["title"] == "My Thread"
    assert create_cmd["modelSelection"] == {"provider": "anthropic", "model": "claude"}
    assert create_cmd["runtimeMode"] == "approval-required"
    assert create_cmd["threadId"] == thread_id


def test_thread_create_error_is_swallowed_and_turn_start_still_dispatched(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-create-fails"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(
        snapshots=[completed_snapshot()],
        dispatch_errors={"thread.create": T3Error("thread already exists")},
    )

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=True,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
    )

    types = client.dispatch_types()
    assert "thread.create" in types
    assert "thread.turn.start" in types
    # the swallowed error did not stop the turn from completing normally
    assert result.ok is True


def test_existing_thread_skips_thread_create(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-existing"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(snapshots=[completed_snapshot()])

    run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
    )

    assert "thread.create" not in client.dispatch_types()


def test_turn_start_error_returns_not_ok(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-turnstart-fails"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(dispatch_errors={"thread.turn.start": T3Error("server exploded")})

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
    )

    assert result.ok is False
    assert "server exploded" in result.error


def test_stale_terminal_turn_is_ignored_until_fresh_turn_completes(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-resume"
    mirror = make_mirror(tmp_path, thread_id)
    stale = completed_snapshot(assistant_id="assist-old", requested_at=STALE_TS,
                                messages=[{"id": "assist-old", "streaming": False, "text": "OLD - must be ignored"}])
    fresh = completed_snapshot(assistant_id="assist-new", requested_at=FRESH_TS,
                                messages=[{"id": "assist-new", "streaming": False, "text": "NEW"}])
    client = FakeT3Client(snapshots=[stale, fresh])

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
    )

    assert result.ok is True
    assert result.text == "NEW"
    # exactly two polls happened: the stale one (skipped) and the fresh one
    snapshot_calls = [c for k, c in client.calls if k == "snapshot"]
    assert len(snapshot_calls) == 2


def test_completed_turn_extracts_text_only_from_matching_non_streaming_message(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-text-extract"
    mirror = make_mirror(tmp_path, thread_id)
    messages = [
        {"id": "assist-0", "streaming": False, "text": "WRONG - different message id"},
        {"id": "assist-1", "streaming": True, "text": "WRONG - still streaming"},
        {"id": "assist-1", "streaming": False, "text": "correct final text"},
    ]
    client = FakeT3Client(snapshots=[
        completed_snapshot(assistant_id="assist-1", messages=messages)
    ])

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
    )

    assert result.ok is True
    assert result.text == "correct final text"
    assert result.session_id == thread_id
    assert mirror.is_posted(thread_id, "assist-1") is True


@pytest.mark.parametrize("state", ["error", "interrupted"])
def test_terminal_error_or_interrupted_state_returns_not_ok_with_state_in_message(tmp_path, monkeypatch, state):
    fast_poll(monkeypatch)
    thread_id = f"t3-thread-{state}"
    mirror = make_mirror(tmp_path, thread_id)
    snapshot = {
        "thread": {
            "latestTurn": {
                "state": state,
                "requestedAt": FRESH_TS,
                "assistantMessageId": None,
            },
            "messages": [],
        }
    }
    client = FakeT3Client(snapshots=[snapshot])

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
    )

    assert result.ok is False
    assert state in result.error


def test_timeout_returns_not_ok_and_dispatches_interrupt(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-timeout"
    mirror = make_mirror(tmp_path, thread_id)
    # No terminal snapshot ever provided; timeout=0 means the poll loop body
    # never runs (deadline already passed by the time it's checked).
    client = FakeT3Client(snapshots=[])

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
        timeout=0,
    )

    assert result.ok is False
    assert "timed out" in result.error
    assert "thread.turn.interrupt" in client.dispatch_types()


def test_progress_reports_latest_narration_and_tool_then_final_text(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-progress"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(snapshots=[
        running_snapshot(narration=["I'll start by exploring the frontend."],
                         tool=["Bash: grep -n foo app.py"]),
        running_snapshot(narration=["I'll start by exploring the frontend.",
                                    "Now mirroring the validation rules."],
                         tool=["Bash: grep -n foo app.py",
                               "Read handler.py"]),
        completed_snapshot(),
    ])
    updates: list[str] = []

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
        on_progress=updates.append,
    )

    assert result.ok is True
    assert result.text == "hello from t3"
    assert updates == [
        "I'll start by exploring the frontend.\n`Bash: grep -n foo app.py`",
        "Now mirroring the validation rules.\n`Read handler.py`",
    ]


def test_progress_not_re_emitted_when_snapshot_unchanged(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-progress-dedupe"
    mirror = make_mirror(tmp_path, thread_id)
    same = running_snapshot(narration=["thinking"], tool=["Bash: ls"])
    client = FakeT3Client(snapshots=[same, same, same, completed_snapshot()])
    updates: list[str] = []

    run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
        on_progress=updates.append,
    )

    assert updates == ["thinking\n`Bash: ls`"]


def test_progress_callback_error_does_not_kill_the_turn(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-progress-raises"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(snapshots=[
        running_snapshot(narration=["thinking"]),
        completed_snapshot(),
    ])

    def boom(_text: str) -> None:
        raise RuntimeError("slack edit failed")

    result = run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=False,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
        on_progress=boom,
    )

    assert result.ok is True
    assert result.text == "hello from t3"


def test_runtime_mode_propagates_to_create_and_turn_start(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-runtime-mode"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(snapshots=[completed_snapshot()])

    run_turn(
        prompt="hello",
        thread_id=thread_id,
        is_new=True,
        project_id="proj-1",
        model={},
        title="t",
        client=client,
        mirror=mirror,
        runtime_mode="approval-required",
    )

    create_cmd = next(c for k, c in client.calls if k == "dispatch" and c["type"] == "thread.create")
    turn_start_cmd = next(c for k, c in client.calls if k == "dispatch" and c["type"] == "thread.turn.start")
    assert create_cmd["runtimeMode"] == "approval-required"
    assert turn_start_cmd["runtimeMode"] == "approval-required"


# --- approval-required turns parked on a human in the T3 GUI --------------


def parked_snapshot(turn_id="turn-1", request_id="req-1", resolved=False,
                    kind="approval.requested", detail="Bash: rm -rf build"):
    """A running turn whose newest activity is an approval (or user-input)
    request; `resolved=True` appends the matching resolution."""
    snap = running_snapshot(turn_id=turn_id, narration=["Let me clean up."])
    resolved_kind = backend_t3._BLOCKING_KINDS[kind]
    payload = ({"requestId": request_id, "questions": [{"question": "Which one?"}]}
               if kind == "user-input.requested"
               else {"requestId": request_id, "requestKind": "command", "detail": detail})
    acts = snap["thread"]["activities"]
    acts.append({"id": "a-req", "tone": "approval", "kind": kind, "turnId": turn_id,
                 "summary": "Command approval requested", "payload": payload})
    if resolved:
        acts.append({"id": "a-res", "tone": "approval", "kind": resolved_kind,
                     "turnId": turn_id, "summary": "Approval resolved",
                     "payload": {"requestId": request_id, "decision": "accept"}})
    return snap


def test_pending_requests_tracks_open_minus_resolved():
    thread = parked_snapshot()["thread"]
    pending = backend_t3._pending_requests(thread, "turn-1")
    assert [r["requestId"] for r in pending] == ["req-1"]
    assert pending[0]["kind"] == "approval.requested"
    assert backend_t3._pending_requests(parked_snapshot(resolved=True)["thread"], "turn-1") == []
    # Requests from another turn don't count.
    assert backend_t3._pending_requests(thread, "other-turn") == []


def test_describe_request_labels_kinds():
    assert backend_t3.describe_request(
        {"kind": "approval.requested", "requestKind": "command", "detail": "Bash: ls"}
    ) == "run a command: `Bash: ls`"
    assert backend_t3.describe_request(
        {"kind": "approval.requested", "requestKind": "file-change"}) == "change a file"
    assert backend_t3.describe_request(
        {"kind": "user-input.requested", "questions": [{"question": "Which one?"}]}
    ) == "a question for you: Which one?"


def test_pending_approval_pauses_turn_clock_and_pages_once(tmp_path, monkeypatch):
    """The 900s-style turn timeout must not fire while T3 is waiting on the
    owner: the parked polls extend the deadline, the owner is paged exactly
    once per request, the placeholder says it's paused, and the turn completes
    normally once approved."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-parked"
    mirror = make_mirror(tmp_path, thread_id)
    parked = parked_snapshot()
    # ~40 parked polls at 0.01s each is well past a 0.2s turn timeout.
    client = FakeT3Client(snapshots=[parked] * 40 + [parked_snapshot(resolved=True),
                                                     completed_snapshot()])
    paged: list[list[dict]] = []
    updates: list[str] = []

    result = run_turn(
        prompt="hello", thread_id=thread_id, is_new=False, project_id="p",
        model={}, title="t", client=client, mirror=mirror,
        timeout=0.2, approval_timeout=60,
        on_progress=updates.append, on_approval_wait=paged.append,
        owner_name="Dan",
    )

    assert result.ok is True and result.text == "hello from t3"
    assert "thread.turn.interrupt" not in client.dispatch_types()
    assert len(paged) == 1 and paged[0][0]["requestId"] == "req-1"
    assert any("waiting for Dan to approve run a command" in u for u in updates)
    # After the approval clears, progress drops the paused line again.
    assert "paused" not in updates[-1]


def test_approval_timeout_releases_without_interrupting(tmp_path, monkeypatch):
    """When the owner never acts, the bridge stops holding the Slack thread but
    leaves the T3 turn (and its pending approval) alone so it can still be
    approved later; the result is flagged awaiting_approval, not a generic error."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-parked-forever"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(snapshots=[parked_snapshot()])

    result = run_turn(
        prompt="hello", thread_id=thread_id, is_new=False, project_id="p",
        model={}, title="t", client=client, mirror=mirror,
        timeout=0.1, approval_timeout=0.05,
    )

    assert result.ok is False
    assert result.awaiting_approval is True
    assert "waiting for approval" in result.error
    assert "thread.turn.interrupt" not in client.dispatch_types()


def test_user_input_request_counts_as_parked(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-question"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(snapshots=[parked_snapshot(kind="user-input.requested")] * 5
                          + [completed_snapshot()])
    paged: list[list[dict]] = []

    result = run_turn(
        prompt="hello", thread_id=thread_id, is_new=False, project_id="p",
        model={}, title="t", client=client, mirror=mirror,
        timeout=5, on_approval_wait=paged.append,
    )

    assert result.ok is True
    assert len(paged) == 1 and paged[0][0]["kind"] == "user-input.requested"


def test_approval_wait_callback_error_does_not_kill_the_turn(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-parked-cb-error"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(snapshots=[parked_snapshot(), completed_snapshot()])

    def boom(_):
        raise RuntimeError("slack down")

    result = run_turn(
        prompt="hello", thread_id=thread_id, is_new=False, project_id="p",
        model={}, title="t", client=client, mirror=mirror,
        timeout=5, on_approval_wait=boom,
    )
    assert result.ok is True
