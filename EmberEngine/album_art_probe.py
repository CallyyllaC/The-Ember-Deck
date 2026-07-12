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
import os
import re
import signal
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import yaml

import synapse as bus

try:
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
    "update_hz": 20,
    "metadata_refresh_hz": 4.0,
    "global_refresh_hz": 4.0,
    "now_refresh_hz": 3.0,
    "scope_refresh_hz": 15.0,
    "info_refresh_hz": 1.0,
    "console_refresh_hz": 2.0,
    "palette_refresh_hz": 2.0,
    "system_refresh_s": 4.0,
    "log_refresh_s": 1.0,
    "log_path": "logs/foundry.log",
    "waveform_key": "/audio/waveform",
    "waveform_samples": 160,
}

SOURCE_NONE = 0
SOURCE_PLEX = 1
SOURCE_BLUETOOTH = 2
SOURCE_LOCAL = 3

PLAY_STOPPED = 0
PLAY_PLAYING = 1
PLAY_PAUSED = 2

TEXT_BUFFER = 192
LYRIC_BUFFER = 256


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
    """Read utf8."""
    raw = bus.try_get_array(key, length=length, dtype="u8")
    if not raw:
        return "N/A"
    data = bytes(raw)
    data = data.split(b"\x00", 1)[0]
    try:
        return _n_a(data.decode("utf-8", errors="replace"))
    except Exception:
        return "N/A"


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
    return {
        SOURCE_PLEX: "PLEX",
        SOURCE_BLUETOOTH: "BT",
        SOURCE_LOCAL: "LOCAL",
    }.get(int(source), "N/A")


def source_state(source: int) -> int:
    """Return the source state result."""
    if source == SOURCE_PLEX:
        return bus.get_int("/plex/play_state", PLAY_STOPPED)
    if source == SOURCE_BLUETOOTH:
        return bus.get_int("/bt/play_state", PLAY_STOPPED)
    return PLAY_STOPPED


