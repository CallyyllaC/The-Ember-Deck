#!/usr/bin/env python3
"""Ember Deck Textual UI foundation.

The 640×480 Deck is rendered as a fixed 80 columns × 60 rows Textual canvas.
The screen geometry is intentionally hard-coded below: the Tiled files were an
excellent design sketch, but they are not part of the runtime contract.

Navigation is physical only. While the 3-way source selector is in position 1,
the 4-way selector chooses Now Playing, Oscilloscope, Track Info, or Console.
At every other source-selector position the UI keeps its last selected page.
"""
from __future__ import annotations

import math
import json
import os
import re
import signal
import subprocess
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np
import yaml

import synapse as bus
from spirit_messages import DEFAULT_ROLE_STYLES

try:
    from rich.cells import cell_len
    from rich.text import Text
    from rich.markup import escape as markup_escape
    from textual.app import App, ComposeResult
    from textual.containers import Container
    from textual.widget import Widget
    from textual.widgets import Static
except ModuleNotFoundError as exc:  # pragma: no cover - only happens before install
    raise SystemExit(
        "Textual is not installed in this virtual environment. Run: "
        "source .venv/bin/activate && pip install -r requirements-ui.txt"
    ) from exc


BASE = Path(__file__).resolve().parent
DEFAULTS = {
    "proc_name": "ember_ui",
    "logical_width": 80,
    "logical_height": 60,
    # This is a lightweight scheduler tick, not a demand to redraw the whole UI.
    "update_hz": 50,
    "metadata_refresh_hz": 4.0,
    "global_refresh_hz": 2.0,
    "now_refresh_hz": 2.0,
    # Scope is the only high-rate page. Hidden pages remain throttled.
    "scope_refresh_hz": 50.0,
    "info_refresh_hz": 1.0,
    "console_refresh_hz": 2.0,
    "palette_refresh_hz": 2.0,
    "system_refresh_s": 4.0,
    "log_refresh_s": 1.0,
    "log_path": "logs/foundry.log",
    "structured_log_path": "logs/foundry.structured.jsonl",
    "structured_log_required": True,
    # Shield the visible UI from tiny routing/playerctl gaps. If a source
    # briefly publishes all-N/A metadata or invalid art, keep the previous
    # real now-playing card for this many seconds before accepting the clear.
    "metadata_dead_grace_s": 2.0,
    "art_dead_grace_s": 2.0,
    "waveform_key": "/audio/waveform",
    "waveform_samples": 160,
    # One terminal cell reserved inside the physical bezel on every edge.
    "safe_margin_cells": 1,
}

SOURCE_NONE = 0
SOURCE_PLEX = 1
SOURCE_BLUETOOTH = 2
SOURCE_LOCAL = 3

PLAY_STOPPED = 0
PLAY_PLAYING = 1
PLAY_PAUSED = 2

TEXT_BUFFER = 192
# New lyric bus avoids the old 256-byte segment, which cannot safely hold
# long CJK lines and cannot be resized in place once Synapse has created it.
LYRIC_BUFFER_V2 = 1024
LYRIC_BUFFER_LEGACY = 256
LYRIC_UTF8_V2_KEY = "/lyrics/current_utf8_v2"
LYRIC_PREVIOUS_UTF8_V2_KEY = "/lyrics/previous_utf8_v2"
LYRIC_NEXT_UTF8_V2_KEY = "/lyrics/next_utf8_v2"
LYRIC_UTF8_LEGACY_KEY = "/lyrics/current_utf8"


def load_cfg() -> dict:
    """Load defaults and merge any component-specific YAML configuration."""
    path = Path(os.environ.get("CONFIG_PATH", BASE / "configs" / "ember_ui.yaml"))
    loaded: dict = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = DEFAULTS.copy()
    cfg.update(loaded)
    cfg["_config_path"] = path
    cfg["_config_dir"] = path.parent
    return cfg


@dataclass(frozen=True)
class Region:
    """One fixed Textual-cell rectangle in the 80 × 60 Deck canvas."""

    x: int
    y: int
    width: int
    height: int


# The layout is deliberately code-owned. Coordinates are Textual cells, not
# pixels: the terminal itself is configured to occupy the 640 × 480 display.
HARD_LAYOUTS: Dict[str, Dict[str, Region]] = {
    "global": {
        "state": Region(4, 0, 6, 6),
        "artist": Region(10, 0, 20, 3),
        "album": Region(10, 3, 20, 3),
        "title": Region(30, 0, 30, 3),
        "progress": Region(30, 3, 30, 3),
        "rating": Region(60, 0, 10, 3),
        "popularity": Region(60, 3, 10, 3),
        "time": Region(70, 0, 10, 3),
        "date": Region(70, 3, 10, 3),
        "colours": Region(0, 0, 4, 6),
    },
    "now": {
        "metadata": Region(0, 6, 56, 24),
        "art": Region(56, 6, 24, 24),
        "lyrics": Region(0, 30, 80, 18),
        "logging": Region(0, 48, 80, 12),
    },
    "scope": {
        "wave": Region(0, 6, 80, 42),
        "lyrics": Region(0, 48, 80, 12),
    },
    "info": {
        "identity": Region(0, 6, 50, 29),
        "character": Region(50, 6, 30, 29),
        "provenance": Region(0, 35, 50, 25),
        "format": Region(50, 35, 30, 25),
    },
    "console": {
        "logging": Region(0, 6, 56, 54),
        "heartbeats": Region(56, 6, 24, 24),
        "diagnostics": Region(56, 30, 24, 30),
    },
}

# Physical selector navigation. Pawprint publishes source_selector = 1 for
# the UI position, and visualiser_mode = 1..4 left-to-right.
UI_SOURCE_SELECTOR_POSITION = 1
PAGE_BY_SELECTOR = {
    1: "now",
    2: "scope",
    3: "info",
    4: "console",
}

def _n_a(value: object) -> str:
    """Return display text, substituting N/A for empty values."""
    text = str(value or "").strip()
    return text if text else "N/A"


def safe_markup(value: object) -> str:
    """Escape external metadata/log text before Rich renders it as markup.

    This matters for square brackets in titles and lyrics, and keeps arbitrary
    Unicode metadata as text rather than accidental Rich tags.
    """
    return markup_escape(_n_a(value))


def read_utf8(key: str, length: int = TEXT_BUFFER) -> str:
    """Read a fixed UTF-8 Synapse segment without truth-testing NumPy data."""
    raw = bus.try_get_array(key, length=length, dtype="u8")
    if raw is None:
        return "N/A"
    try:
        if len(raw) == 0:
            return "N/A"
        data = bytes(raw).split(b"\x00", 1)[0]
        return _n_a(data.decode("utf-8", errors="replace"))
    except Exception:
        return "N/A"


def read_lyric_context_utf8() -> Tuple[str, str, str]:
    """Read the v2 previous/current/next lyric context with a safe fallback."""
    current = read_utf8(LYRIC_UTF8_V2_KEY, LYRIC_BUFFER_V2)
    if current == "N/A":
        # During an upgrade, a previous Minstrel may still only publish the
        # legacy single-line key. Keep the UI useful rather than blank.
        return "", read_utf8(LYRIC_UTF8_LEGACY_KEY, LYRIC_BUFFER_LEGACY), ""
    return (
        read_utf8(LYRIC_PREVIOUS_UTF8_V2_KEY, LYRIC_BUFFER_V2),
        current,
        read_utf8(LYRIC_NEXT_UTF8_V2_KEY, LYRIC_BUFFER_V2),
    )


def read_lyric_utf8() -> str:
    """Compatibility helper for older callers that only need the active line."""
    return read_lyric_context_utf8()[1]

