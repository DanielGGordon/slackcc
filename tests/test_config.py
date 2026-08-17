"""Tests for slackcc.config: channel routing, sender policy, settings loading.

All tests are offline/hermetic: they use tmp_path for config files and
monkeypatch for env vars. No network, no Slack/T3/pps, no repo .state/.env.
"""

import json

import pytest

from slackcc.config import (
    ChannelConfig,
    SenderPolicy,
    load_channels,
    load_senders,
    load_settings,
)


def _write_json(path, data):
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# load_channels
# ---------------------------------------------------------------------------


def test_load_channels_valid_claude_channel(tmp_path):
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "C123": {
                    "project": "myproj",
                    "cwd": str(cwd_dir),
                    "persona": "helper",
                    "allowed_tools": ["Bash", "Read"],
                    "permission_mode": "plan",
                    "timeout": 120,
                }
            }
        },
    )
    channels = load_channels(config_path)
    assert set(channels.keys()) == {"C123"}
    cfg = channels["C123"]
    assert cfg.channel_id == "C123"
    assert cfg.project == "myproj"
    assert cfg.cwd == str(cwd_dir)
    assert cfg.persona == "helper"
    assert cfg.allowed_tools == ["Bash", "Read"]
    assert cfg.permission_mode == "plan"
    assert cfg.timeout == 120
    assert cfg.backend == "claude"
    assert cfg.t3_project_id is None


def test_load_channels_valid_t3_channel(tmp_path):
    cwd_dir = tmp_path / "t3project"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "C999": {
                    "project": "t3proj",
                    "cwd": str(cwd_dir),
                    "backend": "t3",
                    "t3_project_id": "proj-abc",
                }
            }
        },
    )
    channels = load_channels(config_path)
    cfg = channels["C999"]
    assert cfg.backend == "t3"
    assert cfg.t3_project_id == "proj-abc"


def test_load_channels_skips_comment_keys(tmp_path):
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "_comment": "this is just documentation and should be ignored",
                "C123": {"project": "myproj", "cwd": str(cwd_dir)},
            }
        },
    )
    channels = load_channels(config_path)
    assert set(channels.keys()) == {"C123"}


def test_load_channels_bad_cwd_raises(tmp_path):
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "C123": {"project": "myproj", "cwd": str(tmp_path / "does_not_exist")},
            }
        },
    )
    with pytest.raises(ValueError, match="cwd does not exist"):
        load_channels(config_path)


def test_load_channels_unknown_backend_raises(tmp_path):
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "C123": {
                    "project": "myproj",
                    "cwd": str(cwd_dir),
                    "backend": "not-a-real-backend",
                },
            }
        },
    )
    with pytest.raises(ValueError, match="unknown backend"):
        load_channels(config_path)


def test_load_channels_t3_without_project_id_raises(tmp_path):
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "C123": {"project": "myproj", "cwd": str(cwd_dir), "backend": "t3"},
            }
        },
    )
    with pytest.raises(ValueError, match="needs t3_project_id"):
        load_channels(config_path)


def test_load_channels_defaults(tmp_path):
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {"channels": {"C123": {"project": "myproj", "cwd": str(cwd_dir)}}},
    )
    cfg = load_channels(config_path)["C123"]
    assert cfg.permission_mode == "acceptEdits"
    assert cfg.timeout == 600
    assert cfg.t3_model == {"instanceId": "claudeAgent", "model": "claude-sonnet-5"}
    assert cfg.persona is None
    assert cfg.allowed_tools == []
    assert cfg.backend == "claude"
    assert cfg.require_mention is False
    assert cfg.approval_timeout == 3600


def test_load_channels_require_mention_true(tmp_path):
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "C123": {
                    "project": "myproj",
                    "cwd": str(cwd_dir),
                    "require_mention": True,
                }
            }
        },
    )
    cfg = load_channels(config_path)["C123"]
    assert cfg.require_mention is True


# ---------------------------------------------------------------------------
# load_senders
# ---------------------------------------------------------------------------


def test_load_senders_missing_file_returns_empty_and_none(tmp_path):
    senders, guest_defaults = load_senders(tmp_path / "no_such_file.json")
    assert senders == {}
    assert guest_defaults is None


def test_load_senders_owner_defaults(tmp_path):
    path = tmp_path / "senders.json"
    _write_json(path, {"senders": {"U_OWNER": {"role": "owner"}}})
    senders, _ = load_senders(path)
    sp = senders["U_OWNER"]
    assert sp.role == "owner"
    assert sp.runtime_mode == "full-access"
    assert sp.pps_mode == "skip"


