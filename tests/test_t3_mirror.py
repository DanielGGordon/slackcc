"""Tests for the T3 outbound mirror sweep (`slackcc.t3_mirror._sweep`).

Fully offline: FakeT3Client returns canned v2 thread projections (no HTTP), a
real MirrorStore backed by a tmp_path JSON file, and FakeSlack records
chat_postMessage calls instead of hitting the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from slackcc import t3_mirror
from slackcc.t3 import MirrorStore, T3Error


def iso_now_minus(seconds: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return ts.isoformat().replace("+00:00", "Z")


class FakeT3Client:
    """Stands in for T3Client.thread_projection; no HTTP involved."""

    def __init__(self, projections: dict[str, dict] | None = None, errors: dict[str, Exception] | None = None):
        self.projections = projections or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def thread_projection(self, thread_id: str) -> dict:
        self.calls.append(thread_id)
        if thread_id in self.errors:
            raise self.errors[thread_id]
        return self.projections.get(thread_id, {"thread": {}, "runs": [], "messages": []})


class FakeSlack:
    """Stands in for WebClient; records chat_postMessage calls."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    def chat_postMessage(self, **kwargs):
        if self.fail:
            raise RuntimeError("slack API exploded")
        self.calls.append(kwargs)
        return {"ok": True}


def make_mirror(tmp_path, thread_id="t1", channel="C1", thread_ts="1000.0001") -> MirrorStore:
    mirror = MirrorStore(tmp_path / "mirror.json")
    mirror.register(thread_id, channel, thread_ts)
    return mirror


RUN1 = "run:thread:t1:ordinal:1"
RUN2 = "run:thread:t1:ordinal:2"


def run(run_id: str = RUN1, status: str = "completed", user_message_id: str = "u1") -> dict:
    return {"id": run_id, "status": status, "userMessageId": user_message_id}


# Origin stamps as T3 writes them: a GUI-typed user message, and agent output.
GUI = {"createdBy": "user", "creationSource": "web"}
AGENT = {"createdBy": "agent", "creationSource": "provider"}


def old_msg(mid: str, role: str, text: str, streaming: bool = False, age: float = 60.0,
            run_id: str | None = RUN1, **origin) -> dict:
    return {
        **(GUI if role == "user" else AGENT),
        **origin,
        "id": mid,
        "role": role,
        "runId": run_id,
        "text": text,
        "streaming": streaming,
        "updatedAt": iso_now_minus(age),
    }


def projection(messages=None, runs=None, **thread) -> dict:
    return {"thread": thread, "runs": runs if runs is not None else [], "messages": messages or []}


def test_completed_run_posts_only_final_assistant_message(tmp_path):
    """Intermediate assistant messages of a completed run (not its final one)
    are skipped AND never ledgered as posted."""
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run()],
        messages=[
            old_msg("a1", "assistant", "intermediate status note"),
            old_msg("a2", "assistant", "final answer"),
        ],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 1
    assert slack.calls[0]["text"] == "final answer"
    assert mirror.is_posted("t1", "a2") is True
    # The skipped intermediate message must NOT be ledgered either.
    assert mirror.is_posted("t1", "a1") is False


def test_running_run_posts_nothing(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run(status="running")],
        messages=[old_msg("a1", "assistant", "still working on it")],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False


@pytest.mark.parametrize("status", ["running", "interrupted", "failed"])
def test_assistant_message_of_non_completed_run_skipped_and_unledgered(tmp_path, status):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run(RUN1, "completed"), run(RUN2, status, "u2")],
        messages=[
            old_msg("a1", "assistant", "first run answer", run_id=RUN1),
            old_msg("a2", "assistant", "second run partial", run_id=RUN2),
        ],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert [c["text"] for c in slack.calls] == ["first run answer"]
    assert mirror.is_posted("t1", "a1") is True
    assert mirror.is_posted("t1", "a2") is False


