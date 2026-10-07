# slackcc — Slack ↔ Claude Code / T3 Code bridge

A Socket Mode daemon that lets people in a configured Slack channel talk to an
AI coding agent **autonomously** (back-and-forth, no human in the loop), scoped
to a single project per channel — with per-sender permissions and a local
prompt-protection screen in front of guests.

## How it works

```
Slack channel ──(Socket Mode)──> slackcc daemon ──┬──> backend "claude": claude -p in project cwd
                    │                             └──> backend "t3": dispatch into a T3 Code
                    │                                  project thread (live in the T3 GUI, one
                    │                                  Slack thread <-> one T3 thread, mirrored
                    │                                  bidirectionally)
                    └── guests only: blocking pps check (local LLM judge) before dispatch
```

- **Scoping:** the bot only reacts in channels listed in `config/channels.json`.
  Each channel maps to a project (`cwd` + persona for the `claude` backend, or a
  T3 project id for the `t3` backend).
- **Mention gating (opt-in, `require_mention`):** by default a configured
  channel is the "works without me" loop -- every plain message gets a reply,
  autonomously. Set `require_mention: true` on a channel that's ALSO used for
  unrelated conversation (e.g. a channel that doubles as general chat) and the
  bot stays silent on a plain message unless it's actually @-mentioned; once it
  has replied in a thread, further replies in that same thread keep working
  without re-tagging every time.
- **Sender permissions** (`config/senders.json`): owners run full-access;
  everyone else is a guest — messages are screened by pps, the Prompt
  Protection Service (a local sandboxed LLM judge, blocking,
  fail-closed) and their turns run `approval-required` so risky tool calls wait
  for owner approval. A PreToolUse guard hook in each bridged project
  deterministically blocks mass deletion, secret-file reads, and env dumps
  regardless of what any model decides.
- **Approval waits are visible, not fatal:** when a guest turn parks on an
  approval (or an agent question) in the T3 GUI, the Slack placeholder says so,
  every owner gets one DM per request with "Open in T3" / thread links (set
  `SLACKCC_T3_GUI_URL`), and the per-turn `timeout` clock pauses -- it only
  counts the agent's own time. If nobody acts within the channel's
  `approval_timeout` (default 3600s) the bridge posts a "waiting for approval"
  note and lets go of the thread *without* interrupting the T3 turn, so it can
  still be approved later; the mirror delivers the eventual reply.
