"""Tests for backend_t3.run_turn: the T3-backed turn runner (orchestration v2).

Fully offline: FakeT3Client below never touches the network; MirrorStore is a
real store backed by a tmp_path file.
"""

from __future__ import annotations

import pytest

from slackcc import backend_t3
from slackcc.backend_t3 import run_turn
from slackcc.t3 import TERMINAL_RUN_STATUSES, MirrorStore, T3Error

RUN_ID = "run:thread:t1:ordinal:1"
OLD_RUN_ID = "run:thread:t1:ordinal:0"


class FakeT3Client:
    """Scripted T3Client double: records every call, can raise on dispatch (or
    upload) by command type, and hands back a scripted sequence of thread
    projections (the last one repeats if polled more times than scripted).

    The inbound message id is generated inside run_turn, so each scripted
    projection may be a plain dict or a callable taking that id (learned from
    the recorded `message.dispatch` command) and returning a dict."""

    def __init__(self, projections=None, dispatch_errors=None, dispatch_hook=None,
                 upload_errors=None):
        self.calls: list[tuple[str, dict]] = []
        self._projections = list(projections or [])
        self.dispatch_errors = dict(dispatch_errors or {})
        self.dispatch_hook = dispatch_hook
        self.upload_errors = dict(upload_errors or {})  # image name -> exception
        self.message_id: str | None = None

    def dispatch(self, command: dict) -> dict:
        self.calls.append(("dispatch", command))
        if command["type"] == "message.dispatch":
            self.message_id = command["messageId"]
        if self.dispatch_hook is not None:
            self.dispatch_hook(command)
        err = self.dispatch_errors.get(command["type"])
        if err is not None:
            raise err
        return {}

    def upload_image(self, *, name: str, mime_type: str, data: bytes) -> dict:
        self.calls.append(("upload", {"name": name, "mimeType": mime_type, "data": data}))
        err = self.upload_errors.get(name)
        if err is not None:
            raise err
        return {"type": "image", "id": f"att-{name}", "name": name,
                "mimeType": mime_type, "sizeBytes": len(data)}

    def thread_projection(self, thread_id: str) -> dict:
        self.calls.append(("projection", {"threadId": thread_id}))
        if not self._projections:
            return {}
        if len(self._projections) > 1:
            entry = self._projections.pop(0)
        else:
            entry = self._projections[0]
        return entry(self.message_id) if callable(entry) else entry

    def dispatch_types(self) -> list[str]:
        return [cmd["type"] for kind, cmd in self.calls if kind == "dispatch"]

    def dispatched(self, type_: str) -> dict:
        return next(c for k, c in self.calls if k == "dispatch" and c["type"] == type_)

    def poll_count(self) -> int:
        return sum(1 for k, _ in self.calls if k == "projection")


def make_mirror(tmp_path, thread_id, channel="C123", thread_ts="111.222"):
    mirror = MirrorStore(tmp_path / "mirror.json")
    mirror.register(thread_id, channel, thread_ts)
    return mirror


def fast_poll(monkeypatch):
    monkeypatch.setattr(backend_t3, "_POLL_SECS", 0.01)
    monkeypatch.setattr(backend_t3, "_PROGRESS_MIN_SECS", 0.0)


def go(client, mirror, thread_id, **kwargs):
    """run_turn with the boring arguments filled in."""
    args = dict(prompt="hello", thread_id=thread_id, is_new=False, project_id="proj-1",
                model={}, title="t", client=client, mirror=mirror)
    args.update(kwargs)
    return run_turn(**args)


def assistant_msg(msg_id, text, run_id=RUN_ID, streaming=False):
    return {"id": msg_id, "role": "assistant", "runId": run_id, "text": text,
            "streaming": streaming, "updatedAt": "2026-10-07T12:00:00Z"}


def tool_item(item_id, title, run_id=RUN_ID, type_="command_execution", **extra):
    return {"id": item_id, "type": type_, "runId": run_id, "title": title, **extra}


