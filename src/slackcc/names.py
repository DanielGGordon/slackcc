"""Human-readable names for the attribution line a bridged message carries.

The T3 GUI shows the owner the user message verbatim, so a turn reads
"Berish Perlman from #sofer-ai: …" rather than a Slack user id and a channel
id. Ids stay in the hidden routing comment (see app.py:bridge_header); this
module only decides what the *human* sees. (On the claude backend the same
prefix rides in the `claude -p` prompt; nobody but the agent reads it there.)

Names come from Slack profiles, which are user-controlled text that ends up
OUTSIDE any untrusted-content fence, so `clean_name()` is an allow-list, not a
scrub: letters, digits, marks, spaces and a few name punctuation marks survive;
everything that could mean something to a Markdown renderer, a bidi algorithm,
or the attribution grammar itself is dropped. Lookups are cached per process
and never fail a turn -- a missing `users:read` scope, a transient API error,
or a client without the method (test fakes) all fall back to the id.
"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata

log = logging.getLogger(__name__)

NAME_MAX = 48
# Real names contain these; nothing in Markdown or the "<name> from #<chan>:"
# grammar hinges on them.
_NAME_PUNCT = frozenset(".'-,&")
# How long a failed lookup's fallback (raw id / project name) is remembered
# before Slack is asked again. Successful names are cached for the process
# lifetime: a rename needs a daemon restart, which is acceptable.
FALLBACK_TTL = 300.0


def _keep(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return cat[0] in "LNM" or cat == "Zs" or ch in _NAME_PUNCT


def clean_name(raw: object, limit: int = NAME_MAX) -> str:
    """Reduce a profile-sourced string to something safe to print unfenced.

    Allow-list by Unicode category: letters (L*), digits (N*), combining marks
    (M*), plain spaces (Zs) and `.'-,&`. That drops control chars, format chars
    (zero-width space, bidi overrides), and every ASCII symbol Markdown or our
    own grammar cares about: backticks, `~`, `#`, `*`, `_`, `[]()!`, `<>`, `:`,
    `|`, `@`. Whitespace is collapsed, the result clipped, and a name with no
    alphanumeric at all becomes "" so the caller falls back to the id -- an
    all-punctuation "name" is never worth showing."""
    if not isinstance(raw, str):
        return ""
    t = "".join(ch if _keep(ch) else " " for ch in raw)
    t = " ".join(t.split())[:limit].rstrip()
    if not any(ch.isalnum() for ch in t):
        return ""
    return t


def _first_clean(*candidates: object) -> str:
    for c in candidates:
        cleaned = clean_name(c)
        if cleaned:
            return cleaned
    return ""


class NameResolver:
    """Cached user/channel name lookups against a Slack WebClient.

    `reserved` is the set of names the operator configured in senders.json
    (lower-cased): an unconfigured sender whose Slack profile happens to read
    "Dan" must not be shown as the owner, so such a hit falls back to the id."""

    def __init__(self, reserved: set[str] | frozenset[str] = frozenset()) -> None:
        # value -> (name, expires_at); expires_at is None for a real name,
        # a monotonic deadline for a fallback (see FALLBACK_TTL).
        self._users: dict[str, tuple[str, float | None]] = {}
        self._channels: dict[str, tuple[str, float | None]] = {}
        self._reserved = {r.lower() for r in reserved}
        self._lock = threading.Lock()
        self._warned_users = False
        self._warned_channels = False

    # -- cache helpers ----------------------------------------------------

    def _get(self, cache: dict[str, tuple[str, float | None]], key: str) -> str | None:
        with self._lock:
            hit = cache.get(key)
        if hit is None:
            return None
        value, expires_at = hit
        if expires_at is not None and time.monotonic() >= expires_at:
            return None
        return value

    def _put(self, cache: dict[str, tuple[str, float | None]], key: str,
             value: str, *, fallback: bool) -> str:
        expires_at = time.monotonic() + FALLBACK_TTL if fallback else None
        with self._lock:
            cache[key] = (value, expires_at)
        return value

    # -- public -----------------------------------------------------------

    def sender(self, client, user_id: str, event: dict | None = None,
               configured: str | None = None) -> str:
        """Display name for `user_id`, best source first.

        `configured` is the operator's own label from senders.json and wins
        outright when set (it's trusted, and it's what the pps log lines use
        too). The event's `user_profile` (present on most message events) saves
        an API call; `users.info` is the fallback, then the raw id."""
        if configured and configured != user_id:
            return configured
        if not user_id or user_id == "unknown":
            # A userless event has nothing to look up; don't spend the one-shot
            # "missing scope?" warning on it.
            return user_id or "unknown"
        cached = self._get(self._users, user_id)
        if cached:
            return cached
        profile = (event or {}).get("user_profile") or {}
        name = _first_clean(profile.get("display_name"), profile.get("real_name"))
        if not name:
            name = self._lookup_user(client, user_id)
        if name and name.lower() in self._reserved:
            log.warning("sender %s has a profile name matching a configured "
                        "sender (%r); showing the id instead", user_id, name)
            name = ""
        if not name:
            return self._put(self._users, user_id, user_id, fallback=True)
        return self._put(self._users, user_id, name, fallback=False)

    def channel(self, client, channel_id: str, fallback: str) -> str:
        """Channel name without the `#`, or `fallback` (the configured project
        name) when Slack won't tell us."""
        cached = self._get(self._channels, channel_id)
        if cached:
            return cached
        name = self._lookup_channel(client, channel_id)
        if name:
            return self._put(self._channels, channel_id, name, fallback=False)
        return self._put(self._channels, channel_id,
                         clean_name(fallback) or channel_id, fallback=True)

    # -- Slack calls --------------------------------------------------------

    def _lookup_user(self, client, user_id: str) -> str:
        try:
            info = client.users_info(user=user_id)
        except Exception:  # noqa: BLE001 - any failure means "use the id"
            if not self._warned_users:
                self._warned_users = True
                log.warning("users.info failed (missing users:read scope?); "
                            "falling back to user ids", exc_info=True)
            else:
                log.debug("users.info failed for %s", user_id, exc_info=True)
            return ""
        u = (info or {}).get("user") or {}
        profile = u.get("profile") or {}
        return _first_clean(profile.get("display_name"), u.get("real_name"),
                            profile.get("real_name"), u.get("name"))

    def _lookup_channel(self, client, channel_id: str) -> str:
        try:
            info = client.conversations_info(channel=channel_id)
        except Exception:  # noqa: BLE001 - any failure means "use the fallback"
            if not self._warned_channels:
                self._warned_channels = True
                log.warning("conversations.info failed (missing channels:read / "
                            "groups:read scope?); falling back to project names "
                            "for channel labels", exc_info=True)
            else:
                log.debug("conversations.info failed for %s", channel_id, exc_info=True)
            return ""
        return clean_name(((info or {}).get("channel") or {}).get("name"))
