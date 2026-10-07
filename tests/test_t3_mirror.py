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


def old_msg(mid: str, role: str, text: str, streaming: bool = False, age: float = 60.0,
            run_id: str | None = RUN1) -> dict:
    return {
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
