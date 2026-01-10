"""
foxfire.py
Audio spectrum → per-LED brightness → 4-tier palette colouring.

Reads:
  /audio/seq, /audio/seq2, /audio/bins, /audio/fft, /audio/sr, /audio/spectrum
  /theme/ultra/tl_rgba, /tr_rgba, /bl_rgba, /br_rgba

Controls:
  /leds/main/mode        int: 0=bounce, 1=aurora (stub), 2=tails, 3=illusion
  /leds/main/gain        float
  /leds/main/brightness  float 0..1

Writes:
  /leds/main/output_u8      uint8[LED_COUNT] (0..100 brightness)
  /leds/main/output_rgb_u8  uint8[LED_COUNT*3] (flat R,G,B,...)
Heartbeat:
  /proc/foxfire/heartbeat
"""
import os, time, math, yaml, numpy as np
from pathlib import Path
from typing import Any
import synapse as bus
from signal import signal, SIGINT, SIGTERM


# ---------- defaults ----------
DEFAULTS = dict(
    proc_name="foxfire",
    led_count=40,
    fps=50,
    mode_key="/leds/main/mode",
    gain_key="/leds/main/gain",
    brightness_key="/leds/main/brightness",
    output_key="/leds/main/output_u8",
    release=0.99, attack=0.50, decay=0.999, rise=0.2,
    epsilon=1e-12, gamma=2.2,
)


def load_cfg() -> dict[str, Any]:
    """Load YAML config for this instance."""
    p = os.environ.get("CONFIG_PATH")
    cfg = {}
    if p and Path(p).exists():
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    out = DEFAULTS.copy()
    out.update(cfg)
    out["period"] = 1.0 / max(1, int(out["fps"]))
    return out


_should_stop = False
def _stop(*_):
    """Signal handler to stop the loop cleanly."""
    global _should_stop
    _should_stop = True


# ---------- DSP helpers ----------
def downbin_log(spec, sr, fft_size, n_bands, fmin=30.0, fmax=None, p=90, split_hz=1500.0):
    """Logarithmically down-bin an FFT magnitude spectrum into n_bands."""
    if fmax is None:
        fmax = sr * 0.5
    freqs = np.arange(spec.shape[0], dtype=np.float32) * (sr / float(fft_size))
    edges = np.geomspace(max(1e-3, fmin), max(fmin * 1.001, fmax), num=n_bands + 1).astype(np.float32)
    out = np.zeros(n_bands, dtype=np.float32)
    centers = np.sqrt(edges[:-1] * edges[1:])
    for i in range(n_bands):
        lo, hi = edges[i], edges[i + 1]
        m = (freqs >= lo) & (freqs < hi)
        if not np.any(m):
            out[i] = 0.0
            continue
        s = spec[m]
        out[i] = float(np.mean(s) if centers[i] < split_hz else np.percentile(s, p))
    return out, centers


def tilt_gentle(bands, centers, pivot_hz=1000.0, power=0.25, cap=3.0):
    """Simple high-frequency tilt to compensate human loudness perception."""
    tilt = (np.maximum(centers, 1.0) / pivot_hz) ** power
    return np.clip(bands * tilt, 0.0, np.max(bands) * cap + 1e-6)


def apply_envelope_agc(raw, env, peak, release, attack, decay, rise, epsilon):
    """Attack/decay envelope and auto-gain compression."""
    env = np.maximum(raw, env * release)
    env = attack * env + (1.0 - attack) * raw
    above = env > peak
    peak[above] += rise * (env[above] - peak[above])
    peak[~above] *= decay
    levels = np.clip(env / (peak + epsilon), 0.0, 1.0)
    return levels, env, peak


def gamma_and_gain(levels, gain, gamma):
    """Apply user gain and gamma correction."""
    scaled = np.clip(levels * float(gain), 0.0, 1.0)
    return np.power(scaled, float(gamma), dtype=np.float32) if gamma != 1.0 else scaled


# ---------- visualisation modes ----------
def bounce(levels, t, state):  # default, simple passthrough
    return levels


def aurora_mode(levels, t, state):  # reserved for later
    return levels


def tails(levels, t, state):
    """Leave fading trails behind bright spikes."""
    trail = state.setdefault("trail", np.zeros_like(levels))
    trail *= 0.92
    np.maximum(trail, levels, out=trail)
    return trail


def illusion(levels, t, state):
    """Persistent peak overlay effect."""
    peaks = state.setdefault("peaks", np.zeros_like(levels))
    up = levels > peaks
    peaks[up] += 0.15 * (levels[up] - peaks[up])
    peaks[~up] *= 0.96
    return np.maximum(levels, peaks)


