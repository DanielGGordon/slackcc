"""Maps a Slack thread to a Claude Code session id so each thread is one
continuous conversation. Simple JSON file store; fine for a single daemon."""

from __future__ import annotations

import json
import threading
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    @staticmethod
    def key(channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def get(self, channel_id: str, thread_ts: str) -> str | None:
        with self._lock:
            return self._data.get(self.key(channel_id, thread_ts))

    def set(self, channel_id: str, thread_ts: str, session_id: str) -> None:
        with self._lock:
            self._data[self.key(channel_id, thread_ts)] = session_id
            self._flush()

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self._path)
