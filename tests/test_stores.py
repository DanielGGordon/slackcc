"""Tests for SessionStore, ClaimStore, and MirrorStore (t3.py) — all fully
offline, JSON-file-backed stores. No network, no Slack, no live T3/pps."""

from __future__ import annotations

import json

from slackcc.claims import ClaimStore
from slackcc.sessions import SessionStore
from slackcc.t3 import MirrorStore


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


def test_session_store_key_format():
    assert SessionStore.key("C123", "1234.5678") == "C123:1234.5678"


def test_session_store_get_returns_none_when_unset(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    assert store.get("C1", "111.222") is None


def test_session_store_set_then_get_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.set("C1", "111.222", "session-abc")
    assert store.get("C1", "111.222") == "session-abc"


def test_session_store_set_persists_to_disk_with_correct_key(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.set("C1", "111.222", "session-abc")

    on_disk = json.loads(path.read_text())
    assert on_disk == {"C1:111.222": "session-abc"}


def test_session_store_persists_across_new_instance(tmp_path):
    path = tmp_path / "sessions.json"
    store1 = SessionStore(path)
    store1.set("C1", "111.222", "session-abc")

    store2 = SessionStore(path)
    assert store2.get("C1", "111.222") == "session-abc"


def test_session_store_tolerates_missing_file(tmp_path):
    path = tmp_path / "does_not_exist.json"
    store = SessionStore(path)
    assert store.get("C1", "111.222") is None


def test_session_store_tolerates_corrupt_json(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not valid json!!")
    store = SessionStore(path)
    assert store.get("C1", "111.222") is None
    # Still usable afterward.
    store.set("C1", "111.222", "session-abc")
    assert store.get("C1", "111.222") == "session-abc"


def test_session_store_tolerates_empty_file(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("")
    store = SessionStore(path)
    assert store.get("C1", "111.222") is None
    store.set("C1", "111.222", "session-abc")
    assert store.get("C1", "111.222") == "session-abc"


def test_session_store_overwrite_updates_value(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    store.set("C1", "111.222", "session-a")
    store.set("C1", "111.222", "session-b")
    assert store.get("C1", "111.222") == "session-b"


# ---------------------------------------------------------------------------
# ClaimStore
# ---------------------------------------------------------------------------


def test_claim_store_key_format():
    assert ClaimStore.key("C123", "1234.5678") == "C123:1234.5678"


def test_claim_store_not_claimed_by_default(tmp_path):
    store = ClaimStore(tmp_path / "claims.json")
    assert store.is_claimed("C1", "111.222") is False


def test_claim_store_claim_then_is_claimed(tmp_path):
    store = ClaimStore(tmp_path / "claims.json")
    store.claim("C1", "111.222")
    assert store.is_claimed("C1", "111.222") is True


def test_claim_store_claim_is_idempotent(tmp_path):
    store = ClaimStore(tmp_path / "claims.json")
    store.claim("C1", "111.222")
    store.claim("C1", "111.222")
    on_disk = json.loads((tmp_path / "claims.json").read_text())
    assert on_disk == ["C1:111.222"]


def test_claim_store_release_unclaims(tmp_path):
    store = ClaimStore(tmp_path / "claims.json")
    store.claim("C1", "111.222")
    store.release("C1", "111.222")
    assert store.is_claimed("C1", "111.222") is False


def test_claim_store_release_missing_claim_is_noop(tmp_path):
    store = ClaimStore(tmp_path / "claims.json")
    # Never claimed; release should not raise.
    store.release("C1", "111.222")
    assert store.is_claimed("C1", "111.222") is False


def test_claim_store_is_claimed_rereads_file_each_call(tmp_path):
    # Per the module docstring: is_claimed re-reads the file every call rather
    # than caching, so it reflects out-of-process writes made by another
    # ClaimStore instance pointed at the same file.
    path = tmp_path / "claims.json"
    writer = ClaimStore(path)
    reader = ClaimStore(path)

    assert reader.is_claimed("C1", "111.222") is False
    writer.claim("C1", "111.222")
    assert reader.is_claimed("C1", "111.222") is True


def test_claim_store_tolerates_missing_file(tmp_path):
    store = ClaimStore(tmp_path / "does_not_exist.json")
    assert store.is_claimed("C1", "111.222") is False


def test_claim_store_tolerates_corrupt_json(tmp_path):
    path = tmp_path / "claims.json"
    path.write_text("{not valid json!!")
    store = ClaimStore(path)
    assert store.is_claimed("C1", "111.222") is False
    store.claim("C1", "111.222")
    assert store.is_claimed("C1", "111.222") is True


def test_claim_store_claims_independent_across_threads(tmp_path):
    store = ClaimStore(tmp_path / "claims.json")
    store.claim("C1", "111.222")
    assert store.is_claimed("C1", "999.888") is False
    assert store.is_claimed("C2", "111.222") is False


# ---------------------------------------------------------------------------
# MirrorStore
# ---------------------------------------------------------------------------


def test_mirror_store_register_creates_entry(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")
    threads = store.threads()
    assert threads["thread-1"] == {
        "channel": "C1",
        "thread_ts": "111.222",
        "posted": [],
    }


def test_mirror_store_reregister_updates_channel_and_thread_ts_keeps_posted(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")
    store.mark_posted("thread-1", ["m1", "m2"])

    store.register("thread-1", "C2", "999.888")

    threads = store.threads()
    entry = threads["thread-1"]
    assert entry["channel"] == "C2"
    assert entry["thread_ts"] == "999.888"
    assert entry["posted"] == ["m1", "m2"]


def test_mirror_store_mark_posted_dedups(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")
    store.mark_posted("thread-1", ["m1", "m2"])
    store.mark_posted("thread-1", ["m2", "m3"])

    assert store.threads()["thread-1"]["posted"] == ["m1", "m2", "m3"]


def test_mirror_store_mark_posted_trims_to_cap_keeping_newest(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")

    # Post more than _POSTED_CAP (500) ids one at a time.
    ids = [f"m{i}" for i in range(520)]
    store.mark_posted("thread-1", ids)

    posted = store.threads()["thread-1"]["posted"]
    assert len(posted) == 500
    # Newest ids retained; oldest trimmed off the front.
    assert posted[0] == "m20"
    assert posted[-1] == "m519"
    assert "m0" not in posted
    assert "m19" not in posted


def test_mirror_store_is_posted_true_and_false(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")
    store.mark_posted("thread-1", ["m1"])

    assert store.is_posted("thread-1", "m1") is True
    assert store.is_posted("thread-1", "m2") is False


def test_mirror_store_is_posted_unregistered_thread_is_false(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    assert store.is_posted("no-such-thread", "m1") is False


def test_mirror_store_mark_posted_on_unregistered_thread_is_noop(tmp_path):
    path = tmp_path / "mirror.json"
    store = MirrorStore(path)
    # No register() call first.
    store.mark_posted("ghost-thread", ["m1"])

    assert store.is_posted("ghost-thread", "m1") is False
    assert "ghost-thread" not in store.threads()


def test_mirror_store_remove_existing_thread(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")
    store.remove("thread-1")
    assert "thread-1" not in store.threads()


def test_mirror_store_remove_missing_thread_is_noop(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    # Should not raise even though "thread-1" was never registered.
    store.remove("thread-1")
    assert store.threads() == {}


def test_mirror_store_threads_returns_deep_copy(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")

    snapshot = store.threads()
    snapshot["thread-1"]["posted"].append("injected")
    snapshot["thread-2"] = {"channel": "bogus"}

    fresh = store.threads()
    assert fresh["thread-1"]["posted"] == []
    assert "thread-2" not in fresh


def test_mirror_store_persists_across_new_instance(tmp_path):
    path = tmp_path / "mirror.json"
    store1 = MirrorStore(path)
    store1.register("thread-1", "C1", "111.222")
    store1.mark_posted("thread-1", ["m1", "m2"])

    store2 = MirrorStore(path)
    threads = store2.threads()
    assert threads["thread-1"]["channel"] == "C1"
    assert threads["thread-1"]["thread_ts"] == "111.222"
    assert threads["thread-1"]["posted"] == ["m1", "m2"]


def test_mirror_store_tolerates_missing_file(tmp_path):
    store = MirrorStore(tmp_path / "does_not_exist.json")
    assert store.threads() == {}


def test_mirror_store_tolerates_corrupt_json(tmp_path):
    path = tmp_path / "mirror.json"
    path.write_text("{not valid json!!")
    store = MirrorStore(path)
    assert store.threads() == {}
    # Still usable afterward.
    store.register("thread-1", "C1", "111.222")
    assert "thread-1" in store.threads()


def test_mirror_store_settled_notice_defaults_to_none(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")
    assert store.settled_notice("thread-1") is None
    assert store.settled_notice("unknown") is None


def test_mirror_store_settled_notice_set_and_clear_roundtrip(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")
    store.set_settled_notice("thread-1", "2026-07-30T12:00:00Z")
    assert store.settled_notice("thread-1") == "2026-07-30T12:00:00Z"
    store.set_settled_notice("thread-1", None)
    assert store.settled_notice("thread-1") is None


def test_mirror_store_settled_notice_persists_across_new_instance(tmp_path):
    path = tmp_path / "mirror.json"
    store1 = MirrorStore(path)
    store1.register("thread-1", "C1", "111.222")
    store1.set_settled_notice("thread-1", "2026-07-30T12:00:00Z")

    store2 = MirrorStore(path)
    assert store2.settled_notice("thread-1") == "2026-07-30T12:00:00Z"


def test_mirror_store_settled_notice_missing_key_in_legacy_json(tmp_path):
    path = tmp_path / "mirror.json"
    path.write_text(json.dumps({
        "threads": {
            "thread-1": {
                "channel": "C1",
                "thread_ts": "111.222",
                "posted": [],
            }
        }
    }))
    store = MirrorStore(path)
    assert store.settled_notice("thread-1") is None
    store.set_settled_notice("thread-1", "2026-07-30T12:00:00Z")
    assert store.settled_notice("thread-1") == "2026-07-30T12:00:00Z"


def test_mirror_store_register_does_not_clobber_settled_notice(tmp_path):
    store = MirrorStore(tmp_path / "mirror.json")
    store.register("thread-1", "C1", "111.222")
    store.set_settled_notice("thread-1", "2026-07-30T12:00:00Z")
    store.register("thread-1", "C1", "111.222")
    assert store.settled_notice("thread-1") == "2026-07-30T12:00:00Z"
