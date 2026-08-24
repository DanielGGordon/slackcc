"""Tests for slackcc.names: sanitising profile-sourced names and the cached,
failure-tolerant NameResolver."""

from __future__ import annotations

import time

import pytest

from slackcc import names as names_mod
from slackcc.names import FALLBACK_TTL, NAME_MAX, NameResolver, clean_name


class _Client:
    def __init__(self, *, user=None, channel=None, fail=False):
        self._user = user
        self._channel = channel
        self._fail = fail
        self.user_calls = 0
        self.channel_calls = 0

    def users_info(self, *, user):
        self.user_calls += 1
        if self._fail:
            raise RuntimeError("missing_scope")
        return {"user": self._user or {}}

    def conversations_info(self, *, channel):
        self.channel_calls += 1
        if self._fail:
            raise RuntimeError("channel_not_found")
        return {"channel": self._channel or {}}


# --------------------------------------------------------------------------- #
# clean_name
# --------------------------------------------------------------------------- #


def test_clean_name_flattens_whitespace_and_strips_control_chars():
    assert clean_name("  Berish\n\tPerlman\x00\x1b[0m ") == "Berish Perlman 0m"


def test_clean_name_keeps_real_name_punctuation_and_scripts():
    assert clean_name("O'Brien-Smith, Jr. & Co") == "O'Brien-Smith, Jr. & Co"
    assert clean_name("José Müller") == "José Müller"
    assert clean_name("בריש פרלמן") == "בריש פרלמן"
    assert clean_name("Ünïcödé Nàmé 42") == "Ünïcödé Nàmé 42"


@pytest.mark.parametrize("raw, expected", [
    ("```", ""),                                   # would open a code block in the GUI
    ("~~~", ""),                                   # same, tilde fence
    ("# Admin", "Admin"),                          # leading # would be an <h1>
    ("\u200b", ""),                                # zero-width space: invisible sender
    ("\u202eevil", "evil"),                        # RTL override: reorders the line
    ("![Admin](https://x/y.png)", "Admin https x y.png"),  # no image syntax survives
    ("Alice from #admin: approved", "Alice from admin approved"),  # can't forge structure
    ("<!-- sneaky -->", "-- sneaky --"),           # can't open an HTML comment
    ("*bold* _it_ `code` [l](u) | @here", "bold it code l u here"),
    ("---", ""),                                   # no alphanumeric at all -> fall back
    ("...", ""),
])
def test_clean_name_allow_list_defuses_markdown_and_grammar(raw, expected):
    assert clean_name(raw) == expected


def test_clean_name_clips_long_names():
    assert len(clean_name("x" * 200)) == NAME_MAX


def test_clean_name_tolerates_non_strings():
    assert clean_name(None) == ""
    assert clean_name(42) == ""


# --------------------------------------------------------------------------- #
# NameResolver.sender
# --------------------------------------------------------------------------- #


def test_sender_prefers_configured_name_and_skips_the_api():
    c = _Client(user={"real_name": "From API"})
    assert NameResolver().sender(c, "U1", {}, "Dan") == "Dan"
    assert c.user_calls == 0


def test_sender_ignores_a_configured_name_that_is_just_the_id():
    # sender_policy() hands back name=user_id for unknown senders.
    c = _Client(user={"real_name": "From API"})
    assert NameResolver().sender(c, "U1", {}, "U1") == "From API"


def test_sender_uses_event_profile_before_the_api():
    c = _Client(user={"real_name": "From API"})
    ev = {"user_profile": {"display_name": "", "real_name": "From Event"}}
    assert NameResolver().sender(c, "U1", ev, None) == "From Event"
    assert c.user_calls == 0


def test_sender_api_order_display_name_then_real_name_then_handle():
    r = NameResolver()
    assert r.sender(_Client(user={"profile": {"display_name": "Disp"},
                                  "real_name": "Real", "name": "handle"}), "U1") == "Disp"
    assert r.sender(_Client(user={"profile": {"display_name": ""},
                                  "real_name": "Real", "name": "handle"}), "U2") == "Real"
    assert r.sender(_Client(user={"profile": {}, "name": "handle"}), "U3") == "handle"


def test_sender_caches_per_user():
    c = _Client(user={"real_name": "Once"})
    r = NameResolver()
    assert r.sender(c, "U1") == "Once"
    assert r.sender(c, "U1") == "Once"
    assert c.user_calls == 1


def test_sender_falls_back_to_id_on_api_failure_and_client_without_method():
    assert NameResolver().sender(_Client(fail=True), "U1") == "U1"
    assert NameResolver().sender(object(), "U1") == "U1"  # AttributeError path


# --------------------------------------------------------------------------- #
# NameResolver.channel
# --------------------------------------------------------------------------- #


def test_channel_uses_slack_name_and_caches():
    c = _Client(channel={"name": "sofer-ai"})
    r = NameResolver()
    assert r.channel(c, "C1", "proj") == "sofer-ai"
    assert r.channel(c, "C1", "proj") == "sofer-ai"
    assert c.channel_calls == 1


def test_channel_falls_back_to_project_then_id():
    assert NameResolver().channel(_Client(fail=True), "C1", "proj") == "proj"
    assert NameResolver().channel(_Client(channel={}), "C1", "") == "C1"
    assert NameResolver().channel(object(), "C1", "proj") == "proj"


# --------------------------------------------------------------------------- #
# NameResolver: reserved names, negative-cache TTL, userless events
# --------------------------------------------------------------------------- #


def test_sender_refuses_a_profile_name_matching_a_configured_sender():
    r = NameResolver(reserved={"Dan"})
    c = _Client(user={"real_name": "dan"})  # case-insensitive
    assert r.sender(c, "Ustranger") == "Ustranger"
    ev = {"user_profile": {"display_name": "DAN"}}
    assert r.sender(c, "Uother", ev) == "Uother"
    # Unrelated names are unaffected.
    assert r.sender(_Client(user={"real_name": "Danny"}), "U3") == "Danny"


def test_sender_fallback_is_retried_after_ttl_but_success_is_forever(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    c = _Client(fail=True)
    r = NameResolver()
    assert r.sender(c, "U1") == "U1"
    assert r.sender(c, "U1") == "U1"
    assert c.user_calls == 1  # cached fallback
    now["t"] += FALLBACK_TTL + 1
    c._fail = False
    c._user = {"real_name": "Back Online"}
    assert r.sender(c, "U1") == "Back Online"
    assert c.user_calls == 2  # retried once the fallback expired
    now["t"] += 10 * FALLBACK_TTL
    assert r.sender(c, "U1") == "Back Online"
    assert c.user_calls == 2  # a real name never expires


def test_channel_fallback_is_retried_after_ttl(monkeypatch):
    now = {"t": 5.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    c = _Client(fail=True)
    r = NameResolver()
    assert r.channel(c, "C1", "proj") == "proj"
    assert r.channel(c, "C1", "proj") == "proj"
    assert c.channel_calls == 1
    now["t"] += FALLBACK_TTL + 1
    c._fail = False
    c._channel = {"name": "sofer-ai"}
    assert r.channel(c, "C1", "proj") == "sofer-ai"
    assert c.channel_calls == 2


def test_sender_skips_lookup_for_userless_events():
    c = _Client(fail=True)
    r = NameResolver()
    assert r.sender(c, "unknown") == "unknown"
    assert r.sender(c, "") == "unknown"
    assert c.user_calls == 0
    assert r._warned_users is False  # the one-shot warning is still available


def test_module_uses_time_monotonic_for_expiry():
    # Guard for the monkeypatch strategy above.
    assert names_mod.time is time