def test_multiple_completed_runs_each_post_their_final_reply_once(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run(RUN1, "completed", "u1"), run(RUN2, "completed", "u2")],
        messages=[
            old_msg("a1a", "assistant", "run1 narration", run_id=RUN1),
            old_msg("a1b", "assistant", "run1 final", run_id=RUN1),
            old_msg("a2a", "assistant", "run2 narration", run_id=RUN2),
            old_msg("a2b", "assistant", "run2 final", run_id=RUN2),
        ],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")
    t3_mirror._sweep(t3, slack, mirror, owner="dan")  # second sweep must not re-post

    assert [c["text"] for c in slack.calls] == ["run1 final", "run2 final"]
    assert mirror.is_posted("t1", "a1a") is False
    assert mirror.is_posted("t1", "a2a") is False


def test_v1_imported_history_posts_no_assistant_messages(tmp_path):
    """T3's v1 -> v2 import yields no runs and runId-less messages: assistant
    history must not flood Slack, and is not ledgered."""
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[],
        messages=[
            old_msg("a1", "assistant", "old answer", run_id=None),
            old_msg("a2", "assistant", "older answer", run_id=None),
        ],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False
    assert mirror.is_posted("t1", "a2") is False


def test_assistant_message_with_null_run_id_skipped_even_with_completed_run(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run()],
        messages=[
            old_msg("a0", "assistant", "orphan", run_id=None),
            old_msg("a1", "assistant", "final answer"),
        ],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert [c["text"] for c in slack.calls] == ["final answer"]
    assert mirror.is_posted("t1", "a0") is False


def test_soft_deleted_thread_is_unregistered_and_posts_nothing(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run()],
        messages=[
            old_msg("u1", "user", "hello"),
            old_msg("a1", "assistant", "final answer"),
        ],
        deletedAt="2026-07-30T12:00:00Z",
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert "t1" not in mirror.threads()


def test_null_deleted_at_keeps_thread(tmp_path):
    mirror = make_mirror(tmp_path)
    t3 = FakeT3Client({"t1": projection(deletedAt=None)})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert "t1" in mirror.threads()


def test_user_message_gets_said_to_agent_prefix(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run()],
        messages=[
            old_msg("u1", "user", "what's the weather", run_id=None),
            old_msg("a1", "assistant", "sunny"),
        ],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 2
    user_call = next(c for c in slack.calls if "said to the agent" in c["text"])
    assert user_call["text"] == "_dan said to the agent:_ what's the weather"


def test_user_message_posts_without_any_completed_run(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run(status="running")],
        messages=[old_msg("u1", "user", "do the thing", run_id=None)],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert [c["text"] for c in slack.calls] == ["_dan said to the agent:_ do the thing"]
    assert mirror.is_posted("t1", "u1") is True


