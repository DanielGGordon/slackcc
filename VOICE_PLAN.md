# Voice for the Slack ↔ Claude Code Daemon — Build Plan

> Produced 2026-06-27 via a planning workflow (4 parallel research agents + synthesis),
> grounded in the actual daemon code and verified machine prerequisites.

> **STATUS (2026-06-30):** The work is being done in two steps — **Step 1 = swap the
> backend to the Claude Agent SDK** (foundation for both Slack streaming and the phone
> bridge); **Step 2 = the phone track (Track C)**.
> **Step 1 is COMPLETE & verified:** `backend.run_turn` now drives `claude_agent_sdk.query()`
> instead of `claude -p`; signature/`TurnResult` contract unchanged, `app.py` untouched;
> fresh + resumed turns confirmed; transcripts still land in `~/.claude/projects/...`
> (Dancode unaffected); daemon restarted on the SDK backend.
> **Billing note (corrected):** Anthropic *paused* the announced Agent-SDK credit-pool
> change on June 15, 2026 — `claude -p`, the Agent SDK, and third-party usage all still
> draw from your subscription's usage limits. So the SDK swap is billing-neutral; no
> separate pool. (An update may come later; they said they'll announce before anything takes effect.)

## 1. TL;DR

Add three audio capabilities, all wrapped as shell CLIs that match the daemon's existing `slack-send`/`slack-upload`/`slack-wait-reply` pattern. **Read inbound voice notes** with a local `whisper.cpp` binary shelled from a new `transcribe.py` (no Python ML deps — immune to the python-3.14 wheel risk on this GPU-less box). **Reply with voice** via the **ElevenLabs TTS API** (`slack-tts`), reusing one `voice_id`. **Take live phone calls** by letting an **ElevenLabs Conversational-AI agent** own real-time turn-taking while it dispatches the heavy thinking to the daemon asynchronously — never putting Claude's 30-90s batch latency on the call's critical path. The unifying decision: one ElevenLabs `voice_id` is the single voice identity shared by Slack voice replies and the phone agent, so build TTS before the phone track.

> **Free Phase 0 (zero infra):** Slack often auto-transcribes voice clips server-side and ships the text on the file object as `transcription.preview.content` (status `complete`). Before building any STT, `slackfiles.py` can just read that field and inject it — covers the common case with no whisper at all. Fall back to local whisper.cpp when Slack's transcript is absent/incomplete.

---

## 2. Build Tracks

### Track A — Read voice notes FROM Slack (local STT)

**Winner: `whisper.cpp` (`whisper-cli` binary + quantized GGML model), shelled from a new `transcribe.py`.** Only option with zero Python/ML dependencies — sidesteps the risk that `ctranslate2`/`onnxruntime`/`torch` lack python-3.14 wheels, and a C++ binary is the same architectural seam the daemon already uses to shell `claude`.

Concrete integration:
- **NEW `src/slackcc/transcribe.py`**: `transcribe(path, *, model, threads=4) -> str`. Step 1: `ffmpeg -nostdin -i <in> -ar 16000 -ac 1 -c:a pcm_s16le -f wav <tmp.wav>` (whisper.cpp needs 16kHz mono PCM; Slack ships audio-only clips in an mp4 container). Step 2: `subprocess.run([whisper_bin, '-m', model, '-f', tmp.wav, '-nt', '-otxt', ...])`. Mirror `backend.run_turn`'s `TimeoutExpired`/`FileNotFoundError` handling.
- **EDIT `src/slackcc/slackfiles.py`**: add `is_voice_note(file_obj)` — audio if subtype `=='slack_audio'` OR name matches `audio_message*.mp4` OR mimetype in `{video/mp4, audio/*}` OR filetype in `{mp4,m4a,mp3,ogg,wav,webm}`. Mimetype is deceptively `video/mp4`; always normalize via ffmpeg first. Also expose Slack's own `transcription.preview.content` for the Phase-0 path.
- **EDIT `src/slackcc/app.py` `handle()`**: after the download loop, for each audio file inject the **transcript TEXT** into `parts` via `sanitize.wrap_untrusted(...)` — **not** the file path. Claude `Read` cannot decode audio.
- **NEW `src/slackcc/transcribe_cli.py`** + `slack-transcribe = "slackcc.transcribe_cli:main"` in `pyproject.toml`, symlinked into `~/.local/bin`.
- **EDIT `src/slackcc/config.py`**: add `whisper_bin` and `whisper_model` Settings (parallels `claude_bin`).
- **One-time provisioning**: `apt install cmake` (MISSING) then build `whisper-cli`, OR grab a prebuilt Linux release binary; fetch `ggml-small.en` (q5_0). `ffmpeg 8.0.1` already present.

**Model:** default `small.en` (~15-25s/30s clip, strong accuracy on 4-core CPU); `base.en` (~5-9s) as latency-first fallback. Both vanish inside the existing 30-90s Claude turn. Drop `.en` for multilingual.

**Effort: Low-Med (~1 day).** Only friction is the cmake/binary install.

### Track B — Reply with voice notes / shared voice (TTS)

**Winner: ElevenLabs TTS API, reusing the exact `voice_id` configured on the phone agent.** Only option that yields a literally identical voice in Slack and on the phone (timbre is owned by the `voice_id`, shared by the TTS API and the Conversational-AI agent). Pure HTTPS — no native deps — returns mp3 bytes directly, no ffmpeg transcode.

Concrete integration:
- **NEW `src/slackcc/tts_cli.py`** mirroring `upload_cli.py`. argparse: positional `channel` + `text`, `--thread/-t`, `--voice` (default from env), `--model` (default `flash_v2_5`), `--seed`, `--comment`. Flow: `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` with header `xi-api-key` and JSON `{text, model_id, seed}` → write mp3 → upload in-thread.
- **REFACTOR `src/slackcc/upload_cli.py`**: extract `files_upload_v2` into `upload_file(channel, path, comment=None, thread_ts=None) -> file_id`; call from both `upload_cli` and `tts_cli`.
- **`pyproject.toml`**: add `slack-tts = "slackcc.tts_cli:main"` and `say = "slackcc.tts_cli:main"`; `pip install -e .`; symlink both into `~/.local/bin`.
- **Secrets**: reuse `paths.resolve_token('ELEVENLABS_API_KEY')` and `resolve_token('ELEVENLABS_VOICE_ID')` from the project `.env`. No `config.py` change required.
- **EDIT `src/slackcc/app.py` `slack_ctx`**: append one sentence so the headless agent knows the capability exists.
- **`config/channels.json`**: add `Bash(slack-tts:*)` (and `Bash(say:*)`) to `allowed_tools` for voice-enabled channels.

**Caveat:** Slack has no public API for a *native* voice-message bubble (waveform + scrubber). `files_upload_v2` renders an inline-playable mp3 attachment — that is the achievable "voice note."

**Optional offline fallback:** Kokoro-82M (Apache-2.0) behind a `--local` flag, accepting that its voice will *not* match the phone agent. Needs `espeak-ng` + `onnxruntime` (likely a separate 3.11/3.12 venv). Defer unless offline TTS is a real requirement.

**Effort: Low (~half a day for the API path).**

### Track C — Live phone calls to the agent (ElevenLabs)

**Winner: B-async — a fast ElevenLabs Conversational-AI agent that owns real-time turn-taking and dispatches to the daemon via an async job tool.** Only architecture that honestly reconciles sub-second phone turn-taking with Claude Code's 30-90s non-streaming latency: ElevenLabs' bundled fast model handles greeting/turn-taking/holding pattern, while `run_turn` grinds out-of-band and is polled to completion — so a slow turn can exceed 120s without timing out.

Concrete integration:
- **NEW standalone `src/slackcc/voice.py`** (FastAPI/uvicorn or stdlib `http.server`). Do **not** modify `app.py`; it stays Slack-only. `voice.py` imports the same backend.
- **REUSE `backend.py` `run_turn()` UNCHANGED.** Async job flow mirrors the existing placeholder→`chat_update` working-indicator pattern.
- **Endpoints**: `POST /voice/start {conversation_id, request}` → launch `run_turn` in background, return `{job_id}`; `POST /voice/check {job_id}` → `{status, result?}`.
- **REUSE `sessions.py` `SessionStore`** for `--resume` continuity, re-keyed from `(channel_id, thread_ts)` to the ElevenLabs `conversation_id`.
- **REUSE `sanitize.py`** — wrap the phone transcript as untrusted input like Slack text.
- **NEW CLI `voice-call`** (symlinked): `POST https://api.elevenlabs.io/v1/convai/twilio/outbound_call` — the phone analog of `slack-send`.
- **EDIT `config.py`**: add `elevenlabs_api_key`, `agent_id`, `agent_phone_number_id`, webhook shared-secret.
- **NEW INFRA (genuinely new vs Socket Mode):** a public HTTPS ingress (Cloudflare Tunnel preferred over ngrok) in front of `voice.py`, since ElevenLabs calls *out* to your webhook. Shared-secret header on every endpoint.
- **Phone provisioning:** Twilio number (~$1.15/mo), paste SID + Auth Token into ElevenLabs Phone Numbers tab (native integration auto-configures webhooks), assign agent for inbound. ElevenLabs+Twilio own the media stream, so **no local STT/TTS needed for the phone path.**

**Cost:** ~$0.10-0.12/min all-in + ~$1/mo number + existing Claude usage.

**Effort: Med.** Ship **B-sync first** as a one-endpoint proof (`POST /voice/ask` returns `run_turn` text verbatim — zero backend changes), then upgrade to the async job table once the holding-pattern prompt is tuned.

---

## 3. Phased Rollout

**Phase 0 — Slack-native transcript (free).** Read `transcription.preview.content` off the file object in `slackfiles.py`/`app.py`. No accounts, no binaries, no money. Covers the common case immediately; whisper is the fallback.

**Phase 1 — Inbound STT (Track A).** Pure-local, no accounts, no ingress. Delivers the most-used feature (read voice notes) robustly. Only blocker: `apt install cmake` + fetch a GGML model. Independent of B/C.

**Phase 2 — Decide the canonical voice, then ship TTS (Track B).** *The pivot.* Create the ElevenLabs account and pick the `voice_id` **now** — that one id is the shared voice identity for both Slack replies and the phone agent. Build `slack-tts`/`say`. Delivers two-way voice in Slack and locks the voice before any phone work.

**Phase 3 — Phone agent, sync proof (Track C, B-sync).** Reuse `run_turn` verbatim behind a single blocking `POST /voice/ask`; stand up Cloudflare Tunnel + Twilio number; configure the ElevenLabs agent with the Phase-2 `voice_id`. Validates end-to-end telephony cheaply. Accept timeout fragility on slow turns as a known limitation.

**Phase 4 — Phone agent, async upgrade (Track C, B-async). Hardest.** Replace the blocking endpoint with the `start`/`check` job table + holding-pattern prompt. Removes the timeout bet; makes the call feel alive. Most prompt tuning.

**Dependencies:** A is independent and first. **B (the `voice_id`) must precede C.** C reuses B's voice identity but its own ingress/Twilio infra.

---

## 4. The Honest Hard Problem: Latency Mismatch

A phone conversation expects **sub-second time-to-first-token**. `backend.run_turn` is a **blocking `subprocess.run` that returns only after the full JSON** — 30-90s — and does **silent tool use** before any user-facing prose. On a live call that is dead air, read as a dropped call.

There is no way to make `claude -p` itself fast. Three ways to cope:

- **Option A (Custom-LLM proxy):** make `claude -p` *be* the ElevenLabs brain via an SSE `/v1/chat/completions` endpoint. Max Claude fidelity, but puts the full 30-90s on the critical path of *every* spoken turn. **Rejected.**
- **Option B-sync:** fast ElevenLabs model up front, one blocking server-tool to `run_turn`. Feels alive for chit-chat but **bets each turn on a webhook timeout**. Fragile on the slow tail. **Good enough for the Phase-3 proof only.**
- **Option B-async (RECOMMENDED end-state):** the fast ElevenLabs model owns 100% of real-time turn-taking; the Claude turn runs **off the critical path** as a background job the agent polls (~every 10s) while keeping the caller engaged. No timeout bet.

**Caveats stated plainly:** (1) a *second*, cloud LLM now sits in front of Claude — minor behavior split + tiny per-token cost for the chit-chat layer; (2) the holding-pattern prompt is genuinely hard to tune; (3) requires a public HTTPS ingress with shared-secret auth, which Socket Mode never needed; (4) caller experience during a long turn is "natural small talk / 'still working on it'," not a streamed answer — set that expectation.

---

## 5. Open Questions & Prerequisites

**Decisions to make:**
1. **One canonical `voice_id`** — which ElevenLabs voice? Drives *both* Slack TTS and the phone agent. (Blocks Phase 2 & 3.)
2. **Model match** — Flash v2.5 (mirror the phone) vs Multilingual v2 (richer async notes) for Slack replies?
3. **Local-only STT, or allow a cloud Whisper (Groq/OpenAI) accelerator?** Default cloud OFF to keep the standalone posture.
4. **Auto vs on-demand transcription** — auto-inject every inbound voice note (recommended) vs CLI-only? Cap very long clips via a `duration_ms` threshold?
5. **Phone UX** — true conversational back-and-forth (forces B-async) vs "ask, hold, hear a paragraph" (B-sync acceptable)?
6. **Phone project/cwd** — one default project, or a spoken "work in project X" command (phone has no channel to scope by)?
7. **Whisper model** — `small.en` (accuracy) vs `base.en` (speed); `.en` vs multilingual.
8. **Accept the UX limits** — inbound transcript-text-in-prompt (not Read), and outbound inline-playable mp3 (no native voice bubble).

**Prerequisites (verified on this box 2026-06-27):**
- **cmake** — MISSING. `apt install cmake` (or prebuilt `whisper-cli` binary) for Track A.
- **ffmpeg** — PRESENT (`/usr/bin/ffmpeg` 8.0.1). Sufficient.
- **GPU** — NONE. Confirms CPU/binary STT; pushes phone STT/TTS to ElevenLabs' cloud.
- **CPU/RAM** — 4 cores, ~10 GB free. Fine for `small.en`/`base.en`.
- **python 3.14.4** — the reason to prefer the binary (whisper.cpp) and pure-HTTPS (ElevenLabs) over `faster-whisper`/`torch`/`onnxruntime`.
- **ElevenLabs plan** — confirm account + API key with the chosen `voice_id`. TTS ~$0.025/500-char note; phone (Conversational-AI) ~$0.08-0.10/min.
- **Twilio account** — US number ~$1.15/mo; SID + Auth Token to paste into ElevenLabs (Phase 3+).
- **Public HTTPS ingress** — Cloudflare Tunnel (recommended) vs ngrok for `voice.py`; confirm ElevenLabs allows a custom auth header so the Claude endpoint isn't publicly callable.
- **Claude billing** — as of the June 15, 2026 pause, Agent SDK + `claude -p` both draw from the Claude subscription's usage limits (no separate SDK credit pool, no per-token API charge) *unless* `ANTHROPIC_API_KEY` is set, which flips to pay-as-you-go. Watch subscription usage limits under heavy phone use; re-check if Anthropic ships the deferred billing change.

Relevant files: `src/slackcc/{app.py,backend.py,slackfiles.py,upload_cli.py,sessions.py,sanitize.py,config.py,paths.py}`, `pyproject.toml`.
