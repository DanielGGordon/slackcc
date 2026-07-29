"""Entry point: `python -m slackcc` or the `slackcc` console script.

No subcommand runs the daemon (the original behavior). `slackcc init-project`
writes the bridge protocol into a project's CLAUDE.md -- see bridgedoc.py.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import bridgedoc
from .app import run
from .config import load_channels, load_settings


def _init_project(argv: list[str]) -> int:
    """Install the bridge protocol into one project, or into every project a
    channel is configured to route to."""
    dirs = [Path(a).expanduser() for a in argv]
    if not dirs:
        config_path = Path(os.environ.get("SLACKCC_CONFIG", "./config/channels.json"))
        try:
            channels = load_channels(config_path)
        except (OSError, ValueError, KeyError) as e:
            print(f"config error ({config_path}): {e}", file=sys.stderr)
            print("usage: slackcc init-project [PROJECT_DIR ...]", file=sys.stderr)
            return 2
        # dict.fromkeys: two channels can share a cwd; write it once.
        dirs = [Path(c) for c in dict.fromkeys(
            cfg.cwd for cfg in channels.values() if cfg.backend == "t3")]
        if not dirs:
            print("no t3-backend channels configured; nothing to do. "
                  "(The claude backend gets the protocol via its system prompt.)")
            return 0

    failed = False
    for d in dirs:
        if not d.is_dir():
            print(f"not a directory: {d}", file=sys.stderr)
            failed = True
            continue
        try:
            action = bridgedoc.install(d)
        except OSError as e:
            print(f"{d}/CLAUDE.md: {e}", file=sys.stderr)
            failed = True
            continue
        print(f"{action}: {d}/CLAUDE.md")

    if not failed:
        print("\nCommit CLAUDE.md — T3 threads can run in a git worktree, which "
              "only sees committed files.")
    return 1 if failed else 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    argv = sys.argv[1:]
    if argv and argv[0] == "init-project":
        return _init_project(argv[1:])
    if argv:
        print(f"unknown arguments: {' '.join(argv)}", file=sys.stderr)
        print("usage: slackcc [init-project [PROJECT_DIR ...]]", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
    except (RuntimeError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if not settings.channels:
        print("warning: no channels configured; the bot will ignore everything.",
              file=sys.stderr)

    run(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
