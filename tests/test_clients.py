"""Offline, hermetic tests for pps.PPSClient, t3.T3Client, slackfiles, paths.

No Slack API, no live T3/pps/llama services, no repo .state/.env access.
HTTP-speaking clients are exercised against a stdlib http.server started on
an ephemeral localhost port inside each test.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from websockets.sync.server import serve as ws_serve

from slackcc import paths, slackfiles
from slackcc.pps import PPSClient
from slackcc.t3 import T3Client, T3Error, final_reply, reply_streaming


# --------------------------------------------------------------------------
# shared local-HTTP-server helpers
# --------------------------------------------------------------------------

def _make_handler(responder, requests_log):
    """Build a BaseHTTPRequestHandler that records each request into
    `requests_log` and delegates status/body decisions to `responder`.

    `responder(method, path, headers, body_bytes) -> (status_code, payload_bytes_or_None)`
    A responder may return a third element to set the Content-Type header.
    """

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            requests_log.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )
            status, payload, *rest = responder(
                self.command, self.path, dict(self.headers), body)
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", rest[0] if rest
                                 else "application/octet-stream")
            self.end_headers()
            if payload is not None:
                self.wfile.write(payload)

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def log_message(self, *args, **kwargs):  # silence test noise
            pass

    return Handler


class _LocalServer:
    """Small context-manager wrapper around a threaded HTTPServer."""

    def __init__(self, responder):
        self.requests: list[dict] = []
        handler_cls = _make_handler(responder, self.requests)
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _closed_port_url() -> str:
    """Return a localhost URL whose port is guaranteed to have nothing
    listening (bind then immediately close), to simulate a down server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------
# PPSClient.judge
# --------------------------------------------------------------------------

def test_judge_happy_path_posts_correct_body_and_returns_verdict():
    def responder(method, path, headers, body):
        assert method == "POST"
        assert path == "/v1/judge"
        payload = {"verdict": "allow", "category": "none",
                   "reason": "ok", "stage": "fast", "latency_ms": 12}
        return 200, json.dumps(payload).encode()

    with _LocalServer(responder) as srv:
        client = PPSClient(srv.url)
        verdict = client.judge(sender="alice", policy="default",
                                text="hello there", context="ctx-blob")

        assert verdict == {"verdict": "allow", "category": "none",
                            "reason": "ok", "stage": "fast", "latency_ms": 12}
        assert len(srv.requests) == 1
        sent_body = json.loads(srv.requests[0]["body"].decode())
        assert sent_body == {"sender": "alice", "policy": "default",
                              "text": "hello there", "context": "ctx-blob"}
        assert srv.requests[0]["headers"]["Content-Type"] == "application/json"


def test_judge_server_down_returns_error_verdict():
    client = PPSClient(_closed_port_url(), timeout=2.0)
    verdict = client.judge(sender="alice", policy="default", text="hi", context="")
    assert verdict["verdict"] == "error"
    assert verdict["category"] == "other"
    assert "pps unreachable" in verdict["reason"]
    assert verdict["stage"] == "none"
    assert verdict["latency_ms"] == 0


def test_judge_malformed_verdict_value_returns_error():
    def responder(method, path, headers, body):
        return 200, json.dumps({"verdict": "maybe-allow-idk"}).encode()

    with _LocalServer(responder) as srv:
        client = PPSClient(srv.url)
        verdict = client.judge(sender="a", policy="p", text="t", context="c")
        assert verdict["verdict"] == "error"
        assert verdict["reason"] == "malformed pps response"


def test_judge_non_json_response_returns_error():
    def responder(method, path, headers, body):
        return 200, b"not-json-at-all{{{"

    with _LocalServer(responder) as srv:
        client = PPSClient(srv.url)
        verdict = client.judge(sender="a", policy="p", text="t", context="c")
        assert verdict["verdict"] == "error"
        # Reachable-but-garbage is distinguished from an outage in the reason.
        assert "malformed pps response" in verdict["reason"]


