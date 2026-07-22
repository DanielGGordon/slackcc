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
