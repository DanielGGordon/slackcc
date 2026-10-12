"""Outbound scrubbing: nothing that looks like a secret leaves for Slack.

Prompt injection usually cashes out as exfiltration through the reply, so
every message slackcc posts (turn replies AND mirror posts) passes through
`scrub` first. Two layers: exact-match redaction of the daemon's own known
secrets (strongest — zero false negatives for the credentials we hold), then
pattern matching for common token shapes.

The scanning itself lives in `pps.secrets`, the one secret scanner shared with
the software factory's gate (README of pps: "Secret scanner (shared library)").
This module only decides *which* secrets are ours and keeps `scrub`'s signature.
"""

from __future__ import annotations

import logging
import os

from pps.secrets import redact

log = logging.getLogger(__name__)

# Env vars whose live values must never appear in an outbound message.
_OWN_SECRET_VARS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACKCC_T3_TOKEN")


def scrub(text: str) -> tuple[str, list[str]]:
    """Returns (clean_text, list of redaction labels applied)."""
    known = {var: val for var in _OWN_SECRET_VARS if (val := os.environ.get(var))}
    text, findings = redact(text, known)
    if findings:
        log.warning("outbound scrub redacted: %s", ", ".join(findings))
    return text, findings
