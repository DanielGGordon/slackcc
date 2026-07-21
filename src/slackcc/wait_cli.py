"""`slack-wait-reply`: block until the next human reply appears in a Slack thread,
then print it as JSON and exit. This is the second half of the agent-driven
outreach loop (the first half is `slack-send`).

Typical loop a Claude session runs:
    ts=$(slack-send C123 "draft v1 ..." --claim --json | jq -r .ts)
    reply=$(slack-wait-reply C123 --thread "$ts")        # blocks for the human
    # revise based on $reply, then:
    slack-send C123 "v2 ..." --thread "$ts"
    reply=$(slack-wait-reply C123 --thread "$ts" --after <last_ts>)
    ... until approved, then: slack-wait-reply ... --release  (frees the daemon)
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .claims import ClaimStore
from .paths import claims_path, resolve_token


def main() -> int:
    p = argparse.ArgumentParser(
        description="Block until the next human reply in a Slack thread; print it as JSON."
    )
    p.add_argument("channel", help="Channel id (e.g. C0123...)")
    p.add_argument("--thread", dest="thread_ts", required=True, help="thread_ts to watch")
    p.add_argument("--after", default=None,
                   help="Only return messages newer than this ts (default: the thread root)")
    p.add_argument("--timeout", type=int, default=1800, help="Give up after N seconds (default 1800)")
    p.add_argument("--interval", type=int, default=3, help="Poll every N seconds (default 3)")
    p.add_argument("--claim", action="store_true",
                   help="Claim the thread before waiting (daemon won't auto-reply in it)")
    p.add_argument("--release", action="store_true",
                   help="Release the claim once a reply arrives")
    args = p.parse_args()

    token = resolve_token()
    if not token:
        print("No SLACK_BOT_TOKEN (env or slack project .env)", file=sys.stderr)
        return 2

    client = WebClient(token=token)
    try:
        bot_id = client.auth_test()["user_id"]
    except SlackApiError as e:
        print(f"Slack error (auth_test): {e.response['error']}", file=sys.stderr)
        return 1

    claims = ClaimStore(claims_path())
    if args.claim:
        claims.claim(args.channel, args.thread_ts)

    after = float(args.after) if args.after else float(args.thread_ts)
    deadline = time.time() + args.timeout

    while time.time() < deadline:
        try:
            resp = client.conversations_replies(
                channel=args.channel, ts=args.thread_ts, oldest=str(after), limit=200
            )
        except SlackApiError as e:
            print(f"Slack error (conversations.replies): {e.response['error']}", file=sys.stderr)
            return 1

        for msg in resp.get("messages", []):
            if float(msg.get("ts", 0)) <= after:
                continue
            if msg.get("user") == bot_id or msg.get("bot_id") or msg.get("subtype"):
                continue  # skip our own posts and system messages
            out = {"ts": msg["ts"], "user": msg.get("user"), "text": msg.get("text", "")}
            if args.release:
                claims.release(args.channel, args.thread_ts)
            print(json.dumps(out))
            return 0

        time.sleep(args.interval)

    print(json.dumps({"timeout": True, "thread_ts": args.thread_ts}), file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
