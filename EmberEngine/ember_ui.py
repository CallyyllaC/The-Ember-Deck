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

import dot_matrix
import synapse as bus
from spirit_messages import DEFAULT_ROLE_STYLES
from runtime_config import load_yaml_mapping

try:
    from rich.cells import cell_len
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.containers import Container
    from textual.widget import Widget
    from textual.widgets import Static
except ModuleNotFoundError as exc:  # pragma: no cover - only happens before install
    raise SystemExit(
        "Textual is not installed in this virtual environment. Install the UI "
        "dependencies as described in docs/INSTALLATION.md, for example: "
        "source .venv/bin/activate && python -m pip install rich textual"
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
    # The physical brightness control also breathes the UI chrome, within a
    # deliberately narrow range. It never changes the monitor backlight.
    "ui_brightness_floor": 0.52,
    "ui_brightness_ceiling": 1.0,
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
    # Draw the current lyric line as large dot-matrix capitals when it fits.
    "large_lyrics": True,
}

from contracts import (  # noqa: E402
    MEDIA_SOURCE_BLUETOOTH as SOURCE_BLUETOOTH,
    MEDIA_SOURCE_MPRIS as SOURCE_LOCAL,
    MEDIA_SOURCE_PLEX as SOURCE_PLEX,
    PLAY_PAUSED,
    PLAY_PLAYING,
    PLAY_STOPPED,
)
from contracts import (  # noqa: E402
    DISCORD_STATUS_AUTHORIZING,
    DISCORD_STATUS_CONNECTED,
    DISCORD_STATUS_CONNECTING,
    DISCORD_STATUS_DISABLED,
    DISCORD_STATUS_ERROR,
    DISCORD_STATUS_NOT_CONFIGURED,
    DISCORD_STATUS_NOT_LINKED,
    DISCORD_STATUS_RECONNECTING,
    DISCORD_STATUS_SDK_UNAVAILABLE,
)

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
        loaded = load_yaml_mapping(path, "ember_ui")
    cfg = DEFAULTS.copy()
    cfg.update(loaded)
    cfg["_config_path"] = path
    cfg["_config_dir"] = path.parent
    return cfg


@dataclass(frozen=True)
class Region:
    """One Textual-cell rectangle, relative to its page container."""

    x: int
    y: int
    width: int
    height: int


# ---- Layout -----------------------------------------------------------------
# The layout is code-owned and computed from the live terminal grid, which is
# set by the LXTerminal profile's font size. A bigger font means fewer cells,
# so every rule below must still hold from about 56 × 20 up to 80 × 60.
# Coordinates are Textual cells, not pixels.

MIN_VIEWPORT_W = 40
MIN_VIEWPORT_H = 18
# Every ".panel" has a round border and one cell of padding left and right.
PANEL_CHROME_W = 4
PANEL_CHROME_H = 2
# Two content rows plus the header's bottom rule, on every grid size.
HEADER_H = 3
HEADER_COLOURS_W = 2
HEADER_STATE_W = 7
HEADER_CLOCK_W = 7
# The Now Playing text keeps at least this much width before the cover grows.
NOW_METADATA_MIN_W = 34
# Below this inner width the cover is too small to read and is hidden.
ART_MIN_INNER_W = 8
# Previous, current and next lyric line inside a bordered panel.
LYRICS_MIN_H = 3 + PANEL_CHROME_H
# Large lyrics are used only where the band holds two or more dot-matrix
# lines (two rows each) and about 48 large characters in all. A smaller band
# would fit only short lines, so the text would flip size from line to line.
LARGE_LYRIC_MIN_ROWS = 4
LARGE_LYRIC_MIN_CHARS = 48
CONSOLE_STATUS_DEFAULT_LINES = 3

PAGES = ("now", "scope", "info", "console")
PAGE_WIDGETS: Dict[str, Tuple[str, ...]] = {
    "now": ("now-metadata", "now-art", "now-lyrics"),
    "scope": ("scope-wave", "scope-lyrics"),
    "info": ("info-identity", "info-character", "info-provenance", "info-format"),
    "console": ("console-status", "console-logging"),
}
HEARTBEAT_SERVICES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("I/O", ("pawprint",)),
    ("AUDIO", ("echo",)),
    ("PLAYER", ("minstrel", "mpris_bridge")),
    ("HDMI-2", ("hdmi2_source_controller",)),
    ("LED", ("foxfire", "willo_wisp")),
    ("COLOUR", ("aurora",)),
    ("BUS", ("whisper_daemon",)),
)
HEALTH_STYLES = {"ok": "bold #6fcf6f", "late": "bold #e0a040", "dead": "bold #e85050"}
PANEL_TITLES = {
    "now-metadata": "NOW PLAYING",
    "now-lyrics": "LYRICS",
    "info-identity": "IDENTITY",
    "info-character": "MUSICAL CHARACTER",
    "info-provenance": "PROVENANCE",
    "info-format": "FORMAT + LEVEL",
    "console-status": "SYSTEM",
    "console-logging": "FOUNDRY LOG",
}
HEADER_WIDGETS = (
    "global-colours", "global-state", "global-artist", "global-album",
    "global-title", "global-progress", "global-time", "global-date",
)


