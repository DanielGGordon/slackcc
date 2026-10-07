"""The customer voice: what a non-coder in a `audience: "customer"` channel sees.

In such a channel the agent ends its final message with a marked block; only
that block reaches Slack, and the owner sees technical summary + block together
in T3. This module holds the pieces every delivery path shares (the Slack turn
in app.py, the GUI mirror in t3_mirror.py):

- `extract_block`  -- the block after the LAST marker line of a final message.
- `leak_reasons`   -- a deterministic, deliberately NARROW check that text bound
  for a customer has no code, paths, PR/commit refs or stack traces in it.
- `resolve_final`  -- block + gate -> what to post, or why not.
- `Ledger`         -- append-only record of what Slack was told, per thread.
- `alert_owners`   -- DM the owners when the fallback had to kick in.

Only ever feed these the final assistant message of a run. User-role messages
(subagent reports, agent sends) can contain anything, including the marker.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .outbound import scrub
from .t3 import typed_by_human

log = logging.getLogger(__name__)

# A markdown heading, so the T3 GUI renders it (it hides HTML comments).
MARKER = "### Message for the customer"
# Any heading level, optional trailing colon, any case: the agent will drift.
_MARKER_LINE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*message for the customer[ \t]*:?[ \t]*$",
                          re.IGNORECASE | re.MULTILINE)

# Posted instead of a block we can't use. Must not claim anything succeeded.
HOLDING_LINE = ("I've finished working on this and I'm putting together an update "
                "for you.")


def has_marker(text: str) -> bool:
    return _MARKER_LINE.search(text or "") is not None


def extract_block(final_text: str) -> str | None:
    """Everything after the last marker line, stripped; None if there is no
    marker or nothing after it. Last wins: a technical summary may quote the
    marker, and the block is by contract written last."""
    last = None
    for last in _MARKER_LINE.finditer(final_text or ""):
        pass
    if last is None:
        return None
    return final_text[last.end():].strip() or None


# --- leak gate -------------------------------------------------------------

_CODE_EXT = ("py|jsx?|tsx?|mjs|cjs|json|ya?ml|toml|md|css|scss|html?|sh|sql|go|rs|java|"
             "kt|rb|php|cpp|vue|svelte|lock|env|ini|cfg|gradle|xml")
_GATE: list[tuple[str, re.Pattern]] = [
    ("code block", re.compile(r"```|~~~")),
    # a path: dir segments then a file with a code extension ...
    ("file path", re.compile(
        rf"(?<![\w/.-])/?(?:[\w.-]+/)+[\w.-]+\.(?:{_CODE_EXT})\b", re.IGNORECASE)),
    # ... a bare file name (not .js: "Node.js", "Next.js" are product names) ...
    ("file name", re.compile(
        rf"(?<![\w/.-])[\w-]+\.(?:{_CODE_EXT.replace('jsx?|', 'jsx|')})\b", re.IGNORECASE)),
    # ... a conventional source dir, or a system path. "/settings" is neither.
    ("file path", re.compile(
        r"(?<![\w/.-])(?:src|lib|node_modules|components|tests|dist|packages|\.github|\.git)/[\w.-]+"
        r"|(?<![\w.])(?:/(?:home|usr|etc|var|opt|tmp|srv|root|workspace|Users|mnt)/|~/)[\w.-]")),
    ("pull request reference", re.compile(
        r"\b(?:PR|(?i:pull[ -]request))s?\s*#?\s*\d+|\(#\d{2,}\)"
        r"|github\.com/\S+/(?:pull|commit|issues)/")),
    # 7+ hex chars with BOTH a digit and a letter: "defaced" and 1234567 are not SHAs.
    ("commit hash", re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")),
    ("stack trace", re.compile(
        r"\bTraceback\b|^\s*at\s+[\w.$<>]+\s*\([^)\n]*:\d+(?::\d+)?\)"
        r"|\bFile \"[^\"\n]+\", line \d+|\b[A-Z][A-Za-z]*(?:Error|Exception):",
        re.MULTILINE)),
    # branch names are kebab-case after the prefix: "feature/bug" is just prose.
    ("branch name", re.compile(
        r"(?<![\w/.-])(?:slack|feature|fix|hotfix|bugfix|chore|release|claude|codex|t3|origin)"
        r"/[\w.]*[-_][\w./-]*")),
]


def leak_reasons(text: str) -> list[str]:
    """Why `text` must not go to a customer; empty means it may. Narrow on
    purpose: a false positive costs a holding line + an owner DM, a miss leaks
    code to a non-coder -- but ordinary prose has to sail through."""
    reasons: list[str] = []
    for reason, pattern in _GATE:
        if pattern.search(text or "") and reason not in reasons:
            reasons.append(reason)
    return reasons


@dataclass(frozen=True)
class Outcome:
    """What a run's final message means for the customer.

    kind: "block" (post `text`), "no_block" (agent wrote none), or "gated"
    (a block was there but tripped the gate; `reasons` says how)."""

    kind: str
    text: str = ""
    reasons: tuple[str, ...] = ()

    @property
    def why(self) -> str:
        if self.kind == "no_block":
            return f"the final message has no `{MARKER}` block"
        if self.kind == "gated":
            return "the customer block looks technical (" + ", ".join(self.reasons) + ")"
        return ""


def resolve_final(final_text: str) -> Outcome:
    block = extract_block(final_text)
    if block is None:
        return Outcome("no_block")
    reasons = leak_reasons(block)
    if reasons:
        return Outcome("gated", reasons=tuple(reasons))
    return Outcome("block", text=block)


# --- #agent: Dan talking to the agent only ---------------------------------------

# "#agent", then whitespace and/or a colon (or nothing). "#agentic" is not it.
_PRIVATE = re.compile(r"\s*#agent(?![\w-])[\s:]*", re.IGNORECASE)


def is_private(text: str) -> bool:
    return _PRIVATE.match(text or "") is not None


def private_run_ids(messages: list[dict], runs: list[dict]) -> set[str]:
    """Runs started (or steered into) by a human-typed `#agent` message: their
    replies are for Dan alone. Found the way backend_t3 finds its own run: by
    the message's `runId`, or the run's `userMessageId`. Pass the FULL
    projection lists, not a window: a long run's first message must still count."""
    private = {m["id"] for m in messages
               if m.get("role") == "user" and m.get("id") and typed_by_human(m)
               and is_private(m.get("text"))}
    return ({m["runId"] for m in messages if m.get("id") in private and m.get("runId")}
            | {r["id"] for r in runs if r.get("userMessageId") in private})


