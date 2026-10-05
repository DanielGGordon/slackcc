"""The loopback config API: slackcc owns its config, end to end.

    GET  /config   the two files as parsed JSON, an etag over their bytes,
                   the EFFECTIVE config the daemon is running with (defaults
                   applied), and which bytes that was loaded from
    PUT  /config   {if_match, channels?, senders?, dry_run?} -> validated by
                   the same parser the daemon starts with, backed up, written
                   atomically, and swapped into the running daemon. No restart.
    GET  /healthz  {"ok": true}, no token

SIGHUP does the same reload from whatever is on disk (a hand edit), and keeps
the running config when the files do not load.

Why here: before this, Alfred's admin wrote these two files itself, validated
them by importing this package's private loader in a subprocess, and
restarted this unit to apply them. Now a client sends JSON and gets a verdict;
what an unset key means lives in exactly one place (config.effective).

Bound to 127.0.0.1 only, and every /config request needs
`Authorization: Bearer $SLACKCC_CONFIG_TOKEN`. With no token set the API does
not start (SIGHUP reload still works). Request bodies are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import signal
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import LiveSettings, Settings, effective, with_config

log = logging.getLogger(__name__)

API_VERSION = 1
MAX_BODY = 256 * 1024
BACKUPS_KEPT = 20
DEFAULT_PORT = 8643
_PUT_KEYS = {"if_match", "channels", "senders", "dry_run"}
_TMP_MARK = ".slackcc-tmp-"


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.body = {"ok": False, "error": error, "message": message, **extra}


def serialize(obj: dict) -> str:
    """How the config API writes a file: 2-space JSON, key order kept,
    non-ASCII as-is, trailing newline -- the format the files already use."""
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def etag_of(channels_text: str | None, senders_text: str | None) -> str:
    h = hashlib.sha256()
    for part in (channels_text, senders_text):
        h.update(b"-" if part is None else b"+%d:" % len(part.encode()) + part.encode())
        h.update(b"|")
    return h.hexdigest()[:16]


def _read_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    """Temp file in the same directory -> fsync -> rename over the target ->
    fsync the directory. An existing file keeps its mode; a new one is 0600."""
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    tmp = path.with_name(f".{path.name}{_TMP_MARK}{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # not every filesystem lets a directory be fsynced


class ConfigService:
    """GET/PUT/reload over the files `live.current` was loaded from.

    One lock serialises every write and reload, so an if_match check and the
    write it guards cannot interleave with another."""

    def __init__(self, live: LiveSettings, *, backup_dir: Path) -> None:
        self.live = live
        self.backup_dir = backup_dir
        self._lock = threading.Lock()

    @property
    def channels_path(self) -> Path:
        return self.live.current.config_path

    @property
    def senders_path(self) -> Path | None:
        return self.live.current.senders_path

    # -- reading ---------------------------------------------------------

    def read_disk(self) -> dict:
        ctext = _read_text(self.channels_path)
        stext = _read_text(self.senders_path)
        problems: list[str] = []

        def parse(text: str | None, name: str) -> dict | None:
            if text is None:
                return None
            try:
                obj = json.loads(text)
            except ValueError as e:
                problems.append(f"{name}: not valid JSON ({e})")
                return None
            if not isinstance(obj, dict):
                problems.append(f"{name}: not a JSON object")
                return None
            return obj

        channels = parse(ctext, "channels.json")
        if ctext is None:
            problems.append("channels.json: missing")
        senders = parse(stext, "senders.json")
        return {"channels_text": ctext, "senders_text": stext, "channels": channels,
                "senders": senders, "problems": problems, "etag": etag_of(ctext, stext)}

    def mark_loaded_from_disk(self) -> None:
        """At start: the settings in `live` were just loaded from these bytes."""
        self.live.loaded = {**self.live.loaded, "etag": self.read_disk()["etag"]}

    def get(self) -> dict:
        # Under the write lock: a GET never sees new senders with old channels
        # halfway through a two-file write.
        with self._lock:
            return self._get_locked()

    def _get_locked(self) -> dict:
        disk = self.read_disk()
        loaded = dict(self.live.loaded)
        return {
            "ok": True,
            "api_version": API_VERSION,
            "etag": disk["etag"],
            "channels": disk["channels"],
            "senders": disk["senders"],
            "senders_file": self.senders_path is not None and disk["senders_text"] is not None,
            "problems": disk["problems"],
            "effective": effective(self.live.current),
            "loaded": loaded,
            # False after a hand edit nobody has reloaded yet (SIGHUP picks it up).
            "loaded_current": loaded.get("etag") == disk["etag"],
        }

    # -- validating ------------------------------------------------------

    def _candidate(self, channels: dict, senders: dict | None) -> Settings:
        if senders is None and self.live.current.guest_defaults is not None:
            # No senders.json means "protection off: everyone is the owner".
            # The running daemon has a sender policy, so a missing file is
            # far likelier an accident (a deletion, a half-done replace) than
            # a decision -- and a reload must never be how screening turns off.
            raise ApiError(422, "senders_missing",
                           "senders.json is missing but the running daemon has a sender "
                           "policy; keeping it. To turn sender protection off, remove the "
                           "file and restart slackcc.")
        try:
            candidate = with_config(self.live.current, channels, senders)
        except Exception as e:  # noqa: BLE001 - whatever the loader raises is a refusal
            raise ApiError(422, "invalid", f"{type(e).__name__}: {e}"[:400]) from e
        if candidate.has_t3_channels() and not self.live.t3_ready:
            raise ApiError(422, "restart_required",
                           "The first t3 channel needs a restart of slackcc, not a reload.")
        return candidate

    # -- writing ---------------------------------------------------------

    def put(self, body: object) -> dict:
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "Send a JSON object.")
        unknown = sorted(set(body) - _PUT_KEYS)
        if unknown:
            raise ApiError(400, "bad_request", f"Unknown field: {', '.join(unknown)}.")
        if_match = body.get("if_match")
        if not isinstance(if_match, str) or not if_match:
            raise ApiError(400, "if_match_required", "Send the etag from GET /config as if_match.")
        if "channels" not in body and "senders" not in body:
            raise ApiError(400, "bad_request", "Send channels, senders, or both.")
        for key in ("channels", "senders"):
            if key in body and not isinstance(body[key], dict):
                raise ApiError(400, "bad_request", f"{key} must be a JSON object.")
        dry_run = body.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise ApiError(400, "bad_request", "dry_run must be true or false.")
        if "senders" in body and self.senders_path is None:
            raise ApiError(409, "no_senders_path", "This daemon was started without a senders.json path.")

        with self._lock:
            disk = self.read_disk()
            if if_match != disk["etag"]:
                raise ApiError(409, "stale", "The Slack config changed since you read it. Read it again.",
                               etag=disk["etag"])
            channels = body["channels"] if "channels" in body else disk["channels"]
            senders = body["senders"] if "senders" in body else disk["senders"]
            if channels is None:
                raise ApiError(422, "invalid", "channels.json on disk does not load; send channels.")
            if "senders" not in body and disk["senders_text"] is not None and senders is None:
                raise ApiError(422, "invalid", "senders.json on disk does not load; send senders.")
            candidate = self._candidate(channels, senders)
            if dry_run:
                return {"ok": True, "dry_run": True, "effective": effective(candidate)}

            texts = {
                "channels": serialize(channels) if "channels" in body else disk["channels_text"],
                "senders": serialize(senders) if "senders" in body else disk["senders_text"],
            }
            changed = (texts["channels"] != disk["channels_text"]
                       or texts["senders"] != disk["senders_text"])
            if changed:
                self._write(disk, texts)
            new_etag = etag_of(texts["channels"], texts["senders"])
            self.live.swap(candidate, etag=new_etag, source="api")
            log.info("config applied via API (changed=%s, etag=%s)", changed, new_etag)
            out = self._get_locked()
        out["changed"] = changed
        return out

    def _write(self, disk: dict, texts: dict) -> None:
        self._backup(disk)
        written: list[tuple[Path, str | None]] = []
        try:
            # senders first: a guest gets narrower before a channel opens up.
            for path, key in ((self.senders_path, "senders"), (self.channels_path, "channels")):
                if path is None or texts[key] == disk[f"{key}_text"]:
                    continue
                _atomic_write(path, texts[key])
                written.append((path, disk[f"{key}_text"]))
        except OSError as e:
            for path, old in reversed(written):
                try:
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, old)
                except OSError:
                    log.exception("could not put %s back after a failed write", path)
            raise ApiError(500, "write_failed", f"Could not write the config: {e.strerror or e}") from e

    def _backup(self, disk: dict) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.backup_dir / stamp
        try:
            target.mkdir(parents=True, mode=0o700)
            for name, key in (("channels.json", "channels_text"), ("senders.json", "senders_text")):
                if disk[key] is not None:
                    _atomic_write(target / name, disk[key])
            for old in sorted(p for p in self.backup_dir.iterdir() if p.is_dir())[:-BACKUPS_KEPT]:
                shutil.rmtree(old, ignore_errors=True)
        except OSError as e:
            raise ApiError(500, "backup_failed", f"Could not back up the config first: {e.strerror or e}") from e

    # -- reloading -------------------------------------------------------

    def reload(self, source: str = "sighup") -> tuple[bool, str]:
        """Load what is on disk into the running daemon; keep the running
        config when it does not load. -> (ok, message)."""
        with self._lock:
            disk = self.read_disk()
            try:
                if disk["channels"] is None or (disk["senders_text"] is not None and disk["senders"] is None):
                    raise ApiError(422, "invalid", "; ".join(disk["problems"]) or "config does not load")
                candidate = self._candidate(disk["channels"], disk["senders"])
            except ApiError as e:
                log.error("config reload (%s) refused, keeping the running config: %s", source, e)
                return False, str(e)
            self.live.swap(candidate, etag=disk["etag"], source=source)
        log.info("config reloaded (%s): %d channel(s), %d sender(s), etag=%s", source,
                 len(candidate.channels), len(candidate.senders), disk["etag"])
        return True, "reloaded"


# --- HTTP ------------------------------------------------------------------------


def make_handler(service: ConfigService, token: str):
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "slackcc-config"
        sys_version = ""

        def log_message(self, fmt, *args):  # method + path + status only; never a body
            log.debug("config api: " + fmt, *args)

        def _send(self, status: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            got = (self.headers.get("Authorization") or "").encode()
            return hmac.compare_digest(got, expected)

        def _route(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if path == "/healthz" and method == "GET":
                    return self._send(200, {"ok": True})
                if path != "/config":
                    raise ApiError(404, "not_found", "No such route.")
                if not self._authorized():
                    raise ApiError(401, "unauthorized", "Send Authorization: Bearer <SLACKCC_CONFIG_TOKEN>.")
                if method == "GET":
                    return self._send(200, service.get())
                if method != "PUT":
                    raise ApiError(405, "method_not_allowed", "GET or PUT.")
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    raise ApiError(415, "unsupported_media_type", "Send application/json.")
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    raise ApiError(413, "too_large", "The request body is too large.")
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode() or "null")
                except ValueError:
                    raise ApiError(400, "bad_request", "The request body was not valid JSON.") from None
                return self._send(200, service.put(body))
            except ApiError as e:
                return self._send(e.status, e.body)
            except Exception:  # noqa: BLE001 - never a stack trace on the wire
                log.exception("config api: %s %s failed", method, path)
                return self._send(500, {"ok": False, "error": "internal_error",
                                        "message": "Something went wrong in slackcc."})

        def do_GET(self):  # noqa: N802 - http.server's naming
            self._route("GET")

        def do_PUT(self):  # noqa: N802
            self._route("PUT")

        def do_POST(self):  # noqa: N802
            self._route("POST")

        def do_PATCH(self):  # noqa: N802
            self._route("PATCH")

        def do_DELETE(self):  # noqa: N802
            self._route("DELETE")

    return Handler


def serve(service: ConfigService, token: str, *, host: str = "127.0.0.1",
          port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Start the API on a daemon thread; the caller keeps the server to close it."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the config API binds loopback only")
    server = ThreadingHTTPServer((host, port), make_handler(service, token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="config-api", daemon=True).start()
    log.info("config API listening on %s:%d", host, port)
    return server


def install_sighup(service: ConfigService) -> None:
    """SIGHUP = reload from disk (e.g. `systemctl --user reload slackcc` with
    ExecReload=/bin/kill -HUP $MAINPID). The reload runs off the signal
    handler, on its own thread."""
    def _on_hup(_signum, _frame):
        threading.Thread(target=service.reload, kwargs={"source": "sighup"},
                         name="config-reload", daemon=True).start()
    signal.signal(signal.SIGHUP, _on_hup)


def start_from_env(live: LiveSettings) -> ConfigService:
    """What run() calls: the service, SIGHUP, and -- when SLACKCC_CONFIG_TOKEN
    is set -- the HTTP API on SLACKCC_CONFIG_PORT (default 8643)."""
    service = ConfigService(live, backup_dir=live.current.sessions_path.parent / "config-backups")
    service.mark_loaded_from_disk()
    install_sighup(service)
    token = os.environ.get("SLACKCC_CONFIG_TOKEN") or ""
    if not token:
        log.info("SLACKCC_CONFIG_TOKEN not set: config API off (SIGHUP reload still works)")
        return service
    if len(token) < 24:
        log.error("SLACKCC_CONFIG_TOKEN is shorter than 24 characters: config API off")
        return service
    try:
        port = int(os.environ.get("SLACKCC_CONFIG_PORT") or DEFAULT_PORT)
        serve(service, token, port=port)
    except (OSError, ValueError):
        # The bridge itself matters more than its admin API: log and carry on.
        log.exception("config API did not start; the bridge runs without it")
    return service
