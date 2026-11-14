"""Phosphor oscilloscope for the Scope page, drawn as pixels.

Turns Echo's ``/audio/waveform`` (160 mono samples of the latest 42.7 ms
window, published 50 times a second) into an RGB image the panel shows
through kitty's graphics protocol (see kitty_graphics.py).

The look is an analogue tube: a bright trace with a soft halo in the album's
accent colour (ember without one), fading to the next album's colour over
about a second. There is no grid. Behind the live trace, the last few traces stay on screen as
faint afterimages and the trace leaves a short afterglow, kept deliberately
secondary so they never compete with the live line.

Only numpy and Pillow are used, both existing UI dependencies. Silence draws
a flat line: the gain never amplifies noise below ``MIN_GAIN``.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Deque, Optional, Sequence, Tuple

import numpy as np

try:  # Pillow is a UI dependency; tests and tools can import this module without it.
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover - the Deck always has Pillow
    Image = ImageDraw = ImageFilter = None

RGB = Tuple[int, int, int]

BACKGROUND: RGB = (11, 3, 5)
IVORY: RGB = (255, 245, 214)       # the live trace
EMBER: RGB = (239, 138, 67)        # its halo
HEADROOM = 0.86                    # a full-scale trace uses this much of the half-height
MIN_GAIN = 0.02
GAIN_RELEASE = 0.94                # the auto-gain relaxes slowly after a peak
LINE_WIDTH = 3                     # at twice the output size
HALO = 1.0
HALO_BLUR = 1.7                    # radius at a quarter of the output size
AFTERIMAGES = 4                    # earlier traces kept on screen
AFTERIMAGE_STRENGTH = 0.34         # brightness of the newest one; older ones fade
AFTERGLOW = 0.40                   # share of the trace's glow kept into the next frame
GLOW_PEAK = 210                    # a dark album accent is lifted to at least this, so it shows
COLOUR_FADE_S = 0.4                # time constant of the fade to a new album's colour (~1 s)


def glow_colour(palette) -> np.ndarray:
    """The halo colour for a palette: its accent (the second colour), lifted
    until it is visible on the dark panel. Ember without a palette."""
    if palette is None or not len(palette):
        return np.asarray(EMBER, np.float32)
    accent = np.clip(np.asarray(palette[min(1, len(palette) - 1)], np.float32)[:3], 0, 255)
    peak = float(accent.max())
    if peak < 1:
        return np.full(3, 180.0, np.float32)
    if peak < GLOW_PEAK:
        accent = accent * (GLOW_PEAK / peak)
    return accent


class PhosphorScope:
    """Stateful renderer: keeps the gain, afterimages and afterglow between frames."""

    def __init__(self) -> None:
        self.gain = MIN_GAIN
        self._glow: Optional[np.ndarray] = None
        self._history: Deque[np.ndarray] = deque(maxlen=AFTERIMAGES)
        self._colour: Optional[np.ndarray] = None
        self._colour_at: Optional[float] = None

    def _points(self, wave: np.ndarray, big_w: int, big_h: int) -> list:
        xs = np.linspace(0.0, wave.size - 1, big_w, dtype=np.float32)
        ys = np.clip(np.interp(xs, np.arange(wave.size, dtype=np.float32), wave) / self.gain, -1.0, 1.0)
        points = np.column_stack((np.arange(big_w, dtype=np.float32), big_h / 2 - ys * (big_h / 2) * HEADROOM))
        return points.ravel().tolist()

    def _ease_colour(self, target: np.ndarray, now: float) -> np.ndarray:
        """Move the glow colour toward ``target``: a new album fades in over
        about a second, at the same speed whatever the frame rate."""
        if self._colour is None or self._colour_at is None:
            self._colour = target.copy()
        else:
            step = 1.0 - math.exp(-max(0.0, now - self._colour_at) / COLOUR_FADE_S)
            self._colour += (target - self._colour) * step
        self._colour_at = now
        return self._colour

    def render(self, waveform: Sequence[float], size: Tuple[int, int], palette=None,
               now: Optional[float] = None) -> np.ndarray:
        """One frame as an (height, width, 3) uint8 array, in the album's
        colours when ``palette`` is given (the UI's four display colours)."""
        if Image is None:
            raise RuntimeError("the scope needs Pillow (pip install pillow)")
        width, height = max(8, int(size[0])), max(8, int(size[1]))
        wave = np.asarray(waveform, dtype=np.float32).reshape(-1)
        if wave.size < 2:
            wave = np.zeros(2, np.float32)
        self.gain = max(MIN_GAIN, float(np.max(np.abs(wave))), self.gain * GAIN_RELEASE)
        big_w, big_h = width * 2, height * 2

        # Afterimages: earlier traces, drawn oldest and faintest first.
        ghosts = Image.new("L", (big_w, big_h), 0)
        draw = ImageDraw.Draw(ghosts)
        recent = list(self._history)
        for age, old in enumerate(recent):
            fade = AFTERIMAGE_STRENGTH * ((age + 1) / len(recent)) ** 1.5
            draw.line(self._points(old, big_w, big_h), fill=int(255 * fade), width=LINE_WIDTH - 1)
        self._history.append(wave.copy())

        # Live trace, drawn at twice the size and halved for smooth edges.
        canvas = Image.new("L", (big_w, big_h), 0)
        ImageDraw.Draw(canvas).line(self._points(wave, big_w, big_h), fill=255, width=LINE_WIDTH)
        trace_img = canvas.reduce(2)
        trace = np.asarray(trace_img, np.float32) * (1.0 / 255.0)
        ghost = np.asarray(ghosts.reduce(2), np.float32) * (1.0 / 255.0)

        # Halo: blurred at a quarter of the size, which is far cheaper.
        small = trace_img.resize((max(1, width // 4), max(1, height // 4)), Image.BOX)
        halo = np.asarray(small.filter(ImageFilter.GaussianBlur(HALO_BLUR)).resize((width, height), Image.BILINEAR),
                          np.float32) * (2.4 * HALO / 255.0)

        # Afterglow: the trace fading out over the next few frames.
        if self._glow is None or self._glow.shape != trace.shape:
            self._glow = np.zeros_like(trace)
        self._glow = np.maximum(self._glow * AFTERGLOW, trace)
        light = np.minimum(1.0, halo + self._glow * 0.5)

        # Compose one channel at a time: 2-D arrays are much cheaper than
        # broadcasting every layer across an (h, w, 3) float image.
        image = np.empty((height, width, 3), np.uint8)
        ember = self._ease_colour(glow_colour(palette), time.monotonic() if now is None else now)
        ivory = ember * 0.55 + 255 * 0.45
        for channel, (bg, em, iv) in enumerate(zip(BACKGROUND, ember, ivory)):
            value = light * np.float32(em)
            value += ghost * np.float32(em * 0.55 + iv * 0.45)
            value += trace * np.float32(iv)
            value += np.float32(bg)
            np.clip(value, 0, 255, out=value)
            image[:, :, channel] = value
        return image
