#!/usr/bin/env python3
"""Discord Rich Presence: an optional, ordinary Ember Deck integration.

Foundry runs this worker last, with ``restart_scope="worker"``: nothing that
happens here (no SDK, Box64 trouble, a crashed bridge, Discord offline) can
restart the hardware workers.

    rich_presence.py  (this process, native ARM64 Python)
          | newline-delimited JSON over stdin/stdout
    discord_bridge    (C++20; native ARM64, or x86_64 under Box64)
          |
    Discord Social SDK

Responsibilities here: Discord authentication state and token persistence,
the privacy policy and presence model (presence_model.py), reconnect/backoff
policy, heartbeat and status. The bridge owns only the SDK.

With ``enabled: false`` or no Discord application ID the worker stays idle and
healthy: it never launches the bridge and never contacts Discord.

Linking uses the Social SDK's desktop Public Client flow (Authorize with a
PKCE challenge, then GetToken), driven entirely inside the bridge, and is
started only by ``python rich_presence.py --link`` from the Pi's graphical
session. The worker itself is headless: without stored tokens it reports
"Discord not linked" and never opens a browser. ``--unlink`` revokes (when the
bridge can run) and clears the stored tokens. A running worker picks up either
change from the token file.

Presence content comes from presence_model.py (two states, Idle and
Listening, for Plex, DAB, YT Music and Bluetooth). Artwork is the registered
static assets, except with ``presence.artwork_mode: on`` where DAB and YT
Music may use their own artwork after artwork_policy.py has vetted it (and
the bridge re-checks it). ``audit`` only logs sanitised classifications
(artwork_audit.py).
"""

from __future__ import annotations

import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import synapse as bus
from contracts import (
    DISCORD_AUTH_LINKED,
    DISCORD_AUTH_REFRESHING,
    DISCORD_AUTH_UNLINKED,
    DISCORD_BACKEND_BOX64,
    DISCORD_BACKEND_NATIVE,
    DISCORD_BACKEND_NONE,
    DISCORD_CONNECTION_CONNECTING,
    DISCORD_CONNECTION_DISCONNECTED,
    DISCORD_CONNECTION_READY,
    DISCORD_CONNECTION_RECONNECTING,
    DISCORD_STATUS_CONNECTED,
    DISCORD_STATUS_CONNECTING,
    DISCORD_STATUS_DISABLED,
    DISCORD_STATUS_ERROR,
    DISCORD_STATUS_NOT_CONFIGURED,
    DISCORD_STATUS_NOT_LINKED,
    DISCORD_STATUS_RECONNECTING,
    DISCORD_STATUS_SDK_UNAVAILABLE,
)
from discord_ipc import BackendChoice, BridgeError, BridgeProcess, select_backend
import artwork_policy
from artwork_audit import ArtworkAuditor, format_audit
from presence_model import (
    ASSET_EMBER_DECK,
    REGISTERED_ASSETS,
    PresencePolicy,
    PresenceScheduler,
    PresenceTimeline,
    build_presence,
    dynamic_artwork_allowed,
    read_media_snapshot,
)
from runtime_config import ConfigError, load_yaml_mapping, validate_rich_presence
from secret_store import Redactor, SecretFileError, permission_problem, read_private_yaml, write_private_yaml
from whisper_daemon import log_error, log_event, log_heartbeat, log_info, log_recovery

BASE = Path(__file__).resolve().parent

DEFAULTS: Dict[str, Any] = {
    "proc_name": "rich_presence",
    "enabled": True,
    "discord": {
        "application_id": None,
        "backend": "auto",
        "native_bridge": "discord_bridge/build/discord_bridge",
        "sdk_library_dir": None,
        "box64": {"executable": "box64", "bridge": None},
        "auth_state_path": "discord_secrets.yaml",
        "request_timeout_s": 5.0,
        "retry_initial_s": 5.0,
        "retry_max_s": 300.0,
        "auth_timeout_s": 900.0,
        "refresh_margin_s": 86400.0,
    },
    "presence": {
        "show_source": True,
        "show_media_metadata": False,
        "show_idle": True,
        "activity_type": "listening",
        "artwork_mode": "audit",
        "project_url": None,  # the shipped rich_presence.yaml sets the project page
        "update_debounce_s": 2.0,
        "min_update_interval_s": 5.0,
    },
}

