"""Offline tests for `slack-wait-reply` -- the half of the agent-driven loop
that the daemon deliberately stays out of (the thread is claimed), which is why
inbound attachments have to be downloaded here too.

No Slack API: the WebClient is faked and the file download is stubbed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from slackcc import slackfiles as slackfiles_mod
from slackcc import wait_cli


BOT = "UBOT"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A wait_cli with a fake Slack, a stubbed downloader and its own state."""
    replies: list[dict] = []
    downloaded: list[dict] = []

    class FakeWebClient:
        def __init__(self, token=None):
            self.token = token

        def auth_test(self):
            return {"user_id": BOT}

        def conversations_replies(self, channel, ts, oldest, limit):
            return {"messages": replies}

    def fake_download(file_obj, dest_dir, token):
        downloaded.append({"file_obj": file_obj, "dest_dir": dest_dir, "token": token})
        if file_obj.get("_boom"):
            raise slackfiles_mod.SlackFileError("no files:read scope")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / file_obj["name"]
        path.write_bytes(b"png-bytes")
        return path

    monkeypatch.setattr(wait_cli, "WebClient", FakeWebClient)
    monkeypatch.setattr(wait_cli, "resolve_token", lambda: "tok-abc")
    monkeypatch.setattr(wait_cli, "claims_path", lambda: tmp_path / "claimed.json")
    monkeypatch.setattr(wait_cli, "channel_cwd", lambda cid: None)
    monkeypatch.setattr(slackfiles_mod, "download_slack_file", fake_download)
    # No real waiting: the clock jumps, so a no-reply run hits its deadline at once.
    clock = {"t": 0.0}

    def fake_time():
        clock["t"] += 5.0
        return clock["t"]

    monkeypatch.setattr(wait_cli, "time",
                        SimpleNamespace(time=fake_time, sleep=lambda s: None))
    return SimpleNamespace(replies=replies, downloaded=downloaded, tmp_path=tmp_path)


def _run(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["slack-wait-reply", *argv])
    return wait_cli.main()


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip())


def test_plain_reply_is_returned_as_json(env, monkeypatch, capsys):
    env.replies.append({"ts": "100.2", "user": "U1", "text": "looks good"})

    assert _run(monkeypatch, "C1", "--thread", "100.1") == 0
    assert _out(capsys) == {"ts": "100.2", "user": "U1", "text": "looks good"}


def test_attachment_is_downloaded_and_reported_as_a_local_path(
        env, monkeypatch, capsys, tmp_path):
    """The regression: an image sent mid-loop used to reach the agent as a
    caption with nothing behind it."""
    env.replies.append({
        "ts": "100.2", "user": "U1", "text": "image for the ticker",
        "files": [{"name": "chart.png", "url_private": "https://slack/x"}],
    })

    dest = tmp_path / "project"
    assert _run(monkeypatch, "C1", "--thread", "100.1", "--files-dir", str(dest)) == 0

    out = _out(capsys)
    assert out["text"] == "image for the ticker"
    saved = dest / ".slack-incoming" / "100_1" / "chart.png"
    assert out["files"] == [{"name": "chart.png", "path": str(saved)}]
    assert saved.read_bytes() == b"png-bytes"
    assert env.downloaded[0]["token"] == "tok-abc"


def test_file_share_subtype_is_a_real_message_not_a_system_event(
        env, monkeypatch, capsys, tmp_path):
    """Slack marks some uploads `subtype: file_share`; the old blanket
    "any subtype is noise" skip dropped exactly the messages with files."""
    env.replies.append({
        "ts": "100.2", "user": "U1", "text": "", "subtype": "file_share",
        "files": [{"name": "logo.png", "url_private": "https://slack/x"}],
    })

    assert _run(monkeypatch, "C1", "--thread", "100.1",
                "--files-dir", str(tmp_path / "p")) == 0
    assert [f["name"] for f in _out(capsys)["files"]] == ["logo.png"]


def test_other_subtypes_and_our_own_posts_are_still_skipped(env, monkeypatch, capsys):
    env.replies.extend([
        {"ts": "100.2", "user": BOT, "text": "draft v1"},
        {"ts": "100.3", "bot_id": "B1", "text": "posted by an app"},
        {"ts": "100.4", "user": "U1", "text": "joined", "subtype": "channel_join"},
    ])

    assert _run(monkeypatch, "C1", "--thread", "100.1", "--timeout", "10") == 3
    err = capsys.readouterr().err
    assert json.loads(err.strip())["timeout"] is True


