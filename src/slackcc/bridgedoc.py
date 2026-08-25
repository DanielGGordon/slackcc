"""The Slack bridge protocol: one document, shipped with the package.

The protocol (reply semantics, the outbound CLIs, the trust model) never changes
between turns, so it has no business in the per-turn prompt. It ships as package
data here and reaches the agent one of two ways, depending on backend:

- `claude` backend: rendered into `--append-system-prompt`. Free, invisible in
  the channel, present on every turn. Nothing to install.
- `t3` backend: `thread.turn.start` has no system-prompt field, and T3 spawns
  its sessions with setting sources `user,project,local` -- so the project's own
  `CLAUDE.md` is the free channel. `slackcc init-project` writes this document
  into a marker-delimited section there. Until that's done, `app.py` falls back
  to injecting it inline on the first turn of each thread.

Nothing here depends on the operator's personal dotfiles: the document lives in
the package, and the copy in a project's CLAUDE.md is written from it.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

MARK_BEGIN = "<!-- slackcc:begin -- managed by `slackcc init-project`; edits are overwritten -->"
MARK_END = "<!-- slackcc:end -->"

DOC_PATH = Path(__file__).resolve().parent / "data" / "slack-bridge.md"


def cli_dir() -> str:
    """Absolute directory holding `slack-send`/`slack-upload`/`slack-wait-reply`.

    Interpolated into the document because a T3-spawned session inherits the
    daemon's environment but not necessarily a PATH that includes it. Prefer
    what's actually resolvable; fall back to the running interpreter's bin dir,
    which is where a venv install puts the console scripts."""
    found = shutil.which("slack-send")
    if found:
        return str(Path(found).resolve().parent)
    return str(Path(sys.executable).resolve().parent)


def render() -> str:
    """The protocol with real paths baked in."""
    return DOC_PATH.read_text().replace("{cli_dir}", cli_dir()).strip()


def section() -> str:
    """The protocol wrapped in the markers that make it re-writable in place."""
    return f"{MARK_BEGIN}\n{render()}\n{MARK_END}"


def _claude_md(project_dir: Path | str) -> Path:
    return Path(project_dir) / "CLAUDE.md"


def _installed_body(project_dir: Path | str) -> str | None:
    """Text between the markers in this project's CLAUDE.md, or None if there's
    no section (or no readable file). A truncated section (no end marker) reads
    through to EOF, mirroring what install() would replace."""
    path = _claude_md(project_dir)
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    _, has_begin, rest = content.partition(MARK_BEGIN)
    if not has_begin:
        return None
    body, _, _ = rest.partition(MARK_END)
    return body


def is_installed(project_dir: Path | str) -> bool:
    """Has the protocol been written into this project's CLAUDE.md?

    Only the begin marker is checked: a file that has it will be rewritten (not
    appended to) by install(), so a truncated section still counts as installed
    rather than silently duplicating."""
    return _installed_body(project_dir) is not None


# An absolute directory sitting in front of one of our CLIs, e.g. the
# `/…/.venv/bin` that render() baked in. Masked back to the placeholder so two
# installs that differ only in where `slack-send` lives compare equal.
_CLI_DIR = re.compile(r"(?:/[^\s/`$()]+)+(?=/slack-(?:send|upload|wait-reply)\b)")


def _normalise(text: str) -> str:
    return _CLI_DIR.sub("{cli_dir}", text.strip())


def is_current(project_dir: Path | str) -> bool:
    """Is the installed section the protocol this package ships?

    A project that ran `slackcc init-project` under an older release still has
    that release's protocol in its CLAUDE.md (e.g. the old visible routing line
    instead of the HTML comment). The daemon uses this, not is_installed(), to
    decide whether the agent can be trusted to know the current protocol; a
    stale copy is treated like a missing one until the operator re-runs
    init-project.

    The baked-in CLI directory is deliberately ignored: the daemon's PATH and
    the operator's init-project shell may resolve `slack-send` differently, and
    that must not make every project read as stale forever."""
    body = _installed_body(project_dir)
    return body is not None and _normalise(body) == _normalise(DOC_PATH.read_text())


def install(project_dir: Path | str) -> str:
    """Write/refresh the managed section in `<project_dir>/CLAUDE.md`.

    Idempotent: replaces an existing section in place, appends if there's none,
    creates the file if it doesn't exist. Returns "created", "updated" or
    "unchanged" so callers can report honestly."""
    path = _claude_md(project_dir)
    new_section = section()

    if not path.exists():
        path.write_text(f"{new_section}\n")
        return "created"

    old = path.read_text()
    if MARK_BEGIN not in old:
        joiner = "" if old.endswith("\n\n") else "\n" if old.endswith("\n") else "\n\n"
        path.write_text(f"{old}{joiner}{new_section}\n")
        return "updated"

    head, _, rest = old.partition(MARK_BEGIN)
    # No end marker means someone truncated the file mid-section; replacing
    # through to EOF is the only safe read of that, and beats appending a
    # second copy.
    _, has_end, after = rest.partition(MARK_END)
    tail = after if (has_end and after) else "\n"
    updated = head + new_section + tail
    if updated == old:
        return "unchanged"
    path.write_text(updated)
    return "updated"
