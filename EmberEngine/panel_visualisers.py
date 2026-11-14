"""The Scope page's other visualisers, drawn as pixels for kitty.

Beside the phosphor oscilloscope (scope_phosphor.py), three spectrum
visualisers built from Echo's ``/audio/spectrum`` (1025 rFFT magnitudes of a
Hann-windowed 2048-sample mono window at 48 kHz, 50 per second):

* ``PeakBars``     an LED-meter spectrum in the album's four colours, with
                   peak caps that hang, then fall;
* ``Spectrogram``  frequency history scrolling right to left, coloured as a
                   heat map of the album's four colours;
* ``RadialLogo``   a spectrum ring in Awoo colours around the Awoo logo,
                   breathing with the bass, over the Awoo starfield with
                   stars that drift outward and twinkle with the music.

Each is a stateful renderer: ``render(spectrum, size, palette)`` returns an
(height, width, 3) uint8 frame. Only numpy and Pillow are used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

try:  # Pillow is a UI dependency; tests and tools can import this module without it.
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover - the Deck always has Pillow
    Image = ImageDraw = ImageFilter = None

RGB = Tuple[int, int, int]

SAMPLE_RATE = 48000.0
FFT_SIZE = 2048                  # Echo's default; the real size is read from the spectrum's length
FLOOR_DB = -72.0
BACKGROUND: RGB = (11, 3, 5)

# Awoo brand colours (as the ignition and the boot screen).
AWOO_VIOLET: RGB = (0x79, 0x57, 0xE8)
AWOO_FUCHSIA: RGB = (0xE4, 0x55, 0xC7)
AWOO_CYAN: RGB = (0x42, 0xCB, 0xD8)
AWOO_NIGHT: RGB = (0x0B, 0x0B, 0x0F)

# The radial's own copy of the boot screen's logo and stars. Not read from
# boot/: a Deck owner may flip or edit the boot theme for their screens, and
# the panel (already rotated by the compositor) must not follow that.
PANEL_ART = Path(__file__).resolve().parent / "panel_art"


# --------------------------------------------------------------------------- shared helpers
_BAND_PLANS: Dict[Tuple[int, int], tuple] = {}


def band_levels(spectrum: Sequence[float], count: int, low: float = 32.0, high: float = 16000.0) -> np.ndarray:
    """0..1 levels of ``count`` log-spaced bands (max of each band's bins, in dB)."""
    values = np.asarray(spectrum, dtype=np.float32)
    key = (values.size, count)
    if key not in _BAND_PLANS:
        fft_size = max(2, (values.size - 1) * 2)     # rFFT: N/2 + 1 bins
        freqs = np.arange(values.size) * SAMPLE_RATE / fft_size
        edges = np.geomspace(low, high, count + 1)
        lo = np.minimum(np.searchsorted(freqs, edges[:-1]), values.size - 1)
        hi = np.searchsorted(freqs, edges[1:])
        has_bins = hi > lo
        # A full-scale sine through a Hann window peaks near N/4.
        _BAND_PLANS[key] = (freqs, lo, hi, has_bins, (edges[:-1] + edges[1:]) / 2, fft_size / 4.0)
    freqs, lo, hi, has_bins, centre, full_scale = _BAND_PLANS[key]
    out = np.interp(centre, freqs, values).astype(np.float32)  # bands narrower than a bin
    starts = lo[has_bins]
    if starts.size:
        maxima = np.maximum.reduceat(values, starts)
        maxima[-1] = values[starts[-1]:max(starts[-1] + 1, hi[has_bins][-1])].max()
        out[has_bins] = maxima
    db = 20.0 * np.log10(np.maximum(out, 1e-9) / full_scale)
    return np.clip((db - FLOOR_DB) / -FLOOR_DB, 0.0, 1.0)


def by_luma(colours: Sequence[Sequence[int]]) -> np.ndarray:
    """The palette as float RGB, darkest first."""
    table = np.asarray([tuple(c)[:3] for c in colours] or [(90, 90, 90)], dtype=np.float32)
    luma = table @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    return table[np.argsort(luma)]


def lift(colour: np.ndarray, minimum_luma: float = 70.0) -> np.ndarray:
    """Brighten a colour that would vanish on the dark panel, keeping its hue."""
    luma = float(colour @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))
    if luma < 1.0:
        return np.array((140, 90, 70), np.float32)
    return np.clip(colour * max(1.0, minimum_luma / luma), 0, 255)


