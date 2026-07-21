"""Minimal T3 Code HTTP client + shared bridge state.

T3's server (t3code.service, loopback :3773) exposes a typed command API:
  POST /api/orchestration/dispatch            (ClientOrchestrationCommand)
  GET  /api/orchestration/threads/<threadId>  (OrchestrationThreadDetailSnapshot)
All entity ids are client-generated non-empty strings, so the bridge derives
deterministic thread ids from the Slack channel/thread (mirroring T3's own
`claude-import-<sessionId>` convention).

`MirrorStore` is the loop-prevention ledger shared by the turn backend
(`backend_t3.py`) and the outbound mirror (`t3_mirror.py`): every T3 message id
that has already been posted to (or originated from) Slack is recorded here so
it is never posted twice.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

_POSTED_CAP = 500  # per-thread ledger bound; old ids age out


class T3Error(Exception):
    pass


class T3Client:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise T3Error(f"T3 {method} {path} -> {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise T3Error(f"T3 unreachable at {self.base_url}: {exc.reason}") from exc

    def dispatch(self, command: dict) -> dict:
        return self._request("POST", "/api/orchestration/dispatch", command)

    def thread_snapshot(self, thread_id: str) -> dict:
        return self._request("GET", f"/api/orchestration/threads/{thread_id}")


class MirrorStore:
    """channel/thread mapping + already-posted message ids, JSON-persisted."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        try:
            self._data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {"threads": {}}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self._data, indent=1))
        tmp.replace(self._path)

    def register(self, thread_id: str, channel: str, thread_ts: str) -> None:
        with self._lock:
            entry = self._data["threads"].setdefault(
                thread_id, {"channel": channel, "thread_ts": thread_ts, "posted": []}
            )
            entry["channel"], entry["thread_ts"] = channel, thread_ts
            self._save()

    def mark_posted(self, thread_id: str, message_ids: list[str]) -> None:
        with self._lock:
            entry = self._data["threads"].get(thread_id)
            if entry is None:
                return
            for mid in message_ids:
                if mid not in entry["posted"]:
                    entry["posted"].append(mid)
            entry["posted"] = entry["posted"][-_POSTED_CAP:]
            self._save()

    def is_posted(self, thread_id: str, message_id: str) -> bool:
        with self._lock:
            entry = self._data["threads"].get(thread_id)
            return entry is not None and message_id in entry["posted"]

    def remove(self, thread_id: str) -> None:
        with self._lock:
            if self._data["threads"].pop(thread_id, None) is not None:
                self._save()

    def threads(self) -> dict[str, dict]:
        with self._lock:
            return json.loads(json.dumps(self._data["threads"]))
