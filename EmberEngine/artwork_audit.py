"""Artwork audit for Discord Rich Presence: observe, classify, never transmit.

``presence.artwork_mode: audit`` inspects which artwork each source currently
offers so that a future dynamic-artwork feature can be designed from real
observations. It never changes what is sent to Discord: presence always uses
the registered ``ember-deck`` and ``source-*`` assets.

Everything here treats artwork references as untrusted (including MPRIS
``artUrl`` values) and reports only coarse, non-identifying facts:

    source=ytmusic kind=https has_query=false host=known_cdn classification=public_candidate
    source=plex kind=plex_server classification=authenticated
    source=dab kind=file classification=local
    source=bluetooth kind=none classification=missing

Full URLs, query strings, credentials, local paths and hostnames are never
logged; exceptions are reduced to a fixed classification.
"""

from __future__ import annotations

import ipaddress
from typing import Callable, Dict, Optional
from urllib.parse import parse_qsl, urlsplit

from presence_model import (
    SOURCE_BLUETOOTH,
    SOURCE_DAB,
    SOURCE_PLEX,
    SOURCE_YTMUSIC,
    MediaSnapshot,
    mpris_text_width,
    read_raw_text,
)

MAX_URL_CHARS = 8192

# Query parameter names that indicate a credential or signed access.
_SENSITIVE_PARAMS = ("token", "key", "sig", "signature", "auth", "access", "secret", "session", "password",
                     "credential", "expires", "policy")
# Public image CDNs commonly behind YouTube Music / Google artwork. Only this
# coarse fact ("known_cdn") is logged, never the hostname itself.
_KNOWN_CDN_SUFFIXES = (".googleusercontent.com", ".ytimg.com", ".ggpht.com")
_LOCAL_NAME_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa")


def classify_url(raw: Optional[str]) -> Dict[str, object]:
    """Classify one artwork reference without retaining any part of it."""
    text = (raw or "").strip()
    if not text or text.upper() == "N/A":
        return {"kind": "none", "classification": "missing"}
    if len(text) > MAX_URL_CHARS:
        return {"kind": "other", "classification": "invalid"}
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if scheme == "file":
            return {"kind": "file", "classification": "local"}
        if scheme == "data":
            return {"kind": "data", "classification": "embedded"}
        if scheme not in ("http", "https"):
            return {"kind": "other", "classification": "unsupported_scheme"}
        host = (parts.hostname or "").rstrip(".").lower()
        has_query = bool(parts.query)
        info: Dict[str, object] = {"kind": scheme, "has_query": has_query}
        if not host:
            info["classification"] = "invalid"
            return info
        if parts.username is not None or parts.password is not None:
            info["classification"] = "authenticated"
            return info
        if has_query:
            names = [name.lower() for name, _ in parse_qsl(parts.query, keep_blank_values=True)]
            if any(marker in name for name in names for marker in _SENSITIVE_PARAMS):
                info["classification"] = "authenticated"
                return info
        if _is_local_host(host):
            info["classification"] = "private_network"
            return info
        info["host"] = "known_cdn" if host.endswith(_KNOWN_CDN_SUFFIXES) else "other"
        info["classification"] = "public_candidate" if scheme == "https" else "public_insecure"
        return info
    except Exception:  # malformed input: never echo it
        return {"kind": "other", "classification": "invalid"}


def _is_local_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(_LOCAL_NAME_SUFFIXES)
    return not address.is_global


def inspect_source(bus, source: str) -> Dict[str, object]:
    """Return the classification for the artwork the given source offers now."""
    if source == SOURCE_PLEX:
        # Minstrel's optional public poster URL, if it found one; otherwise
        # the artwork exists only on the user's Plex server (token-bearing,
        # never published as a URL).
        public = read_raw_text(bus, "/plex/art_public_url_utf8", 512)
        if public and public.strip():
            return classify_url(public)
        available = bus.get_int("/plex/art_valid", 0) == 1
        return ({"kind": "plex_server", "classification": "authenticated"} if available
                else {"kind": "none", "classification": "missing"})
    if source in (SOURCE_DAB, SOURCE_YTMUSIC):
        return classify_url(read_raw_text(bus, "/mpris/art_url_utf8", mpris_text_width(bus)))
    if source == SOURCE_BLUETOOTH:
        return {"kind": "none", "classification": "missing"}  # AVRCP cover art is not used
    return {"kind": "none", "classification": "missing"}


class ArtworkAuditor:
    """Log one sanitised line whenever a playing source's artwork class changes."""

    def __init__(self, log: Callable[[Dict[str, object]], None]):
        self._log = log
        self._last: Dict[str, Dict[str, object]] = {}

    def observe(self, bus, snapshot: MediaSnapshot) -> None:
        if snapshot.playback != "playing" or snapshot.source is None:
            return
        info = inspect_source(bus, snapshot.source)
        if self._last.get(snapshot.source) == info:
            return
        self._last[snapshot.source] = info
        self._log({"source": snapshot.source, **info})


def format_audit(info: Dict[str, object]) -> str:
    """``source=… kind=… has_query=… host=… classification=…`` (values are fixed words)."""
    order = ("source", "kind", "has_query", "host", "classification")
    return " ".join(f"{key}={str(info[key]).lower()}" for key in order if key in info)
