"""Plex credentials, cached connection state and unattended re-authentication.

Minstrel's tracked ``configs/minstrel.yaml`` holds only non-secret settings.
Credentials and the refreshed server URL/token live in the gitignored
``configs/minstrel_secrets.yaml`` (mode 0600, see ``secret_store.py``)::

    version: 1
    credentials:          # written by the operator, never rewritten by Minstrel
      username: ...
      password: ...
    cache:                # rewritten by Minstrel after each account login
      url: https://...:32400
      token: ...

Precedence, highest first, per value:

* ``username``/``password``: ``EMBERDECK_PLEX_USERNAME``/``_PASSWORD``, then the
  secrets file ``credentials``, then legacy values left in ``minstrel.yaml``.
* direct ``url`` + ``token`` (used as a pair, tried first for a fast start):
  ``EMBERDECK_PLEX_URL``/``_TOKEN``, then the secrets file ``cache``, then
  legacy values in ``minstrel.yaml``.
* ``server_name`` and ``plex_client_name``: ``minstrel.yaml``, overridden by
  their ``EMBERDECK_PLEX_*`` variables (applied by ``minstrel.load_cfg``).

A token is only ever a shortcut. If it fails and account credentials exist, it
is marked unusable and the account login runs at once; the fresh token then
replaces the cache. A stale token can therefore cost one failed request, but
can never block a working username/password.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from contracts import (
    PLEX_AUTH_FAILED,
    PLEX_CONNECTED,
    PLEX_CONNECTING,
    PLEX_UNAVAILABLE,
    PLEX_UNCONFIGURED,
)
from runtime_config import ConfigError
from secret_store import (
    Redactor,
    SecretFileError,
    permission_problem,
    read_private_yaml,
    write_private_yaml,
)
from whisper_daemon import log_error, log_event, log_info, log_recovery

SECRETS_FILENAME = "minstrel_secrets.yaml"
ENV_USERNAME = "EMBERDECK_PLEX_USERNAME"
ENV_PASSWORD = "EMBERDECK_PLEX_PASSWORD"
ENV_URL = "EMBERDECK_PLEX_URL"
ENV_TOKEN = "EMBERDECK_PLEX_TOKEN"

# Plex request URLs carry the token in the query string (also URL-encoded).
PLEX_TOKEN_QUERY = re.compile(r"(X-Plex-Token(?:=|%3D))[^&\s'\"]+", re.IGNORECASE)

UNCONFIGURED_RECHECK_S = 30.0

FAULT_UNCONFIGURED = "minstrel:plex:unconfigured"
FAULT_CONNECTION = "minstrel:plex:connection"
FAULT_LOGIN = "minstrel:plex:login"
FAULT_STATE_WRITE = "minstrel:plex:state_write"
FAULT_CONFIG = "minstrel:plex:config"


def new_redactor() -> Redactor:
    return Redactor([PLEX_TOKEN_QUERY])


def is_auth_failure(exc: Exception) -> bool:
    """Return True when Plex rejected credentials or a token.

    plexapi does not use one consistent exception type for every 401 path, so
    inspect both an attached HTTP response and the message text.
    """
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in (401, 403):
        return True
    text = str(exc).lower()
    markers = ("401", "403", "unauthorized", "forbidden", "not authorized",
               "authentication", "token is invalid", "token invalid", "invalid credentials")
    return any(marker in text for marker in markers)


def secrets_path_for(cfg: Mapping[str, Any]) -> Path:
    """Resolve ``secrets_path`` relative to the directory holding minstrel.yaml."""
    path = Path(os.path.expanduser(str(cfg.get("secrets_path") or SECRETS_FILENAME)))
    if not path.is_absolute():
        base = cfg.get("_config_dir") or Path(__file__).resolve().parent / "configs"
        path = Path(base) / path
    return path


# ------------------------------------------------------------------ secret file


def _check_section(path: Path, data: Mapping[str, Any], section: str, keys: Tuple[str, ...]) -> Dict[str, Optional[str]]:
    value = data.get(section)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SecretFileError(f"{path}: '{section}' must be a mapping")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise SecretFileError(f"{path}: unknown option(s) in '{section}': {', '.join(map(str, unknown))}")
    for key, item in value.items():
        if item is not None and not isinstance(item, str):
            # Name the option, never the value: it may be a password.
            raise SecretFileError(f"{path}: '{section}.{key}' must be a string or null")
    return {key: value.get(key) for key in keys}


def parse_secret_document(path: Path, data: Any) -> Tuple[Dict[str, Optional[str]], Dict[str, Optional[str]]]:
    """Validate a parsed secrets file; return ``(credentials, cache)``."""
    if data is None:
        return {}, {}
    if not isinstance(data, dict):
        raise SecretFileError(f"{path}: the file must contain a mapping")
    unknown = sorted(set(data) - {"version", "credentials", "cache"})
    if unknown:
        raise SecretFileError(f"{path}: unknown option(s): {', '.join(map(str, unknown))}")
    if data.get("version", 1) != 1:
        raise SecretFileError(f"{path}: unsupported version (expected 1)")
    return (_check_section(path, data, "credentials", ("username", "password")),
            _check_section(path, data, "cache", ("url", "token")))


def store_cached_connection(path: Path, url: str, token: str) -> None:
    """Replace only the ``cache`` section, keeping the operator's credentials."""
    data = read_private_yaml(path)
    parse_secret_document(path, data)  # never overwrite a file we cannot understand
    document: Dict[str, Any] = dict(data or {})
    document.setdefault("version", 1)
    document["cache"] = {"url": url, "token": token}
    write_private_yaml(path, document)