def wrap_terminal_cells(value: object, width: int, max_lines: int) -> str:
    """Wrap at terminal-cell boundaries, including CJK double-width glyphs.

    Rich is Unicode-aware, but pre-wrapping here means the lyric widget never
    tries to clip a Japanese/Korean/CJK line mid-glyph while the panel is small.
    It also keeps narrow terminal grids predictable.
    """
    text = _n_a(value)
    if text == "N/A":
        return text
    width = max(2, int(width))
    max_lines = max(1, int(max_lines))
    lines: List[str] = []

    for source_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not source_line:
            lines.append("")
            continue
        current: List[str] = []
        used = 0
        for char in source_line:
            char_width = max(0, cell_len(char))
            if char_width and used + char_width > width and current:
                lines.append("".join(current).rstrip())
                current = []
                used = 0
            current.append(char)
            used += char_width
        if current or not lines:
            lines.append("".join(current).rstrip())

    if len(lines) <= max_lines:
        return "\n".join(lines)

    visible = lines[:max_lines]
    tail = visible[-1]
    while tail and cell_len(tail + "…") > width:
        tail = tail[:-1]
    visible[-1] = (tail.rstrip() + "…") if tail else "…"
    return "\n".join(visible)


def truncate_terminal_cells(value: object, width: int) -> str:
    """Return one terminal-cell-safe lyric line, with an ellipsis when needed."""
    text = _n_a(value)
    if text == "N/A":
        return ""
    text = text.replace("\r", "").replace("\n", " ").strip()
    width = max(2, int(width))
    out: List[str] = []
    used = 0
    for char in text:
        char_width = max(0, cell_len(char))
        if char_width and used + char_width > width:
            break
        out.append(char)
        used += char_width
    if len(out) == len(text):
        return "".join(out)
    while out and cell_len("".join(out) + "…") > width:
        out.pop()
    return ("".join(out).rstrip() + "…") if out else "…"


def marquee_terminal_cells(text: object, width: int, *, now: float, step_s: float = 0.35, gap: str = "   ") -> str:
    """Return a one-line marquee slice without hiding the end of long values."""
    value = _n_a(text).replace("\r", "").replace("\n", " ").strip()
    width = max(1, int(width))
    if cell_len(value) <= width:
        return value

    loop = value + gap
    loop_cells = max(1, cell_len(loop))
    offset = int(max(0.0, float(now)) / max(0.05, float(step_s))) % loop_cells
    doubled = loop + loop

    out = []
    used = 0
    skipped = 0
    for ch in doubled:
        w = max(0, cell_len(ch))
        if skipped + w <= offset:
            skipped += w
            continue
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() or value[:1]


def lyric_context_widget(context: Tuple[str, str, str], width: int, height: int) -> Text:
    """Render a controlled previous/current/next lyric stanza.

    The current timed line is visually dominant. Neighbours remain present as
    dim context, but each is constrained to one terminal row so CJK or long
    English lines cannot turn the Now Playing page into a scrolling paragraph.
    """
    previous, current, next_line = context
    usable_width = max(4, int(width) - 4)  # panel border + horizontal padding
    show_heading = int(height) >= 6
    rendered = Text()
    if show_heading:
        rendered.append("LYRICS\n", style="bold #b9a78c")

    previous_text = truncate_terminal_cells(previous, usable_width) or " "
    current_text = truncate_terminal_cells(current, usable_width) or "—"
    next_text = truncate_terminal_cells(next_line, usable_width) or " "
    rendered.append(previous_text + "\n", style="#77706a")
    rendered.append(current_text + "\n", style="bold #f0e4d0")
    rendered.append(next_text, style="#77706a")
    return rendered


def lyric_text_widget(value: object, width: int, height: int) -> Text:
    """Legacy one-line adapter retained for external utility callers."""
    return lyric_context_widget(("", _n_a(value), ""), width, height)

def clamp01(value: float) -> float:
    """Clamp a numeric value to the inclusive range from zero to one."""
    return max(0.0, min(1.0, float(value)))


def fmt_time(seconds: float) -> str:
    """Format a duration as a compact clock value."""
    if not math.isfinite(seconds) or seconds <= 0:
        return "--:--"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:d}:{secs:02d}"


def progress_bar(position: float, duration: float, width: int) -> str:
    """Build a fixed-width textual playback progress bar."""
    width = max(6, int(width))
    fraction = clamp01(position / duration) if duration > 0 else 0.0
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def state_name(state: int) -> str:
    """Return the state name result."""
    return {PLAY_PLAYING: "PLAY", PLAY_PAUSED: "PAUSE", PLAY_STOPPED: "STOP"}.get(int(state), "STOP")


def source_name(source: int) -> str:
    """Return the source name result."""
    if source == SOURCE_LOCAL:
        label = read_utf8("/mpris/source_utf8", TEXT_BUFFER).upper()
        if label != "N/A":
            return label
        selected = bus.get_int("/hdmi2/selected_source", 1)
        return {2: "RADIO", 3: "YOUTUBE"}.get(selected, "MPRIS")
    return {
        SOURCE_PLEX: "PLEX",
        SOURCE_BLUETOOTH: "BT",
    }.get(int(source), "N/A")


def source_state(source: int) -> int:
    """Return the source state result."""
    if source == SOURCE_PLEX:
        return bus.get_int("/plex/play_state", PLAY_STOPPED)
    if source == SOURCE_BLUETOOTH:
        return bus.get_int("/bt/play_state", PLAY_STOPPED)
    if source == SOURCE_LOCAL:
        return bus.get_int("/mpris/play_state", PLAY_STOPPED)
    return PLAY_STOPPED


