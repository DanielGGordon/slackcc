"""`slack-send`: post a message into a channel/thread from the outside.

The primitive for *agent-initiated* outbound. Pair with `slack-wait-reply` to run
an iterate-with-a-human loop, and `--claim` to keep the daemon out of that thread.
"""

from __future__ import annotations

import argparse
import json
import sys

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .claims import ClaimStore
from .paths import claims_path, resolve_token


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a message to a Slack channel.")
    parser.add_argument("channel", help="Channel id (e.g. C0123...) or #name")
    parser.add_argument("text", help="Message text")
    parser.add_argument("--thread", dest="thread_ts", default=None,
                        help="Reply in this thread_ts instead of starting a new thread")
    parser.add_argument("--claim", action="store_true",
                        help="Mark this thread agent-owned so the daemon won't auto-reply in it")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Print {ts, channel, thread_ts} as JSON (for scripting)")
    args = parser.parse_args()

    token = resolve_token()
    if not token:
        print("No SLACK_BOT_TOKEN (env or slack project .env)", file=sys.stderr)
        return 2

    client = WebClient(token=token)
    try:
        resp = client.chat_postMessage(
            channel=args.channel, text=args.text, thread_ts=args.thread_ts
        )
    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}", file=sys.stderr)
        return 1

    ts = resp["ts"]
    channel = resp["channel"]
    thread_root = args.thread_ts or ts  # a new top-level post is its own thread root
    if args.claim:
        ClaimStore(claims_path()).claim(channel, thread_root)

    if args.as_json:
        print(json.dumps({"ts": ts, "channel": channel, "thread_ts": thread_root}))
    else:
        suffix = " (claimed)" if args.claim else ""
        print(f"ok ts={ts} channel={channel} thread={thread_root}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
