# slackcc — Slack ↔ Claude Code / T3 Code bridge

A Socket Mode daemon that lets people in a configured Slack channel talk to an
AI coding agent **autonomously** (back-and-forth, no human in the loop), scoped
to a single project per channel — with per-sender permissions and a local
prompt-protection screen in front of guests.

## How it works

```
Slack channel ──(Socket Mode)──> slackcc daemon ──┬──> backend "claude": claude -p in project cwd
                    │                             └──> backend "t3": HTTP dispatch into a T3 Code
                    │                                  project thread (live in the T3 GUI, one
                    │                                  Slack thread <-> one T3 thread, mirrored
                    │                                  bidirectionally)
                    └── guests only: blocking pps check (local LLM judge) before dispatch
```

- **Scoping:** the bot only reacts in channels listed in `config/channels.json`.
  Each channel maps to a project (`cwd` + persona for the `claude` backend, or a
  T3 project id for the `t3` backend).
- **Sender permissions** (`config/senders.json`): owners run full-access;
  everyone else is a guest — messages are screened by pps, the Prompt
  Protection Service (a local sandboxed LLM judge, blocking,
  fail-closed) and their turns run `approval-required` so risky tool calls wait
  for owner approval. A PreToolUse guard hook in each bridged project
  deterministically blocks mass deletion, secret-file reads, and env dumps
  regardless of what any model decides.
- **Bidirectional mirror (t3 backend):** messages typed into the T3 GUI on a
  Slack-originated thread are posted back into the Slack thread
  ("_Owner said to the agent:_ …"), and replies land in both places.
- **Outbound scrubbing:** every message posted to Slack is scanned and
  secret-shaped strings (tokens, keys, JWTs) are redacted.
- **Injection hygiene (baseline):** untrusted Slack text is fenced in
  `<<<EXTERNAL_UNTRUSTED_CONTENT>>>` markers and the system prompt tells the
  model to treat it as data.

## 1. Create the Slack app (one-time)

You need a **bot token** (`xoxb-`) and an **app-level token** (`xapp-`).

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
   Name it, pick your workspace.
2. **Socket Mode** (left sidebar) → toggle **Enable Socket Mode**. When prompted,
   create an **App-Level Token** with the `connections:write` scope. Copy it —
   this is your `SLACK_APP_TOKEN` (`xapp-...`).
3. **OAuth & Permissions** → **Bot Token Scopes**, add:
   - `chat:write` — post messages
   - `app_mentions:read` — see @mentions
   - `channels:history` — read messages in public channels
   - `groups:history` — read messages in private channels (if you'll use one)
   - `channels:read` — resolve channel info
   *(For later: `files:read` for voice notes/attachments.)*
4. **Event Subscriptions** (left sidebar) → **Enable Events**. (No Request URL
   needed — Socket Mode delivers events.) Under **Subscribe to bot events**, add:
   - `message.channels`, `message.groups`, `app_mention`
5. **Install App** (or **OAuth & Permissions** → **Install to Workspace**).
   Approve. Copy the **Bot User OAuth Token** — this is `SLACK_BOT_TOKEN`
   (`xoxb-...`). *(Re-install if you change scopes later.)*
6. In Slack, open the target channel and **invite the bot**: type
   `/invite @YourBotName`.
7. Get the **channel id**: right-click the channel → *View channel details* →
   it's at the bottom (e.g. `C0123ABC`), or it's the last path segment of the
   channel URL.

## 2. Configure

```bash
cd ~/projects/slack
cp .env.example .env                          # fill in the tokens
cp config/channels.example.json config/channels.json   # map channel id -> project
cp config/senders.example.json config/senders.json     # owner + guest permissions
```

Edit `config/channels.json` — set the real channel id and either the T3 project
id (`backend: "t3"`) or the project `cwd` + `persona` + `allowed_tools`
(`backend: "claude"`; keep it least-privilege for friend-facing channels).
Put your own Slack member id in `config/senders.json` as `role: "owner"` —
everyone else defaults to a screened, approval-required guest.

For the `t3` backend, also set `SLACKCC_T3_URL`/`SLACKCC_T3_TOKEN` in `.env`,
and run the pps judge (separate repo/service) at `SLACKCC_PPS_URL` if you have
guest senders — guests fail closed when it's unreachable.

## 3. Install & run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .

./scripts/run.sh        # loads .env, starts the daemon
```

Now post a message in the configured channel — the bot replies in-thread, and
each thread stays one continuous Claude Code conversation.

## Agent-initiated outbound

To have Claude Code (or you) post into a channel from a project:

```bash
slack-send C0123ABC "Here's the latest flyer draft — thoughts?"
slack-send C0123ABC "follow-up" --thread 1700000000.000100
```

## Layout

```
src/slackcc/
  app.py        Socket Mode daemon: routing, loop-prevention, pps gate
  backend.py    claude -p runner (the swappable agent seam)
  backend_t3.py T3 Code turn runner (dispatch + poll over HTTP)
  t3.py         T3 HTTP client + mirror ledger store
  t3_mirror.py  background poller: T3 GUI turns -> Slack thread
  pps.py        client for the Prompt Protection Service judge
  outbound.py   secret scrubbing for everything posted to Slack
  config.py     env + channels.json + senders.json loading, per-sender policy
  sessions.py   thread -> session/thread id store (resume continuity)
  sanitize.py   baseline injection fencing
  send_cli.py   `slack-send` outbound CLI
config/channels.example.json
config/senders.example.json
BACKLOG.md
```
