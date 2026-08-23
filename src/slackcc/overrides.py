"""Owner override for pps denials: when the safety screen declines a guest's
message, the bot asks the owners (in-thread) whether to permit it anyway. This
module holds the pending-question store, the record of what was granted, and
the yes/no parser for the owner's answer.

Persistence is a JSON file in `.state/` (same pattern as ClaimStore): the
question may sit unanswered across a daemon restart, and re-reading on every
call keeps the file the single source of truth rather than an in-memory cache."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

# An unanswered question older than this is treated as gone: a stale "yes"
# days later shouldn't replay a request nobody remembers.
_OVERRIDE_TTL_SECS = 24 * 3600

_YES = {
    "y", "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "approve", "approved",
    "allow", "allowed", "permit", "permitted", "go ahead",
}
_NO = {
    "n", "no", "nope", "nah", "deny", "denied", "decline", "declined", "reject",
    "rejected", "not allowed",
}

# Leading Slack mention(s) of any user, e.g. "<@UBOT> yes" -> "yes".
_LEADING_MENTION = re.compile(r"^(?:\s*<@[A-Z0-9]+(?:\|[^>]*)?>\s*)+")


def parse_yes_no(text: str) -> bool | None:
    """Whole-message yes/no classification of an owner's reply.

    Deliberately strict: only a bare answer (optionally @-mentioning the bot,
    trailing `.`/`!`) counts. "yes please do it" is None so an owner chatting
    in the thread is never mistaken for an approval."""
    t = _LEADING_MENTION.sub("", text or "")
    t = t.strip().rstrip(".!").strip().lower()
    t = " ".join(t.split())  # collapse internal whitespace ("go  ahead")
    if t in _YES:
        return True
    if t in _NO:
        return False
    return None


class OverrideStore:
    """`pending`: at most one unanswered owner question per thread (a newer
    denial replaces the older one). `grants`: every approval ever given in a
    thread, fed back to the judge as context for later messages."""

    def __init__(self, path: Path, now: Callable[[], float] | None = None):
        self._path = path
        self._lock = threading.Lock()
        # Injectable clock for TTL tests; resolved lazily so a monkeypatched
        # time.time() is also honoured.
        self._now = now or (lambda: time.time())

    @staticmethod
    def key(channel: str, thread_ts: str) -> str:
        return f"{channel}:{thread_ts}"

    def _read(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        # Shape-validate: a hand-edited or partially written file must not
        # crash the daemon; a bad section is just treated as empty.
        for section in ("pending", "grants"):
            if not isinstance(data.get(section), dict):
                data[section] = {}
        return data

    def _expired(self, entry: Any) -> bool:
        if not isinstance(entry, dict):
            return True
        asked_at = entry.get("asked_at")
        if not isinstance(asked_at, (int, float)):
            return False  # legacy entry without a timestamp: keep it
        return self._now() - asked_at > _OVERRIDE_TTL_SECS

    def _write(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)

    # -- pending question ---------------------------------------------------

    def set_pending(self, channel: str, thread_ts: str, entry: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            data["pending"][self.key(channel, thread_ts)] = entry
            self._write(data)

    def get_pending(self, channel: str, thread_ts: str) -> dict[str, Any] | None:
        """Cheap pre-check (does not consume). Expired entries read as absent."""
        entry = self._read()["pending"].get(self.key(channel, thread_ts))
        return None if entry is None or self._expired(entry) else entry

    def take_pending(self, channel: str, thread_ts: str) -> dict[str, Any] | None:
        """Atomically consume the pending question: read + pop + write under
        the lock, so two deliveries of the same "yes" can't both replay it.
        An expired entry is dropped and reported as absent."""
        with self._lock:
            data = self._read()
            entry = data["pending"].pop(self.key(channel, thread_ts), None)
            if entry is None:
                return None
            self._write(data)
            return None if self._expired(entry) else entry

    def clear_pending(self, channel: str, thread_ts: str) -> None:
        with self._lock:
            data = self._read()
            data["pending"].pop(self.key(channel, thread_ts), None)
            self._write(data)

    # -- grants -------------------------------------------------------------

    def add_grant(self, channel: str, thread_ts: str, grant: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            k = self.key(channel, thread_ts)
            if not isinstance(data["grants"].get(k), list):
                data["grants"][k] = []
            data["grants"][k].append(grant)
            self._write(data)

    def grants(self, channel: str, thread_ts: str) -> list[dict[str, Any]]:
        items = self._read()["grants"].get(self.key(channel, thread_ts))
        return list(items) if isinstance(items, list) else []