def test_load_senders_guest_defaults(tmp_path):
    path = tmp_path / "senders.json"
    _write_json(path, {"senders": {"U_GUEST": {"role": "guest"}}})
    senders, _ = load_senders(path)
    sp = senders["U_GUEST"]
    assert sp.role == "guest"
    assert sp.runtime_mode == "approval-required"
    assert sp.pps_mode == "enforce"


def test_load_senders_explicit_overrides_win(tmp_path):
    path = tmp_path / "senders.json"
    _write_json(
        path,
        {
            "senders": {
                "U1": {
                    "role": "owner",
                    "runtime_mode": "approval-required",
                    "pps_mode": "log",
                    "name": "Custom Name",
                    "policy_extra": "extra text",
                }
            }
        },
    )
    senders, _ = load_senders(path)
    sp = senders["U1"]
    assert sp.role == "owner"
    assert sp.runtime_mode == "approval-required"
    assert sp.pps_mode == "log"
    assert sp.name == "Custom Name"
    assert sp.policy_extra == "extra text"


def test_load_senders_skips_underscore_prefixed_keys(tmp_path):
    path = tmp_path / "senders.json"
    _write_json(
        path,
        {
            "senders": {
                "_comment": {"role": "owner"},
                "U1": {"role": "guest"},
            }
        },
    )
    senders, _ = load_senders(path)
    assert set(senders.keys()) == {"U1"}


def test_load_senders_unknown_role_raises(tmp_path):
    path = tmp_path / "senders.json"
    _write_json(path, {"senders": {"U1": {"role": "supervillain"}}})
    with pytest.raises(ValueError, match="unknown role"):
        load_senders(path)


def test_load_senders_unknown_pps_mode_raises(tmp_path):
    path = tmp_path / "senders.json"
    _write_json(path, {"senders": {"U1": {"role": "owner", "pps_mode": "bogus"}}})
    with pytest.raises(ValueError, match="unknown pps_mode"):
        load_senders(path)


def test_load_senders_guest_defaults_parsed_from_file(tmp_path):
    path = tmp_path / "senders.json"
    _write_json(
        path,
        {
            "senders": {},
            "guest_defaults": {
                "runtime_mode": "read-only",
                "pps_mode": "log",
                "policy_extra": "be careful",
            },
        },
    )
    _, guest_defaults = load_senders(path)
    assert guest_defaults is not None
    assert guest_defaults.runtime_mode == "read-only"
    assert guest_defaults.pps_mode == "log"
    assert guest_defaults.policy_extra == "be careful"
    assert guest_defaults.role == "guest"


# ---------------------------------------------------------------------------
# Settings.sender_policy
# ---------------------------------------------------------------------------


def _make_settings(tmp_path, senders=None, guest_defaults=None):
    from slackcc.config import Settings

    cwd_dir = tmp_path / "proj"
    cwd_dir.mkdir(exist_ok=True)
    channels = {
        "C1": ChannelConfig(channel_id="C1", project="p", cwd=str(cwd_dir))
    }
    return Settings(
        bot_token="xoxb-test",
        app_token="xapp-test",
        config_path=tmp_path / "channels.json",
        sessions_path=tmp_path / "sessions.json",
        claude_bin="claude",
        channels=channels,
        senders=senders or {},
        guest_defaults=guest_defaults,
    )


def test_sender_policy_known_sender_returned_as_is(tmp_path):
    known = SenderPolicy(
        user_id="U1", name="Known Person", role="owner",
        runtime_mode="full-access", pps_mode="skip",
    )
    settings = _make_settings(tmp_path, senders={"U1": known})
    assert settings.sender_policy("U1") is known


def test_sender_policy_unknown_sender_with_guest_defaults(tmp_path):
    defaults = SenderPolicy(
        user_id="_default", name="_default", role="guest",
        runtime_mode="read-only", pps_mode="log", policy_extra="stay careful",
    )
    settings = _make_settings(tmp_path, guest_defaults=defaults)
    sp = settings.sender_policy("U_UNKNOWN")
    assert sp.role == "guest"
    assert sp.runtime_mode == "read-only"
    assert sp.pps_mode == "log"
    assert sp.policy_extra == "stay careful"
    assert sp.name == "U_UNKNOWN"
    assert sp.user_id == "U_UNKNOWN"


def test_sender_policy_unknown_sender_no_senders_file_backward_compat(tmp_path):
    settings = _make_settings(tmp_path, guest_defaults=None)
    sp = settings.sender_policy("U_UNKNOWN")
    assert sp.role == "owner"
    assert sp.runtime_mode == "full-access"
    assert sp.pps_mode == "skip"
    assert sp.name == "U_UNKNOWN"
    assert sp.user_id == "U_UNKNOWN"


