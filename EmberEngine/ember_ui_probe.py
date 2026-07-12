#!/usr/bin/env python3
"""Read-only Ember Deck UI data-path probe.

Run from ~/EmberDeck with the worker stack running:
    .venv/bin/python ember_ui_probe.py
"""
from __future__ import annotations

import math
import time
import numpy as np
import synapse as bus

TEXT_LEN = 192

def text(key: str) -> str:
    """Return the text result."""
    raw = bus.try_get_array(key, length=TEXT_LEN, dtype="u8")
    if not raw:
        return "N/A"
    return bytes(raw).split(b"\0", 1)[0].decode("utf-8", errors="replace") or "N/A"

def rgba(key: str) -> str:
    """Return the rgba result."""
    arr = bus.try_get_array(key, length=4, dtype="f32")
    if arr is None:
        return "missing"
    return "(" + ", ".join(f"{float(x):.2f}" for x in arr[:3]) + ")"

def age_s(proc: str) -> str:
    """Return the age s result."""
    stamp = bus.get_int(f"/proc/{proc}/heartbeat_ms", 0)
    if stamp <= 0:
        return "missing"
    return f"{(int(time.monotonic()*1000)-stamp)/1000:.1f}s"

def main() -> None:
    """Configure and run the component until shutdown."""
    print("=== Ember Deck UI data-path probe ===")
    print("heartbeats:", ", ".join(f"{p}={age_s(p)}" for p in ("echo", "minstrel", "aurora", "foxfire", "pawprint")))
    print("active source:", bus.get_int("/media/active_source", 0), "Plex state:", bus.get_int("/plex/play_state", 0))
    print("metadata:")
    for label, key in (
        ("title", "/plex/title_utf8"), ("artist", "/plex/artist_utf8"), ("album", "/plex/album_utf8"),
        ("year", "/plex/year_utf8"), ("genre", "/plex/genre_utf8"), ("codec", "/plex/codec_utf8"),
        ("bitrate", "/plex/bitrate_utf8"), ("sample rate", "/plex/sample_rate_utf8"),
        ("plays", "/plex/play_count_utf8"), ("rating", "/plex/rating_utf8"),
    ):
        print(f"  {label:12}: {text(key)}")
    print("album UltraBlur valid:", bus.get_int("/album/ultra/valid", -1))
    print("display palette:")
    for corner in ("tl", "tr", "bl", "br"):
        print(f"  {corner}: {rgba(f'/theme/ui/{corner}_rgba')}")
    waveform = bus.try_get_array("/audio/waveform", length=160, dtype="f32")
    if waveform is None:
        print("waveform: missing")
    else:
        values = np.asarray(waveform, dtype=np.float32)
        print(f"waveform: peak={float(np.max(np.abs(values))):.5f}, rms={float(np.sqrt(np.mean(values*values))):.5f}")
    bus.close_all()

if __name__ == "__main__":
    main()
