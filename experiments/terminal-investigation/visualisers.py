"""Prototype visualiser renderers for the terminal investigation.

Not production code. Each renderer turns the data Echo already publishes,
``/audio/spectrum`` (rFFT magnitudes of a Hann-windowed 2048-sample mono
window at 48 kHz, 1025 bins, 50 Hz) and ``/audio/waveform`` (160 mono samples
decimated from the same window), into either

* an RGB pixel image (for kitty's graphics protocol), or
* rows of styled terminal characters (for any terminal, LXTerminal included).

Only numpy and Pillow are used, both already Ember Deck dependencies.
``bench()`` times every renderer; run ``python visualisers.py`` on the Pi.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

RGB = Tuple[int, int, int]
SAMPLE_RATE = 48000
FFT_SIZE = 2048
# A full-scale sine through a Hann window peaks at about N/4 in |rFFT|.
FULL_SCALE = FFT_SIZE / 4.0
FLOOR_DB = -72.0


def band_edges(count: int, low: float = 32.0, high: float = 16000.0) -> np.ndarray:
    return np.geomspace(low, high, count + 1)


_BAND_CACHE: Dict[Tuple[int, int], Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}


def _band_plan(bins: int, count: int):
    """Per band: first bin, whether it holds any bin, and an interpolation point."""
    key = (bins, count)
    if key not in _BAND_CACHE:
        freqs = np.arange(bins) * SAMPLE_RATE / FFT_SIZE
        edges = band_edges(count)
        lo = np.searchsorted(freqs, edges[:-1])
        hi = np.searchsorted(freqs, edges[1:])
        has_bins = hi > lo
        centre = (edges[:-1] + edges[1:]) / 2
        _BAND_CACHE[key] = (freqs, np.minimum(lo, bins - 1), has_bins, centre)
    return _BAND_CACHE[key]


def spectrum_bands(spectrum: np.ndarray, count: int) -> np.ndarray:
    """Log-spaced bands in dBFS (FLOOR_DB..0), max of the bins in each band.

    Bands narrower than one FFT bin (low frequencies) are interpolated."""
    spectrum = np.asarray(spectrum, dtype=np.float32)
    freqs, lo, has_bins, centre = _band_plan(spectrum.size, count)
    starts = lo[has_bins]
    maxima = np.maximum.reduceat(spectrum, starts) if starts.size else np.empty(0)
    out = np.interp(centre, freqs, spectrum).astype(np.float32)
    # reduceat runs from each start to the next start; the last runs to the end,
    # so cap it at its own band's upper edge.
    if starts.size:
        last_hi = np.searchsorted(freqs, band_edges(count)[1:])[has_bins][-1]
        maxima[-1] = spectrum[starts[-1]:max(starts[-1] + 1, last_hi)].max()
        out[has_bins] = maxima
    db = 20.0 * np.log10(np.maximum(out, 1e-9) / FULL_SCALE)
    return np.clip(db, FLOOR_DB, 0.0)


def to_unit(db: np.ndarray) -> np.ndarray:
    return (db - FLOOR_DB) / -FLOOR_DB


def lerp(a: RGB, b: RGB, t: np.ndarray) -> np.ndarray:
    a_, b_ = np.array(a, np.float32), np.array(b, np.float32)
    return a_ + (b_ - a_) * t[..., None]


def gradient(palette: Sequence[RGB], t: np.ndarray) -> np.ndarray:
    """Map 0..1 through the palette's colours (dark → accent → light)."""
    stops = np.array(palette, np.float32)
    pos = np.clip(t, 0, 1) * (len(stops) - 1)
    i = np.minimum(pos.astype(int), len(stops) - 2)
    f = (pos - i)[..., None]
    return stops[i] * (1 - f) + stops[i + 1] * f


@dataclass
class State:
    """History the time-based effects need between frames."""

    peaks: Optional[np.ndarray] = None
    peak_age: Optional[np.ndarray] = None
    smooth: Optional[np.ndarray] = None
    spectrogram: Optional[np.ndarray] = None
    phosphor: Optional[np.ndarray] = None
    extra: Dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------- pixel renderers
