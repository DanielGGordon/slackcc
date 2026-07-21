"""`slack-upload`: share a file (image/PDF/etc.) into a channel or thread.

Needs the `files:write` bot scope. Used so an agent can actually send the flyer
artifact, not just describe it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .claims import ClaimStore
from .paths import claims_path, resolve_token


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a file to a Slack channel.")
    parser.add_argument("channel", help="Channel id (e.g. C0123...)")
    parser.add_argument("file", help="Path to the file to upload")
    parser.add_argument("--comment", default=None, help="Message to post with the file")
    parser.add_argument("--thread", dest="thread_ts", default=None, help="Upload into this thread_ts")
    parser.add_argument("--claim", action="store_true",
                        help="Mark this thread agent-owned so the daemon won't auto-reply in it")
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"No such file: {args.file}", file=sys.stderr)
        return 2

    token = resolve_token()
    if not token:
        print("No SLACK_BOT_TOKEN (env or slack project .env)", file=sys.stderr)
        return 2

    client = WebClient(token=token)
    try:
        resp = client.files_upload_v2(
            channel=args.channel,
            file=args.file,
            initial_comment=args.comment,
            thread_ts=args.thread_ts,
        )
    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}", file=sys.stderr)
        return 1

    if args.claim and args.thread_ts:
        ClaimStore(claims_path()).claim(args.channel, args.thread_ts)

    file_id = (resp.get("file") or {}).get("id") or (resp.get("files") or [{}])[0].get("id")
    if args.as_json:
        print(json.dumps({"file_id": file_id, "channel": args.channel, "thread_ts": args.thread_ts}))
    else:
        print(f"ok file_id={file_id} channel={args.channel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
