#!/usr/bin/env python3
"""
aurora.py

Ember Deck colour-policy owner.

Aurora produces the four-colour palette used by Foxfire. It chooses either:
  * dynamic Plex artwork colours, but only while Plex is the active source; or
  * a manual static hue palette for Bluetooth, local/system, and fallback use.

Pawprint semantic controls:
  /io/in/control/brightness  master strip brightness, 0..1
  /io/in/control/colour      static hue wheel, 0..1
  /io/in/control/saturation  grayscale 0, neutral 0.5, vivid 1
"""

from __future__ import annotations

import colorsys
import os
import signal
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import yaml

import synapse as bus
from whisper_daemon import log_heartbeat, log_info

MEDIA_SOURCE_PLEX = 1
MEDIA_SOURCE_LOCAL = 3
CONTROL_BRIGHTNESS = "/io/in/control/brightness"
CONTROL_COLOUR = "/io/in/control/colour"
CONTROL_SATURATION = "/io/in/control/saturation"
KEY_MEDIA_ACTIVE_SOURCE = "/media/active_source"

# LED palette: brightness-scaled, consumed by Foxfire / Will-o-Wisp.
THEME_KEYS = (
    "/theme/ultra/tl_rgba",
    "/theme/ultra/tr_rgba",
    "/theme/ultra/bl_rgba",
    "/theme/ultra/br_rgba",
)

# Display palette: same colour policy, but deliberately independent of the
# physical RGBW brightness control. The panel must not go black because the
# owner has dimmed or switched off the LED strip.
UI_THEME_KEYS = (
    "/theme/ui/tl_rgba",
    "/theme/ui/tr_rgba",
    "/theme/ui/bl_rgba",
    "/theme/ui/br_rgba",
)

KEY_ALBUM_ULTRA_VALID = "/album/ultra/valid"
KEY_THEME_DYNAMIC = "/theme/dynamic_active"
KEY_THEME_PALETTE_SEQ = "/theme/palette_seq"

DEFAULTS = dict(
    proc_name="aurora",
    dynamicColour=True,
    scheme="analogous",
    static_base_saturation=0.85,
    # The display is tinted instrument glass, not another LED emitter. At a
    # vivid 0.9 control setting this makes the UI resemble the LED palette at
    # roughly 0.4, while retaining the same hue and palette relationships.
    ui_saturation_scale=0.45,
    fallback_hue=0.0,
    update_hz=10.0,
    album_timeout_s=2.0,
    generated_palette_enabled=True,
)


def load_cfg() -> dict:
    """Load defaults and merge any component-specific YAML configuration."""
    path = os.environ.get("CONFIG_PATH")
    loaded = {}
    if path and Path(path).is_file():
        with Path(path).open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}

    cfg = DEFAULTS.copy()
    cfg.update(loaded)

    # Keep compatibility with the earlier config while retiring its confusing
    # value/alpha controls. Brightness now has one owner: Pawprint -> Aurora.
    if "static_base_saturation" not in loaded and "saturation" in loaded:
        cfg["static_base_saturation"] = loaded["saturation"]
    if "fallback_hue" not in loaded and "base_hue" in loaded:
        try:
            cfg["fallback_hue"] = (float(loaded["base_hue"]) % 360.0) / 360.0
        except (TypeError, ValueError):
            pass
    return cfg


def clamp01(value: float) -> float:
    """Clamp a numeric value to the inclusive range from zero to one."""
    return max(0.0, min(1.0, float(value)))


def finite01(value: object, fallback: float) -> float:
    """Return a finite value clamped between zero and one."""
    try:
        return clamp01(float(value))
    except (TypeError, ValueError):
        return clamp01(fallback)


def hsv_rgb(hue_degrees: float, saturation: float, value: float) -> Tuple[float, float, float]:
    """Return the hsv rgb result."""
    return colorsys.hsv_to_rgb((float(hue_degrees) % 360.0) / 360.0, clamp01(saturation), clamp01(value))


def synthesize_palette(hue_degrees: float, saturation: float, scheme: str) -> List[Tuple[float, float, float]]:
    """Return the synthesize palette result."""
    saturation = clamp01(saturation)
    if scheme == "shades":
        hues, values = [hue_degrees] * 4, [0.30, 0.55, 0.80, 1.00]
    elif scheme == "split_comp":
        hues, values = [hue_degrees, hue_degrees + 150, hue_degrees + 210, hue_degrees], [0.70, 0.85, 0.90, 1.00]
    elif scheme == "triadic":
        hues, values = [hue_degrees, hue_degrees + 120, hue_degrees + 240, hue_degrees], [0.75, 0.85, 0.90, 1.00]
    elif scheme == "tetradic":
        hues, values = [hue_degrees, hue_degrees + 90, hue_degrees + 180, hue_degrees + 270], [0.75, 0.85, 0.92, 1.00]
    else:
        hues, values = [hue_degrees - 30, hue_degrees - 10, hue_degrees + 10, hue_degrees + 30], [0.80, 0.85, 0.92, 1.00]
    return [hsv_rgb(h, saturation, value) for h, value in zip(hues, values)]


