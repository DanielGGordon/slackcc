"""Configuration loading: env vars + the channel->project routing map."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ChannelConfig:
    """Routing + scoping for one Slack channel."""

    channel_id: str
    project: str
    cwd: str
    persona: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "acceptEdits"
    timeout: int = 600  # seconds per turn; raise for channels that render/produce files
    # t3 backend: how long a guest turn may sit parked on an owner approval /
    # question in the T3 GUI before the bridge stops holding the Slack thread
    # (the T3 turn keeps waiting; the mirror delivers a late reply). The
    # `timeout` clock above pauses while parked, so it only counts agent time.
    approval_timeout: int = 3600
    # backend "t3" routes turns into T3 Code (1:1 Slack channel <-> T3 project).
    # persona/allowed_tools/permission_mode are NOT applied on that path: the
    # persona lives in the project's CLAUDE.md and T3 runs full-access (beta).
    backend: str = "claude"
    t3_project_id: str | None = None
    t3_model: dict = field(
        default_factory=lambda: {"instanceId": "claudeAgent", "model": "claude-sonnet-5"}
    )
    # Default False preserves the original "works without me" loop: any plain
    # message in a configured channel gets a reply. Opt a channel in when it's
    # shared with unrelated conversation (e.g. also used for non-project chat)
    # and the bot should only speak up when actually @-mentioned or replying
    # in a thread it already joined -- see app.py's handle() for the gating.
    require_mention: bool = False

    def validate(self) -> None:
        if not Path(self.cwd).is_dir():
            raise ValueError(
                f"channel {self.channel_id}: cwd does not exist: {self.cwd}"
            )
        if self.backend not in ("claude", "t3"):
            raise ValueError(f"channel {self.channel_id}: unknown backend {self.backend!r}")
        if self.backend == "t3" and not self.t3_project_id:
            raise ValueError(f"channel {self.channel_id}: backend 't3' needs t3_project_id")


@dataclass(frozen=True)
class SenderPolicy:
    """Per-sender permissions: who runs with which T3 runtime mode and whether
    their messages pass through pps (the prompt-protection judge) first."""

    user_id: str
    name: str
    role: str = "guest"  # "owner" | "guest"
    runtime_mode: str = "approval-required"  # T3 RuntimeMode wire value
    pps_mode: str = "enforce"  # "skip" | "log" | "enforce"
    policy_extra: str = ""  # appended to the generated pps policy text


@dataclass(frozen=True)
class Settings:
    bot_token: str
    app_token: str
    config_path: Path
    sessions_path: Path
    claude_bin: str
    channels: dict[str, ChannelConfig]
    t3_url: str = "http://127.0.0.1:3773"
    t3_token: str | None = None
    t3_owner: str = "Dan"  # display name for GUI-typed messages mirrored to Slack
    # Browser-reachable T3 GUI base (e.g. https://host:7443) for links in
    # approval pings; the loopback t3_url is useless in a phone notification.
    t3_gui_url: str | None = None
    pps_url: str = "http://127.0.0.1:8642"
    senders: dict[str, SenderPolicy] = field(default_factory=dict)
    guest_defaults: SenderPolicy | None = None

    def channel(self, channel_id: str) -> ChannelConfig | None:
        return self.channels.get(channel_id)

    def has_t3_channels(self) -> bool:
        return any(c.backend == "t3" for c in self.channels.values())

    def owner_ids(self) -> list[str]:
        """Slack member ids of role=owner senders -- who gets paged when a guest
        turn parks on an approval. Empty without a senders.json (no guests then)."""
        return [uid for uid, sp in self.senders.items() if sp.role == "owner"]

    def t3_thread_url(self, thread_id: str) -> str | None:
        base = (self.t3_gui_url or "").rstrip("/")
        return f"{base}/primary/{thread_id}" if base else None

    def sender_policy(self, user_id: str) -> SenderPolicy:
        """Known senders get their entry; unknown senders get guest defaults.
        With no senders.json at all, everyone is treated as owner (protection
        off — preserves pre-pps behavior for claude-only deployments)."""
        if user_id in self.senders:
            return self.senders[user_id]
        if self.guest_defaults is not None:
            return SenderPolicy(
                user_id=user_id,
                name=user_id,
                role="guest",
                runtime_mode=self.guest_defaults.runtime_mode,
                pps_mode=self.guest_defaults.pps_mode,
                policy_extra=self.guest_defaults.policy_extra,
            )
        return SenderPolicy(user_id=user_id, name=user_id, role="owner",
                            runtime_mode="full-access", pps_mode="skip")


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def load_channels(config_path: Path) -> dict[str, ChannelConfig]:
    raw = json.loads(config_path.read_text())
    out: dict[str, ChannelConfig] = {}
    for channel_id, spec in raw.get("channels", {}).items():
        if channel_id.startswith("_"):  # allow `_comment` style keys
            continue
        cfg = ChannelConfig(
            channel_id=channel_id,
            project=spec["project"],
            cwd=os.path.expanduser(spec["cwd"]),
            persona=spec.get("persona"),
            allowed_tools=list(spec.get("allowed_tools", [])),
            permission_mode=spec.get("permission_mode", "acceptEdits"),
            timeout=int(spec.get("timeout", 600)),
            approval_timeout=int(spec.get("approval_timeout", 3600)),
            backend=spec.get("backend", "claude"),
            t3_project_id=spec.get("t3_project_id"),
            t3_model=spec.get(
                "t3_model", {"instanceId": "claudeAgent", "model": "claude-sonnet-5"}
            ),
            require_mention=bool(spec.get("require_mention", False)),
        )
        cfg.validate()
        out[channel_id] = cfg
    return out


def load_senders(path: Path) -> tuple[dict[str, SenderPolicy], SenderPolicy | None]:
    if not path.exists():
        return {}, None
    raw = json.loads(path.read_text())

    def _mk(user_id: str, spec: dict, base_role: str) -> SenderPolicy:
        role = spec.get("role", base_role)
        owner = role == "owner"
        return SenderPolicy(
            user_id=user_id,
            name=spec.get("name", user_id),
            role=role,
            runtime_mode=spec.get("runtime_mode",
                                  "full-access" if owner else "approval-required"),
            pps_mode=spec.get("pps_mode", "skip" if owner else "enforce"),
            policy_extra=spec.get("policy_extra", ""),
        )

    senders = {uid: _mk(uid, spec, "guest")
               for uid, spec in raw.get("senders", {}).items()
               if not uid.startswith("_")}
    guest_defaults = _mk("_default", raw.get("guest_defaults", {}), "guest")
    for sp in senders.values():
        if sp.role not in ("owner", "guest"):
            raise ValueError(f"sender {sp.user_id}: unknown role {sp.role!r}")
        if sp.pps_mode not in ("skip", "log", "enforce"):
            raise ValueError(f"sender {sp.user_id}: unknown pps_mode {sp.pps_mode!r}")
    return senders, guest_defaults


def load_settings() -> Settings:
    config_path = Path(os.environ.get("SLACKCC_CONFIG", "./config/channels.json"))
    sessions_path = Path(os.environ.get("SLACKCC_SESSIONS", "./.state/sessions.json"))
    senders_path = Path(os.environ.get("SLACKCC_SENDERS", "./config/senders.json"))
    channels = load_channels(config_path)
    senders, guest_defaults = load_senders(senders_path)
    settings = Settings(
        bot_token=_require_env("SLACK_BOT_TOKEN"),
        app_token=_require_env("SLACK_APP_TOKEN"),
        config_path=config_path,
        sessions_path=sessions_path,
        claude_bin=os.environ.get("SLACKCC_CLAUDE_BIN", "claude"),
        channels=channels,
        t3_url=os.environ.get("SLACKCC_T3_URL", "http://127.0.0.1:3773"),
        t3_token=os.environ.get("SLACKCC_T3_TOKEN"),
        t3_owner=os.environ.get("SLACKCC_T3_OWNER", "Dan"),
        t3_gui_url=os.environ.get("SLACKCC_T3_GUI_URL") or None,
        pps_url=os.environ.get("SLACKCC_PPS_URL", "http://127.0.0.1:8642"),
        senders=senders,
        guest_defaults=guest_defaults,
    )
    if settings.has_t3_channels() and not settings.t3_token:
        raise RuntimeError("A channel uses backend 't3' but SLACKCC_T3_TOKEN is not set")
    return settings