def projection(message_id, status="completed", *, run_id=RUN_ID, reply="hello from t3",
               reply_id="assist-1", narration=None, tools=None, extra_messages=(),
               extra_runs=(), user_message_run_id=..., run_user_message_id=...,
               runtime_requests=None, items=None):
    """A v2 thread projection for the run carrying our message.

    By default the user message is attributed to `run_id` and the run's
    `userMessageId` is our message. `reply=None` omits the assistant reply;
    `narration` adds assistant segments (still streaming while the run is
    live, finished once it is terminal) and `tools` adds tool items (title
    strings) before it."""
    if user_message_run_id is ...:
        user_message_run_id = run_id
    if run_user_message_id is ...:
        run_user_message_id = message_id
    messages = [{"id": message_id, "role": "user", "runId": user_message_run_id,
                 "text": "the inbound prompt", "streaming": False,
                 "updatedAt": "2026-10-07T12:00:00Z"}]
    for i, text in enumerate(narration or []):
        messages.append(assistant_msg(f"assist-seg-{i}", text, run_id,
                                      streaming=status not in TERMINAL_RUN_STATUSES))
    if reply is not None:
        messages.append(assistant_msg(reply_id, reply, run_id))
    messages.extend(extra_messages)
    turn_items = [tool_item(f"item-{i}", t, run_id) for i, t in enumerate(tools or [])]
    turn_items.extend(items or [])
    return {
        "thread": {"id": "t1", "title": "t"},
        "runs": [*extra_runs,
                 {"id": run_id, "status": status, "userMessageId": run_user_message_id}],
        "messages": messages,
        "turnItems": turn_items,
        "runtimeRequests": list(runtime_requests or []),
    }


def completed(**kwargs):
    return lambda mid: projection(mid, "completed", **kwargs)


def running(**kwargs):
    kwargs.setdefault("reply", None)
    return lambda mid: projection(mid, "running", **kwargs)


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

    result = go(client, mirror, thread_id, prompt="hi")

    assert result.ok is False
    # sanity: the hook did in fact run (dispatch was called at least once)
    assert client.calls


