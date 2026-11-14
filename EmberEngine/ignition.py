"""The LED strip's start-up "ignition" in Awoo colours.

Will-o'-Wisp plays this once, as soon as the BlinkStick connects and before
the first audio frame, so the strip announces the Deck coming to life:

1. sweep  (0.00-0.80 s)  a head runs along the strip; each pixel fades in
                         behind it in the awoo gradient (violet, fuchsia,
                         cyan) at low intensity;
2. rise   (0.80-1.60 s)  the whole strip climbs to full intensity while the
                         gradient drifts;
3. flash  (1.60-1.75 s)  a white flash (the RGBW strip's white channel);
4. fade   (1.75-2.60 s)  back through the colours to dark.

Frames are RGBA in 0..1, the same format Foxfire publishes: RGB is the colour
already scaled by the physical brightness control (as Aurora does for the
normal palette), A is intensity. Will-o'-Wisp's power budget still applies.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

import numpy as np

RGB = Tuple[int, int, int]

# Awoo brand colours (the awoo website palette).
VIOLET: RGB = (0x79, 0x57, 0xE8)
FUCHSIA: RGB = (0xE4, 0x55, 0xC7)
CYAN: RGB = (0x42, 0xCB, 0xD8)
GRADIENT: Tuple[RGB, ...] = (VIOLET, FUCHSIA, CYAN, FUCHSIA, VIOLET)

SWEEP_END = 0.80
RISE_END = 1.60
FLASH_END = 1.75
DURATION = 2.60
SWEEP_INTENSITY = 0.35
PIXEL_FADE_S = 0.25


def _gradient(positions: np.ndarray, stops: Sequence[RGB] = GRADIENT) -> np.ndarray:
    """Colours (N, 3) in 0..1 for positions in 0..1, wrapping smoothly."""
    table = np.asarray(stops, dtype=np.float32) / 255.0
    scaled = (positions % 1.0) * (len(table) - 1)
    index = np.minimum(scaled.astype(int), len(table) - 2)
    frac = (scaled - index)[:, None]
    return table[index] * (1.0 - frac) + table[index + 1] * frac


def _ease_in(x: float) -> float:
    return x * x


def _ease_out(x: float) -> float:
    return 1.0 - (1.0 - x) * (1.0 - x)


def frame(t: float, leds: int, brightness: float = 1.0) -> Optional[np.ndarray]:
    """The RGBA frame (leds, 4) at ``t`` seconds, or None once it has finished."""
    if t >= DURATION or leds <= 0:
        return None
    brightness = float(np.clip(brightness, 0.0, 1.0))
    positions = np.linspace(0.0, 1.0, leds, dtype=np.float32)
    drift = max(0.0, t - SWEEP_END) * 0.18
    colour = _gradient(positions * 0.999 + drift)
    alpha = np.zeros(leds, dtype=np.float32)

    if t < SWEEP_END:
        # When the head passed each pixel; pixels then fade in over PIXEL_FADE_S.
        passed_at = positions * (SWEEP_END - PIXEL_FADE_S)
        alpha = np.clip((t - passed_at) / PIXEL_FADE_S, 0.0, 1.0) * SWEEP_INTENSITY
    elif t < RISE_END:
        progress = (t - SWEEP_END) / (RISE_END - SWEEP_END)
        alpha[:] = SWEEP_INTENSITY + (1.0 - SWEEP_INTENSITY) * _ease_in(progress)
    elif t < FLASH_END:
        colour[:] = 1.0
        alpha[:] = 1.0
    else:
        progress = (t - FLASH_END) / (DURATION - FLASH_END)
        white = max(0.0, 1.0 - progress * 4.0)  # white falls away in the first quarter
        colour = colour * (1.0 - white) + white
        alpha[:] = 1.0 - _ease_out(progress)

    rgba = np.empty((leds, 4), dtype=np.float32)
    rgba[:, :3] = colour * brightness
    rgba[:, 3] = alpha
    return rgba


def frames(leds: int, fps: float, brightness: float = 1.0):
    """Every frame at ``fps``, for playing back or previewing."""
    step = 1.0 / max(1.0, float(fps))
    t = 0.0
    while True:
        rgba = frame(t, leds, brightness)
        if rgba is None:
            return
        yield rgba
        t += step
