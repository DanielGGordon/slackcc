# Backlog

Roughly in priority order. Part 1 (autonomous back-and-forth in a configured
channel) is the only thing built today.

## Customer voice: deferred
Built: per-channel `audience: "customer"` (marked block, leak gate, fallback,
plain progress, told-so-far ledger, `#agent`). Not built yet:
- LLM fallback translator: turn the raw final into a customer message when the
  agent wrote no block -- only worth it if the block-miss rate (owner DMs) is high.
- Agent-reported progress counts (e.g. "3 of 12 done") instead of the daemon's
  tool-step count; needs the agent to publish them, not TodoWrite scraping.
- WebSocket subscribe for the mirror and progress instead of polling.
- The deploy pipeline posting its own "it's live" line, so the agent's block can
  stay at "being published".
- A staging-environment workflow to replace guest auto-ship straight to production.

## Prompt-injection protection (explicitly requested)
Built: pps (the sandboxed LLM judge) screens every non-owner message before
dispatch and fails closed, so content that reaches the agent is trusted and
passes through unfenced. Fencing (`sanitize.py`) now only covers the ungated
case: a guest in `pps_mode: "log"`. Still to do:
- Regex/heuristic pre-screen for known injection patterns before dispatch.
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

## Session ids survive a backend/project change
`sessions.json` maps `channel:thread` -> session id with no note of which
backend or project wrote it. Move a channel from `t3` to `claude` (or to
another project) and an existing thread's next turn resumes an id that belongs
to the old one -- after a restart as much as after a config reload (a reload
only adds the case of a turn still in flight across the change). Fix: store
`{backend, project, id}` and treat a mismatch as "no session".
