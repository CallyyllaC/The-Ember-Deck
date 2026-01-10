"""
Aurora.py
Color manager for EmberEngine.

- If dynamicColour: true
    Forward album-art Ultra Blur colors from /album/ultra/* to /theme/ultra/*
    If missing for album_timeout_s, fall back to a synthetic palette.
- If dynamicColour: false
    Generate a 4-color palette from base_hue + scheme and publish to /theme/ultra/*

Schemes available:
  shades | analogous | split_comp | triadic | tetradic
"""

import os
import time
import yaml
import colorsys
from pathlib import Path
import synapse as bus

# ---------- defaults ----------
DEFAULTS = dict(
    dynamicColour=True,     # true = use album-art colours
    base_hue=25.0,          # used if dynamicColour = false
    saturation=0.85,
    value=0.95,
    alpha=1.0,
    scheme="analogous",     # shades | analogous | split_comp | triadic | tetradic
    update_hz=2.0,
    album_timeout_s=2.0,    # fallback to palette after this time without album colours
)


def load_cfg():
    """Load YAML configuration."""
    p = os.environ.get("CONFIG_PATH")
    cfg = {}
    if p and Path(p).exists():
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    out = DEFAULTS.copy()
    out.update(cfg)
    return out


def clamp01(x):
    """Clamp to 0–1 range."""
    return max(0.0, min(1.0, float(x)))


def hsv_to_rgb01(h_deg, s, v):
    """Convert HSV (degrees, 0–1, 0–1) to RGB 0–1."""
    h = (h_deg % 360.0) / 360.0
    r, g, b = colorsys.hsv_to_rgb(h, clamp01(s), clamp01(v))
    return (r, g, b)


def synthesize_palette(h, s, v, scheme):
    """Return list of 4 RGB colours (0–1 floats) using the chosen colour scheme."""
    if scheme == "shades":
        vals = [0.30*v, 0.55*v, 0.80*v, 1.00*v]
        cols = [hsv_to_rgb01(h, s, vv) for vv in vals]

    elif scheme == "analogous":
        hs   = [h-30, h-10, h+10, h+30]
        vals = [0.80*v, 0.85*v, 0.92*v, 1.00*v]
        cols = [hsv_to_rgb01(hh, s, vv) for hh, vv in zip(hs, vals)]

    elif scheme == "split_comp":
        hs   = [h, h+150, h+210, h]
        vals = [0.70*v, 0.85*v, 0.90*v, 1.00*v]
        cols = [hsv_to_rgb01(hh, s, vv) for hh, vv in zip(hs, vals)]

    elif scheme == "triadic":
        hs   = [h, h+120, h+240, h]
        vals = [0.75*v, 0.85*v, 0.90*v, 1.00*v]
        cols = [hsv_to_rgb01(hh, s, vv) for hh, vv in zip(hs, vals)]

    elif scheme == "tetradic":
        hs   = [h, h+90, h+180, h+270]
        vals = [0.75*v, 0.85*v, 0.92*v, 1.00*v]
        cols = [hsv_to_rgb01(hh, s, vv) for hh, vv in zip(hs, vals)]

    else:
        # default to analogous if unknown
        hs   = [h-30, h-10, h+10, h+30]
        vals = [0.80*v, 0.85*v, 0.92*v, 1.00*v]
        cols = [hsv_to_rgb01(hh, s, vv) for hh, vv in zip(hs, vals)]

    return cols  # order preserved; Foxfire sorts by luma later


def read_album_rgba():
    """
    Attempt to read 4 RGBA colours from /album/ultra/* keys.
    Returns list of 4 lists [r,g,b,a] or None if incomplete.
    """
    keys = [
        "/album/ultra/tl_rgba",
        "/album/ultra/tr_rgba",
        "/album/ultra/bl_rgba",
        "/album/ultra/br_rgba",
    ]
    out = []
    for k in keys:
        arr = bus.get_array(k, length=4, dtype="f32")
        if not arr or len(arr) < 4:
            return None
        out.append([float(arr[0]), float(arr[1]), float(arr[2]), float(arr[3])])
    return out


def publish_theme_rgba(rgba4, alpha=1.0):
    """Publish 4 RGBA colours to /theme/ultra/* keys."""
    keys = [
        "/theme/ultra/tl_rgba",
        "/theme/ultra/tr_rgba",
        "/theme/ultra/bl_rgba",
        "/theme/ultra/br_rgba",
    ]
    for k, (r, g, b, a) in zip(keys, rgba4):
        bus.set_array(k, [float(r), float(g), float(b), clamp01(alpha if a is None else a)], "f32")


def main():
    cfg = load_cfg()
    dt = 1.0 / max(1.0, float(cfg["update_hz"]))
    miss_started = None

    try:
        while True:
            if cfg["dynamicColour"]:
                rgba = read_album_rgba()
                if rgba:
                    # Album-art colours found: forward them.
                    publish_theme_rgba(rgba, cfg["alpha"])
                    miss_started = None
                else:
                    # Missing album colours; fall back after timeout.
                    now = time.time()
                    if miss_started is None:
                        miss_started = now
                    if (now - miss_started) >= float(cfg["album_timeout_s"]):
                        cols = synthesize_palette(
                            cfg["base_hue"], cfg["saturation"], cfg["value"], cfg["scheme"]
                        )
                        rgba4 = [(r, g, b, cfg["alpha"]) for (r, g, b) in cols]
                        publish_theme_rgba(rgba4, cfg["alpha"])
            else:
                # Static palette mode
                cols = synthesize_palette(
                    cfg["base_hue"], cfg["saturation"], cfg["value"], cfg["scheme"]
                )
                rgba4 = [(r, g, b, cfg["alpha"]) for (r, g, b) in cols]
                publish_theme_rgba(rgba4, cfg["alpha"])

            time.sleep(dt)
    finally:
        bus.close_all()


if __name__ == "__main__":
    main()