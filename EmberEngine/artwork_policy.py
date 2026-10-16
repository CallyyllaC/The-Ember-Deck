"""Which external artwork URLs may be shown in Discord Rich Presence.

With ``presence.artwork_mode: on`` the large image may be the playing item's
own artwork, as published over MPRIS (DAB via Shortwave, YT Music via Pear).
The Deck never fetches it: the URL is only handed to Discord. The concern is
therefore not the image host but what the URL itself would reveal, so a URL
is rejected when it would expose:

* credentials: user info, or query parameters whose names look like tokens,
  keys, signatures, sessions or passwords (e.g. ``X-Plex-Token``,
  ``access_token``, ``X-Amz-Signature``);
* the local network: ``localhost``, single-label or local-suffix names
  (``.local``, ``.lan``, ``.home.arpa``, ...), ``*.plex.direct`` (which encodes
  a Plex server's address) and IP-address literals (public artwork is
  published by hostname; an address usually means a LAN server);
* anything that is not an ordinary https URL: other schemes (``file:``,
  ``data:``, ``http:``), explicit ports, fragments, control characters or
  more than 300 characters (the SDK's image-field limit).

Ordinary public https URLs from any provider, including ordinary query
parameters, are accepted. For Plex the only candidate is the selected album
poster's original public URL, which Minstrel publishes (``public_artwork_lookup``)
after the same vetting; Plex server artwork is never a candidate. Bluetooth
has none. The C++ bridge repeats the basic checks (``protocol::external_image``).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

MAX_IMAGE_URL_CHARS = 300  # ActivityAssets::SetLargeImage: 1..300 characters

# Sources whose artwork reference may be used (MPRIS artUrl; Plex public poster).
DYNAMIC_ARTWORK_SOURCES = frozenset({"dab", "ytmusic", "plex"})

_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".localhost",
                   ".plex.direct", ".arpa", ".onion", ".test", ".invalid")
# Query parameter names that indicate credentials or signed access.
_SENSITIVE_PARAM = re.compile(r"token|auth|key|sig|secret|passw|session|credential|x-plex|x-amz-|x-goog-",
                              re.IGNORECASE)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _is_local_name(host: str) -> bool:
    return host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES)


def vet_artwork_url(raw: Optional[str], source: Optional[str]) -> Tuple[Optional[str], str]:
    """Return ``(url, "accepted")`` or ``(None, reason)``; reasons are fixed words."""
    text = (raw or "").strip()
    if not text or text.upper() == "N/A":
        return None, "missing"
    if source not in DYNAMIC_ARTWORK_SOURCES:
        return None, "unsupported_source"
    if len(text) > MAX_IMAGE_URL_CHARS:
        return None, "too_long"
    if not text.isascii() or any(ch.isspace() or ord(ch) < 0x21 or ch == "\x7f" for ch in text):
        return None, "invalid"
    try:
        parts = urlsplit(text)
        port = parts.port
        params = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return None, "invalid"
    if parts.scheme != "https":
        return None, "not_https"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None, "credentials"
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return None, "invalid"
    if _is_ip_literal(host):
        return None, "ip_address"      # artwork is published by name; an address is usually a LAN server
    if _is_local_name(host.lower()):
        return None, "private_network"
    if port is not None:
        return None, "port"
    if parts.fragment or "#" in text:
        return None, "fragment"
    if any(_SENSITIVE_PARAM.search(name) for name, _ in params) or "x-plex-token" in text.lower():
        return None, "credentials"
    return text, "accepted"