def test_new_thread_dispatches_create_before_message_with_expected_fields(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-new"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()])

    result = go(client, mirror, thread_id, is_new=True, project_id="proj-42",
                model={"provider": "anthropic", "model": "claude"}, title="My Thread",
                runtime_mode="approval-required")

    assert result.ok is True
    types = client.dispatch_types()
    assert types.index("thread.create") < types.index("message.dispatch")

    create_cmd = client.dispatched("thread.create")
    assert create_cmd["projectId"] == "proj-42"
    assert create_cmd["title"] == "My Thread"
    assert create_cmd["modelSelection"] == {"provider": "anthropic", "model": "claude"}
    assert create_cmd["runtimeMode"] == "approval-required"
    assert create_cmd["threadId"] == thread_id
    assert create_cmd["createdBy"] == "user"
    assert create_cmd["creationSource"] == "web"
    assert create_cmd["interactionMode"] == "default"
    assert create_cmd["branch"] is None
    assert create_cmd["worktreePath"] is None
    assert create_cmd["commandId"]
    assert "createdAt" not in create_cmd


def test_thread_create_error_is_swallowed_and_message_still_dispatched(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-create-fails"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(
        projections=[completed()],
        dispatch_errors={"thread.create": T3Error("thread already exists")},
    )

    result = go(client, mirror, thread_id, is_new=True)

    types = client.dispatch_types()
    assert "thread.create" in types
    assert "message.dispatch" in types
    # the swallowed error did not stop the run from completing normally
    assert result.ok is True


def test_message_dispatch_command_shape(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-shape"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()])

    go(client, mirror, thread_id, prompt="do the thing", runtime_mode="approval-required")

    cmd = client.dispatched("message.dispatch")
    assert cmd["threadId"] == thread_id
    assert cmd["messageId"].startswith("slack-user-")
    assert cmd["text"] == "do the thing"
    assert cmd["createdBy"] == "user"
    assert cmd["creationSource"] == "web"
    assert cmd["deliveryIntent"] == "auto"
    assert cmd["dispatchMode"] == {"type": "start_immediately"}
    assert cmd["commandId"]
    # runtimeMode lives on thread.create only
    assert "runtimeMode" not in cmd
    assert "message" not in cmd
    # the id we ledgered is the id we dispatched
    assert mirror.is_posted(thread_id, cmd["messageId"]) is True


def test_runtime_mode_goes_on_thread_create_only(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-runtime-mode"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()])

    go(client, mirror, thread_id, is_new=True, runtime_mode="approval-required")

    assert client.dispatched("thread.create")["runtimeMode"] == "approval-required"
    assert "runtimeMode" not in client.dispatched("message.dispatch")


def test_default_runtime_mode_is_full_access(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-default-mode"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()])

    go(client, mirror, thread_id, is_new=True)

    assert client.dispatched("thread.create")["runtimeMode"] == "full-access"


def test_existing_thread_skips_thread_create(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-existing"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()])

    go(client, mirror, thread_id)

    assert "thread.create" not in client.dispatch_types()


def test_no_images_defaults_to_empty_attachments_and_no_uploads(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-no-images"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()])

    go(client, mirror, thread_id)

    assert client.dispatched("message.dispatch")["attachments"] == []
    assert not [k for k, _ in client.calls if k == "upload"]


def test_images_are_uploaded_and_attached_to_message_dispatch(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-images"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()])
    images = [
        {"name": "shot.png", "mimeType": "image/png", "data": b"abc"},
        {"name": "photo.jpg", "mimeType": "image/jpeg", "data": b"12345"},
    ]

    result = go(client, mirror, thread_id, images=images)

    assert result.ok is True
    uploads = [c for k, c in client.calls if k == "upload"]
    assert uploads == [
        {"name": "shot.png", "mimeType": "image/png", "data": b"abc"},
        {"name": "photo.jpg", "mimeType": "image/jpeg", "data": b"12345"},
    ]
    assert client.dispatched("message.dispatch")["attachments"] == [
        {"type": "image", "id": "att-shot.png", "name": "shot.png",
         "mimeType": "image/png", "sizeBytes": 3},
        {"type": "image", "id": "att-photo.jpg", "name": "photo.jpg",
         "mimeType": "image/jpeg", "sizeBytes": 5},
    ]
    # uploads happen before the message that references them is dispatched
    kinds = [k for k, _ in client.calls]
    assert kinds.index("upload") < kinds.index("dispatch")


def test_failed_image_upload_drops_only_that_image(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-image-fails"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()],
                          upload_errors={"bad.png": T3Error("upload rejected")})
    images = [
        {"name": "bad.png", "mimeType": "image/png", "data": b"x"},
        {"name": "good.png", "mimeType": "image/png", "data": b"yy"},
    ]

    result = go(client, mirror, thread_id, images=images)

    assert result.ok is True
    attachments = client.dispatched("message.dispatch")["attachments"]
    assert [a["name"] for a in attachments] == ["good.png"]


def test_all_images_failing_still_dispatches_message(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-images-all-fail"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()],
                          upload_errors={"a.png": T3Error("nope")})

    result = go(client, mirror, thread_id,
                images=[{"name": "a.png", "mimeType": "image/png", "data": b"x"}])

    assert result.ok is True
    assert client.dispatched("message.dispatch")["attachments"] == []


def test_message_dispatch_error_returns_not_ok(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-dispatch-fails"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(dispatch_errors={"message.dispatch": T3Error("server exploded")})

    result = go(client, mirror, thread_id)

    assert result.ok is False
    assert "server exploded" in result.error
    assert result.session_id is None
    # we never got as far as polling
    assert client.poll_count() == 0


def test_preexisting_completed_run_is_ignored_until_our_run_completes(tmp_path, monkeypatch):
    """A finished run for an earlier message is in the projection from the
    start; it must not be mistaken for ours. We keep polling until our message
    shows up attached to a run and that run completes."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-resume"
    mirror = make_mirror(tmp_path, thread_id)
    old_run = {"id": OLD_RUN_ID, "status": "completed", "userMessageId": "slack-user-old"}
    old_messages = [
        {"id": "slack-user-old", "role": "user", "runId": OLD_RUN_ID, "text": "earlier",
         "streaming": False},
        assistant_msg("assist-old", "OLD - must be ignored", OLD_RUN_ID),
    ]

    def only_old(mid):  # our message not visible yet
        return {"runs": [old_run], "messages": list(old_messages), "turnItems": [],
                "runtimeRequests": []}

    def message_visible_run_unknown(mid):  # message landed but no run row yet
        proj = only_old(mid)
        proj["messages"].append({"id": mid, "role": "user", "runId": RUN_ID, "text": "hi",
                                 "streaming": False})
        return proj

    def ours_completed(mid):
        proj = projection(mid, "completed", reply="NEW", reply_id="assist-new",
                          extra_runs=[old_run])
        proj["messages"] = old_messages + proj["messages"]
        return proj

    client = FakeT3Client(projections=[only_old, message_visible_run_unknown, ours_completed])

    result = go(client, mirror, thread_id)

    assert result.ok is True
    assert result.text == "NEW"
    assert client.poll_count() == 3
    assert mirror.is_posted(thread_id, "assist-new") is True
    assert mirror.is_posted(thread_id, "assist-old") is False


def test_run_found_by_user_message_id_before_message_has_run_id(tmp_path, monkeypatch):
    """While a run is queued the message may not carry runId yet; the run's
    userMessageId still identifies it."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-usermessageid"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[
        lambda mid: projection(mid, "completed", user_message_run_id=None),
    ])

    result = go(client, mirror, thread_id)

    assert result.ok is True
    assert result.text == "hello from t3"


def test_message_steered_into_active_run_follows_that_run(tmp_path, monkeypatch):
    """T3 steers our message into a run the owner already started in the GUI:
    message.runId points at that run, but the run's userMessageId is the
    owner's message. We follow that run to completion and return its reply."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-steered"
    mirror = make_mirror(tmp_path, thread_id)
    steered = dict(user_message_run_id=OLD_RUN_ID, run_user_message_id="gui-user-msg",
                   run_id=OLD_RUN_ID)
    client = FakeT3Client(projections=[
        lambda mid: projection(mid, "running", reply=None, narration=["working..."],
                               **steered),
        lambda mid: projection(mid, "completed", reply="steered answer", **steered),
    ])

    result = go(client, mirror, thread_id)

    assert result.ok is True
    assert result.text == "steered answer"
    assert result.session_id == thread_id


def test_final_reply_is_the_last_eligible_assistant_message(tmp_path, monkeypatch):
    """Earlier assistant segments are narration; the answer is the last
    non-streaming, non-empty assistant message of this run."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-last-wins"
    mirror = make_mirror(tmp_path, thread_id)
    extra = [
        assistant_msg("assist-narration", "narration - not the final answer"),
        assistant_msg("assist-final", "the real answer"),
        assistant_msg("assist-other", "WRONG - different run", OLD_RUN_ID),
        assistant_msg("assist-empty", "   "),
    ]
    client = FakeT3Client(projections=[
        lambda mid: projection(mid, "completed", reply=None, extra_messages=extra),
    ])

    result = go(client, mirror, thread_id)

    assert result.ok is True
    assert result.text == "the real answer"
    assert result.session_id == thread_id
    assert mirror.is_posted(thread_id, "assist-final") is True
    assert mirror.is_posted(thread_id, "assist-narration") is False
    assert mirror.is_posted(thread_id, "assist-other") is False


def test_completed_run_final_text_is_stripped_and_ledgered(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-text-final"
    mirror = make_mirror(tmp_path, thread_id)
    extra = [
        assistant_msg("assist-other", "WRONG - different run", OLD_RUN_ID),
    ]
    client = FakeT3Client(projections=[
        lambda mid: projection(mid, "completed", reply="  correct final text \n",
                               reply_id="assist-final", narration=["earlier narration"],
                               extra_messages=extra),
    ])

    result = go(client, mirror, thread_id)

    assert result.ok is True
    assert result.text == "correct final text"
    assert mirror.is_posted(thread_id, "assist-final") is True


def test_terminal_run_waits_for_a_still_streaming_last_segment(tmp_path, monkeypatch):
    """A run can read terminal a beat before its last segment stops streaming:
    the earlier finished segment is narration, not the answer -- wait a poll."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-late-segment"
    mirror = make_mirror(tmp_path, thread_id)
    narration = assistant_msg("assist-narration", "narration - not the answer")
    client = FakeT3Client(projections=[
        lambda mid: projection(mid, "completed", reply=None, extra_messages=[
            narration, assistant_msg("assist-final", "the ans", streaming=True)]),
        lambda mid: projection(mid, "completed", reply=None, extra_messages=[
            narration, assistant_msg("assist-final", "the answer")]),
    ])

    result = go(client, mirror, thread_id)

    assert result.ok is True
    assert result.text == "the answer"
    assert mirror.is_posted(thread_id, "assist-final") is True
    assert mirror.is_posted(thread_id, "assist-narration") is False


def test_completed_run_without_reply_is_ok_with_empty_text(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-no-reply"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed(reply=None)])

    result = go(client, mirror, thread_id)

    assert result.ok is True
    assert result.text == ""
    assert result.session_id == thread_id


def test_terminal_status_set_is_what_we_expect():
    assert TERMINAL_RUN_STATUSES == {"completed", "interrupted", "failed", "cancelled",
                                     "rolled_back"}


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "rolled_back"])
def test_non_completed_terminal_status_returns_not_ok_with_status_in_error(tmp_path, monkeypatch, status):
    fast_poll(monkeypatch)
    thread_id = f"t3-thread-{status}"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[
        lambda mid: projection(mid, status, reply=None),
    ])

    result = go(client, mirror, thread_id)

    assert result.ok is False
    assert result.error == f"T3 run ended in state '{status}'"
    assert result.session_id == thread_id
    assert result.text == ""


def test_non_completed_terminal_status_still_returns_partial_reply(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-failed-partial"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[
        lambda mid: projection(mid, "failed", reply="got this far", reply_id="assist-part"),
    ])

    result = go(client, mirror, thread_id)

    assert result.ok is False
    assert result.text == "got this far"
    assert "failed" in result.error
    assert mirror.is_posted(thread_id, "assist-part") is True


def test_projection_poll_error_is_retried(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-poll-error"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[completed()])
    real = client.thread_projection
    attempts = {"n": 0}

    def flaky(tid):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise T3Error("transient")
        return real(tid)

    client.thread_projection = flaky

    result = go(client, mirror, thread_id)

    assert result.ok is True
    assert attempts["n"] == 2


def test_timeout_before_run_found_does_not_dispatch_interrupt(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-timeout-norun"
    mirror = make_mirror(tmp_path, thread_id)
    # timeout=0: the poll loop body never runs, so no run is ever found.
    client = FakeT3Client(projections=[])

    result = go(client, mirror, thread_id, timeout=0)

    assert result.ok is False
    assert result.error == "T3 turn timed out after 0s (interrupted)"
    assert "run.interrupt" not in client.dispatch_types()


def test_timeout_with_message_never_picked_up_does_not_dispatch_interrupt(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-timeout-unseen"
    mirror = make_mirror(tmp_path, thread_id)
    old_run = {"id": OLD_RUN_ID, "status": "running", "userMessageId": "someone-else"}
    client = FakeT3Client(projections=[{"runs": [old_run], "messages": []}])

    result = go(client, mirror, thread_id, timeout=0.05)

    assert result.ok is False
    assert "timed out" in result.error
    assert client.poll_count() > 0
    assert "run.interrupt" not in client.dispatch_types()


def test_timeout_with_active_run_dispatches_interrupt_for_that_run(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-timeout"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[running(narration=["still going"])])

    result = go(client, mirror, thread_id, timeout=0.05)

    assert result.ok is False
    assert result.error == "T3 turn timed out after 0.05s (interrupted)"
    assert result.session_id == thread_id
    interrupt = client.dispatched("run.interrupt")
    assert interrupt["threadId"] == thread_id
    assert interrupt["runId"] == RUN_ID
    assert interrupt["commandId"]
    assert set(interrupt) == {"type", "commandId", "threadId", "runId"}


def test_interrupt_failure_on_timeout_is_swallowed(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-timeout-int-fails"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[running()],
                          dispatch_errors={"run.interrupt": T3Error("cannot interrupt")})

    result = go(client, mirror, thread_id, timeout=0.05)

    assert result.ok is False
    assert "timed out" in result.error


# --- progress ---------------------------------------------------------------


def test_progress_reports_latest_narration_and_tool_then_final_text(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-progress"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[
        running(narration=["I'll start by exploring the frontend."],
                tools=["grep -n foo app.py"]),
        running(narration=["I'll start by exploring the frontend.",
                           "Now mirroring the validation rules."],
                tools=["grep -n foo app.py", "Read handler.py"]),
        completed(),
    ])
    updates: list[str] = []

    result = go(client, mirror, thread_id, on_progress=updates.append)

    assert result.ok is True
    assert result.text == "hello from t3"
    assert updates == [
        "I'll start by exploring the frontend.\n`grep -n foo app.py`",
        "Now mirroring the validation rules.\n`Read handler.py`",
    ]


def test_progress_not_re_emitted_when_projection_unchanged(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-progress-dedupe"
    mirror = make_mirror(tmp_path, thread_id)
    same = running(narration=["thinking"], tools=["ls"])
    client = FakeT3Client(projections=[same, same, same, completed()])
    updates: list[str] = []

    go(client, mirror, thread_id, on_progress=updates.append)

    assert updates == ["thinking\n`ls`"]


def test_progress_is_rate_limited(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    monkeypatch.setattr(backend_t3, "_PROGRESS_MIN_SECS", 3600.0)
    thread_id = "t3-thread-progress-rate"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[
        running(narration=["first"]),
        running(narration=["first", "second"]),
        running(narration=["first", "second", "third"]),
        completed(),
    ])
    updates: list[str] = []

    go(client, mirror, thread_id, on_progress=updates.append)

    # the first emission is allowed; the rest fall inside the rate-limit window
    assert updates == ["first"]


def test_progress_ignores_other_runs_and_non_tool_items(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-progress-scope"
    mirror = make_mirror(tmp_path, thread_id)
    items = [
        tool_item("i-other", "WRONG other run", OLD_RUN_ID),
        {"id": "i-reason", "type": "reasoning", "runId": RUN_ID, "title": "WRONG reasoning"},
        tool_item("i-edit", None, type_="file_change", fileName="src/app.py"),
        tool_item("i-blank", "   "),
    ]
    extra = [assistant_msg("assist-other", "WRONG other run narration", OLD_RUN_ID, True)]
    client = FakeT3Client(projections=[
        running(narration=["editing"], items=items, extra_messages=extra),
        completed(),
    ])
    updates: list[str] = []

    go(client, mirror, thread_id, on_progress=updates.append)

    assert updates == ["editing\n`src/app.py`"]


def test_progress_tool_label_falls_back_to_input_and_clips_narration():
    proj = {"messages": [assistant_msg("a", "x" * 1000, streaming=True)],
            "turnItems": [{"type": "dynamic_tool", "runId": RUN_ID, "input": "mcp__brain__search"}]}

    summary = backend_t3._progress_summary(proj, RUN_ID)

    narration, tool = summary.split("\n")
    assert len(narration) == backend_t3._NARRATION_CLIP
    assert narration.endswith("…")
    assert tool == "`mcp__brain__search`"


def test_progress_callback_error_does_not_kill_the_turn(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-progress-raises"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[running(narration=["thinking"]), completed()])

    def boom(_text: str) -> None:
        raise RuntimeError("slack edit failed")

    result = go(client, mirror, thread_id, on_progress=boom)

    assert result.ok is True
    assert result.text == "hello from t3"


# --- runs parked on a human in the T3 GUI -----------------------------------


def parked(request_id="req-1", kind="command", resolved=False, run_id=RUN_ID,
           prompt="rm -rf build"):
    """A running run whose newest runtime request is pending (or, with
    `resolved=True`, already answered). `kind="user_input"` makes it a question."""
    if kind == "user_input":
        item = {"id": "item-req", "type": "user_input_request", "runId": run_id,
                "requestId": request_id, "title": "Question",
                "questions": [{"question": "Which one?", "header": "Pick"}]}
    else:
        item = {"id": "item-req", "type": "approval_request", "runId": run_id,
                "requestId": request_id, "requestKind": kind, "title": "Approve?",
                "prompt": prompt}
    request = {"id": request_id, "kind": kind,
               "status": "resolved" if resolved else "pending"}
    return running(narration=["Let me clean up."], items=[item], runtime_requests=[request])


def test_pending_requests_only_pending_and_scoped_to_run():
    proj = parked()("slack-user-x")
    pending = backend_t3._pending_requests(proj, RUN_ID)
    assert pending == [{
        "requestId": "req-1", "kind": "command", "requestKind": "command",
        "prompt": "rm -rf build", "questions": [],
    }]
    assert backend_t3._pending_requests(parked(resolved=True)("slack-user-x"), RUN_ID) == []
    # Requests whose item belongs to another run don't count.
    assert backend_t3._pending_requests(proj, OLD_RUN_ID) == []


def test_pending_requests_item_without_run_id_is_allowed_and_item_is_optional():
    proj = {
        "runtimeRequests": [
            {"id": "req-a", "kind": "command", "status": "pending"},
            {"id": "req-b", "kind": "user_input", "status": "pending"},
            {"id": "req-c", "kind": "command", "status": "answered"},
        ],
        "turnItems": [
            {"type": "user_input_request", "requestId": "req-b", "runId": None,
             "title": "Question title", "questions": [{"question": "Which?"}]},
            {"type": "approval_request", "requestId": "req-c", "runId": RUN_ID},
        ],
    }

    pending = backend_t3._pending_requests(proj, RUN_ID)

    assert [r["requestId"] for r in pending] == ["req-a", "req-b"]
    # no item: requestKind falls back to the runtime request kind, prompt is empty
    assert pending[0]["requestKind"] == "command" and pending[0]["prompt"] == ""
    # prompt falls back to the item title
    assert pending[1]["prompt"] == "Question title"
    assert pending[1]["questions"] == [{"question": "Which?"}]


def test_describe_request_labels_kinds():
    assert backend_t3.describe_request(
        {"kind": "command", "requestKind": "command", "prompt": "ls"}
    ) == "run a command: `ls`"
    assert backend_t3.describe_request(
        {"kind": "file-change", "requestKind": "file-change"}) == "change a file"
    assert backend_t3.describe_request(
        {"kind": "file-read", "requestKind": "file-read", "prompt": "a.txt"}
    ) == "read a file: `a.txt`"
    assert backend_t3.describe_request(
        {"kind": "mystery", "requestKind": "mystery"}) == "a tool call"
    assert backend_t3.describe_request(
        {"kind": "user_input", "requestKind": "user_input",
         "questions": [{"question": "Which one?", "header": "Pick"}]}
    ) == "a question for you: Which one?"
    assert backend_t3.describe_request(
        {"kind": "user_input", "questions": [{"header": "Pick"}]}
    ) == "a question for you: Pick"
    assert backend_t3.describe_request({"kind": "user_input"}) == "a question for you"


def test_pending_request_pauses_run_clock_and_pages_once(tmp_path, monkeypatch):
    """The turn timeout must not fire while T3 is waiting on the owner: the
    parked polls extend the deadline, the owner is paged exactly once per
    request, the placeholder says it's paused, and the run completes normally
    once the request is answered."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-parked"
    mirror = make_mirror(tmp_path, thread_id)
    # ~40 parked polls at 0.01s each is well past a 0.2s turn timeout.
    client = FakeT3Client(projections=[parked()] * 40 + [parked(resolved=True), completed()])
    paged: list[list[dict]] = []
    updates: list[str] = []

    result = go(client, mirror, thread_id, timeout=0.2, approval_timeout=60,
                on_progress=updates.append, on_approval_wait=paged.append,
                owner_name="Dan")

    assert result.ok is True and result.text == "hello from t3"
    assert "run.interrupt" not in client.dispatch_types()
    assert len(paged) == 1 and paged[0][0]["requestId"] == "req-1"
    assert any(":raised_hand: _paused -- waiting for Dan to approve run a command: "
               "`rm -rf build` in T3_" in u for u in updates)
    # After the request clears, progress drops the paused line again.
    assert "paused" not in updates[-1]


def test_new_request_ids_page_again(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-parked-twice"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[
        parked("req-1"), parked("req-1"), parked("req-2"), parked("req-2"), completed(),
    ])
    paged: list[list[dict]] = []

    result = go(client, mirror, thread_id, timeout=5, on_approval_wait=paged.append)

    assert result.ok is True
    assert [p[0]["requestId"] for p in paged] == ["req-1", "req-2"]


def test_approval_timeout_releases_without_interrupting(tmp_path, monkeypatch):
    """When the owner never acts, the bridge stops holding the Slack thread but
    leaves the T3 run (and its pending request) alone so it can still be
    answered later; the result is flagged awaiting_approval, not a generic error."""
    fast_poll(monkeypatch)
    thread_id = "t3-thread-parked-forever"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[parked()])

    result = go(client, mirror, thread_id, timeout=0.1, approval_timeout=0.05)

    assert result.ok is False
    assert result.awaiting_approval is True
    assert result.session_id == thread_id
    assert "waiting for approval" in result.error
    assert "run.interrupt" not in client.dispatch_types()


def test_user_input_request_counts_as_parked(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-question"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[parked(kind="user_input")] * 5 + [completed()])
    paged: list[list[dict]] = []
    updates: list[str] = []

    result = go(client, mirror, thread_id, timeout=5, on_approval_wait=paged.append,
                on_progress=updates.append)

    assert result.ok is True
    assert len(paged) == 1
    assert paged[0][0]["kind"] == "user_input"
    assert paged[0][0]["questions"] == [{"question": "Which one?", "header": "Pick"}]
    assert any("waiting for the owner to approve a question for you: Which one? in T3"
               in u for u in updates)


def test_approval_wait_callback_error_does_not_kill_the_turn(tmp_path, monkeypatch):
    fast_poll(monkeypatch)
    thread_id = "t3-thread-parked-cb-error"
    mirror = make_mirror(tmp_path, thread_id)
    client = FakeT3Client(projections=[parked(), completed()])

    def boom(_):
        raise RuntimeError("slack down")

    result = go(client, mirror, thread_id, timeout=5, on_approval_wait=boom)

    assert result.ok is True
