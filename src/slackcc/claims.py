"""Thread-claim registry: marks a (channel, thread) as owned by an external
Claude session driving an outreach loop, so the Socket Mode daemon stays out of
it (no double-replies).

The daemon and the CLI tools are separate processes, so `is_claimed` re-reads the
file every call rather than caching."""

from __future__ import annotations

import json
import threading
from pathlib import Path


class ClaimStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    @staticmethod
    def key(channel: str, thread_ts: str) -> str:
        return f"{channel}:{thread_ts}"

    def _read(self) -> set[str]:
        try:
            return set(json.loads(self._path.read_text()))
        except (OSError, json.JSONDecodeError):
            return set()

    def _write(self, items: set[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(sorted(items), indent=2))
        tmp.replace(self._path)

    def is_claimed(self, channel: str, thread_ts: str) -> bool:
        return self.key(channel, thread_ts) in self._read()

    def claim(self, channel: str, thread_ts: str) -> None:
        with self._lock:
            items = self._read()
            items.add(self.key(channel, thread_ts))
            self._write(items)

    def release(self, channel: str, thread_ts: str) -> None:
        with self._lock:
            items = self._read()
            items.discard(self.key(channel, thread_ts))
            self._write(items)