def read_album_rgb() -> Optional[List[Tuple[float, float, float]]]:
    # Newer Minstrel builds explicitly clear validity when a track has no
    # usable UltraBlur palette. Treat a missing key as legacy/unknown so old
    # running instances remain compatible during staged upgrades.
    """Read album rgb."""
    if bus.get_int(KEY_ALBUM_ULTRA_VALID, -1) == 0:
        return None

    palette: List[Tuple[float, float, float]] = []
    for key in (
        "/album/ultra/tl_rgba",
        "/album/ultra/tr_rgba",
        "/album/ultra/bl_rgba",
        "/album/ultra/br_rgba",
    ):
        values = bus.try_get_array(key, length=4, dtype="f32")
        if values is None or len(values) < 3:
            return None
        try:
            palette.append(tuple(clamp01(float(values[i])) for i in range(3)))
        except (TypeError, ValueError):
            return None
    return palette


ART_BUS_WIDTH = 48
ART_BUS_HEIGHT = 48
ART_BUS_BYTES = ART_BUS_WIDTH * ART_BUS_HEIGHT * 3


def _average_region_rgb(
    pixels: Sequence[int],
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> Optional[Tuple[float, float, float]]:
    """Return the average region rgb result."""
    r_total = g_total = b_total = count = 0
    x0 = max(0, min(width, x0))
    x1 = max(0, min(width, x1))
    y0 = max(0, min(height, y0))
    y1 = max(0, min(height, y1))
    for y in range(y0, y1):
        row = y * ART_BUS_WIDTH * 3
        for x in range(x0, x1):
            idx = row + x * 3
            r, g, b = int(pixels[idx]), int(pixels[idx + 1]), int(pixels[idx + 2])
            # Ignore completely empty canvas padding. Black album covers still
            # contribute through their non-zero neighbours; if everything is
            # black we fall back to the static palette.
            if r == 0 and g == 0 and b == 0:
                continue
            r_total += r
            g_total += g
            b_total += b
            count += 1
    if count <= 0:
        return None
    return (r_total / (count * 255.0), g_total / (count * 255.0), b_total / (count * 255.0))


def _tidy_generated_colour(rgb: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """Return the tidy generated colour result."""
    hue, sat, val = colorsys.rgb_to_hsv(clamp01(rgb[0]), clamp01(rgb[1]), clamp01(rgb[2]))
    # Album-art averages are often beige sludge. Keep the hue, but lift it into
    # a usable display/LED colour. The world has enough beige rectangles.
    sat = max(0.35, min(1.0, sat * 1.35))
    val = max(0.22, min(0.92, val))
    return colorsys.hsv_to_rgb(hue, sat, val)


def read_generated_art_palette(prefix: str) -> Optional[List[Tuple[float, float, float]]]:
    """Read generated art palette."""
    if bus.get_int(f"{prefix}/art_valid", 0) != 1:
        return None
    width = bus.get_int(f"{prefix}/art_width", 0)
    height = bus.get_int(f"{prefix}/art_height", 0)
    if not (2 <= width <= ART_BUS_WIDTH and 2 <= height <= ART_BUS_HEIGHT):
        return None
    pixels = bus.try_get_array(f"{prefix}/art_rgb", length=ART_BUS_BYTES, dtype="u8")
    if pixels is None or len(pixels) < ART_BUS_BYTES:
        return None

    mid_x = max(1, width // 2)
    mid_y = max(1, height // 2)
    regions = (
        (0, 0, mid_x, mid_y),
        (mid_x, 0, width, mid_y),
        (0, mid_y, mid_x, height),
        (mid_x, mid_y, width, height),
    )
    palette = []
    for region in regions:
        colour = _average_region_rgb(pixels, width, height, *region)
        if colour is None:
            return None
        palette.append(_tidy_generated_colour(colour))
    return palette


def read_dynamic_palette_for_source(active_source: int) -> Optional[List[Tuple[float, float, float]]]:
    # Prefer Plex UltraBlur when Plex provides it, because it is the server's
    # purpose-built palette. If it is missing, generate our own from the actual
    # cover art. Generic local MPRIS sources only have artwork, so generated
    # palette is their normal dynamic path.
    """Read dynamic palette for source."""
    if active_source == MEDIA_SOURCE_PLEX:
        return read_album_rgb() or read_generated_art_palette("/plex")
    if active_source == MEDIA_SOURCE_LOCAL:
        return read_generated_art_palette("/mpris")
    return None


def saturation_factor(control: float) -> float:
    # 0.5 preserves the palette's native saturation.
    """Return the saturation factor result."""
    return 2.0 * clamp01(control)


def transform_dynamic(
    source: Sequence[Tuple[float, float, float]],
    saturation_control: float,
    brightness_control: float,
    saturation_scale: float = 1.0,
) -> List[Tuple[float, float, float]]:
    """Return the transform dynamic result."""
    factor = saturation_factor(saturation_control) * max(0.0, float(saturation_scale))
    brightness = clamp01(brightness_control)
    transformed = []
    for red, green, blue in source:
        hue, sat, value = colorsys.rgb_to_hsv(clamp01(red), clamp01(green), clamp01(blue))
        red, green, blue = colorsys.hsv_to_rgb(hue, clamp01(sat * factor), value)
        transformed.append((red * brightness, green * brightness, blue * brightness))
    return transformed


def static_palette(
    hue_control: float,
    saturation_control: float,
    brightness_control: float,
    cfg: dict,
    saturation_scale: float = 1.0,
) -> List[Tuple[float, float, float]]:
    """Return the static palette result."""
    base_sat = finite01(cfg.get("static_base_saturation"), 0.85)
    saturation = clamp01(
        base_sat * saturation_factor(saturation_control) * max(0.0, float(saturation_scale))
    )
    rgb = synthesize_palette(clamp01(hue_control) * 360.0, saturation, str(cfg.get("scheme", "analogous")))
    brightness = clamp01(brightness_control)
    return [(r * brightness, g * brightness, b * brightness) for r, g, b in rgb]


def publish_theme(
    led_palette: Sequence[Tuple[float, float, float]],
    display_palette: Sequence[Tuple[float, float, float]],
    dynamic_active: bool,
) -> None:
    """Publish two deliberate palette lanes.

    ``/theme/ultra/*`` remains the brightness-scaled LED palette. The UI reads
    ``/theme/ui/*`` so panel artwork, scope strokes and palette ticks stay
    visible even when the physical RGBW brightness is set very low or zero.
    """
    if len(led_palette) != 4 or len(display_palette) != 4:
        raise ValueError("theme palettes must each contain four colours")

    for key, (red, green, blue) in zip(THEME_KEYS, led_palette):
        bus.set_array(key, [clamp01(red), clamp01(green), clamp01(blue), 1.0], "f32")

    for key, (red, green, blue) in zip(UI_THEME_KEYS, display_palette):
        bus.set_array(key, [clamp01(red), clamp01(green), clamp01(blue), 1.0], "f32")

    bus.set_int(KEY_THEME_DYNAMIC, int(bool(dynamic_active)))
    bus.set_int(KEY_THEME_PALETTE_SEQ, bus.get_int(KEY_THEME_PALETTE_SEQ, 0) + 1)


def heartbeat(proc_name: str) -> None:
    """Publish the component heartbeat and diagnostic event."""
    now_ms = int(time.monotonic() * 1000)
    bus.set_int(f"/proc/{proc_name}/heartbeat_ms", now_ms)
    key = f"/proc/{proc_name}/hb_seq"
    bus.set_int(key, bus.get_int(key, 0) + 1)
    log_heartbeat(proc_name)


def main() -> None:
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    proc_name = str(cfg.get("proc_name", "aurora"))
    period = 1.0 / max(1.0, float(cfg.get("update_hz", 10.0)))
    album_timeout_s = max(0.0, float(cfg.get("album_timeout_s", 2.0)))

    log_info(proc_name, "started")
    stopping = False

    def stop_handler(signum, frame):
        """Mark the component for an orderly shutdown."""
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    last_album_raw: Optional[List[Tuple[float, float, float]]] = None
    last_album_seen_at: Optional[float] = None
    next_tick = time.monotonic()
    last_hb = 0.0

    try:
        while not stopping:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
                now = time.monotonic()
            next_tick = max(next_tick + period, now)

            brightness = finite01(bus.get_float(CONTROL_BRIGHTNESS, 1.0), 1.0)
            hue = finite01(bus.get_float(CONTROL_COLOUR, cfg.get("fallback_hue", 0.0)), cfg.get("fallback_hue", 0.0))
            saturation = finite01(bus.get_float(CONTROL_SATURATION, 0.5), 0.5)
            active_source = bus.get_int(KEY_MEDIA_ACTIVE_SOURCE, 0)

            wants_dynamic = (
                bool(cfg.get("dynamicColour", True))
                and bool(cfg.get("generated_palette_enabled", True))
                and active_source in (MEDIA_SOURCE_PLEX, MEDIA_SOURCE_LOCAL)
            )
            live_album = read_dynamic_palette_for_source(active_source) if wants_dynamic else None
            if live_album is not None:
                last_album_raw = live_album
                last_album_seen_at = now

            cached_album_is_fresh = (
                wants_dynamic
                and last_album_raw is not None
                and last_album_seen_at is not None
                and now - last_album_seen_at <= album_timeout_s
            )

            if cached_album_is_fresh:
                ui_saturation_scale = max(0.0, float(cfg.get("ui_saturation_scale", 0.45)))
                publish_theme(
                    transform_dynamic(last_album_raw, saturation, brightness),
                    transform_dynamic(last_album_raw, saturation, 1.0, ui_saturation_scale),
                    True,
                )
            else:
                ui_saturation_scale = max(0.0, float(cfg.get("ui_saturation_scale", 0.45)))
                publish_theme(
                    static_palette(hue, saturation, brightness, cfg),
                    static_palette(hue, saturation, 1.0, cfg, ui_saturation_scale),
                    False,
                )

            if now - last_hb >= 1.0:
                heartbeat(proc_name)
                last_hb = now
    finally:
        bus.close_all()
        log_info(proc_name, "stopped")


if __name__ == "__main__":
    main()
