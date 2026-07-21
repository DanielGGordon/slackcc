"""Entry point: `python -m slackcc` or the `slackcc` console script."""

from __future__ import annotations

import logging
import sys

from .app import run
from .config import load_settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