# ------------------------------------------------------------------ resolution


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first(*candidates: Tuple[str, Any]) -> Tuple[Optional[str], str]:
    for source, value in candidates:
        text = _text(value)
        if text is not None:
            return text, source
    return None, ""


@dataclass(frozen=True)
class PlexAuth:
    """Resolved Plex identity and login material. ``repr`` hides secrets."""

    client_name: Optional[str] = None
    server_name: Optional[str] = None
    username: Optional[str] = field(default=None, repr=False)
    password: Optional[str] = field(default=None, repr=False)
    url: Optional[str] = None
    token: Optional[str] = field(default=None, repr=False)
    credential_source: str = ""
    token_source: str = ""
    secrets_path: Optional[Path] = None
    problems: Tuple[Tuple[str, str, str], ...] = ()  # (fault key, event, message)

    @property
    def has_account(self) -> bool:
        return bool(self.username and self.password and self.server_name)

    @property
    def has_token(self) -> bool:
        return bool(self.url and self.token)

    @property
    def configured(self) -> bool:
        return bool(self.client_name) and (self.has_account or self.has_token)

    def missing_description(self) -> str:
        needs = []
        if not self.client_name:
            needs.append("set plex_client_name in minstrel.yaml (or EMBERDECK_PLEX_CLIENT_NAME)")
        if not (self.has_account or self.has_token):
            needs.append(
                f"add credentials.username/password to {self.secrets_path or SECRETS_FILENAME} "
                "and server_name to minstrel.yaml (or EMBERDECK_PLEX_* environment values)"
            )
        return "; ".join(needs)


def resolve_plex_auth(cfg: Mapping[str, Any], environ: Mapping[str, str]) -> PlexAuth:
    """Apply the documented precedence. Never raises for a bad secrets file."""
    path = secrets_path_for(cfg)
    problems = []
    credentials: Dict[str, Optional[str]] = {}
    cache: Dict[str, Optional[str]] = {}
    try:
        credentials, cache = parse_secret_document(path, read_private_yaml(path))
    except SecretFileError as exc:
        problems.append(("minstrel:plex:secrets_file", "plex_secrets_file_invalid", f"{exc}; ignoring it"))
    mode_issue = permission_problem(path)
    if mode_issue:
        problems.append(("minstrel:plex:secrets_mode", "plex_secrets_file_permissions", mode_issue))
    legacy = [key for key in ("username", "password", "url", "token") if _text(cfg.get(key))]
    if legacy:
        problems.append((
            "minstrel:plex:legacy_credentials", "plex_credentials_in_tracked_config",
            f"minstrel.yaml contains {', '.join(legacy)}; move them to {path.name} (gitignored, mode 0600)",
        ))

    username, user_source = _first(("environment", environ.get(ENV_USERNAME)),
                                   ("secrets file", credentials.get("username")),
                                   ("minstrel.yaml", cfg.get("username")))
    password, _ = _first(("environment", environ.get(ENV_PASSWORD)),
                         ("secrets file", credentials.get("password")),
                         ("minstrel.yaml", cfg.get("password")))
    url = token = None
    token_source = ""
    for source, pair_url, pair_token in (
        ("environment", environ.get(ENV_URL), environ.get(ENV_TOKEN)),
        ("cache", cache.get("url"), cache.get("token")),
        ("minstrel.yaml", cfg.get("url"), cfg.get("token")),
    ):
        if _text(pair_url) and _text(pair_token):
            url, token, token_source = _text(pair_url), _text(pair_token), source
            break

    return PlexAuth(
        client_name=_text(cfg.get("plex_client_name")),
        server_name=_text(cfg.get("server_name")),
        username=username,
        password=password,
        url=url,
        token=token,
        credential_source=user_source,
        token_source=token_source,
        secrets_path=path,
        problems=tuple(problems),
    )