def bars(spectrum, waveform, state: State, palette, size, dt=0.02, count=56) -> np.ndarray:
    """Log-frequency bars with peak-hold caps that fall after a short hold."""
    w, h = size
    db = spectrum_bands(spectrum, count)
    level = to_unit(db)
    if state.smooth is None:
        state.smooth = level.copy()
        state.peaks = level.copy()
        state.peak_age = np.zeros(count)
    # Fast attack, slower release, like a VU ballistics.
    state.smooth = np.where(level > state.smooth, level, state.smooth - dt * 1.6)
    state.smooth = np.maximum(state.smooth, 0)
    rising = state.smooth >= state.peaks
    state.peaks = np.where(rising, state.smooth, state.peaks)
    state.peak_age = np.where(rising, 0.0, state.peak_age + dt)
    falling = state.peak_age > 0.35
    state.peaks = np.where(falling, np.maximum(state.peaks - dt * 0.5, state.smooth), state.peaks)

    img = np.zeros((h, w, 3), np.float32)
    img[:] = np.array(palette[0], np.float32) * 0.18
    slot = w / count
    bar_w = max(1, int(slot * 0.72))
    ys = np.arange(h)[:, None]
    heat = gradient(palette, 1 - ys[:, 0] / h)  # colour by height
    for i in range(count):
        x0 = int(i * slot + (slot - bar_w) / 2)
        top = int(h * (1 - state.smooth[i]))
        img[top:, x0:x0 + bar_w] = heat[top:, None, :]
        cap = int(h * (1 - state.peaks[i]))
        img[max(0, cap - 2):cap, x0:x0 + bar_w] = palette[-1]
    # Faint horizontal segment lines give an LED-meter feel.
    img[::6] *= 0.55
    return img.clip(0, 255).astype(np.uint8)