- **Owner override for screened messages:** when pps declines a guest's
  message (`enforce` mode), the bot posts the denial and then asks the owners
  in the same thread, quoting the request so it's unambiguous what a "yes"
  unlocks -- `@owner This prompt was determined to be off topic by the safety
  screen (category: reason): > "<first 200 chars>" Do you permit the AI to
  work on this request? *Yes/No*`. An owner replying with a bare **yes** (or
  no) in that thread settles it: *no* leaves it declined; *yes* replays the
  guest's original message (their id, text, files) through the normal path
  with the judge skipped for that one message only, so attribution and the
  guest's `runtime_mode` still apply. Scope is per thread, one open question
  at a time: a newer denial replaces it and the ask says so ("a *yes* applies
  to this request only"); an unanswered question expires after 24h. Only
  owners' whole-message answers count -- a guest typing "yes", or an owner
  chatting normally, is handled as usual. Later guest messages in the thread
  are still judged, but the judge sees the granted request as fenced,
  data-only context, so plain follow-ups to it pass while unrelated asks are
  still declined. State: `.state/pps_overrides.json`.
- **Guest model (t3 backend):** non-owner conversations start on Sonnet 5.5
  (`claude-sonnet-5-5`); owners keep the channel's `t3_model`. Override with a
  `t3_model` object (`{"instanceId": …, "model": …}`) on a `senders.json`
  entry or in `guest_defaults` (precedence: sender, `guest_defaults`, built-in).
  T3 fixes a thread's model when it is created, so **whoever starts the thread
  decides it** and it is kept for the thread's life: an owner joining a guest's
  thread does not upgrade it, and a guest replying in an owner's thread does
  not downgrade it. (Start a new thread to change model.)
- **Bidirectional mirror (t3 backend):** messages typed into the T3 GUI on a
  Slack-originated thread are posted back into the Slack thread
  ("_Owner said to the agent:_ …"), and replies land in both places. Settling
  a chat in the T3 UI posts a notice into the Slack thread; replying in Slack
  un-settles it.
- **Outbound scrubbing:** every message posted to Slack is scanned and
  secret-shaped strings (tokens, keys, JWTs) are redacted.
- **Trust boundary:** pps is the screen, so a message that reaches the agent is
  trusted content and arrives as plain text. The residual case — a guest in
  `pps_mode: "log"`, where the judge watches but never blocks — still gets fenced
  in `<<<EXTERNAL_UNTRUSTED_CONTENT>>>` markers plus the matching directive
  (`sanitize.py`).
- **Bridge protocol lives outside the prompt:** each turn carries one routing
  comment (`<!-- slack channel=… thread=… -->`) that the T3 GUI hides from the
  reader, plus a human attribution line (`Berish Perlman from #sofer-ai: …`,
  names resolved via `users.info`/`conversations.info` and cached). How to
  reply, upload files, and post extra messages ships with the package
  (`src/slackcc/data/slack-bridge.md`) and reaches the agent through its system
  prompt (`claude` backend) or the project's `CLAUDE.md` (`t3` backend — see
  step 4). A project whose `CLAUDE.md` copy is missing *or out of date* gets the
  protocol injected inline on each thread's first turn (with a log warning)
  until `slackcc init-project` is re-run.

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
   - `channels:read` — resolve channel info (channel names for attribution)
   - `groups:read` — same for private channels (`conversations.info` needs it;
     without it the project name is used as the channel label)
   - `users:read` — resolve sender display names for attribution (optional:
     without it, unconfigured senders show as their Slack user id)
   - `files:read` — download attachments a user sends so the agent can read
     them (without it Slack answers file downloads with its HTML sign-in page)
   - `files:write` — `slack-upload` (posting files the agent produced)
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
Add `"require_mention": true` if the channel is also used for unrelated chat
and the bot should only speak up when tagged (see "Mention gating" above).
Put your own Slack member id in `config/senders.json` as `role: "owner"` —
everyone else defaults to a screened, approval-required guest.

For the `t3` backend, also set `SLACKCC_T3_URL`/`SLACKCC_T3_TOKEN` in `.env`
(a T3 session token with `orchestration:read` + `orchestration:operate`). The
bridge speaks T3's orchestration protocol v2: thread reads over HTTP, every
command over the `/ws` WebSocket RPC. A T3 upgrade that changes the protocol
fails loudly: the turn errors with "T3 no longer speaks orchestration protocol
v2; slackcc needs updating". Also run the pps judge (separate repo/service) at `SLACKCC_PPS_URL` if you have
guest senders — guests fail closed when it's unreachable.

### Changing the config while the daemon runs

The daemon reads both files at start and then **reloads without a restart**:

- **SIGHUP** reloads whatever is on disk (a hand edit). If the files don't
  load, the daemon logs why and keeps running the config it had. With the
  systemd unit, add `ExecReload=/bin/kill -HUP $MAINPID` under `[Service]`
  and use `systemctl --user reload slackcc`.