# ------------------------------------------------------------------ connection state machine


class PlexConnector:
    """Owns Minstrel's Plex connection without ever exiting the worker.

    ``poll(now)`` returns a connected server or ``None``. All waiting is by
    deadline, never sleep, so Minstrel's loop and heartbeat keep running.
    Failures back off exponentially from ``plex_retry_initial_s`` to
    ``plex_retry_max_s``; the delay resets only after a successful session
    query (``mark_healthy``), so a server that connects and then fails at once
    cannot cause rapid account logins.
    """

    def __init__(
        self,
        cfg_loader: Callable[[], Dict[str, Any]],
        *,
        environ: Mapping[str, str],
        server_factory: Callable[..., Any],
        account_factory: Callable[..., Any],
        proc_name: str = "minstrel",
        redact: Optional[Redactor] = None,
        initial_cfg: Optional[Dict[str, Any]] = None,
    ):
        self.cfg_loader = cfg_loader
        self.cfg = initial_cfg if initial_cfg is not None else cfg_loader()
        self.environ = environ
        self.server_factory = server_factory
        self.account_factory = account_factory
        self.proc_name = proc_name
        self.redact = redact or new_redactor()
        self.auth = resolve_plex_auth(self.cfg, environ)
        self.state = PLEX_CONNECTING
        self.server = None
        self.retry_s = self._retry_initial()
        self.next_attempt_at = 0.0
        self.token_rejected = False
        self.active_faults: Dict[str, str] = {}

    # -- configuration helpers
    def _retry_initial(self) -> float:
        return max(0.5, float(self.cfg.get("plex_retry_initial_s", 2.0)))

    def _retry_max(self) -> float:
        return max(self._retry_initial(), float(self.cfg.get("plex_retry_max_s", 300.0)))

    def _timeout(self) -> float:
        return max(0.5, float(self.cfg.get("plex_timeout_s", 5.0)))

    @property
    def client_name(self) -> Optional[str]:
        return self.auth.client_name

    # -- logging
    def _fault(self, key: str, event: str, summary: str, details: str) -> None:
        details = self.redact(details)
        if self.active_faults.get(key) == details:
            return  # already reported; Whisper also deduplicates
        self.active_faults[key] = details
        log_error(self.proc_name, summary, details, event=event, dedupe_key=key)

    def _recover(self, key: str, event: str) -> None:
        if self.active_faults.pop(key, None) is not None:
            log_recovery(self.proc_name, event, recovers_key=key)

    # -- public API
    def poll(self, now: float):
        if self.server is None and now >= self.next_attempt_at:
            self._attempt(now)
        return self.server

    def mark_healthy(self) -> None:
        self.retry_s = self._retry_initial()

    def connection_lost(self, exc: Exception, now: float) -> None:
        """A session query failed on a connected server."""
        self.server = None
        if is_auth_failure(exc) and self.auth.has_account:
            self.token_rejected = True
        self.state = PLEX_AUTH_FAILED if is_auth_failure(exc) else PLEX_UNAVAILABLE
        self._fault(FAULT_CONNECTION, "plex_query_failed", "Error querying Plex", str(exc))
        self._schedule_retry(now)

    # -- internals
    def _schedule_retry(self, now: float) -> None:
        self.next_attempt_at = now + self.retry_s
        self.retry_s = min(self.retry_s * 2.0, self._retry_max())

    def _reload(self) -> None:
        try:
            self.cfg = self.cfg_loader()
            self._recover(FAULT_CONFIG, "plex_config_valid")
        except ConfigError as exc:
            self._fault(FAULT_CONFIG, "plex_config_invalid",
                        "minstrel.yaml is invalid; keeping the settings Minstrel started with", str(exc))
        self.auth = resolve_plex_auth(self.cfg, self.environ)
        self.redact.add(self.auth.password, self.auth.token)
        current = {key for key, _, _ in self.auth.problems}
        for key, event, message in self.auth.problems:
            self._fault(key, event, "Plex secrets need attention", message)
        for key in [k for k in self.active_faults if k.startswith("minstrel:plex:secrets") or k.endswith("legacy_credentials")]:
            if key not in current:
                self._recover(key, "plex_secrets_ok")
        if not self.auth.has_account:
            self.token_rejected = False

    def _attempt(self, now: float) -> None:
        self._reload()
        auth = self.auth
        if not auth.configured:
            self.state = PLEX_UNCONFIGURED
            self._fault(FAULT_UNCONFIGURED, "plex_not_configured", "Plex is not configured",
                        auth.missing_description())
            self.next_attempt_at = now + UNCONFIGURED_RECHECK_S
            return
        self._recover(FAULT_UNCONFIGURED, "plex_configured")
        self.state = PLEX_CONNECTING
        timeout = self._timeout()

        if auth.has_token and not self.token_rejected:
            try:
                server = self.server_factory(auth.url, auth.token, timeout=timeout)
                # PlexServer is lazy about some requests; force an authenticated one.
                server.library.sections()
                self._connected(server, f"{auth.token_source} token", now)
                return
            except Exception as exc:
                if not auth.has_account:
                    self._failed(exc, now, login=False)
                    return
                self.token_rejected = True
                log_info(self.proc_name, "plex_token_rejected_trying_account_login")

        try:
            account = self.account_factory(auth.username, auth.password, timeout=timeout)
            resource = account.resource(auth.server_name)
            if resource is None:
                raise RuntimeError(f"Plex server not found on this account: {auth.server_name!r}")
            server = resource.connect(timeout=timeout)
            server.library.sections()
            token = getattr(server, "_token", None)
            url = getattr(server, "_baseurl", None)
            if not token or not url:
                raise RuntimeError("Plex account login did not yield a server URL and token")
        except Exception as exc:
            self._failed(exc, now, login=True)
            return
        self.redact.add(token)
        self.token_rejected = False
        self._persist(url, token)
        self._connected(server, "account login", now)

    def _persist(self, url: str, token: str) -> None:
        path = self.auth.secrets_path or secrets_path_for(self.cfg)
        try:
            store_cached_connection(path, url, token)
            self._recover(FAULT_STATE_WRITE, "plex_auth_state_saved")
        except (SecretFileError, OSError) as exc:
            # Keep running on the in-memory token; the next start logs in again.
            self._fault(FAULT_STATE_WRITE, "plex_auth_state_write_failed",
                        "Could not save refreshed Plex login state", str(exc))

    def _connected(self, server: Any, via: str, now: float) -> None:
        self.server = server
        self.state = PLEX_CONNECTED
        self.next_attempt_at = 0.0
        for key in (FAULT_CONNECTION, FAULT_LOGIN):
            self._recover(key, "plex_connection_recovered")
        log_event(self.proc_name, "connected", {
            "server": str(getattr(server, "friendlyName", "Plex")),
            "via": via,
        })

    def _failed(self, exc: Exception, now: float, *, login: bool) -> None:
        self.server = None
        rejected = is_auth_failure(exc)
        self.state = PLEX_AUTH_FAILED if rejected else PLEX_UNAVAILABLE
        if login:
            self._fault(FAULT_LOGIN, "plex_login_failed" if rejected else "plex_connection_failed",
                        "Plex account login failed", str(exc))
        else:
            self._fault(FAULT_CONNECTION, "plex_connection_failed", "Failed to connect to Plex", str(exc))
        self._schedule_retry(now)