# artwork_service.py liveness (read only).
KEY_ARTWORK_SERVICE_READY = "/artwork/service_ready"
KEY_ARTWORK_SERVICE_HEARTBEAT = "/proc/artwork_service/heartbeat_ms"
ARTWORK_SERVICE_FRESH_MS = 10_000

# Synapse keys owned by this worker. None of them ever holds a token.
KEY_ENABLED = "/discord/enabled"
KEY_BACKEND = "/discord/backend"
KEY_BACKEND_AVAILABLE = "/discord/backend_available"
KEY_AUTH_STATE = "/discord/auth_state"
KEY_CONNECTION_STATE = "/discord/connection_state"
KEY_STATUS = "/discord/status"
KEY_STATUS_SEQ = "/discord/status_seq"

_TOKEN_TEXT = re.compile(r"^[A-Za-z0-9._~+/=-]{8,4096}$")


# ------------------------------------------------------------------ configuration

def _merge(defaults: Dict[str, Any], loaded: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(defaults)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_cfg() -> Dict[str, Any]:
    """Load and strictly validate rich_presence.yaml (ConfigError → exit 2)."""
    raw = os.environ.get("CONFIG_PATH")
    path = Path(raw) if raw else BASE / "configs" / "rich_presence.yaml"
    loaded = load_yaml_mapping(path, "rich_presence") if path.is_file() else {}
    cfg = _merge(DEFAULTS, loaded)
    # YAML 1.1 reads an unquoted ``artwork_mode: on``/``off`` as a boolean.
    mode = cfg["presence"].get("artwork_mode")
    if isinstance(mode, bool):
        cfg["presence"]["artwork_mode"] = "on" if mode else "off"
    validate_rich_presence(cfg)
    cfg["_config_path"] = str(path)
    cfg["_config_dir"] = str(path.parent)
    return cfg


def auth_state_path(cfg: Dict[str, Any]) -> Path:
    path = Path(os.path.expanduser(str(cfg["discord"]["auth_state_path"])))
    if not path.is_absolute():
        path = Path(cfg.get("_config_dir") or BASE / "configs") / path
    return path


def application_id(cfg: Dict[str, Any]) -> Optional[str]:
    value = cfg["discord"].get("application_id")
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if text.isdigit() else None


# ------------------------------------------------------------------ token store

@dataclass(frozen=True)
class DiscordTokens:
    access_token: str
    refresh_token: str
    expires_at: int          # wall-clock UNIX seconds
    scope: str = ""

    def __repr__(self) -> str:
        return f"DiscordTokens(expires_at={self.expires_at}, scope={self.scope!r})"

    def access_valid(self, wall_now: float, margin_s: float = 0.0) -> bool:
        return self.expires_at - wall_now > margin_s


class TokenStore:
    """``configs/discord_secrets.yaml``: mode 0600, atomic, cache section only.

    The file holds no operator credentials: Discord linking uses the SDK's
    Public Client authorization-code + PKCE flow and the Deck never stores a
    client secret.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def signature(self) -> Optional[Tuple[int, int]]:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self) -> Optional[DiscordTokens]:
        """Return stored tokens, None if absent, or raise SecretFileError (no contents)."""
        data = read_private_yaml(self.path)
        if data is None:
            return None
        if not isinstance(data, dict) or set(data) - {"version", "cache"} or data.get("version", 1) != 1:
            raise SecretFileError(f"{self.path}: unexpected structure")
        cache = data.get("cache")
        if cache is None:
            return None
        if not isinstance(cache, dict) or set(cache) - {"access_token", "refresh_token", "expires_at", "scope"}:
            raise SecretFileError(f"{self.path}: unexpected 'cache' structure")
        access, refresh = cache.get("access_token"), cache.get("refresh_token")
        if access is None and refresh is None:
            return None
        expires = cache.get("expires_at", 0)
        if not (isinstance(access, str) and isinstance(refresh, str) and access and refresh):
            raise SecretFileError(f"{self.path}: 'cache' tokens must be strings")
        if isinstance(expires, bool) or not isinstance(expires, int):
            raise SecretFileError(f"{self.path}: 'cache.expires_at' must be an integer")
        scope = cache.get("scope") or ""
        if not isinstance(scope, str):
            raise SecretFileError(f"{self.path}: 'cache.scope' must be a string")
        return DiscordTokens(access, refresh, expires, scope)

    def save(self, tokens: DiscordTokens) -> None:
        write_private_yaml(self.path, {"version": 1, "cache": {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": int(tokens.expires_at),
            "scope": tokens.scope,
        }})

    def clear(self) -> None:
        write_private_yaml(self.path, {"version": 1, "cache": None})


def parse_token_event(event: Dict[str, Any], wall_now: float) -> Optional[DiscordTokens]:
    """Validate a bridge ``tokens`` event; None if any field is unusable."""
    access, refresh = event.get("access_token"), event.get("refresh_token")
    expires_in = event.get("expires_in")
    scope = event.get("scope", "")
    if not (isinstance(access, str) and _TOKEN_TEXT.match(access)):
        return None
    if not (isinstance(refresh, str) and _TOKEN_TEXT.match(refresh)):
        return None
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not 0 < expires_in <= 365 * 86400:
        return None
    if not isinstance(scope, str) or len(scope) > 512:
        return None
    return DiscordTokens(access, refresh, int(wall_now) + expires_in, scope)


# ------------------------------------------------------------------ helpers

class Backoff:
    def __init__(self, initial_s: float, max_s: float):
        self.initial_s, self.max_s = initial_s, max_s
        self.delay_s = initial_s

    def next(self) -> float:
        delay = self.delay_s
        self.delay_s = min(self.delay_s * 2.0, self.max_s)
        return delay

    def reset(self) -> None:
        self.delay_s = self.initial_s


# ------------------------------------------------------------------ worker

class RichPresenceWorker:
    """All Rich Presence behaviour, driven by ``tick(now)`` from the main loop."""

    def __init__(
        self,
        cfg: Dict[str, Any],
        *,
        bridge_factory: Callable[..., Any] = BridgeProcess,
        backend_selector: Callable[..., BackendChoice] = select_backend,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.proc_name = str(cfg["proc_name"])
        self.discord = cfg["discord"]
        self.policy = PresencePolicy.from_cfg(cfg["presence"])
        configured_link = cfg["presence"].get("project_url")
        if self.policy.project_url is None and not (configured_link is None or str(configured_link).strip() == ""):
            # Not echoed: a malformed URL could carry user:password@. Presence carries on without links.
            log_event(self.proc_name, "presence_links_disabled",
                      {"reason": "presence.project_url is not a usable https URL"})
        self.bridge_factory = bridge_factory
        self.backend_selector = backend_selector
        self.wall_clock = wall_clock
        self.redact = Redactor()
        self.tokens_store = TokenStore(auth_state_path(cfg))

        retry_initial = float(self.discord["retry_initial_s"])
        retry_max = float(self.discord["retry_max_s"])
        self.bridge_backoff = Backoff(retry_initial, retry_max)
        self.auth_backoff = Backoff(retry_initial, retry_max)
        self.request_timeout = float(self.discord["request_timeout_s"])

        self.bridge = None
        self.backend_kind = DISCORD_BACKEND_NONE
        self.backend_available = False
        self.next_bridge_at = 0.0
        self.bridge_crashed = False
        self.sdk_version: Optional[str] = None

        self.tokens: Optional[DiscordTokens] = None
        self._token_signature: Optional[Tuple[int, int]] = None
        self._tokens_loaded = False
        self.token_sent = False
        self.refreshing = False
        self.next_auth_at = 0.0

        self.connection = DISCORD_CONNECTION_DISCONNECTED
        self.status: Optional[int] = None
        self.active_faults: Dict[str, str] = {}
        self.snapshot = None
        self.timeline = PresenceTimeline()
        self.timing: Tuple[Optional[int], Optional[int]] = (None, None)
        self.artwork_auditor: Optional[ArtworkAuditor] = None
        if self.policy.artwork_mode == "audit":
            self.artwork_auditor = ArtworkAuditor(self._log_artwork)
        self.artwork_url: Optional[str] = None          # vetted URL for the current snapshot
        self._artwork_memo: Dict[Tuple[Optional[str], Optional[str]], Tuple[Optional[str], str]] = {}
        self._artwork_decision: Optional[Tuple[Optional[str], str]] = None
        self.scheduler = PresenceScheduler(
            debounce_s=float(cfg["presence"]["update_debounce_s"]),
            min_interval_s=float(cfg["presence"]["min_update_interval_s"]),
        )

    # -- logging
    def _fault(self, key: str, event: str, summary: str, details: str) -> None:
        details = self.redact(details)
        if self.active_faults.get(key) == details:
            return
        self.active_faults[key] = details
        log_error(self.proc_name, summary, details, event=event, dedupe_key=f"{self.proc_name}:{key}")

    def _with_stderr(self, details: str, bridge: Any) -> str:
        """Append the bridge's redacted stderr tail (failure reports only)."""
        tail_of = getattr(bridge, "stderr_tail", None)
        if tail_of is None:
            return details
        try:
            tail = tail_of(self.redact)
        except Exception:
            return details
        return f"{details}; bridge stderr: {tail}" if tail else details

    def _recover(self, key: str, event: str) -> None:
        if self.active_faults.pop(key, None) is not None:
            log_recovery(self.proc_name, event, recovers_key=f"{self.proc_name}:{key}")

    # -- tokens on disk
    def _check_token_file(self) -> None:
        signature = self.tokens_store.signature()
        if self._tokens_loaded and signature == self._token_signature:
            return
        self._token_signature = signature
        first_load = not self._tokens_loaded
        self._tokens_loaded = True
        try:
            loaded = self.tokens_store.load()
            self._recover("token_file", "discord_token_file_ok")
        except SecretFileError as exc:
            self._fault("token_file", "discord_token_file_invalid", "Discord token file ignored", str(exc))
            loaded = None
        mode = permission_problem(self.tokens_store.path)
        if mode:
            self._fault("token_mode", "discord_token_file_permissions", "Discord token file permissions", mode)
        if loaded is not None:
            self.redact.add(loaded.access_token, loaded.refresh_token)
        if loaded != self.tokens:
            if not first_load and loaded is None and self.tokens is not None:
                log_info(self.proc_name, "discord_unlinked")
                self._drop_session()
            self.tokens = loaded
            self.token_sent = False

    def _persist(self, tokens: DiscordTokens) -> None:
        try:
            self.tokens_store.save(tokens)
            self._token_signature = self.tokens_store.signature()
            self._recover("token_write", "discord_tokens_saved")
        except (OSError, SecretFileError) as exc:
            # Keep working from memory; the next start will need to refresh or relink.
            self._fault("token_write", "discord_token_write_failed", "Could not save Discord tokens", str(exc))

    def _clear_tokens(self) -> None:
        self.tokens = None
        self.token_sent = False
        try:
            self.tokens_store.clear()
            self._token_signature = self.tokens_store.signature()
        except (OSError, SecretFileError) as exc:
            self._fault("token_write", "discord_token_write_failed", "Could not clear Discord tokens", str(exc))

    # -- bridge lifecycle
    def _start_bridge(self, now: float) -> bool:
        choice = self.backend_selector(self.discord, BASE)
        if choice.backend is None:
            self.backend_available = False
            self.backend_kind = DISCORD_BACKEND_NONE
            self._fault("backend", "discord_backend_unavailable", "Discord bridge unavailable", choice.reason)
            self.next_bridge_at = now + self.bridge_backoff.next()
            return False
        self.backend_available = True
        self.backend_kind = DISCORD_BACKEND_BOX64 if choice.backend.kind == "box64" else DISCORD_BACKEND_NATIVE
        self._recover("backend", "discord_backend_available")
        try:
            self.bridge = self.bridge_factory(choice.backend)
            hello = self.bridge.request("hello", self.request_timeout, protocol=1)
            if hello.get("result") != "success":
                raise BridgeError(f"bridge refused to start ({hello.get('result')})")
            init = self.bridge.request("init", self.request_timeout, application_id=application_id(self.cfg),
                                       external_artwork=self.policy.artwork_mode == "on")
            if init.get("result") != "success":
                raise BridgeError(f"Discord SDK could not start ({init.get('result')})")
        except (BridgeError, OSError) as exc:
            bridge = self.bridge
            self._stop_bridge(clear=False)
            self._fault("bridge", "discord_bridge_failed", "Discord bridge failed to start",
                        self._with_stderr(str(exc), bridge))
            self.next_bridge_at = now + self.bridge_backoff.next()
            return False
        sdk_version = hello.get("sdk_version")
        if isinstance(sdk_version, str) and sdk_version[:32] != self.sdk_version:
            self.sdk_version = sdk_version[:32]
            log_event(self.proc_name, "discord_sdk_loaded",
                      {"sdk_version": self.sdk_version, "backend": choice.backend.kind})
        if self.bridge_crashed:
            log_event(self.proc_name, "discord_bridge_restarted")
        self.bridge_crashed = False
        self._recover("bridge", "discord_bridge_started")
        self.connection = DISCORD_CONNECTION_CONNECTING
        self.token_sent = False
        self.refreshing = False
        self.scheduler.mark_unpublished()
        return True

    def _stop_bridge(self, *, clear: bool) -> None:
        bridge, self.bridge = self.bridge, None
        if bridge is None:
            return
        if clear and bridge.alive() and self.connection == DISCORD_CONNECTION_READY and self.scheduler.has_published:
            try:
                bridge.request("clear_presence", min(2.0, self.request_timeout))
            except BridgeError:
                pass
        try:
            bridge.stop()
        except Exception:
            pass
        self.connection = DISCORD_CONNECTION_DISCONNECTED

    def _drop_session(self) -> None:
        """Clear presence and stop the bridge (disabled, unlinked, shutdown)."""
        self._stop_bridge(clear=True)
        self.refreshing = False
        self.token_sent = False
        self.scheduler.reset()

    # -- bridge events
    def _handle_event(self, event: Dict[str, Any], now: float) -> None:
        kind = event.get("event")
        if kind == "status":
            state = event.get("state")
            if state == "ready":
                if self.connection != DISCORD_CONNECTION_READY:
                    self.scheduler.mark_unpublished()  # republish after (re)connect
                self.connection = DISCORD_CONNECTION_READY
                self.bridge_backoff.reset()
                self._recover("connection", "discord_connected")
                log_event(self.proc_name, "connected", {"service": "Discord"})
            elif state in ("connecting", "reconnecting"):
                self.connection = (DISCORD_CONNECTION_RECONNECTING if state == "reconnecting"
                                   else DISCORD_CONNECTION_CONNECTING)
            elif state == "disconnected":
                was_ready = self.connection == DISCORD_CONNECTION_READY
                self.connection = DISCORD_CONNECTION_RECONNECTING if was_ready else DISCORD_CONNECTION_DISCONNECTED
                self.token_sent = False
                if event.get("result") == "unauthenticated" and self.tokens is not None:
                    # The access token was rejected: force a refresh next tick.
                    self.tokens = DiscordTokens(self.tokens.access_token, self.tokens.refresh_token, 0, self.tokens.scope)
                self._fault("connection", "discord_disconnected", "Discord disconnected",
                            str(event.get("message") or "connection lost"))
        elif kind == "tokens":
            tokens = parse_token_event(event, self.wall_clock())
            source = "refresh" if self.refreshing else "authorize"
            self.refreshing = False
            if tokens is None:
                self._fault("auth", "discord_token_response_invalid", "Discord sent an unusable token response",
                            f"{source} token response was malformed")
                self.next_auth_at = now + self.auth_backoff.next()
                return
            self.redact.add(tokens.access_token, tokens.refresh_token)
            self.tokens = tokens
            self.token_sent = False
            self.auth_backoff.reset()
            self._persist(tokens)  # saved before the access token is used
            self._recover("auth", "discord_linked")
            log_info(self.proc_name, "discord_linked" if source == "authorize" else "discord_token_refreshed")
        elif kind == "auth_failed":
            result = event.get("result")
            source = event.get("source")
            if source == "refresh":
                self.refreshing = False
                if result == "unauthenticated":
                    # Discord rejected the refresh token (revoked or invalid):
                    # unlink and authorise again. Any other failure keeps the
                    # link and retries, so an outage never forces relinking.
                    self._clear_tokens()
                    self._fault("auth", "discord_link_lost", "Discord link expired",
                                "the refresh token was rejected; Discord must be linked again")
                else:
                    self._fault("auth", "discord_refresh_failed", "Discord token refresh failed",
                                str(event.get("message") or "temporary failure"))
                self.next_auth_at = now + self.auth_backoff.next()
            # The worker never starts authorization, so "authorize" failures
            # (from a stray event) need no handling here.

    # -- authentication progression
    def _advance_auth(self, now: float) -> None:
        bridge = self.bridge
        wall = self.wall_clock()
        if self.tokens is None:
            # Headless by design: linking opens a browser, so it is started only
            # by ``rich_presence.py --link`` in the graphical session. The token
            # file it writes is picked up by _check_token_file().
            return

        margin = float(self.discord["refresh_margin_s"])
        if not self.tokens.access_valid(wall, margin) and not self.refreshing and now >= self.next_auth_at:
            # Expired, or close enough to expiry: renew with the refresh token.
            response = bridge.request("refresh_token", self.request_timeout,
                                      refresh_token=self.tokens.refresh_token)
            if response.get("result") == "success":
                self.refreshing = True
            else:
                self._fault("auth", "discord_refresh_failed", "Discord token refresh failed",
                            str(response.get("message") or response.get("result")))
                self.next_auth_at = now + self.auth_backoff.next()
        if not self.tokens.access_valid(wall):
            return  # wait for the refreshed token
        if not self.token_sent and now >= self.next_auth_at:
            response = bridge.request("set_token", self.request_timeout, access_token=self.tokens.access_token)
            if response.get("result") in ("success", "ready"):
                self.token_sent = True
                if self.connection == DISCORD_CONNECTION_DISCONNECTED:
                    self.connection = DISCORD_CONNECTION_CONNECTING
            elif response.get("result") == "unauthenticated":
                self.tokens = DiscordTokens(self.tokens.access_token, self.tokens.refresh_token, 0, self.tokens.scope)
            else:
                self._fault("connection", "discord_connect_failed", "Discord connection failed",
                            str(response.get("message") or response.get("result")))
                self.next_auth_at = now + self.auth_backoff.next()

    # -- media and artwork
    def _observe_media(self) -> None:
        """Read the Deck's media state every tick, connected to Discord or not."""
        self.snapshot = read_media_snapshot(bus, self.snapshot)
        self.timing = self.timeline.update(self.snapshot, self.wall_clock())
        if self.artwork_auditor is not None:
            # Kept apart from presence: an audit failure can never affect it.
            try:
                self.artwork_auditor.observe(bus, self.snapshot)
            except Exception:
                pass

        try:
            self.artwork_url = self._select_artwork()
        except Exception:
            self.artwork_url = None  # artwork is optional; never disturbs presence

    def _select_artwork(self) -> Optional[str]:
        """The vetted artwork URL for the current snapshot, or None (static)."""
        snapshot = self.snapshot
        if snapshot is None or not dynamic_artwork_allowed(snapshot, self.policy):
            self._log_artwork_decision(None, "")
            return None
        # Plex: the Deck's own hosted cover while the artwork service is up,
        # then a public poster URL from Plex's metadata, then static artwork.
        candidates = []
        if snapshot.source == "plex" and self._artwork_service_available():
            candidates.append(("hosted", snapshot.hosted_art_url))
        candidates.append(("public" if snapshot.source == "plex" else "", snapshot.art_url))
        first_reason = None
        for origin, candidate in candidates:
            key = (snapshot.source, candidate)
            result = self._artwork_memo.get(key)
            if result is None:
                result = artwork_policy.vet_artwork_url(candidate, snapshot.source)
                if len(self._artwork_memo) > 64:
                    self._artwork_memo.clear()
                self._artwork_memo[key] = result
            if result[0] is not None:
                self._log_artwork_decision(snapshot.source, result[1], origin)
                return result[0]
            first_reason = first_reason or result[1]
        self._log_artwork_decision(snapshot.source, first_reason or "missing")
        return None

    def _artwork_service_available(self) -> bool:
        """artwork_service.py says it is listening and its heartbeat is fresh."""
        if bus.get_int(KEY_ARTWORK_SERVICE_READY, 0) != 1:
            return False
        beat = bus.get_int(KEY_ARTWORK_SERVICE_HEARTBEAT, 0)
        return beat != 0 and bus.elapsed_ms(bus.monotonic_ms(), beat) <= ARTWORK_SERVICE_FRESH_MS

    def _log_artwork_decision(self, source: Optional[str], reason: str, origin: str = "") -> None:
        """One fixed-word journal line per change of decision; never the URL."""
        decision = (source, reason, origin)
        if decision == self._artwork_decision:
            return
        self._artwork_decision = decision
        if source is not None:
            used = "dynamic" if reason == "accepted" else "static"
            via = f" origin={origin}" if origin and used == "dynamic" else ""
            log_event(self.proc_name, "artwork_selection",
                      {"decision": f"source={source} image={used}{via} reason={reason}"})

    def _log_artwork(self, info: Dict[str, object]) -> None:
        log_event(self.proc_name, "artwork_audit", {"audit": format_audit(info)})

    # -- presence
    def _update_presence(self, now: float) -> None:
        if self.connection != DISCORD_CONNECTION_READY or self.snapshot is None:
            return
        desired = build_presence(self.snapshot, self.policy, self.timing, self.artwork_url)
        if desired is not None:
            # Defence in depth: the small image is always a registered asset;
            # the large image is a registered asset or the URL vetted for this
            # very snapshot. The bridge enforces the same rules again.
            if "small_image" in desired and desired["small_image"] not in REGISTERED_ASSETS:
                desired.pop("small_image")
                desired.pop("small_text", None)
            large = desired.get("large_image")
            if large is not None and large not in REGISTERED_ASSETS and large != self.artwork_url:
                desired["large_image"] = ASSET_EMBER_DECK
        send, payload = self.scheduler.due(desired, now)
        if not send:
            return
        try:
            if payload is None:
                response = self.bridge.request("clear_presence", self.request_timeout)
            else:
                response = self.bridge.request("set_presence", self.request_timeout, presence=payload)
        except BridgeError as exc:
            self._fault("presence", "discord_presence_failed", "Discord presence update failed", str(exc))
            return
        if response.get("result") == "success":
            self.scheduler.sent(payload, now)
            self._recover("presence", "discord_presence_ok")
        else:
            self._fault("presence", "discord_presence_failed", "Discord presence update failed",
                        str(response.get("message") or response.get("result")))
            self.scheduler.sent(self.scheduler.published, now)  # respect the rate limit before retrying
            self.scheduler.mark_unpublished()

    # -- status
    def _compute_status(self) -> int:
        if not self.cfg["enabled"]:
            return DISCORD_STATUS_DISABLED
        if application_id(self.cfg) is None:
            return DISCORD_STATUS_NOT_CONFIGURED
        if self.bridge is None:
            if not self.backend_available:
                return DISCORD_STATUS_SDK_UNAVAILABLE
            return DISCORD_STATUS_RECONNECTING if self.bridge_crashed else DISCORD_STATUS_ERROR
        if self.tokens is None:
            return DISCORD_STATUS_NOT_LINKED
        if self.connection == DISCORD_CONNECTION_READY:
            return DISCORD_STATUS_CONNECTED
        if self.connection == DISCORD_CONNECTION_RECONNECTING:
            return DISCORD_STATUS_RECONNECTING
        return DISCORD_STATUS_CONNECTING

    def _auth_state(self) -> int:
        if self.tokens is None:
            return DISCORD_AUTH_UNLINKED
        return DISCORD_AUTH_REFRESHING if self.refreshing else DISCORD_AUTH_LINKED

    def _publish_status(self) -> None:
        status = self._compute_status()
        bus.set_int(KEY_ENABLED, int(bool(self.cfg["enabled"])))
        bus.set_int(KEY_BACKEND, self.backend_kind)
        bus.set_int(KEY_BACKEND_AVAILABLE, int(self.backend_available))
        bus.set_int(KEY_AUTH_STATE, self._auth_state())
        bus.set_int(KEY_CONNECTION_STATE, self.connection)
        if status != self.status:
            self.status = status
            bus.set_int(KEY_STATUS, status)
            bus.set_int(KEY_STATUS_SEQ, (bus.get_int(KEY_STATUS_SEQ, 0) + 1) & bus.MS_CLOCK_MASK)

    # -- main tick
    def tick(self, now: float) -> None:
        active = bool(self.cfg["enabled"]) and application_id(self.cfg) is not None
        if not active:
            if self.bridge is not None:
                self._drop_session()
            self._publish_status()
            return

        self._check_token_file()
        self._observe_media()

        if self.bridge is not None and not self.bridge.alive():
            # Unexpected exit: report the status and the redacted stderr tail.
            bridge = self.bridge
            code = bridge.exit_status()
            self._stop_bridge(clear=False)
            self.bridge_crashed = True
            details = self._with_stderr(f"exit status {code}", bridge)
            self.refreshing = False
            self._fault("bridge", "discord_bridge_crashed", "Discord bridge stopped unexpectedly", details)
            self.next_bridge_at = now + self.bridge_backoff.next()

        if self.bridge is None:
            if now < self.next_bridge_at or not self._start_bridge(now):
                self._publish_status()
                return

        try:
            for event in self.bridge.poll_events():
                self._handle_event(event, now)
            if self.bridge is not None:
                self._advance_auth(now)
                self._update_presence(now)
        except BridgeError as exc:
            self._fault("bridge", "discord_bridge_failed", "Discord bridge stopped responding", str(exc))
            self._stop_bridge(clear=False)
            self.bridge_crashed = True
            self.refreshing = False
            self.next_bridge_at = now + self.bridge_backoff.next()
        self._publish_status()

    def shutdown(self) -> None:
        self._drop_session()
        self.connection = DISCORD_CONNECTION_DISCONNECTED
        self._publish_status()


# ------------------------------------------------------------------ link / unlink

def link(cfg: Dict[str, Any], *, bridge_factory=BridgeProcess, backend_selector=select_backend,
         clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep, environ=os.environ) -> str:
    """Run one desktop authorization in a separate bridge and store the tokens.

    Returns a short outcome for the terminal. Tokens are never printed.
    """
    app_id = application_id(cfg)
    if app_id is None:
        return "no discord.application_id configured"
    if sys.platform.startswith("linux") and not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        return ("no graphical session (DISPLAY/WAYLAND_DISPLAY not set): run --link from a terminal "
                "in the Pi's desktop or a VNC session")
    choice = backend_selector(cfg["discord"], BASE)
    if choice.backend is None:
        return f"Discord bridge unavailable: {choice.reason}"
    timeout = float(cfg["discord"]["auth_timeout_s"])
    request_timeout = float(cfg["discord"]["request_timeout_s"])
    bridge = None
    try:
        bridge = bridge_factory(choice.backend)
        bridge.request("hello", request_timeout, protocol=1)
        if bridge.request("init", request_timeout, application_id=app_id).get("result") != "success":
            return "the Discord SDK could not start"
        if bridge.request("begin_auth", request_timeout).get("result") != "authorizing":
            return "linking could not start"
        deadline = clock() + timeout
        while clock() < deadline:
            if not bridge.alive():
                return f"the Discord bridge stopped (exit status {bridge.exit_status()})"
            for event in bridge.poll_events():
                if event.get("event") == "tokens":
                    tokens = parse_token_event(event, wall_clock())
                    if tokens is None:
                        return "Discord sent an unusable token response"
                    try:
                        TokenStore(auth_state_path(cfg)).save(tokens)
                    except (OSError, SecretFileError) as exc:
                        return f"linked, but the token file could not be written ({type(exc).__name__})"
                    return "linked"
                if event.get("event") == "auth_failed":
                    return f"linking did not complete ({event.get('result')})"
            sleep(0.25)
        try:
            bridge.request("cancel_auth", request_timeout)
        except BridgeError:
            pass
        return f"not approved within {int(timeout)} s"
    except BridgeError as exc:
        return f"Discord bridge error: {exc}"
    finally:
        if bridge is not None:
            bridge.stop()


def unlink(cfg: Dict[str, Any], *, bridge_factory=BridgeProcess, backend_selector=select_backend) -> str:
    """Revoke (if the bridge can run) and clear the stored Discord tokens."""
    store = TokenStore(auth_state_path(cfg))
    try:
        tokens = store.load()
    except SecretFileError:
        tokens = None
    revoked = "not attempted"
    app_id = application_id(cfg)
    if tokens is not None and app_id is not None:
        choice = backend_selector(cfg["discord"], BASE)
        if choice.backend is not None:
            bridge = None
            try:
                bridge = bridge_factory(choice.backend)
                bridge.request("hello", 5.0, protocol=1)
                bridge.request("init", 5.0, application_id=app_id)
                response = bridge.request("revoke", 10.0, token=tokens.refresh_token)
                revoked = "revoked" if response.get("result") == "success" else "revocation failed"
            except BridgeError:
                revoked = "revocation failed"
            finally:
                if bridge is not None:
                    bridge.stop()
    store.clear()
    return revoked


# ------------------------------------------------------------------ process entry

def main() -> None:
    cfg = load_cfg()
    proc_name = str(cfg["proc_name"])
    stopping = False

    def on_stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)
    worker = RichPresenceWorker(cfg)
    heartbeat = bus.Heartbeat(proc_name, on_beat=log_heartbeat)
    log_info(proc_name, "started")
    try:
        while not stopping:
            now = time.monotonic()
            heartbeat.tick(now)
            worker.tick(now)
            heartbeat.tick()
            time.sleep(0.25)
    finally:
        worker.shutdown()
        bus.close_all()
        log_info(proc_name, "stopped")


if __name__ == "__main__":
    try:
        if "--link" in sys.argv[1:]:
            print("Opening Discord's authorization page (Discord overlay or a browser on this device). "
                  "Approve Ember Deck there; the redirect returns to this device.", flush=True)
            outcome = link(load_cfg())
            print(f"Discord: {outcome}.")
            raise SystemExit(0 if outcome == "linked" else 1)
        elif "--unlink" in sys.argv[1:]:
            outcome = unlink(load_cfg())
            print(f"Discord tokens cleared ({outcome}). To also remove Ember Deck from your Discord "
                  "account, open Discord: User Settings > Authorized Apps.")
        else:
            main()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(2) from None
