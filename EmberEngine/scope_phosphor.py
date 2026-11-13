"""Phosphor oscilloscope for the Scope page, drawn as pixels.

Turns Echo's ``/audio/waveform`` (160 mono samples of the latest 42.7 ms
window, published 50 times a second) into an RGB image the panel shows
through kitty's graphics protocol (see kitty_graphics.py): an anti-aliased
ivory trace, a halo in the album palette's accent colour, a short phosphor
afterglow and a faint graticule.

Only numpy and Pillow are used, both existing UI dependencies. Silence draws
a flat line: the gain never amplifies noise below ``MIN_GAIN``.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

import numpy as np

try:  # Pillow is a UI dependency; tests and tools can import this module without it.
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover - the Deck always has Pillow
    Image = ImageDraw = ImageFilter = None

RGB = Tuple[int, int, int]

BACKGROUND: RGB = (11, 3, 5)
CORE: RGB = (255, 245, 214)
GRID: RGB = (46, 22, 20)
# A full-scale trace may use this fraction of the half-height.
HEADROOM = 0.86
MIN_GAIN = 0.02
# Gain follows peaks at once and relaxes by this factor per frame.
GAIN_RELEASE = 0.94
# Afterglow left from the previous frame (0 = none).
PERSISTENCE = 0.62


def visible_colour(rgb: Sequence[int], minimum_luma: float = 126.0) -> RGB:
    """Lift and saturate a palette colour so its halo shows on the dark screen."""
    r, g, b = (max(0, min(255, int(v))) for v in rgb[:3])
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if luma < 1.0:
        return (210, 95, 28)
    if luma < minimum_luma:
        scale = min(3.5, minimum_luma / luma)
        r, g, b = (min(255, int(round(v * scale))) for v in (r, g, b))
    # Keep the palette's character, but never let a grey cover erase the halo.
    if max(r, g, b) - min(r, g, b) < 38:
        r, g, b = min(255, max(r, 192)), min(255, max(g, 78)), min(255, max(b, 28))
    return (r, g, b)


class PhosphorScope:
    """Stateful renderer: keeps the gain and the afterglow between frames."""

    def __init__(self) -> None:
        self.gain = MIN_GAIN
        self._glow: Optional[np.ndarray] = None
        self._grid: Optional[np.ndarray] = None

    def _graticule(self, width: int, height: int) -> np.ndarray:
        if self._grid is None or self._grid.shape[:2] != (height, width):
            grid = np.zeros((height, width), np.float32)
            for i in range(1, 10):
                grid[:, int(i * width / 10)] = 1.0
            grid[height // 2, :] = 1.6
            self._grid = grid
            self._glow = None
        return self._grid

    def render(self, waveform: Sequence[float], accent: Sequence[int], size: Tuple[int, int]) -> np.ndarray:
        """One frame as an (height, width, 3) uint8 array."""
        if Image is None:
            raise RuntimeError("the phosphor scope needs Pillow (pip install pillow)")
        width, height = max(8, int(size[0])), max(8, int(size[1]))
        wave = np.asarray(waveform, dtype=np.float32).reshape(-1)
        if wave.size < 2:
            wave = np.zeros(2, np.float32)
        peak = float(np.max(np.abs(wave)))
        self.gain = max(MIN_GAIN, peak, self.gain * GAIN_RELEASE)

        # Trace: drawn at twice the size, then reduced, for smooth edges.
        xs = np.linspace(0.0, wave.size - 1, width * 2, dtype=np.float32)
        ys = np.interp(xs, np.arange(wave.size, dtype=np.float32), wave) / self.gain
        ys = np.clip(ys, -1.0, 1.0)
        big_w, big_h = width * 2, height * 2
        points = np.column_stack((np.linspace(0, big_w - 1, xs.size), big_h / 2 - ys * (big_h / 2) * HEADROOM))
        canvas = Image.new("L", (big_w, big_h), 0)
        ImageDraw.Draw(canvas).line(points.ravel().tolist(), fill=255, width=3)
        trace_img = canvas.reduce(2)
        trace = np.asarray(trace_img, np.float32) * (1.0 / 255.0)

        # Halo: blurred at a quarter of the size, which is far cheaper.
        small = trace_img.resize((max(1, width // 4), max(1, height // 4)), Image.BOX)
        halo = np.asarray(small.filter(ImageFilter.GaussianBlur(1.6)).resize((width, height), Image.BILINEAR),
                          np.float32) * (2.4 / 255.0)

        grid = self._graticule(width, height)
        if self._glow is None:
            self._glow = np.zeros((height, width), np.float32)
        self._glow = np.maximum(self._glow * PERSISTENCE, trace)

        # Compose one channel at a time: 2-D arrays are much cheaper than
        # broadcasting every layer across an (h, w, 3) float image.
        light = np.minimum(1.0, halo + self._glow * 0.5)
        image = np.empty((height, width, 3), np.uint8)
        for channel, (bg, gr, ac, co) in enumerate(zip(BACKGROUND, GRID, visible_colour(accent), CORE)):
            value = grid * np.float32(gr)
            value += light * np.float32(ac)
            value += trace * np.float32(co)
            value += np.float32(bg)
            np.clip(value, 0, 255, out=value)
            image[:, :, channel] = value
        return image