def test_subagent_report_relayed_as_user_message_is_not_attributed_to_owner(tmp_path):
    """Regression: T3 relays a finished subagent's report into the parent
    thread as a role-"user" message. It was posted as "<owner> said to the
    agent: Committed as ..." -- words the owner never typed."""
    mirror = make_mirror(tmp_path)
    report = old_msg(
        "message:thread:t1:ordinal:1:0d80054f", "user",
        "Committed as `56538f3` (not pushed, no PR opened, per instructions).",
        run_id=RUN2, createdBy="agent", creationSource="provider",
        notification={"source": {"kind": "background_task", "work": "subagent"},
                      "outcome": "completed", "summary": 'Subagent "Task4" finished'},
    )
    proj = projection(
        runs=[run(RUN1, "completed"), run(RUN2, "completed", report["id"])],
        messages=[old_msg("u1", "user", "use a subagent per task", run_id=RUN1),
                  report,
                  old_msg("a2", "assistant", "Task 4 is done.", run_id=RUN2)],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    texts = [c["text"] for c in slack.calls]
    assert texts == ["_Dan said to the agent:_ use a subagent per task", "Task 4 is done."]
    assert not any("Committed" in t for t in texts)
    # Decided once: ledgered so later sweeps don't re-examine it.
    assert mirror.is_posted("t1", report["id"]) is True


@pytest.mark.parametrize("origin", [
    pytest.param({"createdBy": "agent", "creationSource": "provider"}, id="provider-agent"),
    pytest.param({"createdBy": "agent", "creationSource": "mcp", "senderThreadId": "t0"},
                 id="agent-send-via-mcp"),
    pytest.param({"createdBy": "agent", "creationSource": "provider", "senderThreadId": "t0"},
                 id="subagent-task-prompt"),
    pytest.param({"createdBy": "system", "creationSource": "server"}, id="system"),
    pytest.param({"createdBy": "user", "creationSource": "server"}, id="v1-import"),
    pytest.param({"createdBy": "user", "creationSource": "web", "scheduledTaskId": "st1"},
                 id="scheduled-task"),
    pytest.param({"createdBy": "user", "creationSource": "web", "senderThreadId": "t0"},
                 id="web-but-sent-by-another-thread"),
    pytest.param({"createdBy": None, "creationSource": None}, id="origin-unknown"),
])
def test_non_human_user_message_never_posted_as_owner(tmp_path, origin):
    mirror = make_mirror(tmp_path)
    proj = projection(messages=[old_msg("u1", "user", "not from a person", run_id=None,
                                        **origin)])
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []


@pytest.mark.parametrize("source", ["web", "mobile"])
def test_human_typed_user_message_from_any_t3_client_is_attributed(tmp_path, source):
    mirror = make_mirror(tmp_path)
    proj = projection(messages=[old_msg("u1", "user", "ship it", run_id=None,
                                        creationSource=source)])
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert [c["text"] for c in slack.calls] == ["_dan said to the agent:_ ship it"]


def test_grace_period_skips_fresh_message_and_does_not_ledger(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run()],
        messages=[old_msg("a1", "assistant", "brand new", age=0.1)],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False


def test_message_older_than_grace_secs_posts(tmp_path, monkeypatch):
    monkeypatch.setattr(t3_mirror, "_GRACE_SECS", 5.0)
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run()],
        messages=[old_msg("a1", "assistant", "aged out of the grace window", age=6.0)],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 1
    assert slack.calls[0]["text"] == "aged out of the grace window"
    assert mirror.is_posted("t1", "a1") is True