def oscilloscope(spectrum, waveform, state: State, palette, size, dt=0.02) -> np.ndarray:
    """Phosphor trace: anti-aliased line, glow and decaying persistence."""
    w, h = size
    wave = np.asarray(waveform, np.float32)
    peak = max(0.02, float(np.abs(wave).max()))
    state.extra["gain"] = max(peak, 0.9 * float(state.extra.get("gain", peak)))
    xs = np.linspace(0, wave.size - 1, w * 2)
    ys = np.interp(xs, np.arange(wave.size), wave) / state.extra["gain"]
    scale = 2
    canvas = Image.new("L", (w * scale, h * scale), 0)
    pts = [(i * w * scale / xs.size, (h * scale) / 2 - y * (h * scale) * 0.42) for i, y in enumerate(ys)]
    ImageDraw.Draw(canvas).line(pts, fill=255, width=3)
    trace = np.asarray(canvas.resize((w, h), Image.BILINEAR), np.float32) / 255
    glow = np.asarray(Image.fromarray((trace * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(4)), np.float32) / 255
    if state.phosphor is None:
        state.phosphor = np.zeros((h, w), np.float32)
    state.phosphor = np.maximum(state.phosphor * 0.72, trace)
    core = np.array((255, 245, 214), np.float32)
    halo = np.array(palette[1], np.float32)
    img = np.zeros((h, w, 3), np.float32)
    img += halo * (glow[..., None] * 1.6)
    img += halo * (state.phosphor[..., None] * 0.55)
    img += core * trace[..., None]
    # Graticule.
    img[:, :: max(1, w // 10)] += 18
    img[h // 2] += 24
    return img.clip(0, 255).astype(np.uint8)


def spectrogram(spectrum, waveform, state: State, palette, size, dt=0.02) -> np.ndarray:
    """Scrolling spectrogram: time left to right, log frequency bottom to top."""
    w, h = size
    column = to_unit(spectrum_bands(spectrum, h))[::-1]
    if state.spectrogram is None:
        state.spectrogram = np.zeros((h, w, 3), np.uint8)
    # Shift the coloured image one pixel left and colour only the new column.
    state.spectrogram[:, :-1] = state.spectrogram[:, 1:]
    t = np.clip((column - 0.25) / 0.75, 0, 1) ** 1.4
    stops = [(6, 2, 4), palette[0], palette[1], palette[3]]
    state.spectrogram[:, -1] = gradient(stops, t).clip(0, 255).astype(np.uint8)
    return state.spectrogram


def radial(spectrum, waveform, state: State, palette, size, dt=0.02, art: Optional[np.ndarray] = None, count=72) -> np.ndarray:
    """Mirrored radial spectrum around a circular cover."""
    w, h = size
    level = to_unit(spectrum_bands(spectrum, count // 2))
    if state.smooth is None or state.smooth.size != level.size:
        state.smooth = level.copy()
    state.smooth = np.where(level > state.smooth, level, state.smooth - dt * 1.4)
    values = np.concatenate([state.smooth, state.smooth[::-1]])
    cx, cy = w / 2, h / 2
    r0 = min(w, h) * 0.26
    r1 = min(w, h) * 0.48
    scale = 2
    im = Image.new("RGB", (w * scale, h * scale), tuple(int(c * 0.12) for c in palette[0]))
    d = ImageDraw.Draw(im)
    for i, v in enumerate(values):
        a = 2 * np.pi * i / values.size - np.pi / 2
        ra, rb = r0 + 3, r0 + 3 + (r1 - r0) * float(max(0.02, v))
        col = tuple(int(c) for c in gradient(palette[1:], np.array([v]))[0])
        d.line([(scale * (cx + ra * np.cos(a)), scale * (cy + ra * np.sin(a))),
                (scale * (cx + rb * np.cos(a)), scale * (cy + rb * np.sin(a)))], fill=col, width=5)
    if art is not None:
        side = int(2 * r0 * scale)
        cover = Image.fromarray(art).resize((side, side), Image.NEAREST)
        mask = Image.new("L", (side, side), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, side - 1, side - 1], fill=255)
        im.paste(cover, (int(scale * cx - side / 2), int(scale * cy - side / 2)), mask)
    return np.asarray(im.resize((w, h), Image.LANCZOS))


PIXEL_RENDERERS = {"bars": bars, "oscilloscope": oscilloscope, "spectrogram": spectrogram, "radial": radial}


# --------------------------------------------------------------------------- character renderers
EIGHTHS = " ▁▂▃▄▅▆▇█"


def text_bars(spectrum, waveform, state: State, palette, cells) -> List[List[Tuple[str, RGB]]]:
    """Spectrum bars from eighth blocks: one bar per cell, 8 levels per row.

    Works in any terminal with block elements (VTE draws these itself)."""
    cols, rows = cells
    level = to_unit(spectrum_bands(spectrum, cols))
    if state.smooth is None or state.smooth.size != cols:
        state.smooth = level.copy()
    state.smooth = np.where(level > state.smooth, level, state.smooth - 0.032)
    height = state.smooth * rows * 8
    out = []
    for r in range(rows):
        row = []
        base = (rows - 1 - r) * 8
        colour = tuple(int(c) for c in gradient(palette, np.array([(rows - r) / rows]))[0])
        for c in range(cols):
            n = int(np.clip(height[c] - base, 0, 8))
            row.append((EIGHTHS[n], colour))
        out.append(row)
    return out


def text_braille_scope(spectrum, waveform, state: State, palette, cells) -> List[List[Tuple[str, RGB]]]:
    """Oscilloscope at Braille resolution: 2 × 4 dots per cell."""
    cols, rows = cells
    wave = np.asarray(waveform, np.float32)
    gain = max(0.02, float(np.abs(wave).max()))
    xs = np.linspace(0, wave.size - 1, cols * 2)
    ys = np.interp(xs, np.arange(wave.size), wave) / gain
    dots_h = rows * 4
    yi = np.clip(((1 - ys) / 2 * (dots_h - 1)).round().astype(int), 0, dots_h - 1)
    grid = np.zeros((dots_h, cols * 2), bool)
    for x in range(cols * 2):  # join neighbours vertically so the trace is continuous
        a = yi[x]
        b = yi[x + 1] if x + 1 < yi.size else a
        grid[min(a, b):max(a, b) + 1, x] = True
    bits = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
    colour = tuple(int(c) for c in palette[-1])
    out = []
    for r in range(rows):
        row = []
        for c in range(cols):
            v = 0
            for dy in range(4):
                for dx in range(2):
                    if grid[r * 4 + dy, c * 2 + dx]:
                        v |= bits[dy][dx]
            row.append((chr(0x2800 + v) if v else " ", colour))
        out.append(row)
    return out


TEXT_RENDERERS = {"eighth-block bars": text_bars, "braille oscilloscope": text_braille_scope}


# --------------------------------------------------------------------------- benchmark
def bench(frames: int = 200, size=(592, 240), cells=(74, 15)) -> Dict[str, float]:
    rng = np.random.default_rng(1)
    palette = [(120, 40, 50), (230, 120, 60), (90, 40, 140), (250, 230, 200)]
    results = {}
    for name, fn in {**PIXEL_RENDERERS, **TEXT_RENDERERS}.items():
        state = State()
        spectra = np.abs(rng.normal(0, 40, (frames, FFT_SIZE // 2 + 1))).astype(np.float32)
        waves = rng.normal(0, 0.2, (frames, 160)).astype(np.float32)
        start = time.perf_counter()
        for i in range(frames):
            if name in PIXEL_RENDERERS:
                fn(spectra[i], waves[i], state, palette, size)
            else:
                fn(spectra[i], waves[i], state, palette, cells)
        results[name] = (time.perf_counter() - start) / frames * 1000
    return results


if __name__ == "__main__":
    # Random input only measures cost; it is not shown as audio anywhere.
    for name, ms in bench().items():
        print(f"{name:22s} {ms:6.2f} ms/frame  ({1000 / ms:5.0f} fps max)")