def source_metadata(source: int) -> dict:
    """Return the source metadata result."""
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
        }
    return {
        "source": SOURCE_LOCAL,
        "title": "N/A", "artist": "N/A", "album": "N/A",
        "position": 0.0, "duration": 0.0, "track_number": 0, "track_count": 0,
        "disc_number": 0, "year": "N/A", "genre": "N/A", "codec": "N/A",
        "bitrate": "N/A", "sample_rate": "N/A", "loudness": "N/A", "added": "N/A",
        "play_count": "N/A", "popularity": "N/A", "rating": "N/A", "bpm": "N/A",
        "mood": "N/A",
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


def read_album_art() -> Optional[np.ndarray]:
    """Read Minstrel's small RGB cover mosaic, or return ``None`` safely."""
    if bus.get_int("/plex/art_valid", 0) != 1:
        return None
    width = bus.get_int("/plex/art_width", 0)
    height = bus.get_int("/plex/art_height", 0)
    if not (1 <= width <= ART_BUS_WIDTH and 1 <= height <= ART_BUS_HEIGHT):
        return None
    values = bus.try_get_array("/plex/art_rgb", ART_BUS_BYTES, dtype="u8")
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
    def __init__(self, path: Path):
        """Initialize configuration, dependencies, and runtime state."""
        self.path = path
        self._last_mtime_ns = -1
        self.lines: List[str] = ["N/A"]

    def refresh(self, limit: int = 36) -> List[str]:
        """Refresh object."""
        try:
            stat = self.path.stat()
            if stat.st_mtime_ns == self._last_mtime_ns:
                return self.lines[-limit:]
            self._last_mtime_ns = stat.st_mtime_ns
            # A modest tail avoids reading megabytes just to show a console.
            with self.path.open("rb") as fh:
                fh.seek(max(0, stat.st_size - 24_000))
                data = fh.read().decode("utf-8", errors="replace")
            lines = [line.rstrip() for line in data.splitlines() if line.strip()]
            self.lines = lines[-max(1, limit):] or ["N/A"]
        except Exception:
            self.lines = ["N/A"]
        return self.lines[-limit:]


class ScopeWidget(Widget):
    """A real time-domain scope, rendered from Echo's decimated waveform."""

    DEFAULT_CSS = """
    ScopeWidget { overflow: hidden hidden; background: #050505; }
    """

    def __init__(self, **kwargs) -> None:
        """Initialize configuration, dependencies, and runtime state."""
        super().__init__(**kwargs)
        self.samples = np.zeros(160, dtype=np.float32)
        self._scale = 0.05
        self.colour = (170, 200, 255)

    def set_waveform(self, values: Sequence[float], colour: Tuple[int, int, int]) -> None:
        """Set waveform."""
        array = np.asarray(values, dtype=np.float32).reshape(-1)
        if array.size:
            self.samples = array
        self.colour = colour
        self.refresh()

    def render(self) -> Text:
        """Return the render result."""
        width = max(1, self.size.width)
        height = max(1, self.size.height)
        if self.samples.size < 2:
            samples = np.zeros(width, dtype=np.float32)
        else:
            x_old = np.linspace(0.0, 1.0, self.samples.size, dtype=np.float32)
            x_new = np.linspace(0.0, 1.0, width, dtype=np.float32)
            samples = np.interp(x_new, x_old, self.samples).astype(np.float32)
        peak = max(0.015, float(np.percentile(np.abs(samples), 98)))
        self._scale = self._scale * 0.88 + peak * 0.12
        normal = np.clip(samples / max(0.015, self._scale), -1.0, 1.0)
        grid = [[" " for _ in range(width)] for _ in range(height)]
        center = (height - 1) / 2.0
        previous: Optional[int] = None
        for x, value in enumerate(normal):
            y = int(round(center - value * center * 0.92))
            y = max(0, min(height - 1, y))
            if previous is not None:
                lo, hi = sorted((previous, y))
                for row in range(lo, hi + 1):
                    grid[row][x] = "│" if row not in (previous, y) else "•"
            else:
                grid[y][x] = "•"
            previous = y
        mid = int(round(center))
        for x in range(width):
            if grid[mid][x] == " ":
                grid[mid][x] = "·"
        return Text("\n".join("".join(row) for row in grid), style=rgb_style(self.colour))


class AlbumArtWidget(Widget):
    """Render real Plex cover pixels using normal terminal glyphs.

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
        self._sequence = -1

    def set_art_context(
        self,
        cover: Optional[np.ndarray],
        colours: Sequence[Tuple[int, int, int]],
        sequence: int,
    ) -> None:
        """Set art context."""
        palette = list(colours)[:4] or [(70, 70, 70)] * 4
        if sequence == self._sequence and palette == self.palette:
            return
        self._sequence = int(sequence)
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
        layers: pages overlay;
    }
    .region {
        position: absolute;
        overflow: hidden hidden;
        padding: 0 1;
    }
    .panel { border: round #444444; background: #070707; }
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
        self.live_lyric = "N/A"
        self._album_art: Optional[np.ndarray] = None
        self._album_art_seq = -1
        log_path = Path(str(cfg["log_path"]))
        if not log_path.is_absolute():
            log_path = BASE / log_path
        self.log_tail = LogTail(log_path)
        self.logs = ["N/A"]

    def compose(self) -> ComposeResult:
        # Global overlay. Geometry is fixed in HARD_LAYOUTS above.
        """Return the compose result."""
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
            yield Static(id="now-logging", classes="region panel")

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
        """Fit the original page architecture into the *actual* terminal grid.

        The old UI was hard-coded as 80×60 cells. The panel is 800×480 pixels,
        not 60 terminal rows tall, so any normal LXTerminal font necessarily
        pushed lower regions below the display. This keeps the same pages and
        information groups but derives their rectangles from Textual's live
        viewport on mount and resize.
        """
        width = max(40, int(self.size.width))
        height = max(18, int(self.size.height))
        header_h = 6 if height >= 44 else 5 if height >= 32 else 4
        body_y = header_h
        body_h = max(12, height - body_y)

        # Containers must match their runtime viewport before child placement.
        for container_id in ("global-root", "page-now", "page-scope", "page-info", "page-console"):
            container = self.query_one(f"#{container_id}")
            container.styles.width = width
            container.styles.height = height if container_id != "global-root" else header_h
        self.query_one("#global-root").styles.offset = (0, 0)

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
        art_w = min(max(14, width // 4), 24)
        meta_w = max(20, width - art_w)
        top_h = max(5, min(body_h - 7, int(body_h * 0.48)))
        lyrics_h = max(4, min(body_h - top_h - 3, int(body_h * 0.28)))
        log_h = max(3, body_h - top_h - lyrics_h)
        self._place("now-metadata", Region(0, body_y, meta_w, top_h))
        self._place("now-art", Region(meta_w, body_y, art_w, top_h))
        self._place("now-lyrics", Region(0, body_y + top_h, width, lyrics_h))
        self._place("now-logging", Region(0, body_y + top_h + lyrics_h, width, log_h))

        # ---- Scope ----
        scope_lyrics_h = max(4, min(body_h - 5, int(body_h * 0.30)))
        scope_wave_h = max(5, body_h - scope_lyrics_h)
        self._place("scope-wave", Region(0, body_y, width, scope_wave_h))
        self._place("scope-lyrics", Region(0, body_y + scope_wave_h, width, scope_lyrics_h))

        # ---- Track Info ----
        left_w = max(22, int(width * 0.62))
        right_w = max(18, width - left_w)
        top_info_h = max(6, int(body_h * 0.52))
        bottom_info_h = max(6, body_h - top_info_h)
        self._place("info-identity", Region(0, body_y, left_w, top_info_h))
        self._place("info-character", Region(left_w, body_y, right_w, top_info_h))
        self._place("info-provenance", Region(0, body_y + top_info_h, left_w, bottom_info_h))
        self._place("info-format", Region(left_w, body_y + top_info_h, right_w, bottom_info_h))

        # ---- Console ----
        console_left_w = max(24, int(width * 0.70))
        console_right_w = max(16, width - console_left_w)
        hb_h = max(6, int(body_h * 0.46))
        self._place("console-logging", Region(0, body_y, console_left_w, body_h))
        self._place("console-heartbeats", Region(console_left_w, body_y, console_right_w, hb_h))
        self._place("console-diagnostics", Region(console_left_w, body_y + hb_h, console_right_w, max(6, body_h - hb_h)))

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
        self.logs = self.log_tail.refresh(38)

    def _refresh_live_state(self, now: float) -> None:
        """Refresh live state."""
        source = bus.get_int("/media/active_source", SOURCE_PLEX)
        if source not in (SOURCE_PLEX, SOURCE_BLUETOOTH, SOURCE_LOCAL):
            source = SOURCE_PLEX
        source_changed = source != self.live_source
        self.live_source = source
        self.live_play_state = source_state(source)

        if source_changed or self._due(now, self._last_metadata_at, self.cfg.get("metadata_refresh_hz", 4.0)):
            self._last_metadata_at = now
            self.live_meta = source_metadata(source)

        if self._due(now, self._last_palette_at, self.cfg.get("palette_refresh_hz", 2.0)):
            self._last_palette_at = now
            self.live_colours = palette()

        if self._due(now, self._last_lyrics_at, self.cfg.get("metadata_refresh_hz", 4.0)):
            self._last_lyrics_at = now
            self.live_lyric = read_utf8("/lyrics/current_utf8", LYRIC_BUFFER) if source == SOURCE_PLEX else "N/A"

    def _refresh_album_art(self, now: float) -> None:
        # Art only matters on Now Playing. A tiny polling cadence is enough,
        # and a sequence number makes the expensive Textual invalidation occur
        # once per track rather than on every UI frame.
        """Refresh album art."""
        if now - self._last_art_at < 0.25:
            return
        self._last_art_at = now
        seq = bus.get_int("/plex/art_seq", -1)
        if seq == self._album_art_seq:
            return
        self._album_art_seq = seq
        self._album_art = read_album_art()

    def _heartbeats_text(self) -> str:
        """Return the heartbeats text result."""
        now_ms = int(time.monotonic() * 1000)
        services = [
            ("AUDIO", ["echo"]),
            ("PLAYER", ["minstrel"]),
            ("LYRICS", ["minstrel"]),
            ("COLOUR", ["aurora"]),
            ("LED", ["foxfire", "willo_wisp"]),
            ("I/O", ["pawprint"]),
        ]
        lines = ["[b]HEARTBEATS[/b]"]
        for label, names in services:
            ages = []
            for name in names:
                stamp = bus.get_int(f"/proc/{name}/heartbeat_ms", 0)
                ages.append((now_ms - stamp) / 1000.0 if stamp else 999.0)
            age = max(ages)
            mark = "●" if age < 3.0 else "◐" if age < 10.0 else "×"
            lines.append(f"{label:<8} {mark}  {age:>4.1f}s")
        return "\n".join(lines)

    def _diagnostics_text(self) -> str:
        """Return the diagnostics text result."""
        s = self.system
        return "\n".join([
            "[b]DIAGNOSTICS[/b]",
            f"WiFi  : {safe_markup(s['wifi'])}",
            f"IP    : {safe_markup(s['ip'])}",
            f"VOL   : {safe_markup(s['volume'])}",
            f"CPU   : {safe_markup(s['cpu'])}%",
            f"RAM   : {safe_markup(s['ram'])}",
            f"TEMP  : {safe_markup(s['temp'])}",
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
            self._refresh_logs(now)
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
        self.query_one("#global-progress", Static).update(
            f"{progress_bar(meta['position'], meta['duration'], 26)}\n{fmt_time(meta['position'])} / {fmt_time(meta['duration'])}"
        )
        self.query_one("#global-time", Static).update(now.strftime("[b]%H:%M[/b]\n%a"))
        self.query_one("#global-date", Static).update(now.strftime("%d %b\n%Y"))

    def _update_now(self, meta: dict, source: int, lyric: str, colours: Sequence[Tuple[int, int, int]]) -> None:
        """Update now."""
        lines = [
            "[b]NOW PLAYING[/b]",
            safe_markup(meta["title"]),
            safe_markup(meta["artist"]),
            safe_markup(meta["album"]),
            f"{source_name(source)}  •  TRACK {safe_markup(meta['track_number'] or 'N/A')}  •  DISC {safe_markup(meta['disc_number'] or 'N/A')}",
            "",
            progress_bar(meta["position"], meta["duration"], 44),
            f"{fmt_time(meta['position'])} / {fmt_time(meta['duration'])}",
        ]
        self.query_one("#now-metadata", Static).update("\n".join(lines))
        self.query_one("#now-art", AlbumArtWidget).set_art_context(self._album_art, colours, self._album_art_seq)
        lyric_display = safe_markup(lyric) if lyric != "N/A" else "N/A"
        self.query_one("#now-lyrics", Static).update(f"[b]LYRICS[/b]\n\n{lyric_display}")
        self.query_one("#now-logging", Static).update("[b]LOG[/b]\n" + "\n".join(safe_markup(line) for line in self.logs[-4:]))

    def _update_scope(self, lyric: str, colours: Sequence[Tuple[int, int, int]]) -> None:
        """Update scope."""
        waveform = bus.try_get_array(
            str(self.cfg.get("waveform_key", "/audio/waveform")),
            int(self.cfg.get("waveform_samples", 160)), dtype="f32",
        ) or [0.0] * int(self.cfg.get("waveform_samples", 160))
        self.query_one("#scope-wave", ScopeWidget).set_waveform(waveform, colours[1])
        self.query_one("#scope-lyrics", Static).update(f"[b]LYRICS[/b]\n\n{safe_markup(lyric) if lyric != 'N/A' else 'N/A'}")

    def _update_info(self, meta: dict) -> None:
        """Update info."""
        self.query_one("#info-identity", Static).update("\n".join([
            "[b]IDENTITY[/b]", "", safe_markup(meta["title"]), safe_markup(meta["artist"]), safe_markup(meta["album"]),
            f"YEAR   : {safe_markup(meta['year'])}", f"TRACK  : {safe_markup(meta['track_number'] or 'N/A')}",
        ]))
        self.query_one("#info-character", Static).update("\n".join([
            "[b]MUSICAL CHARACTER[/b]", "", f"BPM    : {safe_markup(meta['bpm'])}",
            f"LENGTH : {fmt_time(meta['duration'])}", f"MOOD   : {safe_markup(meta['mood'])}",
            f"GENRE  : {safe_markup(meta['genre'])}",
        ]))
        self.query_one("#info-provenance", Static).update("\n".join([
            "[b]PROVENANCE[/b]", "", f"ADDED  : {safe_markup(meta['added'])}",
            f"PLAYS  : {safe_markup(meta['play_count'])}", f"POP    : {safe_markup(meta['popularity'])}",
            f"RATE   : {safe_markup(meta['rating'])}",
        ]))
        self.query_one("#info-format", Static).update("\n".join([
            "[b]FORMAT[/b]", "", f"CODEC  : {safe_markup(meta['codec'])}",
            f"BITRATE: {safe_markup(meta['bitrate'])}", f"RATE   : {safe_markup(meta['sample_rate'])}",
            f"LOUD   : {safe_markup(meta['loudness'])}",
        ]))

    def _update_console(self) -> None:
        """Update console."""
        self.query_one("#console-logging", Static).update("[b]FOUNDRY LOG[/b]\n" + "\n".join(safe_markup(line) for line in self.logs[-25:]))
        self.query_one("#console-heartbeats", Static).update(self._heartbeats_text())
        self.query_one("#console-diagnostics", Static).update(self._diagnostics_text())


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
