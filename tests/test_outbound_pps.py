"""outbound.scrub is a thin wrapper over pps.secrets (the shared scanner).

The scanner's own behaviour is tested in pps; these cover the wiring: which
secrets slackcc passes as `known`, that shapes only the shared scanner knows
are caught here, and that the log never carries a value. The pre-existing
scrub() contract is pinned by test_sanitize_outbound.py.
"""

from __future__ import annotations

import base64
import logging

import pps.secrets
from slackcc import outbound
from slackcc.outbound import scrub

OWN = "xoxb-own-wiring-test-unique-marker-424242"


def _only_bot_token(monkeypatch, value=OWN):
    for var in outbound._OWN_SECRET_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", value)


def test_scrub_uses_the_shared_scanner(monkeypatch):
    calls = []
    real = pps.secrets.redact

    def spy(text, known=None, **kw):
        calls.append(dict(known or {}))
        return real(text, known, **kw)

    _only_bot_token(monkeypatch)
    monkeypatch.setattr(outbound, "redact", spy)
    scrub("hello")
    assert calls == [{"SLACK_BOT_TOKEN": OWN}]


def test_only_set_env_secrets_are_passed_as_known(monkeypatch):
    for var in outbound._OWN_SECRET_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SLACKCC_T3_TOKEN", "t3-token-wiring-test-1234")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    seen = {}
    monkeypatch.setattr(outbound, "redact", lambda t, known=None, **kw: (seen.update(known) or (t, [])))
    scrub("x")
    assert seen == {"SLACKCC_T3_TOKEN": "t3-token-wiring-test-1234"}


def test_own_secret_is_caught_in_encoded_forms(monkeypatch):
    _only_bot_token(monkeypatch)
    for enc in (OWN.encode().hex(), base64.b64encode(OWN.encode()).decode()):
        clean, findings = scrub(f"debug dump: {enc} end")
        assert enc not in clean
        assert "[redacted:SLACK_BOT_TOKEN]" in clean
        assert findings == ["own-secret:SLACK_BOT_TOKEN"]


def test_shapes_added_by_the_shared_scanner_are_caught(monkeypatch):
    for var in outbound._OWN_SECRET_VARS:
        monkeypatch.delenv(var, raising=False)
    cases = {
        "clerk-or-stripe-secret-key": "sk_" + "live_" + "A1b2C3d4E5f6G7h8I9j0K1",
        "github-pat": "github" + "_pat_" + "A1b2C3d4E5f6G7h8I9j0K1",
        "slack-webhook": "hooks.slack" + ".com/services/T0123ABCD/B0123ABCD/abcDEF123456xyz",
        "google-api-key": "AI" + "zaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        "aws-key-id": "ASIA" + "IOSFODNN7EXAMPLE",
    }
    for label, token in cases.items():
        clean, findings = scrub(f"leaked {token} oops")
        assert token not in clean
        assert findings == [label]


def test_log_names_labels_but_never_the_value(monkeypatch, caplog):
    _only_bot_token(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        scrub(f"token {OWN} and key AKIA" + "IOSFODNN7EXAMPLE")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "own-secret:SLACK_BOT_TOKEN" in logged
    assert OWN not in logged
    assert "IOSFODNN7EXAMPLE" not in logged


def test_clean_text_does_not_log(monkeypatch, caplog):
    _only_bot_token(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        assert scrub("all clear") == ("all clear", [])
    assert not [r for r in caplog.records if r.name.startswith(("slackcc.outbound", "pps"))]