# ---------------------------------------------------------------------------
# load_settings
# ---------------------------------------------------------------------------


def _base_env(monkeypatch, tmp_path, config_path, senders_path=None):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-abc")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
    monkeypatch.setenv("SLACKCC_CONFIG", str(config_path))
    monkeypatch.setenv("SLACKCC_SESSIONS", str(tmp_path / "sessions.json"))
    # Ensure these are unset so tests don't depend on the real deployment's
    # environment (e.g. this repo's daemon exports SLACKCC_T3_URL etc. on
    # the dev/prod box, which would otherwise leak into these tests).
    monkeypatch.delenv("SLACKCC_T3_URL", raising=False)
    monkeypatch.delenv("SLACKCC_PPS_URL", raising=False)
    monkeypatch.delenv("SLACKCC_T3_OWNER", raising=False)
    monkeypatch.delenv("SLACKCC_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("SLACKCC_T3_TOKEN", raising=False)
    if senders_path is not None:
        monkeypatch.setenv("SLACKCC_SENDERS", str(senders_path))
    else:
        # point at a file that doesn't exist so load_senders returns ({}, None)
        monkeypatch.setenv("SLACKCC_SENDERS", str(tmp_path / "no_senders.json"))


def test_load_settings_happy_path(tmp_path, monkeypatch):
    cwd_dir = tmp_path / "proj"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {"channels": {"C1": {"project": "p", "cwd": str(cwd_dir)}}},
    )
    _base_env(monkeypatch, tmp_path, config_path)

    settings = load_settings()
    assert settings.bot_token == "xoxb-abc"
    assert settings.app_token == "xapp-abc"
    assert "C1" in settings.channels
    assert settings.senders == {}
    assert settings.guest_defaults is None
    assert settings.t3_url == "http://127.0.0.1:3773"
    assert settings.pps_url == "http://127.0.0.1:8642"


def test_load_settings_missing_required_env_raises(tmp_path, monkeypatch):
    cwd_dir = tmp_path / "proj"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {"channels": {"C1": {"project": "p", "cwd": str(cwd_dir)}}},
    )
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("SLACKCC_CONFIG", str(config_path))
    monkeypatch.setenv("SLACKCC_SESSIONS", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("SLACKCC_SENDERS", str(tmp_path / "no_senders.json"))

    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        load_settings()


def test_load_settings_t3_channel_without_t3_token_raises(tmp_path, monkeypatch):
    cwd_dir = tmp_path / "proj"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "C1": {
                    "project": "p",
                    "cwd": str(cwd_dir),
                    "backend": "t3",
                    "t3_project_id": "abc",
                }
            }
        },
    )
    _base_env(monkeypatch, tmp_path, config_path)
    monkeypatch.delenv("SLACKCC_T3_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="SLACKCC_T3_TOKEN"):
        load_settings()


def test_load_settings_t3_channel_with_t3_token_succeeds(tmp_path, monkeypatch):
    cwd_dir = tmp_path / "proj"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(
        config_path,
        {
            "channels": {
                "C1": {
                    "project": "p",
                    "cwd": str(cwd_dir),
                    "backend": "t3",
                    "t3_project_id": "abc",
                }
            }
        },
    )
    _base_env(monkeypatch, tmp_path, config_path)
    monkeypatch.setenv("SLACKCC_T3_TOKEN", "t3-secret")

    settings = load_settings()
    assert settings.t3_token == "t3-secret"
    assert settings.has_t3_channels() is True


def test_load_channels_approval_timeout_override(tmp_path):
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    config_path = tmp_path / "channels.json"
    _write_json(config_path, {"channels": {"C123": {
        "project": "myproj", "cwd": str(cwd_dir), "approval_timeout": 120}}})
    assert load_channels(config_path)["C123"].approval_timeout == 120


def test_owner_ids_and_t3_thread_url(tmp_path):
    owner = SenderPolicy(user_id="U1", name="Dan", role="owner")
    guest = SenderPolicy(user_id="U2", name="Guest", role="guest")
    settings = _make_settings(tmp_path, senders={"U1": owner, "U2": guest})
    assert settings.owner_ids() == ["U1"]
    # No GUI base configured -> no link (loopback t3_url is useless in a DM).
    assert settings.t3_thread_url("slack-C1-1-2") is None
    from dataclasses import replace
    with_gui = replace(settings, t3_gui_url="https://host:7443/")
    assert with_gui.t3_thread_url("slack-C1-1-2") == "https://host:7443/primary/slack-C1-1-2"
