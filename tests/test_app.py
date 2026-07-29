"""Tests for slackcc.app: _SeenSet, _pps_policy_text, and the handle() closure
exposed via build_app(...).slackcc_handle.

Fully offline: slack_bolt.App, PPSClient, backend.run_turn, backend_t3.run_turn
and t3_mirror.start are all monkeypatched to in-memory fakes/stubs. The only
real I/O is JSON file read/write under tmp_path (ClaimStore, SessionStore,
MirrorStore)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from slackcc import app as app_mod
from slackcc.app import _SeenSet, _pps_policy_text, bridge_header
from slackcc.backend import TurnResult
from slackcc.claims import ClaimStore
from slackcc.config import ChannelConfig, SenderPolicy, Settings
from slackcc.sanitize import SAFETY_PREAMBLE, wrap_untrusted
from slackcc.sessions import SessionStore

BOT_USER_ID = "UBOT"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeSlackClient:
    """Stand-in for both app.client (auth_test only) and the `client` param
    handed to handle() (chat_update/chat_postMessage)."""

    def __init__(self):
        self.chat_update_calls: list[dict] = []
        self.chat_postMessage_calls: list[dict] = []

    def auth_test(self):
        return {"user_id": BOT_USER_ID, "user": "bot"}

    def chat_update(self, **kwargs):
        self.chat_update_calls.append(kwargs)
        return {"ok": True}

    def chat_postMessage(self, **kwargs):
        self.chat_postMessage_calls.append(kwargs)
        return {"ok": True, "ts": "9999.0001"}


class FakeApp:
    """Stand-in for slack_bolt.App: only what build_app touches."""

    def __init__(self, token=None, **kwargs):
        self.token = token
        self.client = FakeSlackClient()
        self.handlers: dict[str, object] = {}

    def event(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco


class FakeSay:
    """Recorder for the `say` callable Bolt hands to a listener."""

    def __init__(self):
        self.calls: list[dict] = []
        self._n = 0

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self._n += 1
        return {"ts": f"placeholder-{self._n}"}


class FakeLogger:
    def __init__(self):
        self.warnings: list[tuple] = []
        self.errors: list[tuple] = []

    def warning(self, *a, **kw):
        self.warnings.append((a, kw))

    def error(self, *a, **kw):
        self.errors.append((a, kw))

    def info(self, *a, **kw):
        pass


class FakePPS:
    """Stand-in for PPSClient with a scripted verdict queue."""

    def __init__(self, url, *a, **kw):
        self.url = url
        self.calls: list[dict] = []
        self.verdicts: list[dict] = []

    def queue(self, verdict: dict) -> None:
        self.verdicts.append(verdict)

    def judge(self, *, sender, policy, text, context):
        self.calls.append(
            {"sender": sender, "policy": policy, "text": text, "context": context}
        )
        if self.verdicts:
            return self.verdicts.pop(0)
        return {"verdict": "allow", "category": None, "reason": ""}


def make_event(
    *,
    channel,
    user,
    text="hello there",
    ts="100.0001",
    thread_ts=None,
    subtype=None,
    bot_id=None,
    client_msg_id=None,
    event_ts=None,
    files=None,
):
    ev: dict = {"channel": channel, "user": user, "text": text, "ts": ts}
    if thread_ts is not None:
        ev["thread_ts"] = thread_ts
    if subtype is not None:
        ev["subtype"] = subtype
    if bot_id is not None:
        ev["bot_id"] = bot_id
    if client_msg_id is not None:
        ev["client_msg_id"] = client_msg_id
    if event_ts is not None:
        ev["event_ts"] = event_ts
    if files is not None:
        ev["files"] = files
    return ev


# --------------------------------------------------------------------------- #
# env builder
# --------------------------------------------------------------------------- #


@pytest.fixture
def make_env(tmp_path, monkeypatch):
    """Factory returning a fully-wired, offline build_app() environment."""

    def _make(presession: dict | None = None):
        monkeypatch.setattr(app_mod, "App", FakeApp)

        mirror_start_calls: list[tuple] = []

        def fake_mirror_start(*a, **kw):
            mirror_start_calls.append((a, kw))
            return None

        monkeypatch.setattr(app_mod.t3_mirror, "start", fake_mirror_start)

        pps_registry: list[FakePPS] = []

        class _RegisteringFakePPS(FakePPS):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                pps_registry.append(self)

        monkeypatch.setattr(app_mod, "PPSClient", _RegisteringFakePPS)

        # Stub the network file download so a naive files-path test can't
        # accidentally perform a real HTTP fetch. Default behavior writes a
        # small placeholder file to dest_dir; a test can override via
        # env.download_state["impl"] (e.g. to simulate a failure).
        download_calls: list[dict] = []
        download_state: dict = {"impl": None}

        def fake_download_slack_file(file_obj, dest_dir, token):
            download_calls.append(
                {"file_obj": file_obj, "dest_dir": dest_dir, "token": token}
            )
            if download_state["impl"] is not None:
                return download_state["impl"](file_obj, dest_dir, token)
            dest_dir.mkdir(parents=True, exist_ok=True)
            p = dest_dir / (file_obj.get("name") or file_obj.get("id") or "file")
            p.write_text("fake-downloaded-bytes")
            return p

        monkeypatch.setattr(app_mod, "download_slack_file", fake_download_slack_file)

        backend_calls: list[dict] = []
        backend_result = {"value": TurnResult(ok=True, text="claude reply",
                                               session_id="sess-claude-1")}

        def fake_backend_run_turn(**kwargs):
            backend_calls.append(kwargs)
            return backend_result["value"]

        monkeypatch.setattr(app_mod.backend, "run_turn", fake_backend_run_turn)

        backend_t3_calls: list[dict] = []
        backend_t3_result = {"value": TurnResult(ok=True, text="t3 reply",
                                                  session_id="sess-t3-1")}

        def fake_backend_t3_run_turn(**kwargs):
            # Guard the register-before-dispatch ordering (app.py): mark_posted
            # is a documented no-op for unregistered threads (t3.py), so if
            # mirror.register() were ever moved after this call (or dropped),
            # backend_t3.run_turn's inbound-message ledger would silently do
            # nothing -- risking a Slack message-loop via the outbound mirror.
            mirror_obj = kwargs["mirror"]
            thread_id = kwargs["thread_id"]
            threads_at_call = mirror_obj.threads()
            assert thread_id in threads_at_call, (
                "mirror.register() must run before backend_t3.run_turn() is "
                "called, or mark_posted() silently no-ops"
            )
            kwargs = dict(kwargs)
            kwargs["_mirror_entry_at_call"] = threads_at_call[thread_id]
            backend_t3_calls.append(kwargs)
            return backend_t3_result["value"]

        monkeypatch.setattr(app_mod.backend_t3, "run_turn", fake_backend_t3_run_turn)

        claims_file = tmp_path / "state" / "claimed.json"
        monkeypatch.setattr(app_mod, "claims_path", lambda: claims_file)

        cwd = tmp_path / "proj"
        cwd.mkdir(exist_ok=True)
        sessions_path = tmp_path / "state" / "sessions.json"

        if presession:
            sessions_path.parent.mkdir(parents=True, exist_ok=True)
            sessions_path.write_text(json.dumps(presession))

        channels = {
            "Ct3": ChannelConfig(
                channel_id="Ct3", project="t3proj", cwd=str(cwd),
                backend="t3", t3_project_id="proj-t3",
            ),
            "Cclaude": ChannelConfig(
                channel_id="Cclaude", project="claudeproj", cwd=str(cwd),
                backend="claude", permission_mode="bypassPermissions",
                persona="You are Foo-bot, the friendly helper for #claudeproj.",
            ),
        }
        senders = {
            "Uowner": SenderPolicy(
                user_id="Uowner", name="Dan", role="owner",
                runtime_mode="full-access", pps_mode="skip",
            ),
            "Ulogger": SenderPolicy(
                user_id="Ulogger", name="Logger", role="guest",
                runtime_mode="approval-required", pps_mode="log",
            ),
        }
        guest_defaults = SenderPolicy(
            user_id="_default", name="_default", role="guest",
            runtime_mode="approval-required", pps_mode="enforce",
            policy_extra="extra-guest-rule",
        )

        settings = Settings(
            bot_token="xoxb-settings-token-not-a-real-secret",
            app_token="xapp-settings-token-not-a-real-secret",
            config_path=tmp_path / "channels.json",
            sessions_path=sessions_path,
            claude_bin="claude",
            channels=channels,
            t3_url="http://127.0.0.1:0",
            t3_token="t3-tok",
            t3_owner="Dan",
            pps_url="http://127.0.0.1:0",
            senders=senders,
            guest_defaults=guest_defaults,
        )

        app = app_mod.build_app(settings)
        pps = pps_registry[-1]

        env = SimpleNamespace(
            settings=settings,
            app=app,
            handle=app.slackcc_handle,
            pps=pps,
            backend_calls=backend_calls,
            backend_result=backend_result,
            backend_t3_calls=backend_t3_calls,
            backend_t3_result=backend_t3_result,
            claims_file=claims_file,
            sessions_path=sessions_path,
            mirror_start_calls=mirror_start_calls,
            download_calls=download_calls,
            download_state=download_state,
            client=FakeSlackClient(),
            say=FakeSay(),
            logger=FakeLogger(),
        )
        return env

    return _make


def call_handle(env, event):
    env.handle(event, env.say, env.client, env.logger)


# --------------------------------------------------------------------------- #
# _SeenSet
# --------------------------------------------------------------------------- #


def test_seenset_first_time_false_then_true():
    seen = _SeenSet()
    assert seen.seen("abc") is False
    assert seen.seen("abc") is True


def test_seenset_none_and_empty_key_never_marked_seen():
    seen = _SeenSet()
    assert seen.seen(None) is False
    assert seen.seen(None) is False
    assert seen.seen("") is False
    assert seen.seen("") is False


def test_seenset_maxlen_evicts_oldest():
    seen = _SeenSet(maxlen=3)
    assert seen.seen("k1") is False
    assert seen.seen("k2") is False
    assert seen.seen("k3") is False
    # Bounded at 3: adding k4 pushes len to 4, evicting the oldest (k1) -> {k2,k3,k4}.
    assert seen.seen("k4") is False
    # k1 was forgotten, so it's reported as new again, not as a duplicate.
    assert seen.seen("k1") is False
    # k3 and k4 were never evicted, so they're still remembered as duplicates.
    assert seen.seen("k3") is True
    assert seen.seen("k4") is True


def test_seenset_repeat_hit_refreshes_recency():
    seen = _SeenSet(maxlen=3)
    seen.seen("k1")
    seen.seen("k2")
    seen.seen("k3")
    # Re-seeing k1 marks it recent (LRU refresh), so the next eviction takes
    # k2 — a late Slack retry of k1 stays deduped across window rollover.
    assert seen.seen("k1") is True
    assert seen.seen("k4") is False
    assert seen.seen("k1") is True
    assert seen.seen("k2") is False


# --------------------------------------------------------------------------- #
# _pps_policy_text
# --------------------------------------------------------------------------- #


def test_pps_policy_text_owner_mentions_full_access():
    cfg = ChannelConfig(channel_id="C1", project="proj-x", cwd=".")
    sp = SenderPolicy(user_id="U1", name="Dan", role="owner")
    text = _pps_policy_text(sp, cfg)
    assert "OWNER" in text
    assert "full access" in text
    assert "proj-x" not in text  # owner policy text isn't scoped to one project


def test_pps_policy_text_guest_mentions_project_and_disallowed_items():
    cfg = ChannelConfig(channel_id="C1", project="proj-x", cwd=".")
    sp = SenderPolicy(user_id="U2", name="Alice", role="guest")
    text = _pps_policy_text(sp, cfg)
    assert "GUEST" in text
    assert "proj-x" in text
    assert "NOT allowed" in text
    assert "secrets/credentials/keys" in text


def test_pps_policy_text_appends_policy_extra():
    cfg = ChannelConfig(channel_id="C1", project="proj-x", cwd=".")
    sp = SenderPolicy(user_id="U2", name="Alice", role="guest",
                       policy_extra="Never touch the prod database.")
    text = _pps_policy_text(sp, cfg)
    assert text.endswith("Never touch the prod database.")

    sp_no_extra = SenderPolicy(user_id="U3", name="Bob", role="guest")
    text_no_extra = _pps_policy_text(sp_no_extra, cfg)
    assert "Never touch" not in text_no_extra


# --------------------------------------------------------------------------- #
# handle(): events ignored
# --------------------------------------------------------------------------- #


def test_handle_ignores_bot_id_events(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Cclaude", user="Uowner", bot_id="B123"))
    assert env.say.calls == []
    assert env.backend_calls == []


@pytest.mark.parametrize("subtype", [
    "bot_message", "message_changed", "message_deleted",
    "channel_join", "channel_leave", "thread_broadcast",
])
def test_handle_ignores_configured_subtypes(make_env, subtype):
    env = make_env()
    call_handle(env, make_event(channel="Cclaude", user="Uowner", subtype=subtype))
    assert env.say.calls == []
    assert env.backend_calls == []


def test_handle_ignores_messages_from_the_bot_itself(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Cclaude", user=BOT_USER_ID))
    assert env.say.calls == []
    assert env.backend_calls == []


def test_handle_ignores_duplicate_client_msg_id(make_env):
    env = make_env()
    ev1 = make_event(channel="Cclaude", user="Uowner", ts="1.1",
                      client_msg_id="dup-id")
    ev2 = make_event(channel="Cclaude", user="Uowner", ts="2.2",
                      client_msg_id="dup-id")
    call_handle(env, ev1)
    call_handle(env, ev2)
    assert len(env.backend_calls) == 1
    assert len(env.say.calls) == 1


def test_handle_ignores_unconfigured_channel(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Cnotconfigured", user="Uowner"))
    assert env.say.calls == []
    assert env.backend_calls == []
    assert env.backend_t3_calls == []


def test_handle_ignores_empty_text_and_no_files(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Cclaude", user="Uowner", text=""))
    assert env.say.calls == []
    assert env.backend_calls == []


# --------------------------------------------------------------------------- #
# handle(): claimed thread
# --------------------------------------------------------------------------- #


def test_handle_skips_claimed_thread(make_env):
    env = make_env()
    ClaimStore(env.claims_file).claim("Cclaude", "50.0001")
    call_handle(env, make_event(channel="Cclaude", user="Uowner", ts="50.0001"))
    assert env.say.calls == []
    assert env.backend_calls == []


def test_handle_processes_unclaimed_thread(make_env):
    env = make_env()
    ClaimStore(env.claims_file).claim("Cclaude", "999.9999")  # a different thread
    call_handle(env, make_event(channel="Cclaude", user="Uowner", ts="50.0001"))
    assert len(env.backend_calls) == 1


# --------------------------------------------------------------------------- #
# handle(): pps gate for guests
# --------------------------------------------------------------------------- #


def test_handle_guest_pps_deny_blocks_backend_and_edits_placeholder(make_env):
    env = make_env()
    env.pps.queue({"verdict": "deny", "category": "exfiltration", "reason": "nope"})
    call_handle(env, make_event(channel="Cclaude", user="Uguest", ts="10.1"))

    assert env.backend_calls == []
    assert env.backend_t3_calls == []
    assert len(env.client.chat_update_calls) == 1
    update = env.client.chat_update_calls[0]
    assert "safety screen" in update["text"]
    assert update["ts"] == "placeholder-1"


def test_handle_guest_pps_error_fails_closed(make_env):
    env = make_env()
    env.pps.queue({"verdict": "error", "category": "other", "reason": "pps down"})
    call_handle(env, make_event(channel="Cclaude", user="Uguest", ts="10.2"))

    assert env.backend_calls == []
    assert env.backend_t3_calls == []
    update = env.client.chat_update_calls[0]
    assert ":warning:" in update["text"]
    assert "safety screen is unavailable" in update["text"]


def test_handle_guest_pps_allow_dispatches_to_t3_with_approval_required(make_env):
    env = make_env()
    env.pps.queue({"verdict": "allow", "category": None, "reason": ""})
    call_handle(env, make_event(channel="Ct3", user="Uguest", ts="10.3",
                                 text="please fix the bug"))

    assert len(env.backend_t3_calls) == 1
    call = env.backend_t3_calls[0]
    assert call["runtime_mode"] == "approval-required"

    assert len(env.pps.calls) == 1
    judged = env.pps.calls[0]
    assert judged["sender"] == "Uguest"
    assert "t3proj" in judged["policy"]
    assert "Uguest" in judged["policy"]


def test_handle_owner_skips_pps_judge_and_gets_full_access(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Ct3", user="Uowner", ts="10.4",
                                 text="deploy the project"))

    assert env.pps.calls == []
    assert len(env.backend_t3_calls) == 1
    assert env.backend_t3_calls[0]["runtime_mode"] == "full-access"


# --------------------------------------------------------------------------- #
# handle(): claude-backend permission_mode by role
# --------------------------------------------------------------------------- #


def test_handle_claude_backend_guest_forced_to_default_permission_mode(make_env):
    env = make_env()
    env.pps.queue({"verdict": "allow", "category": None, "reason": ""})
    call_handle(env, make_event(channel="Cclaude", user="Uguest", ts="20.1"))

    assert len(env.backend_calls) == 1
    assert env.backend_calls[0]["permission_mode"] == "default"


def test_handle_claude_backend_owner_gets_channel_permission_mode(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Cclaude", user="Uowner", ts="20.2"))

    assert len(env.backend_calls) == 1
    assert env.backend_calls[0]["permission_mode"] == "bypassPermissions"


# --------------------------------------------------------------------------- #
# handle(): reply flow -- scrubbing, error text, session persistence
# --------------------------------------------------------------------------- #


def test_handle_reply_scrubs_secrets_from_final_text(make_env):
    env = make_env()
    # Assembled at runtime so the literal never trips GitHub push protection.
    leaked_token = "xoxb-" + "1234567890123-abcdefghijklmno"
    env.backend_result["value"] = TurnResult(
        ok=True, text=f"Here you go: {leaked_token}", session_id="sess-scrub",
    )
    call_handle(env, make_event(channel="Cclaude", user="Uowner", ts="30.1"))

    update = env.client.chat_update_calls[0]
    assert leaked_token not in update["text"]
    assert "[redacted:slack-token]" in update["text"]
    assert update["ts"] == "placeholder-1"
    assert update["channel"] == "Cclaude"


def test_handle_reply_failure_produces_warning_text(make_env):
    env = make_env()
    env.backend_result["value"] = TurnResult(ok=False, text="", error="boom")
    call_handle(env, make_event(channel="Cclaude", user="Uowner", ts="30.2"))

    update = env.client.chat_update_calls[0]
    assert ":warning:" in update["text"]
    assert "boom" in update["text"]


def test_handle_stores_session_id_after_successful_turn(make_env):
    env = make_env()
    env.backend_result["value"] = TurnResult(
        ok=True, text="ok", session_id="sess-store-me",
    )
    call_handle(env, make_event(channel="Cclaude", user="Uowner", ts="30.3"))

    store = SessionStore(env.sessions_path)
    assert store.get("Cclaude", "30.3") == "sess-store-me"


# --------------------------------------------------------------------------- #
# handle(): resume via a pre-existing session
# --------------------------------------------------------------------------- #


def test_handle_t3_resume_reuses_thread_id_and_is_not_new(make_env):
    key = SessionStore.key("Ct3", "40.1")
    env = make_env(presession={key: "already-existing-t3-thread"})

    call_handle(env, make_event(channel="Ct3", user="Uowner", ts="40.1"))

    assert len(env.backend_t3_calls) == 1
    call = env.backend_t3_calls[0]
    assert call["thread_id"] == "already-existing-t3-thread"
    assert call["is_new"] is False


def test_handle_t3_no_prior_session_is_new_with_deterministic_thread_id(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Ct3", user="Uowner", ts="40.2"))

    assert len(env.backend_t3_calls) == 1
    call = env.backend_t3_calls[0]
    assert call["is_new"] is True
    assert call["thread_id"] == "slack-Ct3-40-2"


# --------------------------------------------------------------------------- #
# handle(): pps runtime mode "log" -- judge is called but never blocks
# --------------------------------------------------------------------------- #


def test_handle_log_mode_deny_verdict_does_not_block_dispatch(make_env):
    env = make_env()
    env.pps.queue({"verdict": "deny", "category": "exfiltration", "reason": "nope"})
    call_handle(env, make_event(channel="Cclaude", user="Ulogger", ts="70.1"))

    # The judge is still consulted (for visibility/logging)...
    assert len(env.pps.calls) == 1
    # ...but a "log" mode deny must NOT block the turn, unlike "enforce".
    assert len(env.backend_calls) == 1
    assert len(env.client.chat_update_calls) == 1
    assert "safety screen" not in env.client.chat_update_calls[0]["text"]


def test_handle_log_mode_error_verdict_does_not_block_dispatch(make_env):
    env = make_env()
    env.pps.queue({"verdict": "error", "category": "other", "reason": "pps down"})
    call_handle(env, make_event(channel="Cclaude", user="Ulogger", ts="70.2"))

    assert len(env.pps.calls) == 1
    # "log" mode must not fail closed the way "enforce" does on judge errors.
    assert len(env.backend_calls) == 1
    assert len(env.client.chat_update_calls) == 1
    assert "safety screen" not in env.client.chat_update_calls[0]["text"]


# --------------------------------------------------------------------------- #
# handle(): mirror.register() must run before backend_t3.run_turn() dispatch
# --------------------------------------------------------------------------- #


def test_handle_t3_registers_thread_in_mirror_before_dispatching_run_turn(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Ct3", user="Uowner", ts="80.1"))

    assert len(env.backend_t3_calls) == 1
    call = env.backend_t3_calls[0]

    # The fake backend_t3.run_turn asserts (at call time, inside the fixture)
    # that the thread was already registered in the mirror; here we also check
    # the recorded snapshot maps to the *correct* channel/thread_ts, and that
    # the on-disk t3_mirror.json (not just the in-memory object) reflects it --
    # this is what makes mark_posted() a real no-op guard rather than a
    # silently-skipped ledger entry.
    entry_at_call = call["_mirror_entry_at_call"]
    assert entry_at_call["channel"] == "Ct3"
    assert entry_at_call["thread_ts"] == "80.1"

    mirror_path = env.sessions_path.parent / "t3_mirror.json"
    on_disk = json.loads(mirror_path.read_text())
    assert on_disk["threads"][call["thread_id"]]["channel"] == "Ct3"
    assert on_disk["threads"][call["thread_id"]]["thread_ts"] == "80.1"


# --------------------------------------------------------------------------- #
# handle(): files path
# --------------------------------------------------------------------------- #


def test_handle_files_only_message_with_empty_text_reaches_backend(make_env):
    env = make_env()
    files = [{"id": "F1", "name": "notes.txt", "url_private": "https://files.slack.com/f1"}]
    call_handle(env, make_event(channel="Cclaude", user="Uowner", text="",
                                 files=files, ts="60.1"))

    # Empty text + files must still pass the `not text and not files` gate.
    assert len(env.backend_calls) == 1
    assert len(env.download_calls) == 1
    assert env.download_calls[0]["file_obj"] == files[0]
    # Downloaded into a per-thread .slack-incoming dir under the channel's cwd.
    assert ".slack-incoming" in str(env.download_calls[0]["dest_dir"])
    assert "60_1" in str(env.download_calls[0]["dest_dir"])


def test_handle_files_prompt_lists_local_paths_for_the_agent(make_env):
    env = make_env()
    files = [{"id": "F1", "name": "notes.txt", "url_private": "https://files.slack.com/f1"}]
    call_handle(env, make_event(channel="Cclaude", user="Uowner", text="",
                                 files=files, ts="60.2"))

    assert len(env.backend_calls) == 1
    prompt = env.backend_calls[0]["prompt"]
    assert "may Read them" in prompt
    assert "notes.txt" in prompt


def test_handle_guest_pps_judged_text_includes_attached_filenames(make_env):
    # Filenames must ride along with the judged text so a malicious filename
    # can't smuggle content past the judge by hiding outside the text field.
    env = make_env()
    env.pps.queue({"verdict": "allow", "category": None, "reason": ""})
    files = [
        {"id": "F1", "name": "secret.pdf", "url_private": "https://files.slack.com/f1"},
        {"id": "F2", "name": "plan.csv", "url_private": "https://files.slack.com/f2"},
    ]
    call_handle(env, make_event(channel="Ct3", user="Uguest", text="please check",
                                 files=files, ts="60.3"))

    assert len(env.pps.calls) == 1
    judged = env.pps.calls[0]["text"]
    assert "please check" in judged
    assert "[attached files: secret.pdf, plan.csv]" in judged


def test_handle_file_download_exception_does_not_kill_the_turn(make_env):
    env = make_env()

    def boom(file_obj, dest_dir, token):
        raise RuntimeError("network exploded")

    env.download_state["impl"] = boom
    files = [{"id": "F1", "name": "notes.txt", "url_private": "https://files.slack.com/f1"}]
    call_handle(env, make_event(channel="Cclaude", user="Uowner", text="",
                                 files=files, ts="60.4"))

    assert len(env.download_calls) == 1
    assert env.logger.warnings  # the failure is logged...
    # ...but the turn still completes and reaches the backend.
    assert len(env.backend_calls) == 1
    assert len(env.client.chat_update_calls) == 1


# --------------------------------------------------------------------------- #
# handle(): bridge header + fencing only for messages pps didn't gate
# --------------------------------------------------------------------------- #


def test_bridge_header_is_one_line_and_points_at_the_protocol_doc():
    header = bridge_header("C123", "17.42")

    assert header == "[slack channel=C123 thread=17.42 protocol=~/.claude/slack-bridge.md]"
    assert "\n" not in header  # per-turn cost is one line, not a preamble


def test_handle_claude_backend_screened_prompt_is_plain_text(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Cclaude", user="Uowner", text="hello there",
                                 ts="90.1"))

    assert len(env.backend_calls) == 1
    call = env.backend_calls[0]
    # Owner: pps vouches for it, so no fencing and no security lecture.
    assert call["prompt"] == "hello there"
    assert "EXTERNAL_UNTRUSTED_CONTENT" not in call["prompt"]


def test_handle_claude_backend_system_prompt_has_persona_and_bridge_header(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Cclaude", user="Uowner", text="hello there",
                                 ts="90.2"))

    assert len(env.backend_calls) == 1
    system_prompt = env.backend_calls[0]["append_system_prompt"]
    assert "Foo-bot" in system_prompt
    assert bridge_header("Cclaude", "90.2") in system_prompt
    assert SAFETY_PREAMBLE not in system_prompt
    assert system_prompt.index("Foo-bot") < system_prompt.index("[slack channel=")


def test_handle_claude_backend_unscreened_guest_still_gets_fencing(make_env):
    env = make_env()
    # Ulogger is a guest in pps_mode="log": the judge observes but never blocks,
    # so nothing gated this message and the fence has to stay.
    call_handle(env, make_event(channel="Cclaude", user="Ulogger", text="hello there",
                                 ts="90.5"))

    assert len(env.backend_calls) == 1
    call = env.backend_calls[0]
    assert call["prompt"] == wrap_untrusted("slack", "Ulogger", "hello there")
    assert SAFETY_PREAMBLE in call["append_system_prompt"]


def test_handle_claude_backend_enforced_guest_is_trusted(make_env):
    env = make_env()
    # Unknown sender -> guest_defaults -> pps_mode="enforce": a blocking judge
    # already passed it, so it arrives as data the agent can act on.
    call_handle(env, make_event(channel="Cclaude", user="Ustranger", text="hello there",
                                 ts="90.6"))

    assert len(env.backend_calls) == 1
    call = env.backend_calls[0]
    assert call["prompt"] == "hello there"
    assert SAFETY_PREAMBLE not in call["append_system_prompt"]


def test_handle_t3_new_thread_prompt_is_header_plus_plain_text(make_env):
    env = make_env()
    call_handle(env, make_event(channel="Ct3", user="Uowner", text="hi", ts="90.3"))

    assert len(env.backend_t3_calls) == 1
    call = env.backend_t3_calls[0]
    assert call["is_new"] is True
    assert call["prompt"] == f'{bridge_header("Ct3", "90.3")}\n\nhi'


def test_handle_t3_resume_prompt_carries_the_same_one_line_header(make_env):
    key = SessionStore.key("Ct3", "90.4")
    env = make_env(presession={key: "existing-thread-id"})
    call_handle(env, make_event(channel="Ct3", user="Uowner", text="hi again",
                                 ts="90.4"))

    assert len(env.backend_t3_calls) == 1
    call = env.backend_t3_calls[0]
    assert call["is_new"] is False
    assert call["thread_id"] == "existing-thread-id"
    # A resumed thread pays exactly what a new one does: one routing line.
    assert call["prompt"] == f'{bridge_header("Ct3", "90.4")}\n\nhi again'


def test_handle_t3_unscreened_guest_gets_the_guard_inline(make_env):
    env = make_env()
    # No system-prompt field on the t3 wire, so the directive rides in the
    # message -- but only for the sender that nothing gated.
    call_handle(env, make_event(channel="Ct3", user="Ulogger", text="hi", ts="90.7"))

    assert len(env.backend_t3_calls) == 1
    prompt = env.backend_t3_calls[0]["prompt"]
    assert prompt.startswith(bridge_header("Ct3", "90.7"))
    assert SAFETY_PREAMBLE in prompt
    assert wrap_untrusted("slack", "Ulogger", "hi") in prompt
