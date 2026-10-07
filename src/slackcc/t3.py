"""Minimal T3 Code client + shared bridge state.

T3's server (t3code.service, loopback :3773) speaks orchestration protocol v2:
  GET  /api/orchestration/threads/<threadId>   thread projection (HTTP)
  WS   /ws  `orchestration.dispatchCommand`     every command (Effect RPC, JSON)
  WS   /ws  `attachments.createUploadUrl`       then POST the bytes to the URL
Commands are WebSocket-only: v1's `POST /api/orchestration/dispatch` is gone.
Both transports authenticate with the same bearer token and must announce the
protocol version (`x-t3-orchestration-protocol` header / `orchestrationProtocol`
query param); a server on a newer protocol answers 426, surfaced here as a
T3Error naming the version mismatch rather than a bare 404.

All entity ids are client-generated non-empty strings, so the bridge derives
deterministic thread ids from the Slack channel/thread (mirroring T3's own
`claude-import-<sessionId>` convention).

`MirrorStore` is the loop-prevention ledger shared by the turn backend
(`backend_t3.py`) and the outbound mirror (`t3_mirror.py`): every T3 message id
that has already been posted to (or originated from) Slack is recorded here so
it is never posted twice.
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

_POSTED_CAP = 500  # per-thread ledger bound; old ids age out

PROTOCOL_VERSION = "2"
_PROTOCOL_HEADER = "x-t3-orchestration-protocol"
_MAX_RPC_FRAME = 16 * 1024 * 1024


class T3Error(Exception):
    pass


def _rpc_failure(tag: str, exit_: dict) -> str:
    """The readable part of an Effect RPC `Failure` exit: a typed error's
    `message` (e.g. "No orchestration projection exists for thread x"), or a
    defect's text (schema rejections land here), clipped."""
    causes = exit_.get("cause") or []
    first = causes[0] if causes and isinstance(causes[0], dict) else {}
    error = first.get("error")
    if isinstance(error, dict):
        detail = error.get("message") or error.get("_tag") or json.dumps(error)
    else:
        detail = first.get("defect") or first.get("_tag") or json.dumps(exit_)
    return f"T3 {tag} failed: {str(detail)[:500]}"


class T3Client:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ids = itertools.count(1)

    def _request(self, method: str, path: str, *, data: bytes | None = None,
                 content_type: str = "application/json") -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": content_type,
                _PROTOCOL_HEADER: PROTOCOL_VERSION,
            },
            data=data,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise T3Error(f"T3 {method} {path} -> {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise T3Error(f"T3 unreachable at {self.base_url}: {exc.reason}") from exc

    def _rpc(self, tag: str, payload: dict) -> dict:
        """One WebSocket RPC call: connect, send the request, wait for its Exit.

        A connection per call keeps the client stateless; commands are rare
        (a handful per Slack turn) next to the HTTP polling."""
        request_id = str(next(self._ids))
        ws_url = (self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                  + f"/ws?orchestrationProtocol={PROTOCOL_VERSION}")
        try:
            with connect(ws_url,
                         additional_headers={"Authorization": f"Bearer {self.token}"},
                         open_timeout=self.timeout, close_timeout=2,
                         max_size=_MAX_RPC_FRAME) as ws:
                ws.send(json.dumps({"_tag": "Request", "id": request_id, "tag": tag,
                                    "payload": payload, "headers": []}))
                while True:
                    frame = json.loads(ws.recv(timeout=self.timeout))
                    for msg in frame if isinstance(frame, list) else [frame]:
                        kind = msg.get("_tag")
                        if kind == "Ping":
                            ws.send(json.dumps({"_tag": "Pong"}))
                        elif kind == "Defect":
                            raise T3Error(f"T3 {tag} failed: {str(msg.get('defect'))[:500]}")
                        elif kind == "Exit" and str(msg.get("requestId")) == request_id:
                            exit_ = msg.get("exit") or {}
                            if exit_.get("_tag") == "Success":
                                value = exit_.get("value")
                                return value if isinstance(value, dict) else {}
                            raise T3Error(_rpc_failure(tag, exit_))
        except InvalidStatus as exc:
            status = exc.response.status_code
            body = (exc.response.body or b"").decode(errors="replace")[:300]
            if status == 426:
                raise T3Error(f"T3 no longer speaks orchestration protocol v{PROTOCOL_VERSION}; "
                              f"slackcc needs updating: {body}") from exc
            raise T3Error(f"T3 WS {tag} -> {status}: {body}") from exc
        except TimeoutError as exc:
            raise T3Error(f"T3 {tag} timed out after {self.timeout}s") from exc
        except (OSError, WebSocketException) as exc:
            raise T3Error(f"T3 unreachable at {self.base_url}: {exc}") from exc

    def dispatch(self, command: dict) -> dict:
        return self._rpc("orchestration.dispatchCommand", command)

    def thread_projection(self, thread_id: str) -> dict:
        """The thread's full v2 projection: `thread`, `runs`, `messages`,
        `turnItems`, `runtimeRequests`, ..."""
        return self._request("GET", f"/api/orchestration/threads/{thread_id}") \
            .get("projection") or {}

    def upload_image(self, *, name: str, mime_type: str, data: bytes) -> dict:
        """Stage an image with T3 and return the `ChatImageAttachment` that a
        `message.dispatch` references (T3 adopts the pending upload into the
        thread when the message lands)."""
        minted = self._rpc("attachments.createUploadUrl", {
            "type": "image", "name": name, "mimeType": mime_type, "sizeBytes": len(data),
        })
        self._request("POST", minted["relativeUrl"], data=data, content_type=mime_type)
        return {"type": "image", "id": minted["attachmentId"], "name": name,
                "mimeType": mime_type, "sizeBytes": len(data)}


# Run states after which nothing more is coming (OrchestrationV2RunStatus).
TERMINAL_RUN_STATUSES = {"completed", "interrupted", "failed", "cancelled", "rolled_back"}


def final_reply(projection: dict, run_id: str) -> dict | None:
    """A run's reply: its last assistant message with text -- or None while
    any of the run's assistant messages is still streaming.

    A run emits one assistant message per text segment between tool calls;
    only the last one is the answer -- earlier ones are status narration. A
    run can read terminal a beat before its last segment stops streaming, and
    picking the newest *finished* segment then would mistake narration for
    the answer."""
    reply = None
    for msg in projection.get("messages", []):
        if msg.get("role") != "assistant" or msg.get("runId") != run_id:
            continue
        if msg.get("streaming"):
            return None
        if (msg.get("text") or "").strip():
            reply = msg
    return reply


def reply_streaming(projection: dict, run_id: str) -> bool:
    """Whether any of the run's assistant messages is still streaming."""
    return any(msg.get("role") == "assistant" and msg.get("runId") == run_id
               and msg.get("streaming") for msg in projection.get("messages", []))


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

    def settled_notice(self, thread_id: str) -> str | None:
        """The `settledAt` of the settle we already announced in Slack, if any."""
        with self._lock:
            entry = self._data["threads"].get(thread_id)
            if entry is None:
                return None
            return entry.get("settled_notice")

    def set_settled_notice(self, thread_id: str, settled_at: str | None) -> None:
        """Record (or clear, with None) the announced settle."""
        with self._lock:
            entry = self._data["threads"].get(thread_id)
            if entry is None:
                return
            if settled_at is None:
                if "settled_notice" not in entry:
                    return
                del entry["settled_notice"]
            elif entry.get("settled_notice") == settled_at:
                return
            else:
                entry["settled_notice"] = settled_at
            self._save()