def test_judge_url_trailing_slash_is_stripped():
    def responder(method, path, headers, body):
        assert path == "/v1/judge"
        return 200, json.dumps({"verdict": "deny"}).encode()

    with _LocalServer(responder) as srv:
        client = PPSClient(srv.url + "/")
        verdict = client.judge(sender="a", policy="p", text="t", context="c")
        assert verdict["verdict"] == "deny"


# --------------------------------------------------------------------------
# T3Client
# --------------------------------------------------------------------------

class _WsServer:
    """Threaded local WebSocket server speaking just enough Effect RPC.

    `respond(request_dict) -> list[dict]` returns the frames to send back for
    each received request. `reject_status` makes the upgrade fail with that
    HTTP status instead (e.g. 426 for a protocol mismatch)."""

    def __init__(self, respond=None, reject_status: int | None = None):
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self.headers: list[dict] = []
        self.received: list[dict] = []
        self._respond = respond or (lambda req: [])

        def process_request(connection, request):
            self.paths.append(request.path)
            self.headers.append(dict(request.headers.raw_items()))
            if reject_status is not None:
                return connection.respond(reject_status, "nope\n")
            return None

        def handler(ws):
            for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                if msg.get("_tag") != "Request":
                    continue
                self.requests.append(msg)
                for frame in self._respond(msg):
                    ws.send(json.dumps(frame))

        self.server = ws_serve(handler, "127.0.0.1", 0, process_request=process_request)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.socket.getsockname()[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()


def _success(req, value):
    return {"_tag": "Exit", "requestId": req["id"],
            "exit": {"_tag": "Success", "value": value}}


def test_dispatch_sends_rpc_request_with_bearer_and_protocol_version():
    with _WsServer(lambda req: [_success(req, {"sequence": 7})]) as srv:
        client = T3Client(srv.url, token="secret-token-123", timeout=5)
        result = client.dispatch({"type": "thread.unsettle", "threadId": "t1"})

    assert result == {"sequence": 7}
    assert srv.paths == ["/ws?orchestrationProtocol=2"]
    assert srv.headers[0]["Authorization"] == "Bearer secret-token-123"
    (req,) = srv.requests
    assert req["tag"] == "orchestration.dispatchCommand"
    assert req["payload"] == {"type": "thread.unsettle", "threadId": "t1"}
    assert req["headers"] == []


def test_dispatch_answers_ping_and_ignores_other_request_ids():
    def respond(req):
        return [
            {"_tag": "Ping"},
            {"_tag": "Exit", "requestId": "unrelated",
             "exit": {"_tag": "Success", "value": {"sequence": -1}}},
            _success(req, {"sequence": 3}),
        ]

    with _WsServer(respond) as srv:
        client = T3Client(srv.url, token="tok", timeout=5)
        assert client.dispatch({"type": "x"}) == {"sequence": 3}
    assert {"_tag": "Pong"} in srv.received


def test_dispatch_typed_failure_raises_t3error_with_server_message():
    def respond(req):
        return [{"_tag": "Exit", "requestId": req["id"], "exit": {
            "_tag": "Failure",
            "cause": [{"_tag": "Fail", "error": {
                "_tag": "OrchestrationV2DispatchCommandError",
                "message": "No orchestration projection exists for thread t9."}}],
        }}]

    with _WsServer(respond) as srv:
        client = T3Client(srv.url, token="tok", timeout=5)
        with pytest.raises(T3Error) as excinfo:
            client.dispatch({"type": "thread.unsettle", "threadId": "t9"})
    message = str(excinfo.value)
    assert "orchestration.dispatchCommand" in message
    assert "No orchestration projection exists for thread t9." in message


def test_dispatch_schema_defect_raises_t3error_with_defect_text():
    def respond(req):
        return [{"_tag": "Exit", "requestId": req["id"], "exit": {
            "_tag": "Failure",
            "cause": [{"_tag": "Die", "defect": "Expected { readonly type: ... }"}],
        }}]

    with _WsServer(respond) as srv:
        client = T3Client(srv.url, token="tok", timeout=5)
        with pytest.raises(T3Error) as excinfo:
            client.dispatch({"type": "bogus"})
    assert "Expected { readonly type: ... }" in str(excinfo.value)


def test_protocol_mismatch_426_raises_t3error_naming_the_protocol():
    with _WsServer(reject_status=426) as srv:
        client = T3Client(srv.url, token="tok", timeout=5)
        with pytest.raises(T3Error) as excinfo:
            client.dispatch({"type": "x"})
    assert "protocol v2" in str(excinfo.value)


def test_rejected_upgrade_raises_t3error_with_status():
    with _WsServer(reject_status=401) as srv:
        client = T3Client(srv.url, token="bad", timeout=5)
        with pytest.raises(T3Error) as excinfo:
            client.dispatch({"type": "x"})
    assert "401" in str(excinfo.value)


def test_connection_refused_raises_t3error():
    client = T3Client(_closed_port_url(), token="tok", timeout=2)
    with pytest.raises(T3Error) as excinfo:
        client.dispatch({"kind": "x"})
    assert "unreachable" in str(excinfo.value)


def test_thread_projection_gets_v2_path_with_protocol_header():
    def responder(method, path, headers, body):
        assert method == "GET"
        assert path == "/api/orchestration/threads/slack-C1-2-3"
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered["authorization"] == "Bearer tok"
        assert lowered["x-t3-orchestration-protocol"] == "2"
        return 200, json.dumps({"snapshotSequence": 9, "projection": {
            "thread": {"id": "slack-C1-2-3"}, "runs": [], "messages": []}}).encode()

    with _LocalServer(responder) as srv:
        client = T3Client(srv.url, token="tok")
        projection = client.thread_projection("slack-C1-2-3")
    assert projection == {"thread": {"id": "slack-C1-2-3"}, "runs": [], "messages": []}


def test_thread_projection_not_found_raises_t3error_with_reason():
    def responder(method, path, headers, body):
        return 404, json.dumps({"_tag": "EnvironmentResourceNotFoundError",
                                "code": "not_found", "reason": "thread_not_found"}).encode()

    with _LocalServer(responder) as srv:
        client = T3Client(srv.url, token="tok")
        with pytest.raises(T3Error) as excinfo:
            client.thread_projection("gone")
    message = str(excinfo.value)
    assert "404" in message
    assert "thread_not_found" in message


def test_upload_image_mints_url_posts_bytes_and_returns_attachment(monkeypatch):
    def responder(method, path, headers, body):
        return 204, None

    with _LocalServer(responder) as srv:
        client = T3Client(srv.url, token="tok")
        rpc_calls = []

        def fake_rpc(tag, payload):
            rpc_calls.append((tag, payload))
            return {"attachmentId": "pending-abc", "relativeUrl": "/api/attachments/upload/tok.sig",
                    "expiresAt": 0}

        monkeypatch.setattr(client, "_rpc", fake_rpc)
        att = client.upload_image(name="shot.png", mime_type="image/png", data=b"png-bytes")

    assert rpc_calls == [("attachments.createUploadUrl", {
        "type": "image", "name": "shot.png", "mimeType": "image/png", "sizeBytes": 9})]
    (post,) = srv.requests
    assert post["method"] == "POST"
    assert post["path"] == "/api/attachments/upload/tok.sig"
    assert post["headers"]["Content-Type"] == "image/png"
    assert post["body"] == b"png-bytes"
    assert att == {"type": "image", "id": "pending-abc", "name": "shot.png",
                   "mimeType": "image/png", "sizeBytes": 9}


def test_final_reply_is_last_finished_assistant_message_of_the_run():
    projection = {"messages": [
        {"id": "u", "role": "user", "runId": "r1", "text": "hi", "streaming": False},
        {"id": "a1", "role": "assistant", "runId": "r1", "text": "narration", "streaming": False},
        {"id": "a2", "role": "assistant", "runId": "r1", "text": "answer", "streaming": False},
        {"id": "a3", "role": "assistant", "runId": "r1", "text": "  ", "streaming": False},
        {"id": "a4", "role": "assistant", "runId": "r9", "text": "partial", "streaming": True},
        {"id": "b1", "role": "assistant", "runId": "r2", "text": "other run", "streaming": False},
    ]}
    assert final_reply(projection, "r1")["id"] == "a2"
    assert final_reply(projection, "r2")["id"] == "b1"
    assert final_reply(projection, "r3") is None


# --------------------------------------------------------------------------
# slackfiles
# --------------------------------------------------------------------------

def test_safe_name_uses_name_field():
    assert slackfiles._safe_name({"name": "report.pdf", "id": "F123"}) == "report.pdf"


def test_safe_name_falls_back_to_id_when_no_name():
    assert slackfiles._safe_name({"id": "F123"}) == "F123"


def test_safe_name_falls_back_to_file_when_nothing_present():
    assert slackfiles._safe_name({}) == "file"


def test_safe_name_strips_path_traversal():
    # basename guards against directory traversal in a Slack-supplied name
    assert slackfiles._safe_name({"name": "../../etc/passwd"}) == "passwd"
    assert slackfiles._safe_name({"name": "/abs/path/evil.sh"}) == "evil.sh"


def test_safe_name_strips_null_bytes():
    assert slackfiles._safe_name({"name": "a\x00b.txt"}) == "ab.txt"


def test_download_slack_file_returns_none_when_no_url():
    result = slackfiles.download_slack_file({"name": "x.txt"}, Path("/tmp/whatever"), "tok")
    assert result is None


def test_download_slack_file_downloads_bytes_with_bearer_header(tmp_path):
    def responder(method, path, headers, body):
        assert headers["Authorization"] == "Bearer tok-abc"
        return 200, b"file-bytes-content"

    with _LocalServer(responder) as srv:
        file_obj = {"name": "note.txt", "url_private": f"{srv.url}/files/note.txt"}
        dest_dir = tmp_path / "incoming"
        result = slackfiles.download_slack_file(file_obj, dest_dir, "tok-abc")

        assert result == dest_dir / "note.txt"
        assert result.read_bytes() == b"file-bytes-content"


def test_download_slack_file_prefers_url_private_download_over_url_private():
    calls = []

    def responder(method, path, headers, body):
        calls.append(path)
        return 200, b"data"

    with _LocalServer(responder) as srv:
        file_obj = {
            "name": "f.bin",
            "url_private": f"{srv.url}/wrong-path",
            "url_private_download": f"{srv.url}/right-path",
        }
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            slackfiles.download_slack_file(file_obj, Path(d), "tok")

        assert calls == ["/right-path"]


def test_download_slack_file_creates_dest_dir(tmp_path):
    def responder(method, path, headers, body):
        return 200, b"x"

    with _LocalServer(responder) as srv:
        dest_dir = tmp_path / "does" / "not" / "exist" / "yet"
        assert not dest_dir.exists()
        file_obj = {"name": "z.txt", "url_private": f"{srv.url}/z"}
        slackfiles.download_slack_file(file_obj, dest_dir, "tok")
        assert dest_dir.is_dir()
        assert (dest_dir / "z.txt").read_bytes() == b"x"


def test_download_slack_file_rejects_the_html_sign_in_page(tmp_path):
    """No `files:read` scope -> Slack answers 200 with its login page. Saving
    that as `photo.png` would only fail later, somewhere less obvious."""
    def responder(method, path, headers, body):
        return 200, b"<html>Sign in to Slack</html>", "text/html; charset=utf-8"

    with _LocalServer(responder) as srv:
        file_obj = {"name": "photo.png", "mimetype": "image/png",
                    "url_private": f"{srv.url}/files/photo.png"}
        with pytest.raises(slackfiles.SlackFileError, match="files:read"):
            slackfiles.download_slack_file(file_obj, tmp_path, "tok")

    assert not (tmp_path / "photo.png").exists()


def test_download_slack_file_allows_html_when_the_file_really_is_html(tmp_path):
    def responder(method, path, headers, body):
        return 200, b"<html>real content</html>", "text/html"

    with _LocalServer(responder) as srv:
        file_obj = {"name": "page.html", "mimetype": "text/html",
                    "url_private": f"{srv.url}/files/page.html"}
        result = slackfiles.download_slack_file(file_obj, tmp_path, "tok")

    assert result.read_bytes() == b"<html>real content</html>"


def test_incoming_dir_is_one_directory_per_thread(tmp_path):
    assert (slackfiles.incoming_dir(tmp_path, "1787678587.002349")
            == tmp_path / ".slack-incoming" / "1787678587_002349")


def test_download_files_returns_every_saved_path(tmp_path, monkeypatch):
    saved = []

    def fake(file_obj, dest_dir, token):
        path = dest_dir / file_obj["name"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("bytes")
        saved.append(token)
        return path

    monkeypatch.setattr(slackfiles, "download_slack_file", fake)
    out = slackfiles.download_files(
        [{"name": "a.png"}, {"name": "b.pdf"}], tmp_path, "tok")

    assert [p.name for p in out] == ["a.png", "b.pdf"]
    assert saved == ["tok", "tok"]


def test_download_files_skips_the_broken_one_and_reports_it(tmp_path, monkeypatch):
    """One unreadable attachment must not cost the agent the other one."""
    def fake(file_obj, dest_dir, token):
        if file_obj["name"] == "bad.png":
            raise slackfiles.SlackFileError("nope")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / file_obj["name"]
        path.write_text("bytes")
        return path

    errors = []
    monkeypatch.setattr(slackfiles, "download_slack_file", fake)
    out = slackfiles.download_files(
        [{"name": "bad.png"}, {"name": "good.png"}], tmp_path, "tok",
        on_error=lambda fo, e: errors.append((fo["name"], str(e))))

    assert [p.name for p in out] == ["good.png"]
    assert errors == [("bad.png", "nope")]


def test_download_files_drops_entries_with_no_url(tmp_path, monkeypatch):
    monkeypatch.setattr(slackfiles, "download_slack_file",
                        lambda fo, d, t: None)
    assert slackfiles.download_files([{"name": "a"}], tmp_path, "tok") == []


def test_build_t3_attachment_packages_supported_image_for_upload(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(b"fake-png-bytes")

    att = slackfiles.build_t3_attachment(p)

    assert att == {"name": "shot.png", "mimeType": "image/png", "data": b"fake-png-bytes"}


def test_build_t3_attachment_returns_none_for_unsupported_mime_type(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("plain text, not an image")

    assert slackfiles.build_t3_attachment(p) is None


def test_build_t3_attachment_returns_none_for_unknown_extension(tmp_path):
    p = tmp_path / "mystery"
    p.write_bytes(b"\x00\x01")

    assert slackfiles.build_t3_attachment(p) is None


def test_build_t3_attachment_returns_none_over_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(slackfiles, "MAX_T3_IMAGE_BYTES", 4)
    p = tmp_path / "big.png"
    p.write_bytes(b"way-too-big")

    assert slackfiles.build_t3_attachment(p) is None


def test_build_t3_attachment_returns_none_for_empty_file(tmp_path):
    p = tmp_path / "empty.png"
    p.write_bytes(b"")

    assert slackfiles.build_t3_attachment(p) is None


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def test_claims_path_derives_from_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
    assert paths.claims_path() == tmp_path / "claimed.json"


def test_channel_cwd_reads_the_project_dir_from_the_routing_map(tmp_path, monkeypatch):
    cfg = tmp_path / "channels.json"
    cfg.write_text(json.dumps({"channels": {
        "C123": {"project": "demo", "cwd": "~/projects/demo"},
    }}))
    monkeypatch.setattr(paths, "CHANNELS_CONFIG", cfg)
    monkeypatch.setenv("HOME", "/home/tester")

    assert paths.channel_cwd("C123") == Path("/home/tester/projects/demo")


def test_channel_cwd_returns_none_for_unknown_or_unusable_entries(tmp_path, monkeypatch):
    cfg = tmp_path / "channels.json"
    cfg.write_text(json.dumps({"channels": {
        "_comment": "not a channel", "C_NOCWD": {"project": "x"},
    }}))
    monkeypatch.setattr(paths, "CHANNELS_CONFIG", cfg)

    assert paths.channel_cwd("C_MISSING") is None
    assert paths.channel_cwd("_comment") is None
    assert paths.channel_cwd("C_NOCWD") is None


def test_channel_cwd_returns_none_when_the_config_is_missing_or_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CHANNELS_CONFIG", tmp_path / "nope.json")
    assert paths.channel_cwd("C123") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    monkeypatch.setattr(paths, "CHANNELS_CONFIG", broken)
    assert paths.channel_cwd("C123") is None


def test_resolve_token_prefers_env_var(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("SLACK_BOT_TOKEN=from-dotenv\n")
    monkeypatch.setattr(paths, "DOTENV", dotenv)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "from-env")

    assert paths.resolve_token() == "from-env"


def test_resolve_token_falls_back_to_dotenv(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("SLACK_BOT_TOKEN=from-dotenv\n")
    monkeypatch.setattr(paths, "DOTENV", dotenv)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    assert paths.resolve_token() == "from-dotenv"


def test_resolve_token_returns_none_when_missing_everywhere(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "DOTENV", tmp_path / "nonexistent.env")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    assert paths.resolve_token() is None


def test_parse_dotenv_ignores_comments_blank_lines_and_strips_quotes(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n"
        "# a comment\n"
        "  \n"
        "SLACK_BOT_TOKEN=\"quoted-value\"\n"
        "OTHER='single-quoted'\n"
        "NO_EQUALS_SIGN\n"
        "  SPACED_KEY = spaced-value  \n"
    )
    parsed = paths._parse_dotenv(dotenv)
    assert parsed["SLACK_BOT_TOKEN"] == "quoted-value"
    assert parsed["OTHER"] == "single-quoted"
    assert "NO_EQUALS_SIGN" not in parsed
    # note: current impl does not strip the value's leading/trailing spaces
    # after splitting on "=", only surrounding-quote stripping + outer strip
    # of the whole line before split; "SPACED_KEY = spaced-value" -> key has
    # trailing space stripped via .strip(), value keeps its own .strip() too
    assert parsed["SPACED_KEY"] == "spaced-value"


def test_parse_dotenv_returns_empty_dict_for_missing_file(tmp_path):
    assert paths._parse_dotenv(tmp_path / "missing.env") == {}


def test_final_reply_waits_while_any_segment_of_the_run_streams():
    projection = {"messages": [
        {"id": "a1", "role": "assistant", "runId": "r1", "text": "narration", "streaming": False},
        {"id": "a2", "role": "assistant", "runId": "r1", "text": "the ans", "streaming": True},
        {"id": "b1", "role": "assistant", "runId": "r2", "text": "other run", "streaming": False},
    ]}
    assert final_reply(projection, "r1") is None
    assert reply_streaming(projection, "r1") is True
    assert final_reply(projection, "r2")["id"] == "b1"
    assert reply_streaming(projection, "r2") is False