MODES = {0: bounce, 1: aurora_mode, 2: tails, 3: illusion}


# ---------- colour helpers ----------
def _read_ultra_rgba():
    """Read the four /theme/ultra/* RGBA colours from Synapse."""
    def g(key):
        arr = bus.get_array(key, length=4, dtype="f32")
        return np.array(arr[:4] if arr and len(arr) >= 4 else [1.0, 1.0, 1.0, 1.0], dtype=np.float32)
    return (
        g("/theme/ultra/tl_rgba"),
        g("/theme/ultra/tr_rgba"),
        g("/theme/ultra/bl_rgba"),
        g("/theme/ultra/br_rgba"),
    )


def _sort_by_luma(rgb3x):
    """Sort colours darkest→brightest by Rec.709 luminance."""
    luma = rgb3x @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    return rgb3x[np.argsort(luma)]


def write_leds_0to100(values_0_to_1, output_key):
    """Publish simple 0..100 brightness list for debugging."""
    vals = np.rint(np.clip(values_0_to_1, 0.0, 1.0) * 100.0).astype(np.uint8)
    bus.set_array(output_key, vals, "u8")
    return vals


def apply_quartile_palette(bright_0_to_1, rgba_tl, rgba_tr, rgba_bl, rgba_br, out_rgb_key):
    """Map brightness values 0..1 into 4 colour tiers (darkest→brightest)."""
    palette = np.stack([rgba_tl[:3], rgba_tr[:3], rgba_bl[:3], rgba_br[:3]], axis=0).astype(np.float32)
    palette = _sort_by_luma(palette)
    pct = np.minimum((bright_0_to_1 * 100.0).astype(np.int32), 99)
    tier = pct // 25
    cols = palette[tier]
    rgb = np.clip(cols * bright_0_to_1[:, None] * 255.0, 0, 255).astype(np.uint8)
    if out_rgb_key:
        bus.set_array(out_rgb_key, rgb.ravel(), "u8")
    return rgb


def heartbeat(name: str):
    """Heartbeat for FoundryCore to watch."""
    bus.set_float(f"/proc/{name}/heartbeat", time.time())


# ---------- main loop ----------
def main():
    cfg = load_cfg()
    proc = str(cfg["proc_name"])
    leds = int(cfg["led_count"])
    fps = int(cfg["fps"])
    release, attack, decay, rise = map(float, [cfg["release"], cfg["attack"], cfg["decay"], cfg["rise"]])
    epsilon, gamma = float(cfg["epsilon"]), float(cfg["gamma"])
    mode_key, gain_key, bright_key, out_key = (
        cfg["mode_key"],
        cfg["gain_key"],
        cfg["brightness_key"],
        cfg["output_key"],
    )

    signal(SIGINT, _stop)
    signal(SIGTERM, _stop)

    env = np.full(leds, epsilon, dtype=np.float32)
    peak = np.full(leds, 1e-6, dtype=np.float32)
    state = {}

    next_t = time.monotonic()
    last_hb = 0.0

    try:
        while not _should_stop:
            now = time.monotonic()
            if now < next_t:
                time.sleep(max(0.0, next_t - now))
            next_t += 1.0 / max(1, fps)

            bus.wait_consistent("/audio/seq", "/audio/seq2")

            bins = bus.get_int("/audio/bins", 4096)
            sr = bus.get_float("/audio/sr", 48000.0)
            fftn = bus.get_int("/audio/fft", 4096)
            spec = np.array(bus.get_array("/audio/spectrum", length=bins, dtype="f32"), dtype=np.float32)
            spec /= np.max(spec) + 1e-9

            bands, centers = downbin_log(spec, sr, fftn, leds, fmin=30.0, fmax=8000.0, p=90)
            bands = tilt_gentle(bands, centers)
            levels, env, peak = apply_envelope_agc(bands, env, peak, release, attack, decay, rise, epsilon)

            mode_idx = bus.get_int(mode_key, 0)
            gain = bus.get_float(gain_key, 1.0)
            bright = bus.get_float(bright_key, 1.0)

            levels = gamma_and_gain(levels, gain, gamma)
            levels = np.clip(MODES.get(mode_idx, bounce)(levels, now, state) * bright, 0.0, 1.0)

            write_leds_0to100(levels, out_key)

            tl, tr, bl, br = _read_ultra_rgba()
            apply_quartile_palette(levels, tl, tr, bl, br, out_rgb_key="/leds/main/output_rgb_u8")

            if now - last_hb > 1.0:
                heartbeat(proc)
                last_hb = now
    finally:
        bus.close_all()


if __name__ == "__main__":
    main()