# --- progress ----------------------------------------------------------------

_GENERIC_NARRATION = "Working through it now."


def progress_text(narration: str, elapsed_secs: float, steps: int, *,
                  clip: int = 300) -> str:
    """The customer-safe placeholder body: the agent's newest narration if it
    passes the gate (else a generic line) plus elapsed time and step count.
    Minute granularity, so the text only changes when something real does."""
    # Gate the text as the agent wrote it: flattening first would defeat the
    # line-anchored stack-trace patterns. A message carrying the customer-block
    # marker is the technical final, never narration.
    raw = narration or ""
    narration = " ".join(raw.split())
    if not narration or leak_reasons(raw) or has_marker(raw):
        narration = _GENERIC_NARRATION
    elif len(narration) > clip:
        narration = narration[: clip - 1].rstrip() + "…"
    mins = int(elapsed_secs // 60)
    when = "less than a minute in" if mins < 1 else \
        f"about {mins} minute{'s' if mins != 1 else ''} in"
    status = f"Still working on it - {when}"
    if steps:
        status += f", {steps} step{'s' if steps != 1 else ''} so far"
    return f":hourglass_flowing_sand: {narration}\n_{status}._"


# --- ledger of what Slack was told -----------------------------------------------

_NOTE_N = 4
_NOTE_CLIP = 300
_FILE_KEEP = 100   # lines kept when a thread's file is trimmed ...
_FILE_MAX = 200    # ... which happens once it passes this many
_SOURCE_LABEL = {"block": "you", "holding": "you (holding message)", "dan_forward": "Dan"}


def thread_key(channel: str, thread_ts: str) -> str:
    """Ledger key for a Slack thread -- also the id the bridge gives its T3 thread."""
    return f"slack-{channel}-{thread_ts.replace('.', '-')}"


class Ledger:
    """Per-thread append-only JSONL of what the customer's Slack thread was told
    at the final level (agent blocks, holding lines, Dan's forwarded messages;
    not live progress). Entries: {ts, source, text, slack_ts?, t3_message_id?}.
    It is Dan's audit trail of Slack's view and, via `note()`, the agent's
    reminder of it after its context was compacted."""

    def __init__(self, directory: Path):
        self._dir = directory
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        return self._dir / (re.sub(r"[^A-Za-z0-9._-]", "_", key) + ".jsonl")

    def append(self, key: str, source: str, text: str, *, slack_ts: str | None = None,
               t3_message_id: str | None = None) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source": source, "text": text}
        if slack_ts:
            entry["slack_ts"] = slack_ts
        if t3_message_id:
            entry["t3_message_id"] = t3_message_id
        with self._lock:
            path = self._path(key)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a") as f:
                    f.write(json.dumps(entry) + "\n")
                lines = path.read_text().splitlines()
                if len(lines) > _FILE_MAX:
                    path.write_text("\n".join(lines[-_FILE_KEEP:]) + "\n")
            except OSError:
                log.warning("customer ledger write failed for %s", key, exc_info=True)

    def recent(self, key: str, n: int = _NOTE_N) -> list[dict]:
        with self._lock:
            try:
                lines = self._path(key).read_text().splitlines()[-n:]
            except OSError:
                return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def note(self, key: str) -> str:
        """The short reminder prepended to a customer-mode prompt; "" if the
        customer has been told nothing yet."""
        entries = self.recent(key)
        if not entries:
            return ""
        lines = []
        for e in entries:
            text = " ".join(str(e.get("text") or "").split())
            if len(text) > _NOTE_CLIP:
                text = text[: _NOTE_CLIP - 1].rstrip() + "…"
            lines.append(f"- {_SOURCE_LABEL.get(e.get('source'), 'you')}: {text}")
        return ("[What the customer has been told so far in Slack (oldest first):\n"
                + "\n".join(lines) + "]")


# --- fallback: tell the owners ---------------------------------------------------

_RAW_CLIP = 2500


def alert_owners(client, settings, *, channel: str, thread_ts: str, reason: str,
                 raw: str) -> None:
    """DM every owner that a customer-bound message could not be sent as-is,
    with the agent's raw final text and links to the thread. Best effort."""
    owner_ids = settings.owner_ids()
    if not owner_ids:
        return
    cfg = settings.channel(channel)
    links = []
    gui = settings.t3_thread_url(thread_key(channel, thread_ts))
    if gui:
        links.append(f"<{gui}|Open in T3>")
    try:
        link = client.chat_getPermalink(channel=channel, message_ts=thread_ts).get("permalink")
        if link:
            links.append(f"<{link}|Slack thread>")
    except Exception:  # noqa: BLE001 - the link is a nicety
        log.warning("could not get slack permalink", exc_info=True)
    raw = (raw or "").strip().replace("```", "'''")
    if len(raw) > _RAW_CLIP:
        raw = raw[:_RAW_CLIP] + "…"
    text = scrub(
        f":mailbox_with_mail: Customer update needs you in #{cfg.project if cfg else channel}: "
        f"{reason}. I told the customer only that an update is coming.\n"
        f"Thread `{channel}` / `{thread_ts}`" + (" · " + " · ".join(links) if links else "")
        + f"\nThe agent's final message:\n```{raw or '(empty)'}```")[0]
    for owner_id in owner_ids:
        try:
            client.chat_postMessage(channel=owner_id, text=text)
        except Exception:  # noqa: BLE001 - paging must not break delivery
            log.warning("customer-alert DM to %s failed", owner_id, exc_info=True)
