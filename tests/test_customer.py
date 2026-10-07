"""Tests for the customer voice: block parsing, the leak gate, progress text,
the what-Slack-was-told ledger and the owner alert (`slackcc.customer`)."""

from __future__ import annotations

import json

import pytest

from slackcc import customer
from slackcc.config import ChannelConfig, SenderPolicy, Settings

# --- block parsing ---------------------------------------------------------------


def test_block_is_everything_after_the_marker():
    text = "Fixed the thing in the handler.\n\n### Message for the customer\nYour booking page now shows the new price."
    assert customer.extract_block(text) == "Your booking page now shows the new price."


def test_last_marker_wins():
    text = ("Notes: the template says\n### Message for the customer\nquoted example\n\n"
            "More technical detail.\n\n### Message for the customer\nThe real message.")
    assert customer.extract_block(text) == "The real message."


@pytest.mark.parametrize("marker", [
    "## Message for the customer", "#### message for the customer",
    "### Message for the customer:", "###Message for the customer",
])
def test_marker_is_tolerant_of_heading_level_case_and_colon(marker):
    assert customer.extract_block(f"tech\n{marker}\nhello") == "hello"


@pytest.mark.parametrize("text", [
    "", "no block here", "### Message for the customer", "### Message for the customer\n   \n",
    "I wrote ### Message for the customer inline", "<!-- Message for the customer -->\nhi",
])
def test_no_block_when_marker_missing_or_empty(text):
    assert customer.extract_block(text) is None


def test_resolve_final_block_no_block_and_gated():
    ok = customer.resolve_final("tech\n### Message for the customer\nAll set.")
    assert (ok.kind, ok.text) == ("block", "All set.")

    none = customer.resolve_final("just technical")
    assert none.kind == "no_block" and "no `### Message for the customer` block" in none.why

    bad = customer.resolve_final("tech\n### Message for the customer\nSee src/app/page.tsx (PR #12).")
    assert bad.kind == "gated" and bad.text == ""
    assert "file path" in bad.reasons and "pull request reference" in bad.reasons
    assert "looks technical" in bad.why


# --- leak gate ---------------------------------------------------------------------


@pytest.mark.parametrize("text,reason", [
    ("Here:\n```js\nconst a = 1\n```", "code block"),
    ("I changed src/components/Booking.tsx", "file path"),
    ("see components/Header", "file path"),
    ("edited app/routes/index.py", "file path"),
    ("It's in /home/dgordon/projects/x", "file path"),
    ("check ~/notes", "file path"),
    ("updated config.json and README.md", "file name"),
    ("opened PR #123", "pull request reference"),
    ("merged pull request 45", "pull request reference"),
    ("Added the page (#88)", "pull request reference"),
    ("https://github.com/acme/app/pull/9", "pull request reference"),
    ("commit 3f2a9c1 fixes it", "commit hash"),
    ("sha 9b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c", "commit hash"),
    ("Traceback (most recent call last):", "stack trace"),
    ('File "/x/app.py", line 12, in run', "stack trace"),
    ("    at render (page.js:10:5)", "stack trace"),
    ("got a TypeError: x is undefined", "stack trace"),
    ("pushed slack/customer-voice", "branch name"),
    ("on branch feature/add-booking-page", "branch name"),
    ("fix/header_bug is merged", "branch name"),
])
def test_gate_catches_technical_text(text, reason):
    assert reason in customer.leak_reasons(text)


@pytest.mark.parametrize("text", [
    "Done - the booking page now shows the new price. It's being published now.",
    "The /settings page now has a Save button.",
    "Open the /settings and /billing pages to check.",
    "We open Mon/Tue and close Wed/Thu.",
    "Works on and/or off the phone; the bug/feature list is shorter.",
    "Your site runs on Node.js and Next.js, which I left alone.",
    "I fixed 3 of the 12 problems you listed (so 9 to go).",
    "Order 1234567 was refunded.",
    "The defaced banner is fixed.",   # 7 hex letters, no digit: not a SHA
    "Call 555-0100 or email help@example.com.",
    "Version 2.1 is out; see example.com for details.",
    "It was a feature/bug mix-up.",    # no kebab suffix: not a branch
    "Slack/Teams messages are unaffected.",
    "Take care. Go ahead and refresh the page.",
    "PR is short for press release in your team's wording.",   # no number
])
def test_gate_lets_ordinary_prose_through(text):
    assert customer.leak_reasons(text) == []


def test_gate_reports_each_reason_once():
    reasons = customer.leak_reasons("a.py b.py src/x/y.py ```x```")
    assert reasons.count("file path") <= 1 and reasons.count("file name") <= 1
    assert "code block" in reasons


# --- progress text -------------------------------------------------------------


def test_progress_text_clean_narration_elapsed_and_steps():
    out = customer.progress_text("Adding the new price to the booking page.", 4 * 60 + 12, 9)
    assert out == (":hourglass_flowing_sand: Adding the new price to the booking page.\n"
                   "_Still working on it - about 4 minutes in, 9 steps so far._")


def test_progress_text_hides_technical_narration_behind_generic_line():
    out = customer.progress_text("Editing src/app/page.tsx now", 30, 1)
    assert "page.tsx" not in out
    assert customer._GENERIC_NARRATION in out
    assert "less than a minute in, 1 step so far" in out


