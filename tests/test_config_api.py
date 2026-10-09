"""Tests for slackcc.config_api: GET/PUT /config, SIGHUP-style reload, and the
loopback HTTP server. Offline: real files under tmp_path, a real HTTP server
on an ephemeral loopback port, no Slack/T3/pps."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from slackcc.config import LiveSettings, Settings, load_channels, load_senders
from slackcc.config_api import ApiError, ConfigService, etag_of, serialize, serve, start_from_env

TOKEN = "t" * 32


@pytest.fixture
def world(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    channels_path = tmp_path / "config" / "channels.json"
    senders_path = tmp_path / "config" / "senders.json"
    channels_path.parent.mkdir()
    channels = {
        "_comment": "kept verbatim",
        "channels": {
            "C1": {"project": "one", "cwd": str(proj), "backend": "t3", "t3_project_id": "p1"},
            "_muted_C2": {"project": "two", "cwd": str(proj)},
        },
    }
    senders = {
        "senders": {"UOWNER1": {"name": "Dan", "role": "owner"},
                    "UGUEST1": {"name": "Igor", "role": "guest"}},
        "guest_defaults": {"runtime_mode": "approval-required", "pps_mode": "enforce"},
    }
    channels_path.write_text(serialize(channels))
    senders_path.write_text(serialize(senders))
    os.chmod(senders_path, 0o640)
    ch = load_channels(channels_path)
    sd, gd = load_senders(senders_path)
    settings = Settings(
        bot_token="b", app_token="a", config_path=channels_path,
        sessions_path=tmp_path / "state" / "sessions.json", claude_bin="claude",
        channels=ch, t3_token="t3", senders=sd, guest_defaults=gd, senders_path=senders_path,
    )
    live = LiveSettings(settings)
    service = ConfigService(live, backup_dir=tmp_path / "state" / "config-backups")
    service.mark_loaded_from_disk()

    class W:
        pass

    w = W()
    w.tmp, w.proj, w.live, w.service = tmp_path, proj, live, service
    w.channels_path, w.senders_path = channels_path, senders_path
    w.channels, w.senders = channels, senders
    return w


# --------------------------------------------------------------------------- #
# GET
# --------------------------------------------------------------------------- #


def test_get_returns_files_effective_and_etag(world):
    out = world.service.get()
    assert out["ok"] is True and out["api_version"] == 1
    assert out["channels"] == world.channels
    assert out["senders"] == world.senders
    assert out["senders_file"] is True
    assert out["problems"] == []
    assert out["etag"] == etag_of(world.channels_path.read_text(), world.senders_path.read_text())
    # Effective = defaults applied, muted channels skipped, as the daemon runs.
    assert list(out["effective"]["channels"]) == ["C1"]
    c1 = out["effective"]["channels"]["C1"]
    assert c1["timeout"] == 600 and c1["require_mention"] is False
    assert c1["audience"] == "technical" and c1["description"] == ""
    assert c1["t3_model"] == {"instanceId": "claudeAgent", "model": "claude-sonnet-5"}
    assert out["effective"]["senders"]["UOWNER1"]["runtime_mode"] == "full-access"
    assert out["effective"]["senders"]["UOWNER1"]["pps_mode"] == "skip"
    assert out["effective"]["senders"]["UGUEST1"]["pps_mode"] == "enforce"
    assert out["loaded"]["source"] == "start"
    assert out["loaded_current"] is True


def test_get_flags_a_hand_edit_nobody_reloaded(world):
    world.channels_path.write_text(world.channels_path.read_text().replace('"one"', '"uno"'))
    out = world.service.get()
    assert out["loaded_current"] is False
    assert out["channels"]["channels"]["C1"]["project"] == "uno"
    assert out["effective"]["channels"]["C1"]["project"] == "one"  # still running the old one


def test_get_reports_unparseable_files(world):
    world.senders_path.write_text("{ nope")
    out = world.service.get()
    assert out["senders"] is None
    assert any(p.startswith("senders.json: not valid JSON") for p in out["problems"])


# --------------------------------------------------------------------------- #
# PUT
# --------------------------------------------------------------------------- #


def _etag(world):
    return world.service.get()["etag"]


def test_put_writes_backs_up_and_swaps_without_restart(world):
    seen = []
    world.live.subscribe(lambda s: seen.append(sorted(s.senders)))
    before_senders = world.senders_path.read_text()
    senders = json.loads(before_senders)
    senders["senders"]["UNEW1"] = {"name": "New", "role": "guest", "pps_mode": "enforce"}
    out = world.service.put({"if_match": _etag(world), "senders": senders})

    assert out["changed"] is True and out["loaded_current"] is True
    assert out["loaded"]["source"] == "api"
    assert world.senders_path.read_text() == serialize(senders)
    assert (world.senders_path.stat().st_mode & 0o777) == 0o640   # mode kept
    assert "UNEW1" in world.live.current.senders                    # live, no restart
    assert world.live.current.sender_policy("UNEW1").name == "New"
    assert seen == [["UGUEST1", "UNEW1", "UOWNER1"]]
    # channels.json untouched, byte for byte
    assert world.channels_path.read_text() == serialize(world.channels)
    backups = list((world.tmp / "state" / "config-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "senders.json").read_text() == before_senders
    assert not list(world.channels_path.parent.glob(".*slackcc-tmp*"))


def test_put_keeps_comments_and_muted_keys(world):
    channels = json.loads(world.channels_path.read_text())
    channels["channels"]["C1"]["require_mention"] = True
    world.service.put({"if_match": _etag(world), "channels": channels})
    text = world.channels_path.read_text()
    assert '"_comment": "kept verbatim"' in text and "_muted_C2" in text
    assert world.live.current.channel("C1").require_mention is True


def test_put_audience_goes_live_without_a_restart(world):
    channels = json.loads(world.channels_path.read_text())
    channels["channels"]["C1"]["audience"] = "customer"
    out = world.service.put({"if_match": _etag(world), "channels": channels})
    assert world.live.current.channel("C1").audience == "customer"
    assert out["effective"]["channels"]["C1"]["audience"] == "customer"


def test_put_dry_run_returns_the_candidate_and_writes_nothing(world):
    before = (world.channels_path.read_text(), world.senders_path.read_text())
    senders = json.loads(before[1])
    senders["guest_defaults"] = {}
    out = world.service.put({"if_match": _etag(world), "senders": senders, "dry_run": True})
    assert out == {"ok": True, "dry_run": True, "effective": out["effective"]}
    assert out["effective"]["guest_defaults"]["runtime_mode"] == "approval-required"
    assert (world.channels_path.read_text(), world.senders_path.read_text()) == before
    assert world.live.loaded["source"] == "start"


def test_put_stale_etag_is_refused(world):
    with pytest.raises(ApiError) as e:
        world.service.put({"if_match": "0" * 16, "channels": world.channels})
    assert e.value.status == 409 and e.value.body["error"] == "stale"
    assert e.value.body["etag"] == _etag(world)


@pytest.mark.parametrize("mutate,needle", [
    (lambda c, s: s["senders"]["UGUEST1"].update(role="admin"), "unknown role"),
    (lambda c, s: s["senders"]["UGUEST1"].update(pps_mode="off"), "unknown pps_mode"),
    (lambda c, s: c["channels"]["C1"].update(cwd="/definitely/not/here"), "cwd does not exist"),
    (lambda c, s: c["channels"]["C1"].update(timeout="soon"), "ValueError"),
    (lambda c, s: c["channels"]["C1"].pop("project"), "needs project and cwd"),
    # A guest_defaults typo would fail OPEN (enforcement is pps_mode == "enforce").
    (lambda c, s: s["guest_defaults"].update(pps_mode="enfroce"), "guest_defaults: unknown pps_mode"),
    (lambda c, s: s["guest_defaults"].update(role="owner"), "guest_defaults: role must be guest"),
    # Values the old parser let through that break a turn or the next start.
    (lambda c, s: s["senders"]["UGUEST1"].update(name=[]), "name must be a non-empty string"),
    (lambda c, s: s["senders"]["UGUEST1"].update(policy_extra=7), "policy_extra"),
    (lambda c, s: s["senders"].update(UNEW1="guest"), "sender UNEW1: must be a JSON object"),
    (lambda c, s: c["channels"]["C1"].update(t3_model=[]), "t3_model must be an object"),
    (lambda c, s: c["channels"]["C1"].update(t3_model={"instanceId": "claudeAgent"}), "t3_model"),
    (lambda c, s: c["channels"]["C1"].update(timeout=0), "positive whole number"),
    (lambda c, s: c["channels"]["C1"].update(approval_timeout=-5), "positive whole number"),
    (lambda c, s: c["channels"]["C1"].update(timeout=True), "whole number of seconds"),
    (lambda c, s: c["channels"]["C1"].update(persona=5), "persona"),
    (lambda c, s: c["channels"]["C1"].update(allowed_tools="Read"), "allowed_tools"),
    (lambda c, s: c["channels"]["C1"].update(project=None), "project must be a non-empty string"),
    (lambda c, s: c.update(channels=[]), "`channels` must be a JSON object"),
    (lambda c, s: c["channels"]["C1"].update(audience="public"), "unknown audience"),
    (lambda c, s: c["channels"]["C1"].update(audience=3), "audience must be a non-empty string"),
])
def test_put_invalid_is_refused_and_nothing_changes(world, mutate, needle):
    channels, senders = json.loads(world.channels_path.read_text()), json.loads(world.senders_path.read_text())
    before = (world.channels_path.read_text(), world.senders_path.read_text())
    running = world.live.current
    mutate(channels, senders)
    with pytest.raises(ApiError) as e:
        world.service.put({"if_match": _etag(world), "channels": channels, "senders": senders})
    assert e.value.status == 422 and e.value.body["error"] == "invalid"
    assert needle in e.value.body["message"]
    assert (world.channels_path.read_text(), world.senders_path.read_text()) == before
    assert world.live.current is running


def test_put_first_t3_channel_needs_a_restart(world):
    world.live.t3_ready = False
    channels = json.loads(world.channels_path.read_text())
    with pytest.raises(ApiError) as e:
        world.service.put({"if_match": _etag(world), "channels": channels})
    assert e.value.body["error"] == "restart_required"


@pytest.mark.parametrize("body,error", [
    ([], "bad_request"),
    ({"channels": {}}, "if_match_required"),
    ({"if_match": "x"}, "bad_request"),
    ({"if_match": "x", "channels": []}, "bad_request"),
    ({"if_match": "x", "channels": {}, "cwd": "/"}, "bad_request"),
    ({"if_match": "x", "channels": {}, "dry_run": "yes"}, "bad_request"),
])
def test_put_envelope_errors(world, body, error):
    with pytest.raises(ApiError) as e:
        world.service.put(body)
    assert e.value.status == 400 and e.value.body["error"] == error


def test_put_identical_content_does_not_write_but_reloads_a_hand_edit(world):
    # Someone edited by hand; an API write of the same content loads it.
    channels = json.loads(world.channels_path.read_text())
    channels["channels"]["C1"]["project"] = "uno"
    world.channels_path.write_text(serialize(channels))
    mtime = world.channels_path.stat().st_mtime_ns
    out = world.service.put({"if_match": _etag(world), "channels": channels})
    assert out["changed"] is False and out["loaded_current"] is True
    assert world.channels_path.stat().st_mtime_ns == mtime
    assert world.live.current.channel("C1").project == "uno"


def test_failed_second_write_puts_the_first_back(world, monkeypatch):
    from slackcc import config_api
    real = config_api._atomic_write
    calls = []

    def flaky(path, text):
        calls.append(path.name)
        if path.name == "channels.json" and len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(path, text)

    monkeypatch.setattr(config_api, "_atomic_write", flaky)
    channels, senders = json.loads(world.channels_path.read_text()), json.loads(world.senders_path.read_text())
    before = (world.channels_path.read_text(), world.senders_path.read_text())
    senders["senders"]["UGUEST1"]["name"] = "Igor B"
    channels["channels"]["C1"]["timeout"] = 900
    # backups go through _atomic_write too: stop counting them
    monkeypatch.setattr(config_api.ConfigService, "_backup", lambda self, disk: None)
    with pytest.raises(ApiError) as e:
        world.service.put({"if_match": _etag(world), "channels": channels, "senders": senders})
    assert e.value.status == 500 and e.value.body["error"] == "write_failed"
    assert (world.channels_path.read_text(), world.senders_path.read_text()) == before
    assert world.live.current.sender_policy("UGUEST1").name == "Igor"


def test_backups_are_pruned(world, monkeypatch):
    from slackcc import config_api
    monkeypatch.setattr(config_api, "BACKUPS_KEPT", 3)
    for i in range(5):
        senders = json.loads(world.senders_path.read_text())
        senders["senders"]["UGUEST1"]["name"] = f"Igor {i}"
        world.service.put({"if_match": _etag(world), "senders": senders})
    assert len(list((world.tmp / "state" / "config-backups").iterdir())) == 3


# --------------------------------------------------------------------------- #
# reload (SIGHUP)
# --------------------------------------------------------------------------- #


def test_reload_picks_up_a_hand_edit(world):
    senders = json.loads(world.senders_path.read_text())
    senders["senders"]["UGUEST1"]["role"] = "owner"
    world.senders_path.write_text(serialize(senders))
    ok, _ = world.service.reload()
    assert ok is True
    assert world.live.current.sender_policy("UGUEST1").role == "owner"
    assert world.live.loaded["source"] == "sighup"
    assert world.service.get()["loaded_current"] is True


def test_reload_refuses_an_invalid_guest_default(world):
    running = world.live.current
    senders = json.loads(world.senders_path.read_text())
    senders["guest_defaults"]["pps_mode"] = "enfroce"
    world.senders_path.write_text(serialize(senders))
    ok, message = world.service.reload()
    assert ok is False and "guest_defaults: unknown pps_mode" in message
    assert world.live.current is running
    assert world.live.current.sender_policy("USTRANGER").pps_mode == "enforce"


def test_reload_never_turns_protection_off_when_senders_json_vanishes(world):
    running = world.live.current
    world.senders_path.unlink()
    ok, message = world.service.reload()
    assert ok is False and "senders.json is missing" in message
    assert world.live.current is running
    stranger = world.live.current.sender_policy("USTRANGER")
    assert (stranger.role, stranger.pps_mode) == ("guest", "enforce")


def test_channels_only_put_does_not_adopt_a_vanished_senders_json(world):
    running = world.live.current
    world.senders_path.unlink()
    channels = json.loads(world.channels_path.read_text())
    channels["channels"]["C1"]["timeout"] = 900
    before = world.channels_path.read_text()
    with pytest.raises(ApiError) as e:
        world.service.put({"if_match": _etag(world), "channels": channels})
    assert e.value.status == 422 and e.value.body["error"] == "senders_missing"
    assert world.channels_path.read_text() == before
    assert world.live.current is running
    # Sending a senders policy alongside is fine: protection stays on.
    out = world.service.put({"if_match": _etag(world), "channels": channels,
                             "senders": world.senders})
    assert out["changed"] is True and world.senders_path.exists()
    assert world.live.current.sender_policy("USTRANGER").role == "guest"


def test_get_waits_for_an_in_progress_write(world, monkeypatch):
    """GET shares the write lock: it never reports new senders.json bytes
    with old channels.json bytes from halfway through a two-file write."""
    import threading
    from slackcc import config_api
    real = config_api._atomic_write
    in_write, release = threading.Event(), threading.Event()

    def slow(path, text):
        real(path, text)
        if path == world.senders_path:
            in_write.set()
            release.wait(5)

    monkeypatch.setattr(config_api, "_atomic_write", slow)
    channels, senders = json.loads(world.channels_path.read_text()), json.loads(world.senders_path.read_text())
    senders["senders"]["UGUEST1"]["name"] = "Igor B"
    channels["channels"]["C1"]["timeout"] = 900
    etag = _etag(world)
    writer = threading.Thread(target=world.service.put,
                              args=({"if_match": etag, "channels": channels, "senders": senders},))
    writer.start()
    assert in_write.wait(5)
    got: list[dict] = []
    reader = threading.Thread(target=lambda: got.append(world.service.get()))
    reader.start()
    reader.join(timeout=0.2)
    assert reader.is_alive()  # blocked behind the write, not reading half of it
    release.set()
    writer.join(5)
    reader.join(5)
    assert got[0]["channels"]["channels"]["C1"]["timeout"] == 900
    assert got[0]["senders"]["senders"]["UGUEST1"]["name"] == "Igor B"
    assert got[0]["loaded_current"] is True


def test_reload_keeps_the_running_config_when_the_files_do_not_load(world):
    running = world.live.current
    world.senders_path.write_text("{ broken")
    ok, message = world.service.reload()
    assert ok is False and "senders.json" in message
    assert world.live.current is running


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture
def http(world):
    server = serve(world.service, TOKEN, port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, *, token=TOKEN, ctype="application/json"):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call
    server.shutdown()
    server.server_close()


def test_http_requires_the_token(http):
    assert http("GET", "/config", token=None)[0] == 401
    assert http("GET", "/config", token="wrong" * 8)[0] == 401
    assert http("GET", "/healthz", token=None) == (200, {"ok": True})


def test_http_get_and_put_round_trip(http, world):
    status, got = http("GET", "/config")
    assert status == 200 and got["channels"] == world.channels
    senders = got["senders"]
    senders["senders"]["UGUEST1"]["policy_extra"] = "May ask about staging."
    status, put = http("PUT", "/config", {"if_match": got["etag"], "senders": senders})
    assert status == 200 and put["changed"] is True
    assert world.live.current.sender_policy("UGUEST1").policy_extra == "May ask about staging."
    status, again = http("PUT", "/config", {"if_match": got["etag"], "senders": senders})
    assert status == 409 and again["error"] == "stale"


def test_http_refusals(http):
    assert http("PUT", "/config", {"if_match": "x", "channels": {}}, ctype="text/plain")[0] == 415
    assert http("POST", "/config", {})[0] == 405
    assert http("GET", "/nope")[0] == 404


def test_serve_refuses_a_non_loopback_bind(world):
    with pytest.raises(ValueError):
        serve(world.service, TOKEN, host="0.0.0.0", port=0)


def test_start_from_env_without_a_token_serves_nothing(world, monkeypatch):
    monkeypatch.delenv("SLACKCC_CONFIG_TOKEN", raising=False)
    started = []
    monkeypatch.setattr("slackcc.config_api.serve", lambda *a, **k: started.append(1))
    monkeypatch.setattr("slackcc.config_api.install_sighup", lambda service: None)
    service = start_from_env(world.live)
    assert started == [] and service.get()["loaded_current"] is True
    monkeypatch.setenv("SLACKCC_CONFIG_TOKEN", "short")
    start_from_env(world.live)
    assert started == []
    monkeypatch.setenv("SLACKCC_CONFIG_TOKEN", TOKEN)
    start_from_env(world.live)
    assert started == [1]
