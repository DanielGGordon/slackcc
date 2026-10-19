"""Bridge-driven context watch for t3-backed Slack threads.

Sonnet 5.5 runs with a 1M-token window and T3's Claude settings are left alone
(no `autoCompactWindow`), so Claude Code won't compact until the window is nearly
gone. Long Slack threads get a friendlier, earlier hand-off instead, driven from
here:

  1. A turn finishes with the thread's context at or past the threshold
     (default 200k tokens): the reply gets a heads-up appended, and the thread
     is marked.
  2. On the thread's next message the bridge sends `/compact` into T3 first,
     then runs the message (`backend_t3.run_turn(compact_first=True)`).
  3. `app.py` clears the mark once that compact has been sent, and any turn
     that reports a count back under the threshold clears it too.

Only the *marked* set is persisted (a thread id per entry), so a restart
between the warning and the next message still compacts.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

DEFAULT_WARN_TOKENS = 200_000


def _k(tokens: int) -> str:
    return f"{round(tokens / 1000)}k"


def warning_text(tokens: int, *, customer: bool = False) -> str:
    """The heads-up appended to the reply that crossed the threshold."""
    if customer:
        return ("_Heads-up: this conversation has gotten quite long. When you send your "
                "next message I'll automatically tidy it up (older detail gets condensed) "
                "so we can keep going. You can stay right here, or start a fresh "
                "thread if you'd rather begin clean._")
    return (f":warning: _This conversation has reached about {_k(tokens)} tokens of context. "
            "On your next message I'll automatically compact it (older detail gets "
            "summarized) so it can keep going. You can stay in this thread, or start a "
            "new one for a clean slate._")


def append_warning(reply: str, tokens: int, *, customer: bool = False) -> str:
    return f"{reply}\n\n{warning_text(tokens, customer=customer)}" if reply else \
        warning_text(tokens, customer=customer)


class ContextWatch:
    """Which T3 threads have been warned and are due a compaction."""

    def __init__(self, path: Path, warn_tokens: int = DEFAULT_WARN_TOKENS):
        self._path = path
        self.warn_tokens = warn_tokens
        self._lock = threading.Lock()
        try:
            data = json.loads(path.read_text())
            self._marked: dict[str, int] = {
                str(k): int(v) for k, v in (data.get("marked") or {}).items()}
        except (FileNotFoundError, json.JSONDecodeError, AttributeError, ValueError):
            self._marked = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"marked": self._marked}, indent=1))
        tmp.replace(self._path)

    def due(self, thread_id: str) -> bool:
        """Warned earlier -- compact before running this thread's next message."""
        with self._lock:
            return thread_id in self._marked

    def clear(self, thread_id: str) -> None:
        with self._lock:
            if self._marked.pop(thread_id, None) is not None:
                self._save()

    def observe(self, thread_id: str, tokens: int | None) -> bool:
        """Record the context size a finished turn reported. Returns True when
        this turn should carry the warning (newly at/over the threshold and not
        already marked). A count back under the threshold clears the mark."""
        if tokens is None:
            return False
        with self._lock:
            if tokens < self.warn_tokens:
                if self._marked.pop(thread_id, None) is not None:
                    self._save()
                return False
            if thread_id in self._marked:
                return False
            self._marked[thread_id] = tokens
            self._save()
            return True
