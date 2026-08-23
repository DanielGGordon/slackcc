"""Tests for slackcc.overrides: parse_yes_no and the OverrideStore JSON store.
Fully offline (tmp_path file I/O only)."""

from __future__ import annotations

import json

import pytest

from slackcc.overrides import _OVERRIDE_TTL_SECS, OverrideStore, parse_yes_no

NOW = 1_700_000_000.0


# --------------------------------------------------------------------------- #
# parse_yes_no
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", [
    "y", "yes", "Yes", "YES", "Yes!", "yes.", " y ", "yeah", "yep", "yup", "ok",
    "okay", "sure", "approve", "approved", "allow", "allowed", "permit",
    "permitted", "go ahead", "Go  ahead!", "<@UBOT> yes", "<@UBOT>yes",
    "<@UBOT|alfred> Yes.",
])
def test_parse_yes_no_yes_variants(text):
    assert parse_yes_no(text) is True


@pytest.mark.parametrize("text", [
    "n", "no", "No.", "NO!", "nope", "nah", "deny", "denied", "decline",
    "declined", "reject", "rejected", "not allowed", "<@UBOT> no",
])
def test_parse_yes_no_no_variants(text):
    assert parse_yes_no(text) is False


@pytest.mark.parametrize("text", [
    "", "   ", "yes please do it", "no way, but fix the bug", "maybe",
    "yesterday", "nothing", "ok so what about the deploy", "<@UBOT>",
    "<@UBOT> can you fix this", "yes?",
])
def test_parse_yes_no_anything_else_is_none(text):
    assert parse_yes_no(text) is None


def test_parse_yes_no_handles_none_text():
    assert parse_yes_no(None) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# OverrideStore
# --------------------------------------------------------------------------- #


def test_override_store_key_format():
    assert OverrideStore.key("C1", "1.2") == "C1:1.2"


def test_override_store_empty_when_file_missing(tmp_path):
    store = OverrideStore(tmp_path / "state" / "pps_overrides.json")
    assert store.get_pending("C1", "1.2") is None
    assert store.grants("C1", "1.2") == []


def test_override_store_pending_roundtrip_and_on_disk_shape(tmp_path):
    path = tmp_path / "state" / "pps_overrides.json"
    store = OverrideStore(path, now=lambda: NOW)
    entry = {"event": {"user": "Ug", "text": "hi"}, "category": "c",
             "reason": "r", "asked_at": NOW - 60}
    store.set_pending("C1", "1.2", entry)

    assert store.get_pending("C1", "1.2") == entry
    assert store.get_pending("C1", "9.9") is None
    assert json.loads(path.read_text()) == {"pending": {"C1:1.2": entry}, "grants": {}}
    assert not path.with_suffix(".json.tmp").exists()


def test_override_store_new_pending_replaces_old_in_same_thread(tmp_path):
    store = OverrideStore(tmp_path / "o.json")
    store.set_pending("C1", "1.2", {"event": {"text": "first"}})
    store.set_pending("C1", "1.2", {"event": {"text": "second"}})
    assert store.get_pending("C1", "1.2") == {"event": {"text": "second"}}


def test_override_store_clear_pending_is_idempotent(tmp_path):
    store = OverrideStore(tmp_path / "o.json")
    store.set_pending("C1", "1.2", {"event": {}})
    store.clear_pending("C1", "1.2")
    store.clear_pending("C1", "1.2")
    assert store.get_pending("C1", "1.2") is None


def test_override_store_grants_accumulate_per_thread(tmp_path):
    store = OverrideStore(tmp_path / "o.json")
    store.add_grant("C1", "1.2", {"text": "a"})
    store.add_grant("C1", "1.2", {"text": "b"})
    store.add_grant("C1", "3.4", {"text": "other thread"})
    assert [g["text"] for g in store.grants("C1", "1.2")] == ["a", "b"]
    assert [g["text"] for g in store.grants("C1", "3.4")] == ["other thread"]


def test_override_store_survives_corrupt_file(tmp_path):
    path = tmp_path / "o.json"
    path.write_text("{not json")
    store = OverrideStore(path)
    assert store.get_pending("C1", "1.2") is None
    store.add_grant("C1", "1.2", {"text": "a"})
    assert store.grants("C1", "1.2") == [{"text": "a"}]


def test_override_store_rereads_file_each_call(tmp_path):
    """The daemon must see state written by another process/instance."""
    path = tmp_path / "o.json"
    a = OverrideStore(path)
    b = OverrideStore(path)
    a.set_pending("C1", "1.2", {"event": {"text": "x"}})
    assert b.get_pending("C1", "1.2") == {"event": {"text": "x"}}


def test_override_store_take_pending_consumes_atomically(tmp_path):
    store = OverrideStore(tmp_path / "o.json")
    store.set_pending("C1", "1.2", {"event": {"text": "x"}})
    assert store.take_pending("C1", "1.2") == {"event": {"text": "x"}}
    # Second taker (duplicate delivery / second owner) gets nothing.
    assert store.take_pending("C1", "1.2") is None
    assert store.get_pending("C1", "1.2") is None


def test_override_store_pending_expires_after_ttl(tmp_path):
    clock = {"t": NOW}
    store = OverrideStore(tmp_path / "o.json", now=lambda: clock["t"])
    store.set_pending("C1", "1.2", {"event": {"text": "x"}, "asked_at": NOW})

    clock["t"] = NOW + _OVERRIDE_TTL_SECS - 1
    assert store.get_pending("C1", "1.2") is not None

    clock["t"] = NOW + _OVERRIDE_TTL_SECS + 1
    assert store.get_pending("C1", "1.2") is None
    assert store.take_pending("C1", "1.2") is None
    # take_pending dropped the stale entry from disk.
    assert json.loads((tmp_path / "o.json").read_text())["pending"] == {}


def test_override_store_legacy_entry_without_asked_at_never_expires(tmp_path):
    store = OverrideStore(tmp_path / "o.json", now=lambda: NOW + 10 * _OVERRIDE_TTL_SECS)
    store.set_pending("C1", "1.2", {"event": {"text": "x"}})
    assert store.get_pending("C1", "1.2") is not None


def test_override_store_tolerates_wrong_shapes(tmp_path):
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"pending": [], "grants": {"C1:1.2": "x"}}))
    store = OverrideStore(path)
    assert store.get_pending("C1", "1.2") is None
    assert store.grants("C1", "1.2") == []

    store.add_grant("C1", "1.2", {"text": "a"})  # replaces the non-list
    assert store.grants("C1", "1.2") == [{"text": "a"}]
    store.set_pending("C1", "1.2", {"event": {}})
    on_disk = json.loads(path.read_text())
    assert on_disk["pending"] == {"C1:1.2": {"event": {}}}
    assert on_disk["grants"] == {"C1:1.2": [{"text": "a"}]}