- **The loopback config API** (`src/slackcc/config_api.py`) is how another
  program on the box changes the config — Alfred's admin *Slack* screen does.
  Set `SLACKCC_CONFIG_TOKEN` (24+ random characters, e.g.
  `openssl rand -hex 24`) in `.env`; the API binds `127.0.0.1:8643`
  (`SLACKCC_CONFIG_PORT` to move it). No token, no API.

  | Route | Body / answer |
  |---|---|
  | `GET /config` | `{etag, channels, senders, senders_file, problems[], effective, loaded:{etag,at,source}, loaded_current}` — the two files as JSON, and the *effective* config the daemon runs with (every default applied; `effective` is the only place defaults live) |
  | `PUT /config` | `{if_match, channels?, senders?, dry_run?}` → validated by the same parser the daemon starts with, both files backed up to `.state/config-backups/` (last 20), written atomically, swapped into the running daemon. `dry_run: true` answers `{effective}` for the candidate and writes nothing. 409 `stale` when `if_match` isn't the current etag, 422 `invalid` with the loader's message, 422 `restart_required` for the first `t3` channel (the T3 client only starts at boot), 422 `senders_missing` when `senders.json` has gone but the daemon is running a sender policy |
  | `GET /healthz` | `{ok:true}`, no token |

  Every `/config` request needs `Authorization: Bearer $SLACKCC_CONFIG_TOKEN`.
  Only the routing map and sender policy reload; tokens and URLs still need a
  restart. A turn already running finishes on the config it started with.

  Neither a reload nor a PUT ever turns sender protection *off*: a missing
  `senders.json` means "everyone is the owner" only at start. If the file
  vanishes under a daemon running a sender policy, both refuse and keep that
  policy — to really turn protection off, remove the file and restart.
  Every field is type-checked, and every enum (`role`, `pps_mode`) is checked
  in `guest_defaults` too, so nothing that loads can break a turn or the next
  start. A process killed between the two file renames can leave new
  `senders.json` with old `channels.json`; the pair from before the write is
  in `.state/config-backups/`.

## 3. Install & run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .

./scripts/run.sh        # loads .env, starts the daemon
```

Now post a message in the configured channel — the bot replies in-thread, and
each thread stays one continuous Claude Code conversation.

## 4. Teach the project the bridge protocol (`t3` backend only)

The agent needs standing instructions — your reply is auto-posted, here's how to
upload a file, here's the trust model. On the `claude` backend that rides in the
system prompt and there's nothing to do. T3's `message.dispatch` has no
system-prompt field, but T3 spawns sessions with setting sources
`user,project,local`, so the project's own `CLAUDE.md` is the free channel:

```bash
slackcc init-project                    # every t3 channel's cwd in channels.json
slackcc init-project ~/projects/foo     # or a specific project
```

This writes a marker-delimited `## Slack Bridge` section into that project's
`CLAUDE.md`, rewriting it in place on later runs (your own content is
untouched). **Commit it** — T3 threads can run in a git worktree, which only
sees committed files.

Skipping this is not fatal: the daemon detects the missing section and injects
the protocol inline on the first turn of each thread instead, logging a warning.
That costs tokens once per thread rather than never.

## Testing

```bash
.venv/bin/python -m pytest tests/          # offline suite: 242 hermetic tests, ~7s
.venv/bin/python -m pytest tests/ -m live  # + judge-quality tier: real inference
                                           #   against the running pps/guard LLM (~20s)
```

The offline suite is the regression net (no network, no live services — safe
anywhere, including CI). The `live` tier is the model tripwire: attack prompts
must deny, benign guest work must allow — run it after swapping the guard
model, changing quantization, or editing the judge prompt. It auto-skips when
pps isn't running.

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
  backend_t3.py T3 Code turn runner (dispatch over WebSocket RPC, poll over HTTP)
  t3.py         T3 client (orchestration protocol v2) + mirror ledger store
  t3_mirror.py  background poller: T3 GUI turns -> Slack thread
  pps.py        client for the Prompt Protection Service judge
  outbound.py   secret scrubbing for everything posted to Slack
  config.py     env + channels.json + senders.json loading, per-sender policy,
                the effective-config view, LiveSettings (the swappable config)
  config_api.py loopback GET/PUT /config + SIGHUP reload (no restart to change config)
  sessions.py   thread -> session/thread id store (resume continuity)
  overrides.py  owner yes/no override of pps denials (pending asks + grants)
  sanitize.py   fencing for the messages pps didn't gate
  bridgedoc.py  ships/renders/installs the bridge protocol
  data/slack-bridge.md   the protocol itself (package data)
  send_cli.py   `slack-send` outbound CLI
config/channels.example.json
config/senders.example.json
.state/pps_overrides.json   per-thread pending owner asks + granted requests
.state/config-backups/      the files as they were before each config API write (last 20)
BACKLOG.md
```
