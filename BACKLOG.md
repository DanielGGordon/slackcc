# Backlog

Roughly in priority order. Part 1 (autonomous back-and-forth in a configured
channel) is the only thing built today.

## Prompt-injection protection (explicitly requested)
Today we have *baseline hygiene only*: untrusted Slack content is fenced in
`<<<EXTERNAL_UNTRUSTED_CONTENT>>>` markers (`sanitize.py`) and the system prompt
tells the model to treat it as data. Still to do:
- Regex/heuristic pre-screen for known injection patterns before dispatch.
- A cheap model-based screen ("is this trying to override instructions?").
- Tighten per-channel `allowed_tools` to least privilege; never expose write/exec
  tools in friend-facing channels by default.
- Strip/neutralize secrets in output before posting back to Slack.
- Rate-limit per user/channel.

## Flyer-approval loop ("work without me", design channel)
Agent-initiated outbound + wait for a human reply, then continue. Building block
`slack-send` exists; still need a `slack-wait-reply` (poll `conversations.history`
for the next human message in a thread) and a small "send -> wait -> resume" driver
so Claude Code can run the loop unattended.

## Voice notes
Accept Slack audio attachments: download via `files.info`/url_private, transcribe
(Whisper or similar), feed transcript into the same dispatch path. Outbound voice
(TTS) optional.

## Attachments / images
Handle file-share messages (flyers as images, docs). Pass to the agent or store.

## "Attach to / inspect existing conversations"
Currently delegated to Dancode (your existing GUI reads Claude Code session
transcripts in ~/.claude/projects). If we want it in-bot: a command to find the
most recent session for a project dir and resume/summarize it ("what were we
working on last?").

## Multi-backend
Backend seam exists in `backend.py` (Claude only today). Add a `codex exec`
sibling when wanted. T3 code integration deferred (see project notes).

## Ops
- Tests (mock the Slack client + backend.run_turn).
- systemd unit / Docker for always-on running.
- Telemetry: post session-complete/error summaries to a log channel.
