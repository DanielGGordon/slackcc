"""Tests for the T3 outbound mirror sweep (`slackcc.t3_mirror._sweep`).

Fully offline: FakeT3Client returns canned thread snapshots (no HTTP), a
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
    """Stands in for T3Client.thread_snapshot; no HTTP involved."""

    def __init__(self, snapshots: dict[str, dict] | None = None, errors: dict[str, Exception] | None = None):
        self.snapshots = snapshots or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def thread_snapshot(self, thread_id: str) -> dict:
        self.calls.append(thread_id)
        if thread_id in self.errors:
            raise self.errors[thread_id]
        return self.snapshots.get(thread_id, {"thread": {}})


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


def old_msg(mid: str, role: str, text: str, streaming: bool = False, age: float = 60.0) -> dict:
    return {
        "id": mid,
        "role": role,
        "text": text,
        "streaming": streaming,
        "updatedAt": iso_now_minus(age),
    }


def test_completed_turn_posts_only_final_assistant_message(tmp_path):
    """Intermediate assistant messages (not the latestTurn's final id) are
    skipped AND never ledgered as posted."""
    mirror = make_mirror(tmp_path)
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a2"},
            "messages": [
                old_msg("a1", "assistant", "intermediate status note"),
                old_msg("a2", "assistant", "final answer"),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 1
    assert slack.calls[0]["text"] == "final answer"
    assert mirror.is_posted("t1", "a2") is True
    # The skipped intermediate message must NOT be ledgered either.
    assert mirror.is_posted("t1", "a1") is False


def test_running_turn_posts_nothing(tmp_path):
    mirror = make_mirror(tmp_path)
    snapshot = {
        "thread": {
            "latestTurn": {"state": "running", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", "still working on it"),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False


def test_user_message_gets_said_to_agent_prefix(tmp_path):
    mirror = make_mirror(tmp_path)
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a1"},
            "messages": [
                old_msg("u1", "user", "what's the weather"),
                old_msg("a1", "assistant", "sunny"),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 2
    user_call = next(c for c in slack.calls if "said to the agent" in c["text"])
    assert user_call["text"] == "_dan said to the agent:_ what's the weather"


def test_grace_period_skips_fresh_message_and_does_not_ledger(tmp_path):
    mirror = make_mirror(tmp_path)
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", "brand new", age=0.1),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False


def test_message_older_than_grace_secs_posts(tmp_path, monkeypatch):
    monkeypatch.setattr(t3_mirror, "_GRACE_SECS", 5.0)
    mirror = make_mirror(tmp_path)
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", "aged out of the grace window", age=6.0),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 1
    assert slack.calls[0]["text"] == "aged out of the grace window"
    assert mirror.is_posted("t1", "a1") is True


def test_already_posted_id_is_skipped(tmp_path):
    mirror = make_mirror(tmp_path)
    mirror.mark_posted("t1", ["a1"])
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", "already posted before"),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []


def test_empty_text_non_streaming_message_skipped_and_unledgered(tmp_path):
    mirror = make_mirror(tmp_path)
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", "   "),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False


def test_streaming_message_skipped(tmp_path):
    mirror = make_mirror(tmp_path)
    snapshot = {
        "thread": {
            "latestTurn": {"state": "running", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", "typing...", streaming=True),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert slack.calls == []
    assert mirror.is_posted("t1", "a1") is False


def test_long_message_chunked_into_multiple_posts_in_order(tmp_path):
    mirror = make_mirror(tmp_path)
    long_text = "".join(f"{i % 10}" for i in range(9000))  # 9000 chars, no whitespace to strip
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", long_text),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
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
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", f"here is a key: {fake_aws_key}"),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack()

    t3_mirror._sweep(t3, slack, mirror, owner="dan")

    assert len(slack.calls) == 1
    assert fake_aws_key not in slack.calls[0]["text"]
    assert "[redacted:aws-key-id]" in slack.calls[0]["text"]


def test_slack_post_exception_does_not_raise_out_of_sweep(tmp_path):
    mirror = make_mirror(tmp_path)
    snapshot = {
        "thread": {
            "latestTurn": {"state": "completed", "assistantMessageId": "a1"},
            "messages": [
                old_msg("a1", "assistant", "this post will explode"),
            ],
        }
    }
    t3 = FakeT3Client({"t1": snapshot})
    slack = FakeSlack(fail=True)

    t3_mirror._sweep(t3, slack, mirror, owner="dan")  # must not raise

    # It's ledgered as posted before the (failing) post attempt.
    assert mirror.is_posted("t1", "a1") is True