def test_files_land_where_the_daemon_would_put_them(
        env, monkeypatch, capsys, tmp_path):
    """Default destination is the channel's project dir from channels.json, so
    both inbound legs write the same thread's files to the same directory."""
    project = tmp_path / "gphotos"
    monkeypatch.setattr(wait_cli, "channel_cwd", lambda cid: project)
    env.replies.append({
        "ts": "100.2", "user": "U1", "text": "",
        "files": [{"name": "flyer.png", "url_private": "https://slack/x"}],
    })

    assert _run(monkeypatch, "C1", "--thread", "100.1") == 0
    assert _out(capsys)["files"][0]["path"] == str(
        project / ".slack-incoming" / "100_1" / "flyer.png")


def test_unconfigured_channel_falls_back_to_the_cwd(env, monkeypatch, capsys, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    env.replies.append({
        "ts": "100.2", "user": "U1", "text": "",
        "files": [{"name": "a.png", "url_private": "https://slack/x"}],
    })

    assert _run(monkeypatch, "C1", "--thread", "100.1") == 0
    assert _out(capsys)["files"][0]["path"] == str(
        work / ".slack-incoming" / "100_1" / "a.png")


def test_a_failed_download_is_warned_about_not_fatal(env, monkeypatch, capsys, tmp_path):
    env.replies.append({
        "ts": "100.2", "user": "U1", "text": "two files",
        "files": [
            {"name": "bad.png", "url_private": "https://slack/x", "_boom": True},
            {"name": "ok.png", "url_private": "https://slack/y"},
        ],
    })

    assert _run(monkeypatch, "C1", "--thread", "100.1",
                "--files-dir", str(tmp_path / "p")) == 0
    captured = capsys.readouterr()
    assert [f["name"] for f in json.loads(captured.out.strip())["files"]] == ["ok.png"]
    assert "bad.png" in captured.err and "files:read" in captured.err


def test_release_still_frees_the_claim_on_a_reply_with_files(
        env, monkeypatch, capsys, tmp_path):
    env.replies.append({
        "ts": "100.2", "user": "U1", "text": "",
        "files": [{"name": "a.png", "url_private": "https://slack/x"}],
    })

    assert _run(monkeypatch, "C1", "--thread", "100.1", "--claim", "--release",
                "--files-dir", str(tmp_path / "p")) == 0
    capsys.readouterr()
    assert json.loads((tmp_path / "claimed.json").read_text()) == []


def test_messages_at_or_before_the_cursor_are_ignored(env, monkeypatch, capsys):
    env.replies.append({"ts": "100.1", "user": "U1", "text": "the root message"})

    assert _run(monkeypatch, "C1", "--thread", "100.1", "--timeout", "10") == 3


def test_no_token_is_a_clean_failure(env, monkeypatch, capsys):
    monkeypatch.setattr(wait_cli, "resolve_token", lambda: None)
    assert _run(monkeypatch, "C1", "--thread", "100.1") == 2
    assert "SLACK_BOT_TOKEN" in capsys.readouterr().err


def test_downloads_only_touch_the_network_when_something_is_attached(
        env, monkeypatch, capsys):
    env.replies.append({"ts": "100.2", "user": "U1", "text": "no files here"})

    assert _run(monkeypatch, "C1", "--thread", "100.1") == 0
    assert "files" not in _out(capsys)
    assert env.downloaded == []


def test_thread_dir_uses_the_thread_root_not_the_reply_ts(
        env, monkeypatch, capsys, tmp_path):
    """One directory per conversation, not one per reply -- matches the daemon."""
    env.replies.append({
        "ts": "100.9", "user": "U1", "text": "",
        "files": [{"name": "a.png", "url_private": "https://slack/x"}],
    })

    dest = tmp_path / "p"
    assert _run(monkeypatch, "C1", "--thread", "100.1", "--files-dir", str(dest)) == 0
    assert Path(_out(capsys)["files"][0]["path"]).parent == (
        dest / ".slack-incoming" / "100_1")
