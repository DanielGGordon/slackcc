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

from slackcc import paths, slackfiles
from slackcc.pps import PPSClient
from slackcc.t3 import T3Client, T3Error


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

def test_dispatch_posts_with_bearer_header_and_body_passthrough():
    def responder(method, path, headers, body):
        assert method == "POST"
        assert path == "/api/orchestration/dispatch"
        assert headers["Authorization"] == "Bearer secret-token-123"
        assert headers["Content-Type"] == "application/json"
        sent = json.loads(body.decode())
        assert sent == {"kind": "sendMessage", "threadId": "t1"}
        return 200, json.dumps({"ok": True, "echo": sent}).encode()

    with _LocalServer(responder) as srv:
        client = T3Client(srv.url, token="secret-token-123")
        result = client.dispatch({"kind": "sendMessage", "threadId": "t1"})
        assert result == {"ok": True, "echo": {"kind": "sendMessage", "threadId": "t1"}}


def test_thread_snapshot_uses_get_and_correct_path():
    def responder(method, path, headers, body):
        assert method == "GET"
        assert path == "/api/orchestration/threads/claude-import-abc"
        assert body == b""
        return 200, json.dumps({"threadId": "claude-import-abc", "messages": []}).encode()

    with _LocalServer(responder) as srv:
        client = T3Client(srv.url, token="tok")
        snapshot = client.thread_snapshot("claude-import-abc")
        assert snapshot == {"threadId": "claude-import-abc", "messages": []}


def test_non_2xx_raises_t3error_with_status_and_body_detail():
    def responder(method, path, headers, body):
        return 400, b"bad request: missing threadId"

    with _LocalServer(responder) as srv:
        client = T3Client(srv.url, token="tok")
        with pytest.raises(T3Error) as excinfo:
            client.dispatch({"kind": "x"})
        message = str(excinfo.value)
        assert "400" in message
        assert "bad request: missing threadId" in message
        assert "/api/orchestration/dispatch" in message


def test_connection_refused_raises_t3error():
    client = T3Client(_closed_port_url(), token="tok", timeout=2)
    with pytest.raises(T3Error) as excinfo:
        client.dispatch({"kind": "x"})
    assert "unreachable" in str(excinfo.value)


def test_dispatch_with_empty_response_body_returns_empty_dict():
    def responder(method, path, headers, body):
        return 200, b""

    with _LocalServer(responder) as srv:
        client = T3Client(srv.url, token="tok")
        assert client.dispatch({"kind": "x"}) == {}


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


def test_build_t3_attachment_encodes_supported_image_as_data_url(tmp_path):
    import base64

    p = tmp_path / "shot.png"
    p.write_bytes(b"fake-png-bytes")

    att = slackfiles.build_t3_attachment(p)

    assert att == {
        "type": "image",
        "name": "shot.png",
        "mimeType": "image/png",
        "sizeBytes": len(b"fake-png-bytes"),
        "dataUrl": f"data:image/png;base64,{base64.b64encode(b'fake-png-bytes').decode()}",
    }


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
