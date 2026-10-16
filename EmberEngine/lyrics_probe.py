#!/usr/bin/env python3
"""Read-only diagnostic for EmberDeck/Plex lyric retrieval.

Run while a known problematic track is playing in the configured Plexamp client:
    cd ~/EmberDeck
    source .venv/bin/activate
    python lyrics_probe.py

It never writes config, shared memory, or Plex metadata.  It deliberately does
not print lyric text, only stream metadata and response/parse diagnostics.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

from plex_auth import new_redactor, resolve_plex_auth

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "configs" / "minstrel.yaml"
REDACT = new_redactor()
LRC_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def load_cfg() -> dict[str, Any]:
    """Load minstrel.yaml plus the identity environment overrides Minstrel uses."""
    if not CFG_PATH.exists():
        raise FileNotFoundError(f"Missing config: {CFG_PATH}")
    with CFG_PATH.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    for env_name, option in (("EMBERDECK_PLEX_SERVER_NAME", "server_name"),
                             ("EMBERDECK_PLEX_CLIENT_NAME", "plex_client_name")):
        if env_name in os.environ:
            cfg[option] = os.environ[env_name]
    cfg["_config_dir"] = str(CFG_PATH.parent)
    return cfg


def connect(cfg: dict[str, Any]):
    """Use Minstrel's credential precedence (env, minstrel_secrets.yaml, legacy YAML)."""
    auth = resolve_plex_auth(cfg, os.environ)
    REDACT.add(auth.password, auth.token)
    for _, _, problem in auth.problems:
        print(f"Note: {problem}")
    if auth.has_token:
        try:
            plex = PlexServer(auth.url, auth.token)
            _ = plex.friendlyName
            print(f"Connected using the {auth.token_source} token.")
            return plex
        except Exception as exc:
            print(f"Saved token failed: {type(exc).__name__}: {REDACT(exc)}")

    if not auth.has_account:
        raise RuntimeError("No working token and no username/password/server_name fallback.")

    account = MyPlexAccount(auth.username, auth.password)
    plex = account.resource(auth.server_name).connect()
    print("Connected using username/password fallback.")
    return plex


def find_session(plex, wanted_title: str | None):
    """Handle the find session lifecycle step."""
    sessions = plex.sessions()
    candidates = []
    for sess in sessions:
        player = getattr(sess, "player", None)
        if not player:
            continue
        if wanted_title and getattr(player, "title", None) != wanted_title:
            continue
        candidates.append(sess)

    if candidates:
        return candidates[0]

    print("No matching Plexamp session found. Active players:")
    for sess in sessions:
        player = getattr(sess, "player", None)
        if player:
            print(f"  title={getattr(player, 'title', None)!r} product={getattr(player, 'product', None)!r} state={getattr(player, 'state', None)!r}")
    return None


def describe_obj_stream(stream: Any) -> dict[str, Any]:
    """Return the describe obj stream result."""
    return {
        "id": getattr(stream, "id", None),
        "key": getattr(stream, "key", None),
        "timed": bool(getattr(stream, "timed", False)),
        "format": getattr(stream, "format", None),
        "codec": getattr(stream, "codec", None),
        "provider": getattr(stream, "provider", None),
        "title": getattr(stream, "extendedDisplayTitle", None) or getattr(stream, "displayTitle", None),
    }


def extract_xml_streams(plex, metadata_key: str) -> list[dict[str, Any]]:
    """Return the extract xml streams result."""
    root = plex.query(metadata_key)
    out: list[dict[str, Any]] = []
    for elem in root.iter("Stream"):
        attrs = elem.attrib
        if attrs.get("streamType") != "4":
            continue
        out.append({
            "id": attrs.get("id"),
            "key": attrs.get("key"),
            "timed": attrs.get("timed") in {"1", "true", "True"},
            "format": attrs.get("format"),
            "codec": attrs.get("codec"),
            "provider": attrs.get("provider"),
            "title": attrs.get("extendedDisplayTitle") or attrs.get("displayTitle"),
        })
    return out


def parse_count(text: str) -> int:
    """Parse count."""
    count = 0
    for raw in text.splitlines():
        if LRC_TS.search(raw) and LRC_TS.sub("", raw).strip():
            count += len(LRC_TS.findall(raw))
    return count


def probe_stream(plex, stream: dict[str, Any]) -> None:
    """Probe stream."""
    key = stream.get("key")
    if not key:
        print("    no stream key, cannot request content")
        return

    base = plex.url(key, includeToken=True)
    for label, params in (("bare", None), ("format=lrc", {"format": "lrc"})):
        try:
            response = plex._session.get(
                base,
                headers=plex._headers(),
                params=params,
                timeout=8,
            )
            content_type = response.headers.get("content-type", "?")
            text = response.text if response.ok else ""
            print(
                f"    GET {label:<10} status={response.status_code} type={content_type!r} "
                f"bytes={len(response.content)} lrc_entries={parse_count(text)}"
            )
        except Exception as exc:
            print(f"    GET {label:<10} ERROR {type(exc).__name__}: {REDACT(exc)}")


def dedupe(streams: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the dedupe result."""
    seen = set()
    out = []
    for stream in streams:
        marker = stream.get("key") or (stream.get("id"), stream.get("timed"), stream.get("format"))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(stream)
    return out


def main() -> int:
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    plex = connect(cfg)
    sess = find_session(plex, cfg.get("plex_client_name"))
    if sess is None:
        return 2

    player = getattr(sess, "player", None)
    print("\nCurrent track:")
    print(f"  title:  {getattr(sess, 'title', None)!r}")
    print(f"  artist: {getattr(sess, 'grandparentTitle', None)!r}")
    print(f"  album:  {getattr(sess, 'parentTitle', None)!r}")
    print(f"  ratingKey={getattr(sess, 'ratingKey', None)!r} key={getattr(sess, 'key', None)!r}")
    print(f"  player_state={getattr(player, 'state', None)!r}\n")

    streams: list[dict[str, Any]] = []
    print("Session lyric streams:")
    try:
        session_streams = sess.lyricStreams()
        if not session_streams:
            print("  none")
        for stream in session_streams:
            info = describe_obj_stream(stream)
            streams.append(info)
            print(f"  {info}")
    except Exception as exc:
        print(f"  ERROR {type(exc).__name__}: {REDACT(exc)}")

    metadata_key = getattr(sess, "key", None) or f"/library/metadata/{getattr(sess, 'ratingKey', '')}"
    print("\nFull metadata lyric streams:")
    try:
        metadata_streams = extract_xml_streams(plex, metadata_key)
        if not metadata_streams:
            print("  none")
        for info in metadata_streams:
            streams.append(info)
            print(f"  {info}")
    except Exception as exc:
        print(f"  ERROR {type(exc).__name__}: {REDACT(exc)}")

    candidates = [s for s in dedupe(streams) if s.get("timed")]
    print(f"\nTimed candidates to fetch: {len(candidates)}")
    for number, stream in enumerate(candidates, 1):
        print(f"  [{number}] {stream}")
        probe_stream(plex, stream)

    print("\nInterpretation:")
    print("  no metadata streams      -> Minstrel needs a different discovery route, or the server is not exposing them here")
    print("  stream exists, 200 + lrc_entries > 0 -> Minstrel's discovery/selection code is the bug")
    print("  stream exists, 200 + lrc_entries = 0 -> response format/parser needs adapting")
    print("  stream exists, 404/401/etc. -> stream retrieval URL/parameters need adapting")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Fatal: {type(exc).__name__}: {REDACT(exc)}", file=sys.stderr)
        raise SystemExit(1)