def metadata_is_dead(meta: dict) -> bool:
    """Return True for the all-placeholder state caused by transient source gaps."""
    def real_text(key: str) -> bool:
        """Return the real text result."""
        value = str(meta.get(key, "") or "").strip()
        return bool(value and value.upper() != "N/A")

    has_identity = any(real_text(key) for key in ("title", "artist", "album"))
    try:
        duration = float(meta.get("duration", 0.0) or 0.0)
        position = float(meta.get("position", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
        position = 0.0
    return not has_identity and duration <= 0.0 and position <= 0.0


def source_metadata(source: int) -> dict:
    """Return the source metadata result."""
    extra_defaults = {
        "container": "N/A",
        "file_name": "N/A",
        "file_size": "N/A",
        "channels": "N/A",
        "channel_layout": "N/A",
        "bit_depth": "N/A",
        "stream_title": "N/A",
        "track_gain": "N/A",
        "track_peak": "N/A",
        "album_gain": "N/A",
        "album_peak": "N/A",
        "album_range": "N/A",
        "lra": "N/A",
        "last_played": "N/A",
    }
    if source == SOURCE_PLEX:
        return {
            "source": SOURCE_PLEX,
            "title": read_utf8("/plex/title_utf8"),
            "artist": read_utf8("/plex/artist_utf8"),
            "album": read_utf8("/plex/album_utf8"),
            "position": bus.get_float("/plex/position_sec", 0.0),
            "duration": bus.get_float("/plex/duration_sec", 0.0),
            "track_number": bus.get_int("/plex/track_number", 0),
            "track_count": bus.get_int("/plex/track_count", 0),
            "disc_number": bus.get_int("/plex/disc_number", 0),
            "year": read_utf8("/plex/year_utf8"),
            "genre": read_utf8("/plex/genre_utf8"),
            "codec": read_utf8("/plex/codec_utf8"),
            "bitrate": read_utf8("/plex/bitrate_utf8"),
            "sample_rate": read_utf8("/plex/sample_rate_utf8"),
            "loudness": read_utf8("/plex/loudness_utf8"),
            "added": read_utf8("/plex/added_utf8"),
            "play_count": read_utf8("/plex/play_count_utf8"),
            "popularity": read_utf8("/plex/popularity_utf8"),
            "rating": read_utf8("/plex/rating_utf8"),
            "bpm": read_utf8("/plex/bpm_utf8"),
            "mood": read_utf8("/plex/mood_utf8"),
            "container": read_utf8("/plex/container_utf8"),
            "file_name": read_utf8("/plex/file_name_utf8"),
            "file_size": read_utf8("/plex/file_size_utf8"),
            "channels": read_utf8("/plex/channels_utf8"),
            "channel_layout": read_utf8("/plex/channel_layout_utf8"),
            "bit_depth": read_utf8("/plex/bit_depth_utf8"),
            "stream_title": read_utf8("/plex/stream_title_utf8"),
            "track_gain": read_utf8("/plex/track_gain_utf8"),
            "track_peak": read_utf8("/plex/track_peak_utf8"),
            "album_gain": read_utf8("/plex/album_gain_utf8"),
            "album_peak": read_utf8("/plex/album_peak_utf8"),
            "album_range": read_utf8("/plex/album_range_utf8"),
            "lra": read_utf8("/plex/lra_utf8"),
            "last_played": read_utf8("/plex/last_played_utf8"),
        }
    if source == SOURCE_BLUETOOTH:
        return {
            "source": SOURCE_BLUETOOTH,
            "title": read_utf8("/bt/title_utf8"),
            "artist": read_utf8("/bt/artist_utf8"),
            "album": read_utf8("/bt/album_utf8"),
            "position": bus.get_int("/bt/position_ms", 0) / 1000.0,
            "duration": bus.get_int("/bt/duration_ms", 0) / 1000.0,
            "track_number": bus.get_int("/bt/track_number", 0),
            "track_count": bus.get_int("/bt/track_count", 0),
            "disc_number": 0,
            "year": "N/A",
            "genre": "N/A",
            "codec": "Bluetooth",
            "bitrate": "N/A",
            "sample_rate": "N/A",
            "loudness": "N/A",
            "added": "N/A",
            "play_count": "N/A",
            "popularity": "N/A",
            "rating": "N/A",
            "bpm": "N/A",
            "mood": "N/A",
            **extra_defaults,
        }
    return {
        "source": SOURCE_LOCAL,
        "title": read_utf8("/mpris/title_utf8"),
        "artist": read_utf8("/mpris/artist_utf8"),
        "album": read_utf8("/mpris/album_utf8"),
        "position": bus.get_int("/mpris/position_ms", 0) / 1000.0,
        "duration": bus.get_int("/mpris/duration_ms", 0) / 1000.0,
        "track_number": 0,
        "track_count": 0,
        "disc_number": 0,
        "year": "N/A",
        "genre": "N/A",
        "codec": read_utf8("/mpris/identity_utf8"),
        "bitrate": "N/A",
        "sample_rate": "N/A",
        "loudness": "N/A",
        "added": "N/A",
        "play_count": "N/A",
        "popularity": "N/A",
        "rating": "N/A",
        "bpm": "N/A",
        "mood": "N/A",
        **extra_defaults,
        "stream_title": read_utf8("/mpris/player_utf8"),
    }

def palette() -> List[Tuple[int, int, int]]:
    """Read the display palette, never the LED brightness-scaled palette.

    Aurora publishes `/theme/ui/*` specifically so the panel's scope, palette
    ticks and artwork placeholder remain visible when the physical RGBW strip
    is dimmed. Falling back to `/theme/ultra/*` keeps staged upgrades harmless.
    """
    display_keys = (
        "/theme/ui/tl_rgba", "/theme/ui/tr_rgba",
        "/theme/ui/bl_rgba", "/theme/ui/br_rgba",
    )
    legacy_keys = (
        "/theme/ultra/tl_rgba", "/theme/ultra/tr_rgba",
        "/theme/ultra/bl_rgba", "/theme/ultra/br_rgba",
    )
    result: List[Tuple[int, int, int]] = []
    for display_key, legacy_key in zip(display_keys, legacy_keys):
        arr = bus.try_get_array(display_key, length=4, dtype="f32")
        if arr is None:
            arr = bus.try_get_array(legacy_key, length=4, dtype="f32")
        arr = arr or [0.35, 0.35, 0.35, 1.0]
        result.append(tuple(int(clamp01(arr[i]) * 255) for i in range(3)))
    return result


ART_BUS_WIDTH = 48
ART_BUS_HEIGHT = 48
ART_BUS_BYTES = ART_BUS_WIDTH * ART_BUS_HEIGHT * 3


def read_album_art(source: int) -> Optional[np.ndarray]:
    """Read Plex or generic-MPRIS small RGB cover mosaic safely."""
    prefix = "/plex" if source == SOURCE_PLEX else "/mpris" if source == SOURCE_LOCAL else ""
    if not prefix or bus.get_int(f"{prefix}/art_valid", 0) != 1:
        return None
    width = bus.get_int(f"{prefix}/art_width", 0)
    height = bus.get_int(f"{prefix}/art_height", 0)
    if not (1 <= width <= ART_BUS_WIDTH and 1 <= height <= ART_BUS_HEIGHT):
        return None
    values = bus.try_get_array(f"{prefix}/art_rgb", ART_BUS_BYTES, dtype="u8")
    if values is None:
        return None
    try:
        canvas = np.asarray(values, dtype=np.uint8).reshape(ART_BUS_HEIGHT, ART_BUS_WIDTH, 3)
        return canvas[:height, :width].copy()
    except (ValueError, TypeError):
        return None


def rgb_style(rgb: Tuple[int, int, int]) -> str:
    """Format an RGB tuple as a Textual color style."""
    return f"rgb({int(rgb[0])},{int(rgb[1])},{int(rgb[2])})"


def read_mem() -> Tuple[int, int]:
    """Read mem."""
    total = available = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, tail = line.partition(":")
            value = tail.strip().split()
            if not value:
                continue
            if key == "MemTotal":
                total = int(value[0])
            elif key == "MemAvailable":
                available = int(value[0])
    except Exception:
        pass
    return total, available


class CpuMeter:
    """Manage CpuMeter state and behaviour."""
    def __init__(self) -> None:
        """Initialize configuration, dependencies, and runtime state."""
        self.previous: Optional[Tuple[int, int]] = None

    def percent(self) -> int:
        """Return the percent result."""
        try:
            values = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
            nums = [int(value) for value in values]
            total = sum(nums)
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            now = (total, idle)
            if self.previous is None:
                self.previous = now
                return 0
            prev_total, prev_idle = self.previous
            self.previous = now
            delta_total = max(1, total - prev_total)
            busy = max(0, delta_total - (idle - prev_idle))
            return int(round(100 * busy / delta_total))
        except Exception:
            return 0


def cpu_temp_c() -> Optional[float]:
    """Read the CPU temperature in degrees Celsius."""
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0
    except Exception:
        return None


def default_sink_volume() -> str:
    """Read the current default audio-sink volume."""
    try:
        result = subprocess.run(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.0,
        )
        match = re.search(r"(\d+)%", result.stdout)
        return f"{match.group(1)}%" if match else "N/A"
    except Exception:
        return "N/A"


def wifi_status() -> str:
    """Read the current wireless-network status."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                _, ssid, signal = (line.split(":", 2) + ["", ""])[:3]
                return f"{ssid or 'Wi-Fi'} {signal or '?'}%"
    except Exception:
        pass
    return "N/A"


class SystemProbe:
    """Collect slow shell-backed diagnostics outside Textual's event loop."""

    def __init__(self) -> None:
        """Initialize configuration, dependencies, and runtime state."""
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ember-system")
        self._future: Optional[Future] = None
        self._cpu = CpuMeter()

    def _collect(self) -> dict:
        """Return the collect result."""
        total, available = read_mem()
        ram_used = int(round(100 * (1 - available / total))) if total else 0
        temp = cpu_temp_c()
        ip = "N/A"
        try:
            result = subprocess.run(
                ["hostname", "-I"], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=1.0,
            )
            ip = result.stdout.strip().split()[0] if result.stdout.strip() else "N/A"
        except Exception:
            pass
        return {
            "volume": default_sink_volume(),
            "wifi": wifi_status(),
            "ip": ip,
            "cpu": self._cpu.percent(),
            "ram": f"{ram_used}%" if total else "N/A",
            "temp": f"{temp:.0f}°C" if temp is not None else "N/A",
        }

    def request(self) -> None:
        """Handle the request lifecycle step."""
        if self._future is None or self._future.done():
            self._future = self._executor.submit(self._collect)

    def poll(self) -> Optional[dict]:
        """Return the poll result."""
        if self._future is None or not self._future.done():
            return None
        future, self._future = self._future, None
        try:
            return future.result()
        except Exception:
            return None

    def close(self) -> None:
        """Handle the close lifecycle step."""
        self._executor.shutdown(wait=False, cancel_futures=True)


class LogTail:
    """Manage LogTail state and behaviour."""
    def __init__(self, path: Path, structured_path: Optional[Path] = None):
        """Initialize configuration, dependencies, and runtime state."""
        self.path = path
        self.structured_path = structured_path
        self._last_mtime_ns = -1
        self._last_source: Optional[Path] = None
        self.source_name = "waiting"
        self.lines: List[Text] = [Text("N/A")]

    @staticmethod
    def _record_text(record: dict) -> Text:
        """Return the record text result."""
        output = Text()
        fragments = record.get("fragments")
        if isinstance(fragments, list) and fragments:
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    continue
                role = str(fragment.get("role", "secondary"))
                output.append(
                    str(fragment.get("text", "")),
                    style=DEFAULT_ROLE_STYLES.get(role, ""),
                )
        if not output:
            role = str(record.get("style_role", "secondary"))
            output.append(str(record.get("text", "")), style=DEFAULT_ROLE_STYLES.get(role, ""))
        return output

    def _refresh_structured(self, path: Path, limit: int) -> List[Text]:
        """Refresh structured."""
        stat = path.stat()
        if path == self._last_source and stat.st_mtime_ns == self._last_mtime_ns:
            return self.lines[-limit:]
        self._last_source = path
        self.source_name = "structured"
        self._last_mtime_ns = stat.st_mtime_ns
        with path.open("rb") as handle:
            handle.seek(max(0, stat.st_size - 64_000))
            data = handle.read().decode("utf-8", errors="replace")
        records: List[Text] = []
        for raw_line in data.splitlines():
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(record, dict):
                records.append(self._record_text(record))
        self.lines = records[-max(1, limit):] or [Text("N/A")]
        return self.lines[-limit:]

    def refresh(self, limit: int = 36, *, require_structured: bool = True) -> List[Text]:
        """Refresh object."""
        try:
            if self.structured_path is not None and self.structured_path.is_file():
                return self._refresh_structured(self.structured_path, limit)
            if require_structured and self.structured_path is not None:
                self.source_name = "structured-missing"
                self.lines = [Text(
                    f"STRUCTURED LOG UNAVAILABLE\n{self.structured_path}",
                    style="bold red",
                )]
                return self.lines
            stat = self.path.stat()
            self.source_name = "plain-fallback"
            if self.path == self._last_source and stat.st_mtime_ns == self._last_mtime_ns:
                return self.lines[-limit:]
            self._last_source = self.path
            self._last_mtime_ns = stat.st_mtime_ns
            # A modest tail avoids reading megabytes just to show a console.
            with self.path.open("rb") as fh:
                fh.seek(max(0, stat.st_size - 24_000))
                data = fh.read().decode("utf-8", errors="replace")
            lines = [line.rstrip() for line in data.splitlines() if line.strip()]
            self.lines = [Text(line) for line in lines[-max(1, limit):]] or [Text("N/A")]
        except Exception:
            self.lines = [Text("N/A")]
        return self.lines[-limit:]


class ScopeWidget(Widget):
    """Ember CRT: ivory live trace with a genuinely visible palette halo."""

    DEFAULT_CSS = """
    ScopeWidget { overflow: hidden hidden; background: #050301; }
    """

    def __init__(self, **kwargs) -> None:
        """Initialize configuration, dependencies, and runtime state."""
        super().__init__(**kwargs)
        self.samples = np.zeros(160, dtype=np.float32)
        # A small history keeps motion coherent without becoming expensive at 50 Hz.
        self._history: Deque[np.ndarray] = deque(maxlen=4)
        self._scale = 0.05
        self.trail_colour = (214, 120, 45)
        self.core_colour = (255, 245, 214)

    @staticmethod
    def _visible_colour(rgb: Tuple[int, int, int], minimum_luma: float = 126.0) -> Tuple[int, int, int]:
        """Lift and saturate a palette colour so it survives a dark terminal."""
        r, g, b = (max(0, min(255, int(v))) for v in rgb)
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        if luma < 1.0:
            return (210, 95, 28)
        if luma < minimum_luma:
            scale = min(3.5, minimum_luma / luma)
            r, g, b = (min(255, int(round(v * scale))) for v in (r, g, b))

        # Preserve the selected palette's character, but never let a grey/dark
        # album silently erase the Ember halo.
        hi, lo = max(r, g, b), min(r, g, b)
        if hi - lo < 38:
            r = min(255, max(r, 192))
            g = min(255, max(g, 78))
            b = min(255, max(b, 28))
        return (r, g, b)

    @staticmethod
    def _resample(values: np.ndarray, width: int) -> np.ndarray:
        """Return the resample result."""
        if values.size < 2:
            return np.zeros(width, dtype=np.float32)
        x_old = np.linspace(0.0, 1.0, values.size, dtype=np.float32)
        x_new = np.linspace(0.0, 1.0, width, dtype=np.float32)
        return np.interp(x_new, x_old, values).astype(np.float32)

    def set_waveform(self, values: Sequence[float], colour: Tuple[int, int, int]) -> None:
        """Set waveform."""
        array = np.asarray(values, dtype=np.float32).reshape(-1)
        if array.size:
            self.samples = array
            self._history.append(array.copy())
        self.trail_colour = self._visible_colour(colour)
        self.refresh()

    def _y_positions(self, values: np.ndarray, width: int, height: int, scale: float) -> List[int]:
        """Return the y positions result."""
        samples = self._resample(values, width)
        normal = np.clip(samples / max(0.015, scale), -1.0, 1.0)
        centre = (height - 1) / 2.0
        return [max(0, min(height - 1, int(round(centre - value * centre * 0.90)))) for value in normal]

    def _write_cell(
        self,
        glyphs: List[List[str]],
        styles: List[List[Optional[str]]],
        y: int,
        x: int,
        glyph: str,
        style: str,
        *,
        overwrite: bool = True,
    ) -> None:
        """Write cell."""
        if 0 <= y < len(glyphs) and 0 <= x < len(glyphs[0]):
            if overwrite or glyphs[y][x] in (" ", "·", "┊"):
                glyphs[y][x] = glyph
                styles[y][x] = style

    def render(self) -> Text:
        """Return the render result."""
        width = max(1, self.size.width)
        height = max(3, self.size.height)
        current = self._resample(self.samples, width)
        peak = max(0.015, float(np.percentile(np.abs(current), 98)))
        self._scale = self._scale * 0.84 + peak * 0.16

        glyphs = [[" " for _ in range(width)] for _ in range(height)]
        styles: List[List[Optional[str]]] = [[None for _ in range(width)] for _ in range(height)]
        centre = int(round((height - 1) / 2.0))
        grid_style = rgb_style((72, 42, 20))
        division_style = rgb_style((45, 29, 17))

        # Fixed, readable CRT graticule.
        division = max(8, width // 8)
        for x in range(width):
            glyphs[centre][x] = "·"
            styles[centre][x] = grid_style
            if x % division == 0:
                for y in range(0, height, 2):
                    if y != centre:
                        glyphs[y][x] = "┊"
                        styles[y][x] = division_style

        # Persistence: older traces are coloured dotted ghosts. They are
        # intentionally rendered before the live halo/core.
        history = list(self._history)
        historic_style = rgb_style(self.trail_colour)
        for age, historic in enumerate(history[:-1]):
            positions = self._y_positions(historic, width, height, self._scale)
            stride = 2 if age < max(1, len(history) - 2) else 1
            for x, y in enumerate(positions):
                if x % stride == 0:
                    self._write_cell(glyphs, styles, y, x, "·", historic_style, overwrite=False)

        live_positions = self._y_positions(self.samples, width, height, self._scale)
        halo_style = rgb_style(self.trail_colour)
        # This is the part the previous renderer failed to make visible:
        # explicit, fully opaque palette-coloured cells above and below the
        # ivory line. No pretend alpha, no near-black terminal mud.
        for x, y in enumerate(live_positions):
            self._write_cell(glyphs, styles, y - 1, x, "·", halo_style, overwrite=False)
            self._write_cell(glyphs, styles, y + 1, x, "·", halo_style, overwrite=False)
            if x > 0:
                previous = live_positions[x - 1]
                lo, hi = sorted((previous, y))
                for row in range(lo, hi + 1):
                    if row != y:
                        self._write_cell(glyphs, styles, row, x, "·", halo_style, overwrite=False)

        # Live trace is always unmistakably ivory and is drawn last.
        core_style = rgb_style(self.core_colour)
        join_style = rgb_style((244, 214, 164))
        previous: Optional[int] = None
        for x, y in enumerate(live_positions):
            if previous is not None:
                lo, hi = sorted((previous, y))
                for row in range(lo, hi + 1):
                    self._write_cell(
                        glyphs,
                        styles,
                        row,
                        x,
                        "│" if row not in (previous, y) else "•",
                        join_style if row not in (previous, y) else core_style,
                    )
            else:
                self._write_cell(glyphs, styles, y, x, "•", core_style)
            previous = y

        out = Text()
        for row in range(height):
            run_style = styles[row][0]
            run = [glyphs[row][0]]
            for col in range(1, width):
                style = styles[row][col]
                if style == run_style:
                    run.append(glyphs[row][col])
                else:
                    out.append("".join(run), style=run_style)
                    run_style = style
                    run = [glyphs[row][col]]
            out.append("".join(run), style=run_style)
            if row < height - 1:
                out.append("\n")
        return out


class AlbumArtWidget(Widget):
    """Render real Plex or generic-MPRIS cover pixels using terminal glyphs.

    Each ``▀`` cell carries one RGB foreground pixel and one RGB background
    pixel. It is modest by desktop standards, but it is actual album art and
    works in LXTerminal without a non-standard image protocol.
    """

    DEFAULT_CSS = """
    AlbumArtWidget { overflow: hidden hidden; background: #080808; }
    """

    def __init__(self, **kwargs) -> None:
        """Initialize configuration, dependencies, and runtime state."""
        super().__init__(**kwargs)
        self.cover: Optional[np.ndarray] = None
        self.palette: List[Tuple[int, int, int]] = [(70, 70, 70)] * 4
        self._sequence: object = -1

    def set_art_context(
        self,
        cover: Optional[np.ndarray],
        colours: Sequence[Tuple[int, int, int]],
        sequence: object,
    ) -> None:
        """Set art context."""
        palette = list(colours)[:4] or [(70, 70, 70)] * 4
        if sequence == self._sequence and palette == self.palette:
            return
        self._sequence = sequence
        self.cover = None if cover is None else np.asarray(cover, dtype=np.uint8).copy()
        self.palette = palette
        self.refresh()

    def _render_cover(self, width: int, height: int) -> Text:
        """Render cover."""
        assert self.cover is not None
        source = self.cover
        target_h = max(2, height * 2)
        target_w = max(1, width)
        y_index = np.linspace(0, source.shape[0] - 1, target_h).round().astype(np.intp)
        x_index = np.linspace(0, source.shape[1] - 1, target_w).round().astype(np.intp)
        scaled = source[y_index][:, x_index]
        out = Text()
        for row in range(height):
            top = scaled[min(row * 2, target_h - 1)]
            bottom = scaled[min(row * 2 + 1, target_h - 1)]
            for col in range(width):
                out.append(
                    "▀",
                    style=f"{rgb_style(tuple(top[col]))} on {rgb_style(tuple(bottom[col]))}",
                )
            if row < height - 1:
                out.append("\n")
        return out

    def _render_fallback(self, width: int, height: int) -> Text:
        """Render fallback."""
        chars = "░▒▓█"
        out = Text()
        for y in range(height):
            for x in range(width):
                tier = (x * 3 + y * 5 + (x // 3) * (y // 2)) % 4
                out.append(chars[(x + y) % len(chars)], style=rgb_style(self.palette[tier]))
            if y < height - 1:
                out.append("\n")
        return out

    def render(self) -> Text:
        """Return the render result."""
        width = max(1, self.size.width)
        height = max(1, self.size.height)
        return self._render_cover(width, height) if self.cover is not None else self._render_fallback(width, height)


class EmberUI(App[None]):
    """Manage EmberUI state and behaviour."""
    TITLE = "EMBER DECK"
    CSS = """
    /* Textual positions absolute widgets with `offset`, not web-CSS left/top.
       Actual rectangles are calculated from the live terminal viewport. */
    Screen {
        background: #000000;
        color: #e8e8e8;
        overflow: hidden hidden;
        layers: frame pages overlay;
    }
    .region {
        position: absolute;
        overflow: hidden hidden;
        padding: 0 1;
    }
    .panel { border: round #444444; background: #070707; }
    #safe-frame {
        position: absolute;
        offset: 0 0;
        width: 80;
        height: 60;
        border: round #303030;
        background: transparent;
        layer: frame;
    }
    .micro { color: #888888; }
    .label { color: #9d9d9d; }
    #global-root {
        position: absolute;
        offset: 0 0;
        width: 80;
        height: 6;
        background: #030303;
        border-bottom: solid #444444;
        layer: overlay;
    }
    #page-now, #page-scope, #page-info, #page-console {
        position: absolute;
        offset: 0 0;
        width: 80;
        height: 60;
        layer: pages;
    }
    """
    # The physical switches select pages. Keep only a quit key for manual
    # desktop testing; no touchscreen/mouse or keyboard page navigation.
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, cfg: dict) -> None:
        """Initialize configuration, dependencies, and runtime state."""
        super().__init__()
        self.cfg = cfg
        self.page = "now"
        self.last_system_at = 0.0
        self.last_log_at = 0.0
        self._last_global_at = 0.0
        self._last_now_at = 0.0
        self._last_scope_at = 0.0
        self._last_info_at = 0.0
        self._last_console_at = 0.0
        self._last_metadata_at = 0.0
        self._last_palette_at = 0.0
        self._last_lyrics_at = 0.0
        self._last_art_at = 0.0
        self.system = {"volume": "N/A", "wifi": "N/A", "ip": "N/A", "cpu": 0, "ram": "N/A", "temp": "N/A"}
        self.system_probe = SystemProbe()
        self.live_source = SOURCE_PLEX
        self.live_play_state = PLAY_STOPPED
        self.live_meta = source_metadata(SOURCE_PLEX)
        self.live_colours = [(90, 90, 90)] * 4
        self.live_lyric: Tuple[str, str, str] = ("", "N/A", "")
        self._dead_meta_source: Optional[int] = None
        self._dead_meta_since: Optional[float] = None
        self._album_art: Optional[np.ndarray] = None
        self._album_art_seq = -1
        self._dead_art_seq: Optional[Tuple[int, int]] = None
        self._dead_art_since: Optional[float] = None
        self._text_cache: Dict[str, str] = {}
        log_path = Path(str(cfg["log_path"]))
        if not log_path.is_absolute():
            log_path = BASE / log_path
        structured_log_path = Path(str(cfg.get("structured_log_path", "")))
        if not structured_log_path.is_absolute():
            structured_log_path = BASE / structured_log_path
        self.log_tail = LogTail(log_path, structured_log_path)
        self.logs = [Text("N/A")]

    def compose(self) -> ComposeResult:
        # Always-visible bezel-safe frame. Content is laid out one cell inward.
        """Return the compose result."""
        yield Static(id="safe-frame")

        # Global overlay. Geometry is fixed in HARD_LAYOUTS above.
        with Container(id="global-root"):
            yield Static(id="global-colours")
            yield Static(id="global-state")
            yield Static(id="global-artist")
            yield Static(id="global-album")
            yield Static(id="global-title")
            yield Static(id="global-rating")
            yield Static(id="global-popularity")
            yield Static(id="global-progress")
            yield Static(id="global-time")
            yield Static(id="global-date")

        with Container(id="page-now"):
            yield Static(id="now-metadata", classes="region panel")
            yield AlbumArtWidget(id="now-art", classes="region panel")
            yield Static(id="now-lyrics", classes="region panel")

        with Container(id="page-scope"):
            yield ScopeWidget(id="scope-wave", classes="region panel")
            yield Static(id="scope-lyrics", classes="region panel")

        with Container(id="page-info"):
            yield Static(id="info-identity", classes="region panel")
            yield Static(id="info-character", classes="region panel")
            yield Static(id="info-provenance", classes="region panel")
            yield Static(id="info-format", classes="region panel")

        with Container(id="page-console"):
            yield Static(id="console-logging", classes="region panel")
            yield Static(id="console-heartbeats", classes="region panel")
            yield Static(id="console-diagnostics", classes="region panel")
            yield Static(id="console-controls", classes="region panel")

    def on_mount(self) -> None:
        """Handle the on mount lifecycle step."""
        self._apply_adaptive_layout()
        self._set_page("now")
        interval = 1.0 / max(2, int(self.cfg.get("update_hz", 20)))
        self.set_interval(interval, self.refresh_frame)
        self.refresh_frame()

    def _place(self, widget_id: str, region: Region) -> None:
        """Place a widget in one hard-coded Textual-cell rectangle.

        Textual's absolute-position API is ``position: absolute`` plus a two-
        axis ``offset``. It deliberately does not emulate browser ``left`` and
        ``top`` properties.
        """
        widget = self.query_one(f"#{widget_id}")
        widget.styles.position = "absolute"
        widget.styles.offset = (region.x, region.y)
        widget.styles.width = region.width
        widget.styles.height = region.height

    def _place_optional(self, widget_id: str, region: Optional[Region]) -> None:
        """Handle the place optional lifecycle step."""
        widget = self.query_one(f"#{widget_id}")
        if region is None:
            widget.styles.display = "none"
            return
        widget.styles.display = "block"
        self._place(widget_id, region)

    def _apply_adaptive_layout(self) -> None:
        """Fit the page architecture into the live terminal grid.

        The physical case steals a little of the view at some angles, so all
        real content lives inside a permanent one-cell bezel-safe frame. The
        panels then calculate their rectangles from the remaining viewport.
        """
        viewport_w = max(40, int(self.size.width))
        viewport_h = max(18, int(self.size.height))
        requested_safe = max(0, int(self.cfg.get("safe_margin_cells", 1)))
        safe = min(
            requested_safe,
            max(0, (viewport_w - 40) // 2),
            max(0, (viewport_h - 18) // 2),
        )
        width = max(40, viewport_w - safe * 2)
        height = max(18, viewport_h - safe * 2)

        frame = self.query_one("#safe-frame")
        frame.styles.offset = (0, 0)
        frame.styles.width = viewport_w
        frame.styles.height = viewport_h

        header_h = 6 if height >= 44 else 5 if height >= 32 else 4
        body_y = header_h
        body_h = max(12, height - body_y)

        # Containers must match their runtime viewport before child placement.
        for container_id in ("global-root", "page-now", "page-scope", "page-info", "page-console"):
            container = self.query_one(f"#{container_id}")
            container.styles.offset = (safe, safe)
            container.styles.width = width
            container.styles.height = height if container_id != "global-root" else header_h

        # ---- Compact global overlay ----
        colour_w = 2
        state_w = min(9, max(7, width // 10))
        clock_w = 9
        score_w = 0 if width < 94 else 8
        right_reserved = clock_w + score_w
        main_x = colour_w + state_w
        title_x = main_x + max(16, width // 4)
        title_w = max(12, width - title_x - right_reserved)
        artist_w = max(12, title_x - main_x)

        global_regions = {
            "colours": Region(0, 0, colour_w, header_h),
            "state": Region(colour_w, 0, state_w, header_h),
            "artist": Region(main_x, 0, artist_w, max(2, header_h // 2)),
            "album": Region(main_x, max(2, header_h // 2), artist_w, header_h - max(2, header_h // 2)),
            "title": Region(title_x, 0, title_w, max(2, header_h // 2)),
            "progress": Region(title_x, max(2, header_h // 2), title_w, header_h - max(2, header_h // 2)),
            "time": Region(width - clock_w, 0, clock_w, max(2, header_h // 2)),
            "date": Region(width - clock_w, max(2, header_h // 2), clock_w, header_h - max(2, header_h // 2)),
        }
        for widget_id, region_name in {
            "global-state": "state",
            "global-artist": "artist",
            "global-album": "album",
            "global-title": "title",
            "global-progress": "progress",
            "global-time": "time",
            "global-date": "date",
            "global-colours": "colours",
        }.items():
            self._place(widget_id, global_regions[region_name])

        if score_w:
            score_x = width - clock_w - score_w
            half = max(2, header_h // 2)
            self._place_optional("global-rating", Region(score_x, 0, score_w, half))
            self._place_optional("global-popularity", Region(score_x, half, score_w, header_h - half))
        else:
            self._place_optional("global-rating", None)
            self._place_optional("global-popularity", None)

        # ---- Now Playing ----
        # Keep the calm listening page, but spend less of it on empty lyric box
        # space and more on the cover. AlbumArtWidget uses half-block glyphs, so
        # a 2:1 cell rectangle renders roughly square pixel art.
        top_h = max(12, min(body_h - 5, int(body_h * 0.62)))
        desired_art_h = max(8, min(top_h, max(12, width // 4)))
        art_h = min(top_h, desired_art_h)
        art_w = min(max(14, width - 24), art_h * 2)
        art_h = max(7, min(top_h, art_w // 2))
        meta_w = max(22, width - art_w)
        art_y = body_y + max(0, (top_h - art_h) // 2)
        lyrics_h = max(5, body_h - top_h)
        self._place("now-metadata", Region(0, body_y, meta_w, top_h))
        self._place("now-art", Region(meta_w, art_y, art_w, art_h))
        self._place("now-lyrics", Region(0, body_y + top_h, width, lyrics_h))

        # ---- Scope ----
        scope_lyrics_h = max(5, min(body_h - 5, int(body_h * 0.30)))
        scope_wave_h = max(5, body_h - scope_lyrics_h)
        self._place("scope-wave", Region(0, body_y, width, scope_wave_h))
        self._place("scope-lyrics", Region(0, body_y + scope_wave_h, width, scope_lyrics_h))

        # ---- Track Info ----
        left_w = max(22, int(width * 0.62))
        right_w = max(18, width - left_w)
        top_info_h = max(7, int(body_h * 0.47))
        bottom_info_h = max(7, body_h - top_info_h)
        self._place("info-identity", Region(0, body_y, left_w, top_info_h))
        self._place("info-character", Region(left_w, body_y, right_w, top_info_h))
        self._place("info-provenance", Region(0, body_y + top_info_h, left_w, bottom_info_h))
        self._place("info-format", Region(left_w, body_y + top_info_h, right_w, bottom_info_h))

        # ---- Console ----
        console_left_w = max(24, int(width * 0.68))
        console_right_w = max(16, width - console_left_w)
        if body_h >= 20:
            hb_h = max(7, int(body_h * 0.36))
            diag_h = max(7, int(body_h * 0.33))
            controls_h = body_h - hb_h - diag_h
            if controls_h < 6:
                deficit = 6 - controls_h
                shrink_hb = min(deficit, max(0, hb_h - 7))
                hb_h -= shrink_hb
                deficit -= shrink_hb
                diag_h -= min(deficit, max(0, diag_h - 7))
                controls_h = body_h - hb_h - diag_h
        else:
            # Only relevant to a very small desktop-test terminal. Keep panels
            # non-overlapping rather than insisting on the full rich console.
            hb_h = max(4, body_h // 3)
            diag_h = max(4, body_h // 3)
            controls_h = max(4, body_h - hb_h - diag_h)
        self._place("console-logging", Region(0, body_y, console_left_w, body_h))
        self._place("console-heartbeats", Region(console_left_w, body_y, console_right_w, hb_h))
        self._place("console-diagnostics", Region(console_left_w, body_y + hb_h, console_right_w, diag_h))
        self._place("console-controls", Region(console_left_w, body_y + hb_h + diag_h, console_right_w, controls_h))

    def on_resize(self, event) -> None:
        # A resize can arrive during the app's initial mount sequence.
        """Handle the on resize lifecycle step."""
        if self.is_mounted:
            self._apply_adaptive_layout()

    def _update_page_from_selectors(self) -> None:
        """Follow the 4-way selector only when source selector is in UI mode."""
        source_position = bus.get_int("/io/in/selector/source_selector", -1)
        page_position = bus.get_int("/io/in/selector/visualiser_mode", -1)
        if source_position != UI_SOURCE_SELECTOR_POSITION:
            return
        page = PAGE_BY_SELECTOR.get(page_position)
        if page is not None and page != self.page:
            self._set_page(page)

    def _set_page(self, page: str) -> None:
        """Set page."""
        self.page = page
        for candidate in ("now", "scope", "info", "console"):
            self.query_one(f"#page-{candidate}").styles.display = "block" if candidate == page else "none"

    def on_unmount(self) -> None:
        """Handle the on unmount lifecycle step."""
        self.system_probe.close()

    @staticmethod
    def _due(now: float, last: float, hz: float) -> bool:
        """Return the due result."""
        return now - last >= 1.0 / max(0.1, float(hz))

    def _refresh_system(self, now: float) -> None:
        """Refresh system."""
        latest = self.system_probe.poll()
        if latest is not None:
            self.system = latest
        if now - self.last_system_at >= float(self.cfg.get("system_refresh_s", 4.0)):
            self.last_system_at = now
            self.system_probe.request()

    def _refresh_logs(self, now: float) -> None:
        """Refresh logs."""
        if now - self.last_log_at < float(self.cfg.get("log_refresh_s", 1.0)):
            return
        self.last_log_at = now
        self.logs = self.log_tail.refresh(
            38,
            require_structured=bool(self.cfg.get("structured_log_required", True)),
        )

    def _refresh_live_state(self, now: float) -> None:
        """Refresh live state."""
        raw_source = bus.get_int("/media/active_source", SOURCE_PLEX)
        if raw_source not in (SOURCE_PLEX, SOURCE_BLUETOOTH, SOURCE_LOCAL):
            raw_source = SOURCE_PLEX

        source_changed = raw_source != self.live_source
        should_sample_metadata = (
            source_changed
            or self._due(now, self._last_metadata_at, self.cfg.get("metadata_refresh_hz", 4.0))
        )

        candidate_meta = self.live_meta
        if should_sample_metadata:
            self._last_metadata_at = now
            candidate_meta = source_metadata(raw_source)

        candidate_dead = metadata_is_dead(candidate_meta)
        current_good = not metadata_is_dead(self.live_meta)
        if should_sample_metadata and candidate_dead and current_good:
            if self._dead_meta_source != raw_source or self._dead_meta_since is None:
                self._dead_meta_source = raw_source
                self._dead_meta_since = now
            grace = max(0.0, float(self.cfg.get("metadata_dead_grace_s", 2.0)))
            if now - self._dead_meta_since < grace:
                # Hold the last real source/card/colours through brief BlueZ or
                # MPRIS dropouts. The alternative is a tasteful N/A confetti
                # burst, and no, the world has suffered enough.
                if self._due(now, self._last_lyrics_at, self.cfg.get("metadata_refresh_hz", 4.0)):
                    self._last_lyrics_at = now
                    self.live_lyric = read_lyric_context_utf8() if self.live_source == SOURCE_PLEX else ("", "N/A", "")
                return
        else:
            self._dead_meta_source = None
            self._dead_meta_since = None

        if should_sample_metadata:
            self.live_source = raw_source
            self.live_meta = candidate_meta
        self.live_play_state = source_state(self.live_source)

        if self._due(now, self._last_palette_at, self.cfg.get("palette_refresh_hz", 2.0)):
            self._last_palette_at = now
            self.live_colours = palette()

        if self._due(now, self._last_lyrics_at, self.cfg.get("metadata_refresh_hz", 4.0)):
            self._last_lyrics_at = now
            self.live_lyric = read_lyric_context_utf8() if self.live_source == SOURCE_PLEX else ("", "N/A", "")

    def _refresh_album_art(self, now: float) -> None:
        # Art only matters on Now Playing. A tiny polling cadence is enough,
        # and a sequence number makes the expensive Textual invalidation occur
        # once per track rather than on every UI frame.
        """Refresh album art."""
        if now - self._last_art_at < 0.25:
            return
        self._last_art_at = now
        source = self.live_source
        prefix = "/plex" if source == SOURCE_PLEX else "/mpris" if source == SOURCE_LOCAL else ""
        seq = (source, bus.get_int(f"{prefix}/art_seq", -1)) if prefix else (source, -1)
        if seq == self._album_art_seq:
            return

        new_art = read_album_art(source)
        if new_art is None and self._album_art is not None:
            if self._dead_art_seq != seq or self._dead_art_since is None:
                self._dead_art_seq = seq
                self._dead_art_since = now
            grace = max(0.0, float(self.cfg.get("art_dead_grace_s", 2.0)))
            if now - self._dead_art_since < grace:
                return
        else:
            self._dead_art_seq = None
            self._dead_art_since = None

        self._album_art_seq = seq
        self._album_art = new_art

    def _update_static_cached(self, widget_id: str, content: object, cache_key: str) -> None:
        """Avoid invalidating Static widgets when their visible text is unchanged."""
        if self._text_cache.get(widget_id) == cache_key:
            return
        self._text_cache[widget_id] = cache_key
        self.query_one(f"#{widget_id}", Static).update(content)

    def _heartbeats_text(self) -> str:
        """Return the heartbeats text result."""
        now_ms = int(time.monotonic() * 1000)
        services = [
            ("I/O", ["pawprint"]),
            ("AUDIO", ["echo"]),
            ("PLAYER", ["minstrel", "mpris_bridge"]),
            ("HDMI-2", ["hdmi2_source_controller"]),
            ("LED", ["foxfire", "willo_wisp"]),
            ("COLOUR", ["aurora"]),
            ("BUS", ["whisper_daemon"]),
        ]

        rows = []
        for label, names in services:
            ages = []
            for name in names:
                stamp = bus.get_int(f"/proc/{name}/heartbeat_ms", 0)
                ages.append((now_ms - stamp) / 1000.0 if stamp else 999.0)
            age = max(ages)
            mark = "●" if age < 3.0 else "◐" if age < 10.0 else "×"
            rows.append((age, label, mark))

        rows.sort(key=lambda row: row[0], reverse=True)

        lines = ["[b]HEARTBEATS[/b]"]
        for age, label, mark in rows:
            lines.append(f"{label:<8} {mark}  {age:>4.1f}s")
        return "\n".join(lines)

    def _diagnostics_text(self, width: int = 24) -> str:
        """Return the diagnostics text result."""
        s = self.system
        value_width = max(6, int(width) - 10)
        now = time.monotonic()
        step_s = float(self.cfg.get("diagnostics_scroll_step_s", 0.35))
        wifi = marquee_terminal_cells(str(s.get("wifi", "N/A")), value_width, now=now, step_s=step_s)
        ip = marquee_terminal_cells(str(s.get("ip", "N/A")), value_width, now=now, step_s=step_s)
        return "\n".join([
            "[b]DIAGNOSTICS[/b]",
            f"WiFi  : {safe_markup(wifi)}",
            f"IP    : {safe_markup(ip)}",
            f"CPU   : {safe_markup(s['cpu'])}%",
            f"RAM   : {safe_markup(s['ram'])}",
            f"TEMP  : {safe_markup(s['temp'])}",
        ])

    def _controls_text(self) -> str:
        """Show Pawprint's mapped semantic controls, not raw ADC voltages."""
        def value(name: str) -> str:
            """Return the value result."""
            raw = bus.get_float(f"/io/in/control/{name}", 0.0)
            return f"{clamp01(raw):.2f}"

        return "\n".join([
            "[b]CONTROL INPUTS[/b]",
            f"BRIGHT: {value('brightness')}",
            f"GAIN  : {value('gain')}",
            f"COLOUR: {value('colour')}",
            f"SAT   : {value('saturation')}",
        ])

    def refresh_frame(self) -> None:
        """Refresh frame."""
        now = time.monotonic()
        self._update_page_from_selectors()
        self._refresh_live_state(now)

        if self._due(now, self._last_global_at, self.cfg.get("global_refresh_hz", 4.0)):
            self._last_global_at = now
            self._update_global(self.live_meta, self.live_source, self.live_play_state, self.live_colours)

        # Do not render three hidden pages every 50 ms. That was the primary
        # cause of the little UI freezes followed by a burst of delayed logs.
        if self.page == "now":
            self._refresh_album_art(now)
            if self._due(now, self._last_now_at, self.cfg.get("now_refresh_hz", 3.0)):
                self._last_now_at = now
                self._update_now(self.live_meta, self.live_source, self.live_lyric, self.live_colours)
        elif self.page == "scope":
            if self._due(now, self._last_scope_at, self.cfg.get("scope_refresh_hz", 15.0)):
                self._last_scope_at = now
                self._update_scope(self.live_lyric, self.live_colours)
        elif self.page == "info":
            if self._due(now, self._last_info_at, self.cfg.get("info_refresh_hz", 1.0)):
                self._last_info_at = now
                self._update_info(self.live_meta)
        elif self.page == "console":
            self._refresh_logs(now)
            self._refresh_system(now)
            if self._due(now, self._last_console_at, self.cfg.get("console_refresh_hz", 2.0)):
                self._last_console_at = now
                self._update_console()

    def _update_global(self, meta: dict, source: int, play_state: int, colours: Sequence[Tuple[int, int, int]]) -> None:
        """Update global."""
        now = datetime.now()
        ticks = Text()
        for colour in colours:
            # Full blocks survive small terminal grids and low-contrast themes
            # better than the original single-pixel-looking vertical strokes.
            ticks.append("▌", style=rgb_style(colour))
        self.query_one("#global-colours", Static).update(ticks)
        self.query_one("#global-state", Static).update(f"[b]{state_name(play_state)}[/b]\n{source_name(source)}")
        self.query_one("#global-artist", Static).update(f"[b]{safe_markup(meta['artist'])}[/b]")
        self.query_one("#global-album", Static).update(safe_markup(meta["album"]))
        self.query_one("#global-title", Static).update(f"[b]{safe_markup(meta['title'])}[/b] [{safe_markup(meta['play_count'])}]")
        self.query_one("#global-rating", Static).update(f"RATE {safe_markup(meta['rating'])}")
        self.query_one("#global-popularity", Static).update(f"POP  {safe_markup(meta['popularity'])}")
        global_progress = self.query_one("#global-progress", Static)
        global_progress_width = max(12, min(72, global_progress.size.width - 2))
        global_progress.update(
            f"{progress_bar(meta['position'], meta['duration'], global_progress_width)}\n{fmt_time(meta['position'])} / {fmt_time(meta['duration'])}"
        )
        self.query_one("#global-time", Static).update(now.strftime("[b]%H:%M[/b]\n%a"))
        self.query_one("#global-date", Static).update(now.strftime("%d %b\n%Y"))

    def _update_now(
        self,
        meta: dict,
        source: int,
        lyric_context: Tuple[str, str, str],
        colours: Sequence[Tuple[int, int, int]],
    ) -> None:
        """Update now."""
        lines = [
            "[b]NOW PLAYING[/b]",
            safe_markup(meta["title"]),
            safe_markup(meta["artist"]),
            safe_markup(meta["album"]),
            f"{source_name(source)}  •  TRACK {safe_markup(meta['track_number'] or 'N/A')}  •  DISC {safe_markup(meta['disc_number'] or 'N/A')}",
            "",
            progress_bar(meta["position"], meta["duration"], max(12, min(64, self.query_one("#now-metadata", Static).size.width - 4))),
            f"{fmt_time(meta['position'])} / {fmt_time(meta['duration'])}",
        ]
        self.query_one("#now-metadata", Static).update("\n".join(lines))
        self.query_one("#now-art", AlbumArtWidget).set_art_context(self._album_art, colours, self._album_art_seq)
        now_lyrics = self.query_one("#now-lyrics", Static)
        lyric_key = f"{lyric_context!r}|{now_lyrics.size.width}|{now_lyrics.size.height}"
        self._update_static_cached(
            "now-lyrics",
            lyric_context_widget(lyric_context, now_lyrics.size.width, now_lyrics.size.height),
            lyric_key,
        )

    def _update_scope(
        self,
        lyric_context: Tuple[str, str, str],
        colours: Sequence[Tuple[int, int, int]],
    ) -> None:
        """Update scope."""
        waveform = bus.try_get_array(
            str(self.cfg.get("waveform_key", "/audio/waveform")),
            int(self.cfg.get("waveform_samples", 160)), dtype="f32",
        ) or [0.0] * int(self.cfg.get("waveform_samples", 160))
        self.query_one("#scope-wave", ScopeWidget).set_waveform(waveform, colours[1])
        scope_lyrics = self.query_one("#scope-lyrics", Static)
        lyric_key = f"{lyric_context!r}|{scope_lyrics.size.width}|{scope_lyrics.size.height}"
        self._update_static_cached(
            "scope-lyrics",
            lyric_context_widget(lyric_context, scope_lyrics.size.width, scope_lyrics.size.height),
            lyric_key,
        )

    def _update_info(self, meta: dict) -> None:
        """Update info."""
        identity = "\n".join([
            "[b]IDENTITY[/b]",
            "",
            safe_markup(meta["title"]),
            safe_markup(meta["artist"]),
            safe_markup(meta["album"]),
            f"YEAR   : {safe_markup(meta['year'])}",
            f"DISC   : {safe_markup(meta['disc_number'] or 'N/A')}",
            f"TRACK  : {safe_markup(meta['track_number'] or 'N/A')} / {safe_markup(meta['track_count'] or 'N/A')}",
        ])
        character = "\n".join([
            "[b]MUSICAL CHARACTER[/b]",
            "",
            f"GENRE  : {safe_markup(meta['genre'])}",
            f"MOOD   : {safe_markup(meta['mood'])}",
            f"BPM    : {safe_markup(meta['bpm'])}",
            f"LENGTH : {fmt_time(meta['duration'])}",
            f"RATE   : {safe_markup(meta['rating'])}",
            f"POP    : {safe_markup(meta['popularity'])}",
        ])
        provenance = "\n".join([
            "[b]PROVENANCE[/b]",
            "",
            f"ADDED  : {safe_markup(meta['added'])}",
            f"PLAYED : {safe_markup(meta['last_played'])}",
            f"PLAYS  : {safe_markup(meta['play_count'])}",
            f"FILE   : {safe_markup(meta['file_name'])}",
            f"SIZE   : {safe_markup(meta['file_size'])}",
        ])
        format_lines = [
            "[b]FORMAT + LEVEL[/b]",
            "",
            f"TYPE   : {safe_markup(meta['container'])}",
            f"CODEC  : {safe_markup(meta['codec'])}",
            f"AUDIO  : {safe_markup(meta['channels'])} ch · {safe_markup(meta['channel_layout'])}",
            f"RATE   : {safe_markup(meta['sample_rate'])} · {safe_markup(meta['bit_depth'])}",
            f"BITRATE: {safe_markup(meta['bitrate'])}",
            f"STREAM : {safe_markup(meta['stream_title'])}",
            f"GAIN   : {safe_markup(meta['track_gain'])}",
            f"PEAK   : {safe_markup(meta['track_peak'])}",
            f"ALB G  : {safe_markup(meta['album_gain'])}",
            f"ALB P  : {safe_markup(meta['album_peak'])}",
            f"ALB R  : {safe_markup(meta['album_range'])}",
            f"LUFS   : {safe_markup(meta['loudness'])}",
            f"LRA    : {safe_markup(meta['lra'])}",
        ]
        self._update_static_cached("info-identity", identity, identity)
        self._update_static_cached("info-character", character, character)
        self._update_static_cached("info-provenance", provenance, provenance)
        format_text = "\n".join(format_lines)
        self._update_static_cached("info-format", format_text, format_text)

    def _update_console(self) -> None:
        """Update console."""
        source_label = self.log_tail.source_name.upper().replace("-", " ")
        log_text = Text(f"FOUNDRY LOG · {source_label}\n", style="bold")
        visible_logs = self.logs[-25:]
        for index, line in enumerate(visible_logs):
            log_text.append_text(line)
            if index != len(visible_logs) - 1:
                log_text.append("\n")
        log_key = repr([(line.plain, line.spans) for line in visible_logs])
        self._update_static_cached("console-logging", log_text, log_key)
        heartbeat_text = self._heartbeats_text()
        self._update_static_cached("console-heartbeats", heartbeat_text, heartbeat_text)
        diagnostics_widget = self.query_one("#console-diagnostics", Static)
        diagnostics_text = self._diagnostics_text(diagnostics_widget.size.width)
        self._update_static_cached("console-diagnostics", diagnostics_text, diagnostics_text)
        controls_text = self._controls_text()
        self._update_static_cached("console-controls", controls_text, controls_text)


def main() -> None:
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    app = EmberUI(cfg)
    try:
        app.run()
    finally:
        bus.close_all()


if __name__ == "__main__":
    main()
