"""Client for the Prompt Protection Service (pps) — the local sandboxed LLM
judge at ~/projects/pps. Blocking call before each guest turn; the caller
decides fail-open vs fail-closed on {"verdict": "error"}."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class PPSClient:
    def __init__(self, url: str, timeout: float = 35.0):
        self._url = url.rstrip("/")
        self._timeout = timeout

    def judge(self, *, sender: str, policy: str, text: str, context: str) -> dict:
        req = urllib.request.Request(
            f"{self._url}/v1/judge",
            data=json.dumps({"sender": sender, "policy": policy,
                             "text": text, "context": context}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                verdict = json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            log.warning("pps unreachable: %s", exc)
            return {"verdict": "error", "category": "other",
                    "reason": f"pps unreachable: {exc}", "stage": "none", "latency_ms": 0}
        if verdict.get("verdict") not in ("allow", "deny", "error"):
            return {"verdict": "error", "category": "other",
                    "reason": "malformed pps response", "stage": "none", "latency_ms": 0}
        return verdict