@dataclass(frozen=True)
class Layout:
    """Every widget rectangle for one terminal size and visible page.

    Header regions are relative to ``#global-root`` and page regions to their
    ``#page-*`` container; both containers sit at ``(safe, safe)``. A region
    of ``None`` means the widget is hidden at this size or on this page.
    """

    viewport_w: int
    viewport_h: int
    safe: int
    width: int
    height: int
    page: str
    regions: Dict[str, Optional[Region]]

    def inner(self, widget_id: str) -> Tuple[int, int]:
        """Text area of a bordered, padded panel: (columns, rows)."""
        region = self.regions.get(widget_id)
        if region is None:
            return (0, 0)
        return (max(1, region.width - PANEL_CHROME_W), max(1, region.height - PANEL_CHROME_H))

    def span(self, widget_id: str) -> int:
        """Width of an unbordered header widget, or 0 when it is hidden."""
        region = self.regions.get(widget_id)
        return region.width if region is not None else 0


def compute_layout(
    viewport_w: int,
    viewport_h: int,
    safe_margin: int = 1,
    page: str = "now",
    console_status_lines: int = CONSOLE_STATUS_DEFAULT_LINES,
) -> Layout:
    """Fit the header and all four pages into a terminal grid.

    The physical case steals a little of the view at some angles, so content
    lives inside a bezel-safe inset. The header always uses two text rows and
    never wraps; the pages share what is left.
    """
    viewport_w = max(MIN_VIEWPORT_W, int(viewport_w))
    viewport_h = max(MIN_VIEWPORT_H, int(viewport_h))
    safe = min(
        max(0, int(safe_margin)),
        (viewport_w - MIN_VIEWPORT_W) // 2,
        (viewport_h - MIN_VIEWPORT_H) // 2,
    )
    width = viewport_w - safe * 2
    height = viewport_h - safe * 2
    regions: Dict[str, Optional[Region]] = {}

    # ---- Header: colour ticks | state | text | clock, two single-line rows.
    middle_x = HEADER_COLOURS_W + HEADER_STATE_W
    middle_w = width - middle_x - HEADER_CLOCK_W
    clock_x = width - HEADER_CLOCK_W
    regions["global-colours"] = Region(0, 0, HEADER_COLOURS_W, 2)
    regions["global-state"] = Region(HEADER_COLOURS_W, 0, HEADER_STATE_W, 2)
    regions["global-time"] = Region(clock_x, 0, HEADER_CLOCK_W, 1)
    regions["global-date"] = Region(clock_x, 1, HEADER_CLOCK_W, 1)
    if page == "now":
        # The Now Playing panel already shows title, artist, album and
        # progress in larger space, so here the header carries track context.
        regions["global-artist"] = Region(middle_x, 0, middle_w - 1, 1)
        regions["global-album"] = Region(middle_x, 1, middle_w - 1, 1)
        regions["global-title"] = None
        regions["global-progress"] = None
    else:
        artist_w = max(10, middle_w * 2 // 5)
        title_x = middle_x + artist_w
        title_w = middle_w - artist_w - 1
        regions["global-artist"] = Region(middle_x, 0, artist_w - 1, 1)
        regions["global-album"] = Region(middle_x, 1, artist_w - 1, 1)
        regions["global-title"] = Region(title_x, 0, title_w, 1)
        regions["global-progress"] = Region(title_x, 1, title_w, 1)

    body_y = HEADER_H
    body_h = height - HEADER_H

    # ---- Now Playing: text and a square cover above a lyric band.
    # About a third of the page, so large lyrics get two dot-matrix lines
    # with the neighbouring lines around them on an 80 × 30 grid.
    lyrics_h = max(LYRICS_MIN_H, min(body_h - 8, round(body_h * 0.36)))
    top_h = body_h - lyrics_h
    # Half-block art: one cell is one pixel wide and two pixels tall, so a
    # square cover needs twice as many columns as rows.
    art_inner_h = top_h - PANEL_CHROME_H
    art_inner_w = art_inner_h * 2
    max_art_inner_w = width - min(NOW_METADATA_MIN_W, width // 2) - PANEL_CHROME_W
    if art_inner_w > max_art_inner_w:
        art_inner_w = max_art_inner_w - (max_art_inner_w % 2)
        art_inner_h = art_inner_w // 2
    if art_inner_w >= ART_MIN_INNER_W:
        art_w = art_inner_w + PANEL_CHROME_W
        art_h = art_inner_h + PANEL_CHROME_H
        regions["now-art"] = Region(width - art_w, body_y + (top_h - art_h) // 2, art_w, art_h)
    else:
        art_w = 0
        regions["now-art"] = None
    regions["now-metadata"] = Region(0, body_y, width - art_w, top_h)
    regions["now-lyrics"] = Region(0, body_y + top_h, width, lyrics_h)

    # ---- Scope: waveform above the same lyric band.
    scope_lyrics_h = max(LYRICS_MIN_H, min(body_h - 6, round(body_h * 0.3)))
    regions["scope-wave"] = Region(0, body_y, width, body_h - scope_lyrics_h)
    regions["scope-lyrics"] = Region(0, body_y + body_h - scope_lyrics_h, width, scope_lyrics_h)

    # ---- Track Info: identity and character on top, the longer lists below.
    left_w = width * 11 // 20
    right_w = width - left_w
    top_info_h = max(5, min(8, body_h - 7))
    bottom_info_h = body_h - top_info_h
    regions["info-identity"] = Region(0, body_y, left_w, top_info_h)
    regions["info-character"] = Region(left_w, body_y, right_w, top_info_h)
    regions["info-provenance"] = Region(0, body_y + top_info_h, left_w, bottom_info_h)
    regions["info-format"] = Region(left_w, body_y + top_info_h, right_w, bottom_info_h)

    # ---- Console: a compact status band, then the log at full width. The
    # log always keeps at least four rows; extra status rows are clipped.
    status_lines = max(1, min(int(console_status_lines), body_h - PANEL_CHROME_H * 2 - 4))
    status_h = status_lines + PANEL_CHROME_H
    regions["console-status"] = Region(0, body_y, width, status_h)
    regions["console-logging"] = Region(0, body_y + status_h, width, body_h - status_h)

    return Layout(viewport_w, viewport_h, safe, width, height, page, regions)

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


def read_utf8(key: str, length: int = TEXT_BUFFER) -> str:
    """Read a fixed UTF-8 Synapse segment without truth-testing NumPy data."""
    try:
        raw = bus.try_get_array(key, length=length, dtype="u8")
    except bus.SharedMemoryLayoutError:
        return "N/A"
    if raw is None:
        return "N/A"
    try:
        if len(raw) == 0:
            return "N/A"
        data = bytes(raw).split(b"\x00", 1)[0]
        return _n_a(data.decode("utf-8", errors="replace"))
    except Exception:
        return "N/A"


MPRIS_TEXT_BYTES_KEY = "/mpris/text_bytes"


def mpris_text_width() -> int:
    """The bridge's published text segment width (metadata_buffer_bytes)."""
    width = bus.get_int(MPRIS_TEXT_BYTES_KEY, TEXT_BUFFER)
    return width if 16 <= width <= 65536 else TEXT_BUFFER


def read_mpris_text(key: str) -> str:
    """Read a whole /mpris/*_utf8 field; the bridge clips on a UTF-8 boundary."""
    width = mpris_text_width()
    text = read_utf8(key, width)
    if text == "N/A" and width != TEXT_BUFFER:
        # A stale segment narrower than the advertised width reads as N/A;
        # fall back to the historical 192-byte prefix rather than going blank.
        return read_utf8(key, TEXT_BUFFER)
    return text


DISCORD_STATUS_TEXT = {
    DISCORD_STATUS_DISABLED: "Disabled",
    DISCORD_STATUS_NOT_CONFIGURED: "No Discord app ID",
    DISCORD_STATUS_SDK_UNAVAILABLE: "Discord SDK unavailable",
    DISCORD_STATUS_NOT_LINKED: "Discord not linked",
    DISCORD_STATUS_AUTHORIZING: "Authorising Discord",
    DISCORD_STATUS_CONNECTING: "Connecting",
    DISCORD_STATUS_CONNECTED: "Connected",
    DISCORD_STATUS_RECONNECTING: "Reconnecting",
    DISCORD_STATUS_ERROR: "Error",
}


def discord_status_lines() -> List[str]:
    """Rich Presence status for the console status band (plain text, no markup).

    Returns nothing when the Rich Presence worker has never published, so the
    panel is unchanged on a Deck without it. Discord's own authorization page
    (overlay or browser) handles linking, so no code or link is shown here.
    Tokens are never published to Synapse, so they cannot appear here.
    """
    status = bus.get_int("/discord/status", -1)
    if status not in DISCORD_STATUS_TEXT:
        return []
    return [f"RP    : {DISCORD_STATUS_TEXT[status]}"]


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


def wrap_offsets(plain: str, width: int) -> List[int]:
    """Line-break offsets for ``plain`` at ``width`` terminal cells.

    Breaks after spaces where possible and inside a word only when the word is
    wider than the line (long names, CJK text without spaces). A break always
    falls after a run of spaces, so wrapped lines never start with one.
    """
    width = max(1, int(width))
    offsets: List[int] = []
    line_start = 0
    last_break = 0
    used = 0
    for index, char in enumerate(plain):
        char_width = max(0, cell_len(char))
        if char == " ":
            used += char_width
            last_break = index + 1
            continue
        if used + char_width > width and index > line_start:
            brk = last_break if last_break > line_start else index
            offsets.append(brk)
            line_start = brk
            last_break = brk
            used = cell_len(plain[brk:index])
        used += char_width
    return offsets


def wrap_words_cells(value: object, width: int, max_lines: int) -> List[str]:
    """Word-wrap one line of plain text, ending with an ellipsis if it is cut."""
    text = _n_a(value)
    if text == "N/A":
        return []
    text = text.replace("\r", "").replace("\n", " ").strip()
    width = max(2, int(width))
    bounds = [0, *wrap_offsets(text, width), len(text)]
    lines = [text[start:end].rstrip() for start, end in zip(bounds, bounds[1:])]
    if len(lines) > max(1, int(max_lines)):
        lines = lines[:max(1, int(max_lines))]
        lines[-1] = truncate_terminal_cells(lines[-1] + "…", width)
    return lines


def wrap_text(text: Text, width: int) -> List[Text]:
    """Word-wrap a styled Rich Text to ``width`` cells, keeping its styles."""
    lines = text.divide(wrap_offsets(text.plain, width)) if text.plain else [text]
    for line in lines:
        line.rstrip()
    return list(lines)


def tail_wrapped_lines(entries: Sequence[Text], width: int, rows: int) -> List[Text]:
    """The newest log entries that fit ``rows`` once wrapped, oldest first.

    Clipping from the bottom would hide the most recent lines, so the tail is
    chosen here. Only whole entries are shown, except that a single entry
    taller than the panel shows its end.
    """
    rows = max(1, int(rows))
    shown: List[Text] = []
    for entry in reversed(entries):
        wrapped = wrap_text(entry, width)
        if len(shown) + len(wrapped) > rows:
            if not shown:
                shown = wrapped[-rows:]
            break
        shown = wrapped + shown
    return shown


def pack_tokens(tokens: Sequence[Text], width: int, gap: Optional[int] = None) -> List[Text]:
    """Lay short status tokens out left to right, starting a new row as needed.

    Without an explicit ``gap``, use the widest spacing (three cells down to
    one) that still needs no more rows than the tightest packing.
    """
    if gap is None:
        candidates = [pack_tokens(tokens, width, spacing) for spacing in (3, 2, 1)]
        fewest = min(len(rows) for rows in candidates)
        return next(rows for rows in candidates if len(rows) == fewest)
    width = max(1, int(width))
    rows: List[Text] = []
    current: Optional[Text] = None
    for token in tokens:
        token = token.copy()
        token.truncate(width, overflow="ellipsis")
        if current is not None and current.cell_len + gap + token.cell_len <= width:
            current.append(" " * gap)
            current.append_text(token)
        else:
            current = token
            rows.append(current)
    return rows


def fit_line(text: Text, width: int) -> Text:
    """One header/panel row: never wraps, ends with an ellipsis when cut."""
    fitted = text.copy()
    fitted.truncate(max(1, int(width)), overflow="ellipsis")
    return fitted


def known(value: object) -> bool:
    """True for a real metadata value: not empty, zero, N/A or an unset clock."""
    return _n_a(value) not in ("N/A", "--:--")


def known_join(parts: Sequence[object], separator: str = " · ") -> str:
    """Join only the parts a source actually reported."""
    return separator.join(_n_a(part) for part in parts if known(part))


def track_label(number: object, count: object, prefix: str = "TRACK ") -> str:
    """Return "TRACK 3/12", "TRACK 3" or nothing, depending on what is known."""
    if not known(number):
        return ""
    return f"{prefix}{number}/{count}" if known(count) else f"{prefix}{number}"


def bit_depth_label(value: object) -> str:
    """Plex reports a bare bit depth such as "16"; show it as "16-bit"."""
    text = _n_a(value)
    return f"{text}-bit" if text.isdigit() else (text if known(text) else "")


def key_value_block(
    heading: Sequence[object],
    rows: Sequence[Tuple[str, object]],
    width: int,
    height: int,
) -> Text:
    """A heading of bold lines, then aligned label/value rows, cut to fit.

    Rows the source did not report are skipped, so on Bluetooth or MPRIS the
    panels show what is known instead of a column of N/A. Nothing wraps:
    each row is one line, cut with an ellipsis.
    """
    lines = [fit_line(Text(_n_a(value), style="bold"), width) for value in heading if known(value)]
    present = [(label, _n_a(value)) for label, value in rows if known(value)]
    label_w = max((len(label) for label, _ in present), default=0)
    for label, value in present:
        line = Text(f"{label:<{label_w}} ", style="dim")
        line.append(value)
        lines.append(fit_line(line, width))
    if not lines:
        lines.append(Text("Not reported by this source", style="dim"))
    return Text("\n").join(lines[:max(1, int(height))])


DEFAULT_TEXT_PALETTE: Tuple[Tuple[int, int, int], ...] = (
    (118, 83, 75),   # dominant
    (212, 122, 58),  # accent
    (75, 29, 25),    # dark
    (242, 201, 148),  # light
)


def _text_palette_roles(
    colours: Sequence[Tuple[int, int, int]],
) -> Tuple[Tuple[int, int, int], ...]:
    """Return a complete dominant/accent/dark/light palette for rich text."""
    roles = list(colours[:4])
    roles.extend(DEFAULT_TEXT_PALETTE[len(roles):])
    return tuple(roles)


def large_lyrics_fit(width: int, height: int) -> bool:
    """Whether a lyric band of this text area should use large lyrics at all."""
    lines = int(height) // 2
    chars_per_line = int(width) * 2 // dot_matrix.TYPICAL_ADVANCE
    return int(height) >= LARGE_LYRIC_MIN_ROWS and lines * chars_per_line >= LARGE_LYRIC_MIN_CHARS


def lyric_context_widget(
    context: Tuple[str, str, str],
    width: int,
    height: int,
    colours: Sequence[Tuple[int, int, int]] = DEFAULT_TEXT_PALETTE,
    large: bool = False,
) -> Text:
    """Render a controlled previous/current/next lyric stanza.

    The current timed line is visually dominant. Neighbours remain present as
    dim context, but each is constrained to one terminal row so CJK or long
    English lines cannot turn the Now Playing page into a scrolling paragraph.

    ``width`` and ``height`` are the panel's text area. With ``large``, the
    current line is drawn as dot-matrix capitals when the band has room for
    at least two large lines and the line fits; otherwise, and for text the
    dot font cannot draw (CJK, emoji), it falls back to normal text, which
    may take a second row. The block is centred vertically so a tall panel
    does not leave the lyrics stranded at the top.
    """
    previous, current, next_line = context
    width = max(4, int(width))
    height = max(1, int(height))
    dominant, _accent, _dark, light = _text_palette_roles(colours)

    current_rows: List[str] = []
    if large and large_lyrics_fit(width, height) and known(current):
        drawn = dot_matrix.render_lines(current, width, height // 2)
        if drawn:
            current_rows = [row for pair in drawn for row in pair]
    if not current_rows:
        current_rows = wrap_words_cells(current, width, 2 if height >= 4 else 1) or ["—"]
    rows: List[Tuple[str, str]] = [(line, f"bold {rgb_style(light)}") for line in current_rows]
    if height >= len(rows) + 2:
        rows.insert(0, (truncate_terminal_cells(previous, width), rgb_style(dominant)))
        rows.append((truncate_terminal_cells(next_line, width), rgb_style(dominant)))
    rows = rows[:height]

    rendered = Text("\n" * ((height - len(rows)) // 2))
    for index, (line, style) in enumerate(rows):
        rendered.append(line or " ", style=style)
        if index != len(rows) - 1:
            rendered.append("\n")
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


def palette_progress_bar(
    position: float,
    duration: float,
    width: int,
    colours: Sequence[Tuple[int, int, int]],
) -> Text:
    """Build a progress bar using the active palette's accent and dark roles."""
    width = max(6, int(width))
    fraction = clamp01(position / duration) if duration > 0 else 0.0
    filled = int(round(fraction * width))
    _dominant, accent, dark, _light = _text_palette_roles(colours)
    rendered = Text()
    rendered.append("█" * filled, style=f"bold {rgb_style(accent)}")
    rendered.append("░" * (width - filled), style=rgb_style(dark))
    return rendered


def state_name(state: int) -> str:
    """Return the state name result."""
    return {PLAY_PLAYING: "PLAY", PLAY_PAUSED: "PAUSE", PLAY_STOPPED: "STOP"}.get(int(state), "STOP")


def source_name(source: int) -> str:
    """Return the source name result."""
    if source == SOURCE_LOCAL:
        label = read_mpris_text("/mpris/source_utf8").upper()
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
        "title": read_mpris_text("/mpris/title_utf8"),
        "artist": read_mpris_text("/mpris/artist_utf8"),
        "album": read_mpris_text("/mpris/album_utf8"),
        "position": bus.get_int("/mpris/position_ms", 0) / 1000.0,
        "duration": bus.get_int("/mpris/duration_ms", 0) / 1000.0,
        "track_number": 0,
        "track_count": 0,
        "disc_number": 0,
        "year": "N/A",
        "genre": "N/A",
        "codec": read_mpris_text("/mpris/identity_utf8"),
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
        "stream_title": read_mpris_text("/mpris/player_utf8"),
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
    ScopeWidget { overflow: hidden hidden; background: #0b0305; }
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
    AlbumArtWidget { overflow: hidden hidden; background: #0d0506; }
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
        background: #090204;
        color: #e8c9a4;
        overflow: hidden hidden;
        layers: frame pages overlay;
    }
    .region {
        position: absolute;
        overflow: hidden hidden;
        padding: 0 1;
    }
    .panel {
        border: round #6e2d20;
        background: #120607;
        /* Panel headings live in the border, so they cost no text rows. */
        border-title-align: left;
        border-title-style: bold;
        border-title-color: #e8c9a4;
    }
    #global-time, #global-date { text-align: right; }
    #safe-frame {
        position: absolute;
        offset: 0 0;
        width: 80;
        height: 60;
        border: round #4b1d19;
        background: transparent;
        layer: frame;
    }
    .micro { color: #8f5f50; }
    .label { color: #bd7752; }
    #global-root {
        position: absolute;
        offset: 0 0;
        width: 80;
        height: 6;
        background: #170708;
        border-bottom: solid #87351f;
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
        self._ui_chrome_signature: Optional[Tuple[int, Tuple[int, int, int]]] = None
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
        self._console_status_lines = CONSOLE_STATUS_DEFAULT_LINES
        self.layout_plan = compute_layout(80, 30, int(cfg.get("safe_margin_cells", 1)), self.page)
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

        # Global overlay. Geometry comes from compute_layout().
        with Container(id="global-root"):
            for widget_id in HEADER_WIDGETS:
                yield Static(id=widget_id)

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
            yield Static(id="console-status", classes="region panel")
            yield Static(id="console-logging", classes="region panel")

    def on_mount(self) -> None:
        """Handle the on mount lifecycle step."""
        for widget_id, title in PANEL_TITLES.items():
            self.query_one(f"#{widget_id}").border_title = title
        self._set_page("now")
        interval = 1.0 / max(2, int(self.cfg.get("update_hz", 20)))
        self.set_interval(interval, self.refresh_frame)
        self.refresh_frame()

    @staticmethod
    def _scaled_colour(rgb: Tuple[int, int, int], amount: float) -> str:
        """Return a brightness-scaled CSS colour without changing its hue."""
        amount = max(0.0, float(amount))
        channels = tuple(max(0, min(255, int(round(channel * amount)))) for channel in rgb)
        return f"rgb({channels[0]},{channels[1]},{channels[2]})"

    @staticmethod
    def _mix_colour(
        first: Tuple[int, int, int],
        second: Tuple[int, int, int],
        second_weight: float,
    ) -> Tuple[int, int, int]:
        """Blend two RGB colours while retaining exact, predictable output."""
        weight = clamp01(second_weight)
        return tuple(
            int(round(left * (1.0 - weight) + right * weight))
            for left, right in zip(first, second)
        )

    @staticmethod
    def _palette_accent(colours: Sequence[Tuple[int, int, int]]) -> Tuple[int, int, int]:
        """Choose and lift the palette colour that best expresses its hue."""
        candidates = list(colours) or [(180, 76, 34)]
        accent = max(candidates, key=lambda rgb: (max(rgb) - min(rgb)) + max(rgb) * 0.12)
        if max(accent) - min(accent) < 16:
            return (180, 76, 34)
        peak = max(accent)
        if peak < 150:
            scale = 150.0 / max(1, peak)
            accent = tuple(min(255, int(round(channel * scale))) for channel in accent)
        return accent

    def _apply_ui_brightness(self) -> None:
        """Let the brightness control gently drive the instrument chrome."""
        control = clamp01(bus.get_float("/io/in/control/brightness", 0.7))
        floor = clamp01(float(self.cfg.get("ui_brightness_floor", 0.52)))
        ceiling = max(floor, float(self.cfg.get("ui_brightness_ceiling", 1.0)))
        level = floor + (ceiling - floor) * control
        accent = self._palette_accent(self.live_colours)
        signature = (int(round(level * 64)), accent)
        if signature == self._ui_chrome_signature:
            return
        self._ui_chrome_signature = signature

        # Aurora supplies the hue. The fixed ember colours now describe only
        # the material: subtly tinted soot surfaces and warmer readable type.
        # Backgrounds move less than text and metalwork so they never collapse.
        surface_level = 0.72 + level * 0.28
        screen_level = 0.78 + level * 0.22
        screen_rgb = self._mix_colour((7, 3, 4), accent, 0.07)
        panel_rgb = self._mix_colour((13, 7, 7), accent, 0.07)
        header_rgb = self._mix_colour((16, 8, 8), accent, 0.10)
        border_rgb = self._mix_colour((125, 57, 32), accent, 0.44)
        text_rgb = self._mix_colour((236, 210, 174), accent, 0.04)

        self.screen.styles.background = self._scaled_colour(screen_rgb, screen_level)
        self.screen.styles.color = self._scaled_colour(text_rgb, level)

        panel_colour = self._scaled_colour(panel_rgb, surface_level)
        border_colour = self._scaled_colour(border_rgb, level)
        title_colour = self._scaled_colour(self._mix_colour(text_rgb, accent, 0.35), level)
        for panel in self.query(".panel"):
            panel.styles.background = panel_colour
            panel.styles.border = ("round", border_colour)
            panel.styles.border_title_color = title_colour

        safe_frame = self.query_one("#safe-frame")
        safe_frame.styles.border = (
            "round", self._scaled_colour(self._mix_colour((65, 31, 25), accent, 0.28), level)
        )
        global_root = self.query_one("#global-root")
        global_root.styles.background = self._scaled_colour(header_rgb, surface_level)
        global_root.styles.border_bottom = ("solid", border_colour)

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
        """Fit the header and pages into the live terminal grid.

        Rectangles come from compute_layout(), a pure function, so the same
        rules can be checked in tests for every likely font size.
        """
        plan = compute_layout(
            int(self.size.width),
            int(self.size.height),
            int(self.cfg.get("safe_margin_cells", 1)),
            self.page,
            self._console_status_lines,
        )
        self.layout_plan = plan

        frame = self.query_one("#safe-frame")
        frame.styles.offset = (0, 0)
        frame.styles.width = plan.viewport_w
        frame.styles.height = plan.viewport_h

        # Containers must match their runtime viewport before child placement.
        for container_id in ("global-root", *(f"page-{page}" for page in PAGES)):
            container = self.query_one(f"#{container_id}")
            container.styles.offset = (plan.safe, plan.safe)
            container.styles.width = plan.width
            container.styles.height = HEADER_H if container_id == "global-root" else plan.height

        for widget_id, region in plan.regions.items():
            self._place_optional(widget_id, region)

        # Sizes changed, so every cached panel must be drawn again.
        self._text_cache.clear()
        self._last_global_at = self._last_now_at = self._last_scope_at = 0.0
        self._last_info_at = self._last_console_at = 0.0

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
        """Show one page. The header differs on Now Playing, so lay out again;
        that also clears the refresh timers, so the page draws immediately
        instead of showing what it held when it was last visible."""
        self.page = page
        for candidate in PAGES:
            self.query_one(f"#page-{candidate}").styles.display = "block" if candidate == page else "none"
        self._apply_adaptive_layout()

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

    def _heartbeat_tokens(self) -> List[Text]:
        """One token per service group; an age appears only once it is late."""
        now_ms = bus.monotonic_ms()
        tokens: List[Text] = []
        for label, names in HEARTBEAT_SERVICES:
            ages = []
            for name in names:
                stamp = bus.get_int(f"/proc/{name}/heartbeat_ms", 0)
                ages.append(bus.elapsed_ms(now_ms, stamp) / 1000.0 if stamp else math.inf)
            age = max(ages)
            token = Text(f"{label} ", style="dim")
            if age < 3.0:
                token.append("●", style=HEALTH_STYLES["ok"])
            elif age < 10.0:
                token.append(f"◐ {age:.0f}s", style=HEALTH_STYLES["late"])
            elif math.isfinite(age):
                token.append(f"× {age:.0f}s", style=HEALTH_STYLES["dead"])
            else:
                token.append("×", style=HEALTH_STYLES["dead"])
            tokens.append(token)
        return tokens

    def _system_tokens(self) -> List[Text]:
        """Host diagnostics. Long Wi-Fi/IP values scroll inside a fixed width."""
        s = self.system
        now = time.monotonic()
        step_s = float(self.cfg.get("diagnostics_scroll_step_s", 0.35))

        def token(label: str, value: object, scroll_w: int = 0) -> Text:
            text = _n_a(value)
            if scroll_w:
                text = marquee_terminal_cells(text, scroll_w, now=now, step_s=step_s)
            rendered = Text(f"{label} ", style="dim")
            rendered.append(text)
            return rendered

        tokens = [
            token("CPU", f"{s.get('cpu', 'N/A')}%"),
            token("RAM", s.get("ram")),
            token("TEMP", s.get("temp")),
            token("WiFi", s.get("wifi"), 18),
            token("IP", s.get("ip"), 15),
        ]
        for line in discord_status_lines():
            _label, _, value = line.partition(": ")
            tokens.append(token("RP", value.strip(), 20))
        return tokens

    def _control_tokens(self) -> List[Text]:
        """Show Pawprint's mapped semantic controls, not raw ADC voltages."""
        tokens = []
        for label, name in (("BRIGHT", "brightness"), ("GAIN", "gain"), ("COLOUR", "colour"), ("SAT", "saturation")):
            token = Text(f"{label} ", style="dim")
            token.append(f"{clamp01(bus.get_float(f'/io/in/control/{name}', 0.0)):.2f}")
            tokens.append(token)
        return tokens

    def refresh_frame(self) -> None:
        """Refresh frame."""
        now = time.monotonic()
        self._update_page_from_selectors()
        self._refresh_live_state(now)
        self._apply_ui_brightness()

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
        """Header: two single-line rows that never wrap at any grid size."""
        plan = self.layout_plan
        now = datetime.now()
        _dominant, _accent, _dark, light = _text_palette_roles(colours)

        # Full blocks survive small terminal grids and low-contrast themes
        # better than single-pixel-looking vertical strokes. Two per row.
        ticks = Text()
        for index, colour in enumerate(colours[:4]):
            if index == 2:
                ticks.append("\n")
            ticks.append("▌", style=rgb_style(colour))
        self.query_one("#global-colours", Static).update(ticks)

        state_w = plan.span("global-state") - 1
        state = fit_line(Text(state_name(play_state), style="bold"), state_w)
        state.append("\n")
        state.append_text(fit_line(Text(source_name(source)), state_w))
        self.query_one("#global-state", Static).update(state)

        if self.page == "now":
            context = known_join([
                track_label(meta.get("track_number"), meta.get("track_count")),
                f"DISC {_n_a(meta.get('disc_number'))}" if known(meta.get("disc_number")) else "",
                meta.get("year"),
                meta.get("genre"),
            ])
            audio = known_join([
                meta.get("codec"),
                meta.get("sample_rate"),
                bit_depth_label(meta.get("bit_depth")),
                meta.get("bitrate"),
            ])
            self.query_one("#global-artist", Static).update(
                fit_line(Text(context or source_name(source)), plan.span("global-artist")))
            self.query_one("#global-album", Static).update(
                fit_line(Text(audio, style="dim"), plan.span("global-album")))
        else:
            self.query_one("#global-artist", Static).update(
                fit_line(Text(_n_a(meta["artist"]), style="bold"), plan.span("global-artist")))
            self.query_one("#global-album", Static).update(
                fit_line(Text(_n_a(meta["album"])), plan.span("global-album")))
            title_w = plan.span("global-title")
            self.query_one("#global-title", Static).update(
                fit_line(Text(_n_a(meta["title"]), style=f"bold {rgb_style(light)}"), title_w))
            clock = f" {fmt_time(meta['position'])}/{fmt_time(meta['duration'])}"
            bar_w = title_w - cell_len(clock)
            if bar_w >= 6:
                progress = palette_progress_bar(meta["position"], meta["duration"], bar_w, colours)
                progress.append(clock, style=rgb_style(light))
            else:
                progress = palette_progress_bar(meta["position"], meta["duration"], title_w, colours)
            self.query_one("#global-progress", Static).update(progress)

        self.query_one("#global-time", Static).update(Text(now.strftime("%H:%M"), style="bold"))
        self.query_one("#global-date", Static).update(now.strftime("%d %b"))

    def _update_now(
        self,
        meta: dict,
        source: int,
        lyric_context: Tuple[str, str, str],
        colours: Sequence[Tuple[int, int, int]],
    ) -> None:
        """Title, artist, album and progress, centred beside the cover."""
        plan = self.layout_plan
        width, height = plan.inner("now-metadata")
        _dominant, _accent, _dark, light = _text_palette_roles(colours)

        title_rows = wrap_words_cells(meta["title"], width, 2 if height >= 6 else 1) or ["N/A"]
        lines: List[Text] = [Text(row, style=f"bold {rgb_style(light)}") for row in title_rows]
        if height >= len(lines) + 4:
            lines.append(fit_line(Text(_n_a(meta["artist"])), width))
        if height >= len(lines) + 4:
            lines.append(fit_line(Text(_n_a(meta["album"])), width))
        if height >= len(lines) + 3:
            lines.append(Text(""))
        lines.append(palette_progress_bar(meta["position"], meta["duration"], width, colours))
        elapsed = f"{fmt_time(meta['position'])} / {fmt_time(meta['duration'])}"
        remaining = ""
        if meta["duration"] and meta["duration"] > meta["position"] >= 0:
            remaining = f"-{fmt_time(meta['duration'] - meta['position'])}"
        times = Text(elapsed)
        if remaining and cell_len(elapsed) + 1 + cell_len(remaining) <= width:
            times.append(" " * (width - cell_len(elapsed) - cell_len(remaining)))
            times.append(remaining, style="dim")
        lines.append(times)
        lines = lines[-height:]
        body = Text("\n" * ((height - len(lines)) // 2))
        body.append_text(Text("\n").join(lines))
        self.query_one("#now-metadata", Static).update(body)

        self.query_one("#now-art", AlbumArtWidget).set_art_context(self._album_art, colours, self._album_art_seq)
        self._update_lyrics("now-lyrics", lyric_context, colours)

    def _update_lyrics(
        self,
        widget_id: str,
        lyric_context: Tuple[str, str, str],
        colours: Sequence[Tuple[int, int, int]],
    ) -> None:
        """Draw a lyric band only when its words, colours or size change."""
        width, height = self.layout_plan.inner(widget_id)
        lyric_key = f"{lyric_context!r}|{tuple(colours)!r}|{width}|{height}"
        large = bool(self.cfg.get("large_lyrics", True))
        self._update_static_cached(
            widget_id, lyric_context_widget(lyric_context, width, height, colours, large), lyric_key
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
        self._update_lyrics("scope-lyrics", lyric_context, colours)

    def _update_info(self, meta: dict) -> None:
        """Four labelled lists. Fields the source does not report are left out,
        and each list is ordered so the most useful rows survive a small grid."""
        panels = {
            "info-identity": (
                [meta["title"], meta["artist"], meta["album"]],
                [
                    ("YEAR", meta["year"]),
                    ("TRACK", track_label(meta["track_number"], meta["track_count"], prefix="")),
                    ("DISC", meta["disc_number"]),
                ],
            ),
            "info-character": ([], [
                ("GENRE", meta["genre"]),
                ("MOOD", meta["mood"]),
                ("BPM", meta["bpm"]),
                ("LENGTH", fmt_time(meta["duration"]) if meta["duration"] else ""),
                ("RATING", meta["rating"]),
                ("POP", meta["popularity"]),
            ]),
            "info-provenance": ([], [
                ("ADDED", meta["added"]),
                ("PLAYED", meta["last_played"]),
                ("PLAYS", meta["play_count"]),
                ("FILE", meta["file_name"]),
                ("SIZE", meta["file_size"]),
            ]),
            "info-format": ([], [
                ("CODEC", meta["codec"]),
                ("TYPE", meta["container"]),
                ("RATE", known_join([meta["sample_rate"], bit_depth_label(meta["bit_depth"])])),
                ("BITRATE", meta["bitrate"]),
                ("AUDIO", known_join([
                    f"{meta['channels']} ch" if known(meta["channels"]) else "",
                    meta["channel_layout"],
                ])),
                ("LUFS", meta["loudness"]),
                ("LRA", meta["lra"]),
                ("GAIN", meta["track_gain"]),
                ("PEAK", meta["track_peak"]),
                ("ALB GAIN", meta["album_gain"]),
                ("ALB PEAK", meta["album_peak"]),
                ("ALB RANGE", meta["album_range"]),
                ("STREAM", meta["stream_title"]),
            ]),
        }
        for widget_id, (heading, rows) in panels.items():
            width, height = self.layout_plan.inner(widget_id)
            text = key_value_block(heading, rows, width, height)
            self._update_static_cached(widget_id, text, f"{text.plain}|{width}|{height}")

    def _update_console(self) -> None:
        """Status band on top, then as much of the newest log as fits."""
        status_w, _ = self.layout_plan.inner("console-status")
        status_rows: List[Text] = []
        for group in (self._heartbeat_tokens(), self._system_tokens(), self._control_tokens()):
            status_rows.extend(pack_tokens(group, status_w))
        if len(status_rows) != self._console_status_lines:
            # A late service adds its age to the heartbeat row and can push
            # the band onto another line; give that line to the band.
            self._console_status_lines = len(status_rows)
            self._apply_adaptive_layout()
        status_text = Text("\n").join(status_rows)
        self._update_static_cached(
            "console-status", status_text, repr([(row.plain, row.spans) for row in status_rows])
        )

        log_widget = self.query_one("#console-logging", Static)
        title = f"FOUNDRY LOG · {self.log_tail.source_name.upper().replace('-', ' ')}"
        if log_widget.border_title != title:
            log_widget.border_title = title
        log_w, log_rows = self.layout_plan.inner("console-logging")
        visible = tail_wrapped_lines(self.logs, log_w, log_rows)
        log_key = f"{log_w}x{log_rows}|" + repr([(line.plain, line.spans) for line in visible])
        # Bottom-aligned, so the newest line always sits on the panel's edge.
        log_text = Text("\n" * (log_rows - len(visible)))
        log_text.append_text(Text("\n").join(visible))
        self._update_static_cached("console-logging", log_text, log_key)


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
