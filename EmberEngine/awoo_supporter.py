"""Offline verification of shared Awoo supporter licences (format v1).

See AWOO-SUPPORTER-SYSTEM.md. A licence is one ASCII line::

    AWOO1.<kid>.<payload>.<signature>

``payload`` and ``signature`` are canonical, unpadded Base64URL. The Ed25519
signature covers the exact ASCII bytes ``AWOO1.<kid>.<payload>`` as received;
the JSON is never re-serialised before verification. Only after the signature
verifies is the payload parsed, and it must contain exactly::

    {"v": 1, "iss": "awoo.ltd", "ent": "awoo.supporter", "iat": <int>, "lid": "<uuid4>"}

There is no expiry, no device binding and no online check. Unknown versions
and key IDs fail closed. Ed25519 itself is provided by the ``cryptography``
package; nothing here implements cryptography.

Error messages carry a short reason code only. They never contain the
licence, its signature or its payload.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

LICENCE_PREFIX = "AWOO1"
ENTITLEMENT = "awoo.supporter"
ISSUER = "awoo.ltd"
MAX_LICENCE_LENGTH = 512
MAX_LICENCE_FILE_BYTES = 4096
MAX_SAFE_INTEGER = 2 ** 53 - 1

# The only production trust anchor. Same key as Noctaxis's production key
# store; the matching private key is held by awoo.ltd and never by Ember Deck.
PRODUCTION_PUBLIC_KEYS: Mapping[str, str] = {
    "awoo-2026-01": (
        "-----BEGIN PUBLIC KEY-----\n"
        "MCowBQYDK2VwAyEAL/MBg0lKfykWMVkZM1g1Z5LKxXskykdPmt/kyTFNw6s=\n"
        "-----END PUBLIC KEY-----\n"
    ),
}

_KID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_PAYLOAD_KEYS = frozenset({"v", "iss", "ent", "iat", "lid"})


class LicenceError(ValueError):
    """A licence was rejected. ``reason`` is a stable, content-free code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SupporterLicence:
    """A verified licence. Holds no signature and no raw licence text."""

    kid: str
    issued_at: int
    licence_id: str = ""

    def __repr__(self) -> str:  # never echo identifiers into logs by accident
        return f"SupporterLicence(kid={self.kid!r}, entitlement={ENTITLEMENT!r})"