def test_progress_text_without_narration_or_steps_and_clips_long_narration():
    assert customer.progress_text("", 65, 0).endswith("_Still working on it - about 1 minute in._")
    long = customer.progress_text("word " * 200, 0, 0, clip=50)
    assert len(long.splitlines()[0]) < 90 and "…" in long


# --- ledger ----------------------------------------------------------------------


def test_ledger_appends_jsonl_and_returns_recent(tmp_path):
    led = customer.Ledger(tmp_path / "led")
    led.append("k", "block", "first", slack_ts="1.1", t3_message_id="m1")
    led.append("k", "dan_forward", "second")
    lines = (tmp_path / "led" / "k.jsonl").read_text().splitlines()
    first = json.loads(lines[0])
    assert first["source"] == "block" and first["text"] == "first"
    assert first["slack_ts"] == "1.1" and first["t3_message_id"] == "m1" and first["ts"]
    assert "slack_ts" not in json.loads(lines[1])
    assert [e["text"] for e in led.recent("k")] == ["first", "second"]
    assert led.recent("other") == []


def test_ledger_note_is_last_four_entries_clipped(tmp_path):
    led = customer.Ledger(tmp_path)
    assert led.note("k") == ""
    for i in range(6):
        led.append("k", "block" if i % 2 else "dan_forward", f"msg {i} " + "x" * 400)
    note = led.note("k")
    assert note.startswith("[What the customer has been told so far")
    assert note.count("\n- ") == 4
    assert "msg 2" in note and "msg 5" in note and "msg 1" not in note
    assert "Dan: msg 2" in note and "you: msg 3" in note
    assert "…" in note and len(note) < 1800


def test_ledger_file_is_trimmed_not_unbounded(tmp_path):
    led = customer.Ledger(tmp_path)
    for i in range(customer._FILE_MAX + 5):
        led.append("k", "block", f"m{i}")
    lines = (tmp_path / "k.jsonl").read_text().splitlines()
    assert len(lines) <= customer._FILE_MAX
    assert json.loads(lines[-1])["text"] == f"m{customer._FILE_MAX + 4}"


def test_ledger_key_is_sanitised_and_thread_key_matches_t3_id(tmp_path):
    led = customer.Ledger(tmp_path)
    led.append("../../evil", "block", "x")
    assert [p.name for p in tmp_path.iterdir()] == [".._.._evil.jsonl"]
    assert customer.thread_key("C1", "17.42") == "slack-C1-17-42"


# --- owner alert -------------------------------------------------------------------


class _Slack:
    def __init__(self, fail_dm=False):
        self.dms, self.fail_dm = [], fail_dm

    def chat_getPermalink(self, **kw):
        return {"permalink": "https://slack.example/p1"}

    def chat_postMessage(self, **kw):
        if self.fail_dm:
            raise RuntimeError("nope")
        self.dms.append(kw)


def _settings(tmp_path, owners=("Uown", "Uown2")):
    cfg = ChannelConfig(channel_id="C1", project="acme", cwd=str(tmp_path),
                        backend="t3", t3_project_id="p", audience="customer")
    return Settings(
        bot_token="b", app_token="a", config_path=tmp_path / "c.json",
        sessions_path=tmp_path / "s.json", claude_bin="claude", channels={"C1": cfg},
        t3_gui_url="https://t3.example:7443",
        senders={o: SenderPolicy(user_id=o, name=o, role="owner") for o in owners})


def test_alert_owners_dms_every_owner_with_raw_text_links_and_reason(tmp_path):
    slack = _Slack()
    customer.alert_owners(slack, _settings(tmp_path), channel="C1", thread_ts="17.42",
                          reason="the final message has no block", raw="the raw ``` text")
    assert [d["channel"] for d in slack.dms] == ["Uown", "Uown2"]
    text = slack.dms[0]["text"]
    assert "#acme" in text and "the final message has no block" in text
    assert "C1" in text and "17.42" in text
    assert "https://t3.example:7443/primary/slack-C1-17-42" in text
    assert "https://slack.example/p1" in text
    assert "the raw '''" in text   # inner fences neutralised so the quote stays intact


def test_alert_owners_without_owners_or_with_failing_dm_is_silent(tmp_path):
    customer.alert_owners(_Slack(), _settings(tmp_path, owners=()), channel="C1",
                          thread_ts="1.1", reason="r", raw="x")
    customer.alert_owners(_Slack(fail_dm=True), _settings(tmp_path), channel="C1",
                          thread_ts="1.1", reason="r", raw="x")


def test_alert_owners_scrubs_secrets(tmp_path):
    slack = _Slack()
    token = "xoxb-" + "1234567890123-abcdefghijklmno"
    customer.alert_owners(slack, _settings(tmp_path), channel="C1", thread_ts="1.1",
                          reason="r", raw=f"leaked {token}")
    assert token not in slack.dms[0]["text"]


def test_holding_line_claims_no_success():
    low = customer.HOLDING_LINE.lower()
    assert "putting together an update" in low
    assert not any(w in low for w in ("done", "live", "fixed", "published", "deployed"))
