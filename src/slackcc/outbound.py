"""Outbound scrubbing: nothing that looks like a secret leaves for Slack.

Prompt injection usually cashes out as exfiltration through the reply, so
every message slackcc posts (turn replies AND mirror posts) passes through
`scrub` first. Two layers: exact-match redaction of the daemon's own known
secrets (strongest — zero false negatives for the credentials we hold), then
pattern matching for common token shapes.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack-app-token", re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b")),
    ("aws-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b")),
    # anthropic before openai: the generic sk- pattern would otherwise consume
    # sk-ant- keys first and mislabel the finding.
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("keyish-assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
        r"client[_-]?secret|password)\b(\s*[=:]\s*)(['\"]?)([A-Za-z0-9_\-/+.]{16,})")),
]

# Env vars whose live values must never appear in an outbound message.
_OWN_SECRET_VARS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACKCC_T3_TOKEN")


def scrub(text: str) -> tuple[str, list[str]]:
    """Returns (clean_text, list of redaction labels applied)."""
    findings: list[str] = []
    for var in _OWN_SECRET_VARS:
        val = os.environ.get(var)
        if val and val in text:
            text = text.replace(val, f"[redacted:{var}]")
            findings.append(f"own-secret:{var}")
    for label, pat in _PATTERNS:
        if label == "keyish-assignment":
            def _sub(m: re.Match) -> str:
                findings.append(label)
                return f"{m.group(1)}{m.group(2)}{m.group(3)}[redacted]"
            text = pat.sub(_sub, text)
        elif pat.search(text):
            text = pat.sub(f"[redacted:{label}]", text)
            findings.append(label)
    if findings:
        log.warning("outbound scrub redacted: %s", ", ".join(findings))
    return text, findings