def gradient(stops: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Colours for positions 0..1 through ``stops`` (an (n, 3) array)."""
    pos = np.clip(t, 0.0, 1.0) * (len(stops) - 1)
    i = np.minimum(pos.astype(int), len(stops) - 2)
    f = (pos - i)[..., None]
    return stops[i] * (1.0 - f) + stops[i + 1] * f


# --------------------------------------------------------------------------- peak-hold bars
class PeakBars:
    """LED-meter spectrum in the album's colours with falling peak caps."""

    BANDS = 40
    SEGMENT = 7          # segment pitch in pixels (6 lit, 1 gap)
    ATTACK = 0.6         # share of a rise taken at once
    RELEASE = 1.4        # level fall per second
    HOLD_S = 0.45        # how long a cap hangs before falling
    GRAVITY = 2.2        # cap fall acceleration, levels per second²

    def __init__(self, fps: float = 50.0) -> None:
        self.dt = 1.0 / fps
        self.level = np.zeros(self.BANDS, np.float32)
        self.peak = np.zeros(self.BANDS, np.float32)
        self.hold = np.zeros(self.BANDS, np.float32)
        self.fall = np.zeros(self.BANDS, np.float32)

    def render(self, spectrum: Sequence[float], size: Tuple[int, int], palette: Sequence[Sequence[int]]) -> np.ndarray:
        width, height = max(16, int(size[0])), max(16, int(size[1]))
        target = band_levels(spectrum, self.BANDS)
        rising = target > self.level
        self.level = np.where(rising, self.level + (target - self.level) * self.ATTACK,
                              np.maximum(target, self.level - self.RELEASE * self.dt))
        # Caps: jump up with the bar, hang, then fall faster and faster.
        up = self.level >= self.peak
        self.peak = np.where(up, self.level, self.peak)
        self.hold = np.where(up, self.HOLD_S, self.hold - self.dt)
        self.fall = np.where(up | (self.hold > 0), 0.0, self.fall + self.GRAVITY * self.dt)
        self.peak = np.where(self.hold > 0, self.peak, np.maximum(self.level, self.peak - self.fall * self.dt))

        stops = np.stack([lift(c) for c in by_luma(palette)])
        segments = max(4, height // self.SEGMENT)
        seg_colour = gradient(stops, (np.arange(segments) + 0.5) / segments)          # bottom → top
        image = np.empty((height, width, 3), np.float32)
        image[:] = BACKGROUND
        slot = width / self.BANDS
        bar_w = max(2, int(slot * 0.74))
        lit = np.round(self.level * segments).astype(int)
        cap = np.minimum(segments - 1, np.round(self.peak * segments).astype(int))
        for band in range(self.BANDS):
            x0 = int(band * slot + (slot - bar_w) / 2)
            for s in range(segments):
                y1 = height - s * self.SEGMENT
                y0 = y1 - (self.SEGMENT - 1)
                if y0 < 0:
                    break
                if s < lit[band]:
                    image[y0:y1, x0:x0 + bar_w] = seg_colour[s]
                elif s == cap[band] and self.peak[band] > 0.02:
                    image[y0:y1, x0:x0 + bar_w] = stops[-1] * 0.6 + 255 * 0.4
                else:  # unlit segments stay faintly visible, like a real LED meter
                    image[y0:y1, x0:x0 + bar_w] = np.asarray(BACKGROUND) + seg_colour[s] * 0.07
        return np.clip(image, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- spectrogram
class Spectrogram:
    """Frequency history scrolling right to left, in the album's colours.

    Scrolls by the clock when ``add`` is given the time: one panel width
    every ``HISTORY_S`` seconds, however often the UI gets round to it.
    """

    HISTORY_S = 12.0
    MAX_GAP_S = 0.5      # longer than this between adds is a pause, not a reason to smear

    def __init__(self) -> None:
        self.columns: Optional[np.ndarray] = None   # (height, width) levels 0..1
        self._palette_key: Optional[tuple] = None
        self._stops: Optional[np.ndarray] = None
        self._last_add: Optional[float] = None
        self._carry = 0.0

    def add(self, spectrum: Sequence[float], height: int, width: int, now: Optional[float] = None) -> None:
        """Push the spectrum into the history (cheap; can run while hidden).

        Without ``now``, one column. With it, as many columns as the time
        since the last add is worth, blended from the previous column to this
        one, so a slow frame scrolls the same distance as several quick ones.
        """
        if self.columns is None or self.columns.shape != (height, width):
            self.columns = np.zeros((height, width), np.float32)
            self._last_add, self._carry = None, 0.0
        count = 1
        if now is not None:
            elapsed = None if self._last_add is None else now - self._last_add
            self._last_add = now
            if elapsed is not None and 0.0 <= elapsed <= self.MAX_GAP_S:
                self._carry += elapsed * width / self.HISTORY_S
                count = min(width, int(self._carry))
                self._carry -= count
                if count == 0:
                    return
            else:
                self._carry = 0.0
        column = band_levels(spectrum, height)[::-1]               # low frequencies at the bottom
        previous = self.columns[:, -1].copy()
        self.columns[:, :-count] = self.columns[:, count:]
        steps = (np.arange(1, count + 1, dtype=np.float32) / count)[None, :]
        self.columns[:, -count:] = previous[:, None] + (column - previous)[:, None] * steps

    def render(self, spectrum: Sequence[float], size: Tuple[int, int], palette: Sequence[Sequence[int]]) -> np.ndarray:
        """Add one column and draw."""
        width, height = max(16, int(size[0])), max(16, int(size[1]))
        self.add(spectrum, height, width)
        return self.paint(palette)

    def paint(self, palette: Sequence[Sequence[int]]) -> np.ndarray:
        """Draw the history as it stands (``add`` first at least once)."""
        if self.columns is None:
            raise ValueError("the spectrogram has no history yet")
        key = tuple(tuple(c)[:3] for c in palette)
        if key != self._palette_key:
            ordered = [lift(c, 40.0) for c in by_luma(palette)]
            # Black → darkest → … → lightest → near white at the very top.
            self._stops = np.stack([np.asarray(BACKGROUND, np.float32), *ordered,
                                    np.asarray(ordered[-1]) * 0.4 + 255 * 0.6])
            self._palette_key = key
        t = np.clip((self.columns - 0.22) / 0.78, 0.0, 1.0) ** 1.3
        return np.clip(gradient(self._stops, t), 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- radial with the Awoo logo
class RadialLogo:
    """A spectrum ring in Awoo colours around the Awoo logo, over a starfield
    that drifts outward and twinkles, mildly, with the music."""

    SPOKES = 64                                   # mirrored: 32 bands each side
    COLOURS = np.asarray([AWOO_VIOLET, AWOO_FUCHSIA, AWOO_CYAN, AWOO_FUCHSIA, AWOO_VIOLET], np.float32)
    LOGO_STEPS = 6                                # cached logo sizes for the bass "breath"
    # Sizes as fractions of the panel's shorter side. A loud spoke ends at
    # RING_IN + RING_REACH (0.49), just inside the panel; a bigger logo and
    # ring leave the spokes less room to move.
    LOGO_SIZE = 0.58
    RING_IN = 0.35
    RING_REACH = 0.14
    # Centre of the moon the crescent is cut from, in the 500 px art. The art's
    # canvas is not centred on it (the moon runs off its left edge), so the
    # logo is placed by this point: the mask sits in the middle of the ring and
    # the crescent's outer edge runs parallel to it.
    LOGO_CENTRE = (203.0, 248.0)
    LOGO_ART = 500
    STARS = 150                                   # moving stars over the still boot starfield
    STILL_STARS = 0.75                            # the boot starfield, dimmed to sit behind them
    DRIFT_QUIET = 0.012                           # outward drift, fraction of the half-diagonal a second
    DRIFT_LOUD = 0.07
    STAR_TINTS = np.asarray([(255, 250, 240), (255, 250, 240), (255, 250, 240),
                             AWOO_CYAN, AWOO_VIOLET, AWOO_FUCHSIA], np.float32)

    def __init__(self, fps: float = 50.0, seed: Optional[int] = None) -> None:
        self.dt = 1.0 / fps
        self.level = np.zeros(self.SPOKES // 2, np.float32)
        self.bass = 0.0
        self._size: Optional[Tuple[int, int]] = None
        self._stars: Optional[np.ndarray] = None
        self._logos: Dict[int, "Image.Image"] = {}
        self._colours = gradient(self.COLOURS, np.linspace(0, 1, self.SPOKES))
        angles = 2 * np.pi * (np.arange(self.SPOKES) + 0.5) / self.SPOKES - np.pi / 2
        self._cos, self._sin = np.cos(angles), np.sin(angles)

        # Moving stars, in polar form around the centre. Distance is 0..1 of
        # the half-diagonal; each star twinkles with one of the ring's bands,
        # mostly the upper ones (hats and air), so it reads as sparkle.
        rng = np.random.default_rng(seed)
        n = self.STARS
        self._star_angle = rng.uniform(0, 2 * np.pi, n)
        self._star_dist = rng.uniform(0.0, 1.0, n) ** 0.7
        self._star_speed = rng.uniform(0.6, 1.4, n)
        self._star_band = np.minimum(self.level.size - 1, (rng.beta(2.2, 1.3, n) * self.level.size).astype(int))
        self._star_base = rng.uniform(0.18, 0.55, n).astype(np.float32)
        self._star_tint = self.STAR_TINTS[rng.integers(0, len(self.STAR_TINTS), n)]
        self._rng = rng

    def _prepare(self, width: int, height: int) -> None:
        """Starfield and logo sizes, once per panel size."""
        if self._size == (width, height):
            return
        stars = Image.open(PANEL_ART / "stars.png").convert("RGBA")
        left = max(0, (stars.width - width) // 2)
        top = max(0, (stars.height - height) // 2)
        stars = stars.crop((left, top, left + width, top + height))
        base = Image.new("RGBA", (width, height), AWOO_NIGHT + (255,))
        base.alpha_composite(stars.resize((width, height)) if stars.size != (width, height) else stars)
        night = np.asarray(AWOO_NIGHT, np.float32)
        still = np.asarray(base.convert("RGB"), np.float32)
        self._stars = night + (still - night) * self.STILL_STARS
        logo = Image.new("RGBA", (self.LOGO_ART, self.LOGO_ART), (0, 0, 0, 0))
        for name in ("crescent.png", "mask.png"):
            logo.alpha_composite(Image.open(PANEL_ART / name).convert("RGBA"))
        diameter = min(width, height)
        smallest = int(diameter * self.LOGO_SIZE)
        self._logos = {}
        for step in range(self.LOGO_STEPS):
            side = smallest + int(step * diameter * 0.024 / max(1, self.LOGO_STEPS - 1))
            self._logos[step] = logo.resize((side, side), Image.LANCZOS)
        self._size = (width, height)

    def _move_stars(self) -> None:
        """Drift outward, faster (and faster still near the edge) when loud."""
        energy = float(np.mean(self.level))
        drift = self.DRIFT_QUIET + (self.DRIFT_LOUD - self.DRIFT_QUIET) * energy ** 1.5
        self._star_dist += drift * self.dt * self._star_speed * (0.25 + self._star_dist)
        gone = self._star_dist > 1.0
        if gone.any():                    # reborn near the middle, behind the logo
            count = int(gone.sum())
            self._star_angle[gone] = self._rng.uniform(0, 2 * np.pi, count)
            self._star_dist[gone] = self._rng.uniform(0.02, 0.2, count)

    def _draw_stars(self, frame: np.ndarray) -> None:
        """Add the moving stars to ``frame`` (height, width, 3) in place."""
        height, width = frame.shape[:2]
        reach = np.hypot(width, height) / 2
        x = width / 2 + np.cos(self._star_angle) * self._star_dist * reach
        y = height / 2 + np.sin(self._star_angle) * self._star_dist * reach
        # Fade in from the centre, twinkle with the star's band.
        fade = np.clip(self._star_dist / 0.3, 0.0, 1.0)
        twinkle = 0.55 + 0.9 * self.level[self._star_band] ** 1.5
        bright = (self._star_base * fade * twinkle)[:, None] * self._star_tint
        xi, yi = np.rint(x).astype(int), np.rint(y).astype(int)
        # A slightly wider soft star, keeping its core brightness unchanged.
        for dx, dy, weight in ((0, 0, 1.0), (1, 0, 0.5), (-1, 0, 0.5), (0, 1, 0.5), (0, -1, 0.5),
                               (1, 1, 0.18), (-1, 1, 0.18), (1, -1, 0.18), (-1, -1, 0.18),
                               (2, 0, 0.12), (-2, 0, 0.12), (0, 2, 0.12), (0, -2, 0.12)):
            px, py = xi + dx, yi + dy
            inside = (px >= 0) & (px < width) & (py >= 0) & (py < height)
            np.add.at(frame, (py[inside], px[inside]), bright[inside] * weight)

    def render(self, spectrum: Sequence[float], size: Tuple[int, int], palette=None) -> np.ndarray:
        width, height = max(32, int(size[0])), max(32, int(size[1]))
        self._prepare(width, height)
        target = band_levels(spectrum, self.level.size, high=12000.0)
        self.level = np.where(target > self.level, target, np.maximum(target, self.level - 1.5 * self.dt))
        self.bass = max(float(target[:3].max()), self.bass - 1.2 * self.dt)
        self._move_stars()

        # Spokes: drawn at twice the size and halved for smooth edges.
        scale = 2
        cx, cy = width * scale / 2, height * scale / 2
        diameter = min(width, height)
        ring_in = diameter * scale * self.RING_IN
        ring_len = diameter * scale * self.RING_REACH
        values = np.concatenate([self.level, self.level[::-1]])     # mirrored, bass at the top
        reach = ring_in + ring_len * np.maximum(0.04, values) ** 1.2
        rays = Image.new("RGB", (width * scale, height * scale), (0, 0, 0))
        draw = ImageDraw.Draw(rays)
        line_w = max(2, int(scale * 2.2))
        for i in range(self.SPOKES):
            bright = 0.45 + 0.55 * float(values[i])
            colour = tuple(int(c * bright) for c in self._colours[i])
            draw.line([(cx + ring_in * self._cos[i], cy + ring_in * self._sin[i]),
                       (cx + reach[i] * self._cos[i], cy + reach[i] * self._sin[i])], fill=colour, width=line_w)
        rays_small = rays.reduce(2)
        # Glow: blurred at half the output size, then brought back up.
        half = rays_small.resize((max(1, width // 2), max(1, height // 2)), Image.BOX)
        glow = half.filter(ImageFilter.GaussianBlur(2.5)).resize((width, height), Image.BILINEAR)
        frame = self._stars + np.asarray(rays_small, np.float32) \
            + np.asarray(glow, np.float32) * (0.9 + 0.9 * self.bass)
        self._draw_stars(frame)
        image = Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8))

        logo = self._logos[min(self.LOGO_STEPS - 1, int(self.bass * self.LOGO_STEPS))]   # breathes with the bass
        scale = logo.width / self.LOGO_ART
        left = int(round(width / 2 - self.LOGO_CENTRE[0] * scale))
        top = int(round(height / 2 - self.LOGO_CENTRE[1] * scale))
        image.paste(logo, (left, top), logo)
        return np.asarray(image, dtype=np.uint8)


VISUALISERS = {"bars": PeakBars, "spectrogram": Spectrogram, "radial": RadialLogo}