def test_already_posted_id_is_skipped(tmp_path):
    mirror = make_mirror(tmp_path)
    mirror.mark_posted("t1", ["a1"])
    proj = projection(
        runs=[run()],
        messages=[old_msg("a1", "assistant", "already posted before")],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []


def test_empty_text_non_streaming_message_skipped_and_unledgered(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run()],
        messages=[old_msg("a1", "assistant", "   ")],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False


def test_empty_user_message_skipped_and_unledgered(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(messages=[old_msg("u1", "user", "", run_id=None)])
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "u1") is False


def test_streaming_message_skipped(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run(status="running")],
        messages=[old_msg("a1", "assistant", "typing...", streaming=True)],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False


def test_streaming_user_message_skipped(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(messages=[old_msg("u1", "user", "typing...", streaming=True, run_id=None)])
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "u1") is False


def test_long_message_chunked_into_multiple_posts_in_order(tmp_path):
    mirror = make_mirror(tmp_path)
    long_text = "".join(f"{i % 10}" for i in range(9000))  # 9000 chars, no whitespace to strip
    proj = projection(
        runs=[run()],
        messages=[old_msg("a1", "assistant", long_text)],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 3  # 3800 + 3800 + 1400
    reassembled = "".join(c["text"] for c in slack.calls)
    assert reassembled == long_text
    for call in slack.calls:
        assert len(call["text"]) <= 3800


def test_thread_not_found_error_removes_thread_from_store(tmp_path):
    mirror = make_mirror(tmp_path)
    t3 = FakeT3Client(errors={"t1": T3Error("T3 GET /api/... -> 404: thread_not_found")})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert "t1" not in mirror.threads()


def test_other_t3_error_keeps_thread_registered(tmp_path):
    mirror = make_mirror(tmp_path)
    t3 = FakeT3Client(errors={"t1": T3Error("T3 unreachable at http://x: timed out")})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert "t1" in mirror.threads()


def test_outbound_scrub_redacts_secret_shape_in_assistant_text(tmp_path):
    mirror = make_mirror(tmp_path)
    fake_aws_key = "AKIAABCDEFGHIJKLMNOP"  # matches AKIA[0-9A-Z]{16}
    proj = projection(
        runs=[run()],
        messages=[old_msg("a1", "assistant", f"here is a key: {fake_aws_key}")],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 1
    assert fake_aws_key not in slack.calls[0]["text"]
    assert "[redacted:aws-key-id]" in slack.calls[0]["text"]


def test_slack_post_exception_does_not_raise_out_of_sweep(tmp_path):
    mirror = make_mirror(tmp_path)
    proj = projection(
        runs=[run()],
        messages=[old_msg("a1", "assistant", "this post will explode")],
    )
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack(fail=True)

    t3_mirror._sweep(t3, slack, mirror, owner="dan")  # must not raise

    # It's ledgered as posted before the (failing) post attempt.
    assert mirror.is_posted("t1", "a1") is True


def _settled_projection(*, settled_override="settled", settled_at="fresh", messages=None):
    # "fresh" = settled a minute ago, inside the notice's max-age window.
    if settled_at == "fresh":
        settled_at = iso_now_minus(60)
    return projection(
        runs=[run()],
        messages=messages,
        settledOverride=settled_override,
        settledAt=settled_at,
    )


def test_settled_override_posts_notice_with_owner_name(tmp_path):
    mirror = make_mirror(tmp_path)
    settled_at = iso_now_minus(60)
    t3 = FakeT3Client({"t1": _settled_projection(settled_at=settled_at)})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    assert len(slack.calls) == 1
    text = slack.calls[0]["text"]
    assert "settled this chat" in text
    assert "simply reply" in text
    assert "Dan" in text
    assert mirror.settled_notice("t1") == settled_at


def test_fresh_settle_posted_when_settled_at_is_now(tmp_path):
    mirror = make_mirror(tmp_path)
    t3 = FakeT3Client({"t1": _settled_projection(settled_at=iso_now_minus(0))})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    assert len(slack.calls) == 1
    assert "settled this chat" in slack.calls[0]["text"]


def test_stale_settle_recorded_silently(tmp_path):
    """A settle from long ago (e.g. surfaced by the v1 -> v2 history import)
    is recorded so it never re-announces, but not posted."""
    mirror = make_mirror(tmp_path)
    stale = "2026-07-30T12:00:00Z"
    t3 = FakeT3Client({"t1": _settled_projection(settled_at=stale)})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    assert slack.calls == []
    assert mirror.settled_notice("t1") == stale


def test_settle_notice_age_boundary(tmp_path):
    mirror_old = make_mirror(tmp_path / "old")
    t3 = FakeT3Client({"t1": _settled_projection(
        settled_at=iso_now_minus(t3_mirror._SETTLE_NOTICE_MAX_AGE_SECS + 60))})
    slack_old = FakeSlack()
    t3_mirror._sweep(t3, slack_old, mirror_old, owner="Dan")
    assert slack_old.calls == []

    mirror_new = make_mirror(tmp_path / "new")
    t3 = FakeT3Client({"t1": _settled_projection(
        settled_at=iso_now_minus(t3_mirror._SETTLE_NOTICE_MAX_AGE_SECS - 60))})
    slack_new = FakeSlack()
    t3_mirror._sweep(t3, slack_new, mirror_new, owner="Dan")
    assert len(slack_new.calls) == 1


def test_settled_notice_is_idempotent_across_sweeps(tmp_path):
    mirror = make_mirror(tmp_path)
    t3 = FakeT3Client({"t1": _settled_projection()})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    assert len(slack.calls) == 1


def test_unsettle_then_resettle_with_new_settled_at_announces_again(tmp_path):
    mirror = make_mirror(tmp_path)
    first = iso_now_minus(120)
    second = iso_now_minus(30)
    t3 = FakeT3Client({"t1": _settled_projection(settled_at=first)})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    assert len(slack.calls) == 1

    t3.projections["t1"] = _settled_projection(settled_override=None, settled_at=None)
    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    assert len(slack.calls) == 1
    assert mirror.settled_notice("t1") is None

    t3.projections["t1"] = _settled_projection(settled_at=second)
    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    assert len(slack.calls) == 2
    assert mirror.settled_notice("t1") == second


def test_settled_override_active_never_announces(tmp_path):
    mirror = make_mirror(tmp_path)
    t3 = FakeT3Client({"t1": _settled_projection(settled_override="active")})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    assert slack.calls == []
    assert mirror.settled_notice("t1") is None


def test_settle_notice_lands_after_assistant_message_on_mapped_thread(tmp_path):
    mirror = make_mirror(tmp_path, channel="Cmap", thread_ts="2222.3333")
    proj = _settled_projection(messages=[old_msg("a1", "assistant", "final answer")])
    t3 = FakeT3Client({"t1": proj})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    assert len(slack.calls) == 2
    assert slack.calls[0]["text"] == "final answer"
    assert "settled this chat" in slack.calls[1]["text"]
    for call in slack.calls:
        assert call["channel"] == "Cmap"
        assert call["thread_ts"] == "2222.3333"


def test_settled_at_null_announces_once_not_every_sweep(tmp_path):
    # A missing settledAt counts as age 0, so the notice posts.
    mirror = make_mirror(tmp_path)
    t3 = FakeT3Client({"t1": _settled_projection(settled_at=None)})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    assert len(slack.calls) == 1
    assert mirror.settled_notice("t1") == "settled"


def test_settled_at_appearing_late_does_not_reannounce(tmp_path):
    # T3 may write settledOverride a beat before settledAt; the record updates
    # silently because the thread never left the settled state.
    mirror = make_mirror(tmp_path)
    t3 = FakeT3Client({"t1": _settled_projection(settled_at=None)})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="Dan")
    late = iso_now_minus(5)
    t3.projections["t1"] = _settled_projection(settled_at=late)
    t3_mirror._sweep(t3, slack, mirror, owner="Dan")

    assert len(slack.calls) == 1
    assert mirror.settled_notice("t1") == late


def test_long_thread_beyond_ledger_cap_does_not_repost_old_history(tmp_path):
    """300 delivered exchanges = 600 ids > the 500-id ledger. Only the newest
    messages are scanned, so ids that aged out of the ledger never repost."""
    mirror = make_mirror(tmp_path)
    runs, messages = [], []
    for i in range(300):
        run_id = f"run:thread:t1:ordinal:{i}"
        runs.append(run(run_id, user_message_id=f"u{i}"))
        messages += [old_msg(f"u{i}", "user", f"question {i}", run_id=run_id),
                     old_msg(f"a{i}", "assistant", f"answer {i}", run_id=run_id)]
        mirror.mark_posted("t1", [f"u{i}", f"a{i}"])
    t3 = FakeT3Client({"t1": projection(runs=runs, messages=messages)})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")
    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []


# --------------------------------------------------------------------------- #
# customer-audience threads
# --------------------------------------------------------------------------- #

from types import SimpleNamespace  # noqa: E402

from slackcc import customer  # noqa: E402
from slackcc.config import ChannelConfig, SenderPolicy, Settings  # noqa: E402

BLOCK_FINAL = "Edited the handler.\n\n### Message for the customer\nThe new price is on your booking page."
BLOCK_ONLY = "The new price is on your booking page."


class CustomerSlack(FakeSlack):
    """FakeSlack that also hands out ts values and a permalink, like a real client."""

    def chat_postMessage(self, **kwargs):
        resp = super().chat_postMessage(**kwargs)
        return {**resp, "ts": f"9{len(self.calls)}.0"}

    def chat_getPermalink(self, **kwargs):
        return {"permalink": "https://slack.example/p"}


def customer_world(tmp_path, audience="customer"):
    cfg = ChannelConfig(channel_id="C1", project="acme", cwd=str(tmp_path), backend="t3",
                        t3_project_id="p", audience=audience)
    settings = Settings(
        bot_token="b", app_token="a", config_path=tmp_path / "c.json",
        sessions_path=tmp_path / "s.json", claude_bin="claude", channels={"C1": cfg},
        t3_gui_url="https://t3.example:7443",
        senders={"Uown": SenderPolicy(user_id="Uown", name="Dan", role="owner")})
    return SimpleNamespace(current=settings), customer.Ledger(tmp_path / "ledger")


def sweep(tmp_path, proj, *, audience="customer", slack=None, mirror=None, ledger=None):
    mirror = mirror or make_mirror(tmp_path)
    live, fresh_ledger = customer_world(tmp_path, audience)
    ledger = ledger or fresh_ledger
    slack = slack or CustomerSlack()
    t3_mirror._sweep(FakeT3Client({"t1": proj}), slack, mirror, owner="Dan",
                     live=live, ledger=ledger)
    return slack, mirror, ledger


def ledger_of(ledger):
    return ledger.recent(customer.thread_key("C1", "1000.0001"), 50)


def dms(slack):
    return [c for c in slack.calls if "thread_ts" not in c]


def posts(slack):
    return [c for c in slack.calls if "thread_ts" in c]


def test_customer_thread_forwards_dans_gui_message_verbatim_as_dan(tmp_path):
    proj = projection(runs=[run(status="running")],
                      messages=[old_msg("u1", "user", "Please also check the footer")])
    slack, mirror, ledger = sweep(tmp_path, proj)

    [post] = posts(slack)
    assert post["text"] == "*Dan:* Please also check the footer"
    assert "agent" not in post["text"]
    assert mirror.is_posted("t1", "u1")
    [entry] = ledger_of(ledger)
    assert entry["source"] == "dan_forward" and entry["text"] == "Please also check the footer"
    assert entry["slack_ts"] and entry["t3_message_id"] == "u1"


def test_technical_thread_keeps_the_said_to_the_agent_wording(tmp_path):
    proj = projection(runs=[run(status="running")], messages=[old_msg("u1", "user", "hello")])
    slack, _, ledger = sweep(tmp_path, proj, audience="technical")
    assert [c["text"] for c in posts(slack)] == ["_Dan said to the agent:_ hello"]
    assert ledger_of(ledger) == []


def test_customer_forward_is_scrubbed_and_non_human_user_messages_never_forwarded(tmp_path):
    token = "xoxb-" + "1234567890123-abcdefghijklmno"
    proj = projection(runs=[run(status="running")], messages=[
        old_msg("u1", "user", f"use {token}"),
        old_msg("u2", "user", "subagent report", **AGENT),
    ])
    slack, mirror, _ = sweep(tmp_path, proj)
    [post] = posts(slack)
    assert token not in post["text"]
    assert mirror.is_posted("t1", "u2")


@pytest.mark.parametrize("text", ["#agent check the logs", "#AGENT: check the logs",
                                  "  #agent\ncheck", "#agent"])
def test_agent_prefixed_message_is_private_and_its_reply_is_not_posted(tmp_path, text):
    proj = projection(
        runs=[run(user_message_id="u1")],
        messages=[old_msg("u1", "user", text, run_id=None),
                  old_msg("a1", "assistant", BLOCK_FINAL)])
    slack, mirror, ledger = sweep(tmp_path, proj)

    assert slack.calls == []
    assert mirror.is_posted("t1", "u1") and mirror.is_posted("t1", "a1")   # handled
    assert ledger_of(ledger) == []


def test_private_run_is_found_via_the_message_run_id_too(tmp_path):
    proj = projection(
        runs=[run(user_message_id="something-else")],
        messages=[old_msg("u1", "user", "#agent look", run_id=RUN1),
                  old_msg("a1", "assistant", BLOCK_FINAL)])
    slack, mirror, _ = sweep(tmp_path, proj)
    assert slack.calls == [] and mirror.is_posted("t1", "a1")


def test_private_marker_needs_a_word_boundary(tmp_path):
    proj = projection(runs=[run(user_message_id="u1")], messages=[
        old_msg("u1", "user", "#agentic things are great", run_id=None),
        old_msg("a1", "assistant", BLOCK_FINAL)])
    slack, _, _ = sweep(tmp_path, proj)
    assert [c["text"] for c in posts(slack)] == ["*Dan:* #agentic things are great", BLOCK_ONLY]


def test_private_marker_from_a_non_human_message_does_not_silence_the_run(tmp_path):
    # a subagent report that happens to start with #agent is not Dan
    proj = projection(runs=[run(user_message_id="u9")], messages=[
        old_msg("u1", "user", "#agent notes", run_id=RUN1, **AGENT),
        old_msg("a1", "assistant", BLOCK_FINAL)])
    slack, _, _ = sweep(tmp_path, proj)
    assert [c["text"] for c in posts(slack)] == [BLOCK_ONLY]


def test_gui_run_final_posts_only_the_block_and_ledgers_it(tmp_path):
    proj = projection(runs=[run(user_message_id="u1")],
                      messages=[old_msg("u1", "user", "add the price", run_id=None),
                                old_msg("a1", "assistant", BLOCK_FINAL)])
    slack, _, ledger = sweep(tmp_path, proj)

    assert [c["text"] for c in posts(slack)] == ["*Dan:* add the price", BLOCK_ONLY]
    assert "handler" not in posts(slack)[1]["text"]
    assert [(e["source"], e.get("t3_message_id")) for e in ledger_of(ledger)] == [
        ("dan_forward", "u1"), ("block", "a1")]


def test_gui_run_without_a_block_posts_nothing_and_dms_owners_once(tmp_path):
    proj = projection(runs=[run()], messages=[old_msg("a1", "assistant", "Fixed it in src/app.py")])
    slack, mirror, ledger = sweep(tmp_path, proj)

    assert posts(slack) == []
    [dm] = dms(slack)
    assert dm["channel"] == "Uown" and "no `### Message for the customer` block" in dm["text"]
    assert "Fixed it in src/app.py" in dm["text"] and "https://slack.example/p" in dm["text"]
    assert ledger_of(ledger) == []

    sweep(tmp_path, proj, slack=slack, mirror=mirror, ledger=ledger)   # handled: no second DM
    assert len(dms(slack)) == 1


def test_gui_run_with_a_technical_block_posts_holding_line_and_dms_owners(tmp_path):
    proj = projection(runs=[run()], messages=[old_msg(
        "a1", "assistant", "tech\n### Message for the customer\nSee src/app.py, PR #4 merged.")])
    slack, _, ledger = sweep(tmp_path, proj)

    assert [c["text"] for c in posts(slack)] == [customer.HOLDING_LINE]
    [dm] = dms(slack)
    assert "looks technical" in dm["text"] and "src/app.py" in dm["text"]
    assert [e["source"] for e in ledger_of(ledger)] == ["holding"]


def test_user_role_message_with_the_marker_is_never_the_block(tmp_path):
    """Subagent reports are user-role and may carry the marker; only the run's
    final ASSISTANT message is parsed."""
    proj = projection(runs=[run()], messages=[
        old_msg("u1", "user", "report\n### Message for the customer\nLeaked!", **AGENT),
        old_msg("a1", "assistant", "All done, plain technical summary."),
    ])
    slack, _, _ = sweep(tmp_path, proj)
    assert all("Leaked" not in c["text"] for c in slack.calls)
    assert posts(slack) == []   # the assistant message had no block


def test_customer_block_is_scrubbed_and_last_marker_wins(tmp_path):
    token = "xoxb-" + "1234567890123-abcdefghijklmno"
    final = ("### Message for the customer\nold draft\n\nnotes\n\n"
             f"### Message for the customer\nKey was {token}")
    proj = projection(runs=[run()], messages=[old_msg("a1", "assistant", final)])
    slack, _, _ = sweep(tmp_path, proj)
    [post] = posts(slack)
    assert post["text"].startswith("Key was ") and token not in post["text"]


def test_customer_threads_keep_the_grace_period(tmp_path):
    fresh = projection(runs=[run()], messages=[old_msg("a1", "assistant", BLOCK_FINAL, age=1.0)])
    slack, _, _ = sweep(tmp_path, fresh)
    assert slack.calls == []


def test_customer_threads_keep_ledger_dedupe_against_the_slack_turn(tmp_path):
    done = projection(runs=[run()], messages=[old_msg("a1", "assistant", BLOCK_FINAL)])
    mirror = make_mirror(tmp_path)
    mirror.mark_posted("t1", ["a1"])   # the Slack turn already posted it
    slack, _, _ = sweep(tmp_path, done, mirror=mirror)
    assert slack.calls == []


def test_unconfigured_channel_under_live_settings_delivers_nothing(tmp_path):
    """A customer channel removed from config must not fall back to technical
    delivery (it would forward `#agent` messages and full replies)."""
    mirror = make_mirror(tmp_path, channel="Cunknown")
    proj = projection(runs=[run()], messages=[
        old_msg("u1", "user", "#agent secret", run_id=None),
        old_msg("a1", "assistant", "full technical reply")])
    slack, _, ledger = sweep(tmp_path, proj, mirror=mirror)
    assert slack.calls == [] and ledger_of(ledger) == []
    assert not mirror.is_posted("t1", "u1") and not mirror.is_posted("t1", "a1")


def test_without_live_settings_every_thread_stays_technical(tmp_path):
    mirror = make_mirror(tmp_path, channel="Cunknown")
    proj = projection(runs=[run()], messages=[old_msg("a1", "assistant", "plain old reply")])
    slack = CustomerSlack()
    t3_mirror._sweep(FakeT3Client({"t1": proj}), slack, mirror, owner="Dan")
    assert [c["text"] for c in slack.calls] == ["plain old reply"]


def test_private_run_stays_private_when_its_message_is_past_the_tail_window(tmp_path):
    fillers = [old_msg(f"a-mid{i}", "assistant", f"narration {i}")
               for i in range(t3_mirror._TAIL_MESSAGES + 20)]
    proj = projection(
        runs=[run(user_message_id="u1")],
        messages=[old_msg("u1", "user", "#agent dig in", run_id=None), *fillers,
                  old_msg("a-final", "assistant", BLOCK_FINAL)])
    slack, mirror, _ = sweep(tmp_path, proj)
    assert slack.calls == []
    assert mirror.is_posted("t1", "a-final")


def test_settle_notice_still_fires_on_customer_threads(tmp_path):
    proj = projection(settledOverride="settled", settledAt=iso_now_minus(5))
    slack, _, _ = sweep(tmp_path, proj)
    assert "settled this chat" in posts(slack)[0]["text"]