def _load_key(value: Any):
    """Accept an Ed25519 public key object or an SPKI PEM string/bytes."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        raise LicenceError("crypto_unavailable") from None
    if isinstance(value, Ed25519PublicKey):
        return value
    try:
        key = load_pem_public_key(value.encode("ascii") if isinstance(value, str) else bytes(value))
    except Exception:
        raise LicenceError("bad_trust_key") from None
    if not isinstance(key, Ed25519PublicKey):
        raise LicenceError("bad_trust_key")
    return key


def _b64url_decode(text: str) -> bytes:
    """Decode canonical, unpadded Base64URL; reject anything else."""
    if not text or not _B64URL.match(text) or len(text) % 4 == 1:
        raise LicenceError("noncanonical_base64url")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise LicenceError("noncanonical_base64url") from None
    # Non-zero trailing bits decode "successfully" but do not round-trip.
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != text:
        raise LicenceError("noncanonical_base64url")
    return raw


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LicenceError("bad_payload")
        result[key] = value
    return result


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def verify_licence(text: Any, *, trusted_keys: Optional[Mapping[str, Any]] = None) -> SupporterLicence:
    """Verify one licence string and return its entitlement facts.

    ``trusted_keys`` maps key IDs to public keys. Production callers leave it
    unset, which trusts only PRODUCTION_PUBLIC_KEYS; tests inject their own.
    """
    keys = PRODUCTION_PUBLIC_KEYS if trusted_keys is None else trusted_keys
    if not isinstance(text, str):
        raise LicenceError("malformed")
    if len(text) > MAX_LICENCE_FILE_BYTES:
        raise LicenceError("too_long")
    # Only ASCII whitespace around the whole licence is forgiven (a trailing
    # newline from a text editor); str.strip() would also accept Unicode spaces.
    licence = text.strip(" \t\r\n")
    if not licence:
        raise LicenceError("missing")
    if len(licence) > MAX_LICENCE_LENGTH:
        raise LicenceError("too_long")
    if not licence.isascii() or not licence.isprintable() or any(ch.isspace() for ch in licence):
        raise LicenceError("malformed")

    parts = licence.split(".")
    if parts[0] != LICENCE_PREFIX:
        raise LicenceError("unsupported_version" if parts[0].startswith("AWOO") else "malformed")
    if len(parts) != 4:
        raise LicenceError("malformed")
    _, kid, payload_text, signature_text = parts
    if not _KID.match(kid):
        raise LicenceError("malformed")
    if kid not in keys:
        raise LicenceError("unknown_key")

    payload_bytes = _b64url_decode(payload_text)
    signature = _b64url_decode(signature_text)
    if len(signature) != 64:
        raise LicenceError("bad_signature")

    key = _load_key(keys[kid])
    signed = f"{LICENCE_PREFIX}.{kid}.{payload_text}".encode("ascii")
    try:
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        raise LicenceError("crypto_unavailable") from None
    try:
        key.verify(signature, signed)
    except InvalidSignature:
        raise LicenceError("bad_signature") from None

    # Only now is the (authenticated) payload interpreted.
    try:
        payload = json.loads(payload_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except LicenceError:
        raise
    except (UnicodeDecodeError, ValueError):
        raise LicenceError("bad_payload") from None
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        raise LicenceError("bad_payload")
    if not _is_int(payload["v"]) or payload["v"] != 1:
        raise LicenceError("unsupported_version")
    if payload["iss"] != ISSUER:
        raise LicenceError("wrong_issuer")
    if payload["ent"] != ENTITLEMENT:
        raise LicenceError("wrong_entitlement")
    iat = payload["iat"]
    if not _is_int(iat) or not 0 <= iat <= MAX_SAFE_INTEGER:
        raise LicenceError("bad_payload")
    lid = payload["lid"]
    if not isinstance(lid, str) or not _UUID4.match(lid):
        raise LicenceError("bad_payload")
    return SupporterLicence(kid=kid, issued_at=iat, licence_id=lid)


def read_licence_file(path: Path) -> str:
    """Read a licence file's text; raise LicenceError('missing'/'unreadable'/'too_long')."""
    path = Path(os.path.expanduser(str(path)))
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise LicenceError("missing") from None
    except OSError:
        raise LicenceError("unreadable") from None
    if size > MAX_LICENCE_FILE_BYTES:
        raise LicenceError("too_long")
    try:
        raw = path.read_bytes()
    except OSError:
        raise LicenceError("unreadable") from None
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        raise LicenceError("malformed") from None


REASON_TEXT = {
    "missing": "no supporter licence installed",
    "unreadable": "the supporter licence file cannot be read",
    "too_long": "the supporter licence is too long",
    "malformed": "the supporter licence is not in AWOO1 format",
    "unsupported_version": "the supporter licence version is not supported",
    "unknown_key": "the supporter licence was signed with an unknown key",
    "noncanonical_base64url": "the supporter licence encoding is invalid",
    "bad_signature": "the supporter licence signature is invalid",
    "bad_payload": "the supporter licence contents are invalid",
    "wrong_issuer": "the supporter licence was not issued by awoo.ltd",
    "wrong_entitlement": "the licence does not grant awoo.supporter",
    "crypto_unavailable": "the Python 'cryptography' package is not installed",
    "bad_trust_key": "the built-in trust key could not be loaded",
}


def describe(reason: str) -> str:
    """Human-readable, content-free explanation of a reason code."""
    return REASON_TEXT.get(reason, "the supporter licence was rejected")
