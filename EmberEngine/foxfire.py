"""
foxfire.py
Audio spectrum → per-LED brightness → 4-tier palette colouring.

Reads:
  /audio/seq, /audio/seq2, /audio/bins, /audio/fft, /audio/sr, /audio/spectrum
  /theme/ultra/tl_rgba, /tr_rgba, /bl_rgba, /br_rgba
  /io/in/control/gain

Controls:
  /io/in/selector/source_selector        1=TV/radio, 2=none, 3=tape
  /io/in/selector/visualiser_mode_index  0=bounce, 1=stars, 2=tails, 3=illusion
  /io/in/control/gain                    semantic visualiser sensitivity, 0..1

Selector policy:
  The four-way selector updates the active animation only while source selector
  position 3 (Tape) is engaged. Leaving Tape keeps the last tape-selected
  animation running. This makes the source selector a safe "set mode" gate,
  rather than unexpectedly changing the strip whenever the four-way switch is
  moved for some future non-LED purpose.

Writes:
  /leds/main/frame_rgba  float32[LED_COUNT*4] RGBA frame
Heartbeat:
  /proc/foxfire/heartbeat
"""
import os, time, math, yaml, numpy as np
from pathlib import Path
from typing import Any
import synapse as bus
import signal
import inspect

from whisper_daemon import log_info, log_error, log_event, log_heartbeat

epsilon=1e-12
# ---------- defaults ----------
DEFAULTS = dict(
    proc_name="foxfire",
    led_count=40,
    fps=50,
    # Legacy/debug mirror. Foxfire publishes the resolved selector mode here.
    mode_key="/leds/main/mode",
    active_mode_key="/leds/main/active_mode",
    active_mode_seq_key="/leds/main/active_mode_seq",
    tape_mode_engaged_key="/leds/main/tape_mode_engaged",
    source_selector_key="/io/in/selector/source_selector",
    visualiser_mode_index_key="/io/in/selector/visualiser_mode_index",
    tape_source_position=3,
    default_mode=0,
    gain_key="/io/in/control/gain",
    frame_key="/leds/main/frame_rgba",
    gain_floor=0.08,
    gain_mid=1.00,
    gain_ceiling=2.00,
    release=0.99, attack=0.9, decay=0.999, rise=0.5,
    gamma=2.2,
    fmin_hz=10.0,fmax_hz=None, split_hz=1500.0,
    reducer="pctl", pctl_hi=90.0,
    pivot_hz=1000.0,
    tilt_pow=0.25, tilt_cap=3.0,
    fast_mix=0.25,
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

# ---------- DSP helpers ----------
def downbin_log(spec, sr, fft_size, n_bands, fmin=30.0, fmax=None,
                reducer_hi="pctl", p=90, split_hz=1500.0):
    """Handle the downbin log lifecycle step."""
    if fmax is None:
        fmax = sr * 0.5
    freqs = np.arange(spec.shape[0], dtype=np.float32) * (sr / float(fft_size))
    edges = np.geomspace(max(epsilon, fmin), max(fmin * 1.001, fmax), num=n_bands + 1).astype(np.float32)

    out = np.zeros(n_bands, dtype=np.float32)
    centers = np.sqrt(edges[:-1] * edges[1:])  # geometric center per band

    for i in range(n_bands):
        lo, hi = edges[i], edges[i + 1]
        m = (freqs >= lo) & (freqs < hi)
        if not np.any(m):
            out[i] = 0.0
            continue
        s = spec[m]
        if centers[i] < split_hz:
            out[i] = float(np.mean(s))
        else:
            if reducer_hi == "max":
                out[i] = float(np.max(s))
            elif reducer_hi == "rms":
                out[i] = float(np.sqrt(np.mean(s * s)))
            else:  # "pctl"
                out[i] = float(np.percentile(s, p))
    return out, centers

def tilt_gentle(bands, centers, pivot_hz=1000.0, power=0.25, cap=3.0):
    """Handle the tilt gentle lifecycle step."""
    tilt = (np.maximum(centers, 1.0) / pivot_hz) ** power
    tilt = np.clip(tilt, 1.0, cap)
    return bands * tilt


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
    """Apply visualiser gain and gamma correction."""
    scaled = np.clip(levels * float(gain), 0.0, 1.0)
    return np.power(scaled, float(gamma), dtype=np.float32) if gamma != 1.0 else scaled


def map_semantic_gain(control, floor=0.08, midpoint=1.0, ceiling=2.0):
    """Map physical 0..1 gain so 0.5 preserves the old normal level."""
    control = float(np.clip(control, 0.0, 1.0))
    floor = max(0.0, float(floor))
    midpoint = max(floor, float(midpoint))
    ceiling = max(midpoint, float(ceiling))
    if control <= 0.5:
        return floor + (control / 0.5) * (midpoint - floor)
    return midpoint + ((control - 0.5) / 0.5) * (ceiling - midpoint)


# ---------- visualisation modes ----------
def bounce(levels, t, state):
    """
    Simple passthrough: no effect.
    levels : np.ndarray of band levels
    t      : time in seconds (unused here)
    state  : dict for per-mode state (unused)
    """
    return levels


def aurora(levels, t, state):
    """
    Starlane: suggestive starfield shimmer.
    - Stars have lifetimes and gentle fade-out.
    - Limited clustering: no big clumps.
    - Music doesn't spawn new chaos; it 'twinkles' existing stars.
    """
    lv = np.asarray(levels, dtype=np.float32)
    n = lv.size
    if n == 0:
        return lv

    if state is None:
        state = {}

    # --- persistent starfield state ---
    bright = state.get("star_brightness")
    life   = state.get("star_life")
    phase  = state.get("star_phase")

    if bright is None or bright.size != n:
        bright = np.zeros(n, dtype=np.float32)
    if life is None or life.size != n:
        life = np.zeros(n, dtype=np.float32)
    if phase is None or phase.size != n:
        # random per-star phase for subtle per-star twinkle offset
        phase = np.random.uniform(0, 2*np.pi, size=n).astype(np.float32)

    # --- time delta ---
    last_t = state.get("last_t", t)
    dt = max(0.0, t - last_t)
    state["last_t"] = t

    # --- parameters you can tune later ---
    min_life   = 2.0   # seconds
    max_life   = 6.0   # seconds
    fade_rate  = 0.35  # how quickly brightness decays over life
    max_active_frac = 0.30  # max fraction of LEDs that can be 'stars'
    cluster_radius  = 1      # don't spawn right next to another star
    base_spawn_prob = 0.05   # baseline chance per frame to spawn a star
    music_spawn_boost = 0.15 # extra spawn chance scaled by energy
    twinkle_strength = 0.20  # how hard music can twinkle existing stars
    twinkle_speed    = 0.35  # how fast the per-star phase moves

    # --- existing stars: age & fade ---
    active = life > 0.0
    # age stars
    life[active] -= dt
    # exponential-ish fade
    bright[active] *= np.exp(-fade_rate * dt)

    # any dead stars fully clear
    dead = life <= 0.0
    bright[dead] = 0.0
    life[dead]   = 0.0

    # --- music energy drives twinkle and spawn bias ---
    if np.any(lv):
        energy = float(np.clip(np.mean(lv), 0.0, 1.0))
    else:
        energy = 0.0

    # --- twinkle existing stars instead of dancing them ---
    # advance per-star phase very slowly
    phase += twinkle_speed * dt
    state["star_phase"] = phase

    # soft twinkle factor in [0.9, 1.1]
    twinkle_wave = 1.0 + 0.1 * np.sin(phase)
    # music adds an extra small boost, but only for already-active stars
    music_twinkle = 1.0 + twinkle_strength * energy

    bright[active] *= twinkle_wave[active] * music_twinkle
    # clamp everything to sane range
    np.clip(bright, 0.0, 1.0, out=bright)

    # --- spawn new stars with lifetime + cluster rules ---
    active_count = int(np.count_nonzero(life > 0.0))
    max_active   = int(max_active_frac * n)

    if active_count < max_active and n > 0:
        # spawn chance per frame, gently biased by music
        spawn_prob = base_spawn_prob + music_spawn_boost * energy
        if np.random.rand() < spawn_prob:
            # pick from currently empty slots
            candidates = np.flatnonzero(life <= 0.0)
            if candidates.size > 0:
                # try up to a few times to find a non-clustered spot
                for _ in range(5):
                    idx = int(np.random.choice(candidates))
                    lo = max(0, idx - cluster_radius)
                    hi = min(n, idx + cluster_radius + 1)
                    # enforce no immediate neighbours as stars
                    if not np.any(life[lo:hi] > 0.0):
                        # spawn new star here
                        life[idx] = np.random.uniform(min_life, max_life)
                        bright[idx] = np.random.uniform(0.4, 0.9)
                        break

    # --- final output ---
    # base shimmer from stars
    out = bright.copy()

    # very subtle spectral bed so it doesn't feel totally disconnected
    bed = lv * 0.15
    out = np.maximum(out, bed)

    state["star_brightness"] = bright
    state["star_life"]       = life

    return out


def tails(levels, t, state):
    """
    Center-out Tails:
    - Pulse starts at center LED(s)
    - Two independent tails propagate outward left & right
    - Brightness based on loudness (max level)
    """

    lv = np.asarray(levels, dtype=np.float32)
    n = lv.size
    if n == 0:
        return lv

    if state is None:
        state = {}

    # Calculate center index
    mid = n // 2

    # Two tail buffers: left and right
    left_buf = state.get("tails_left")
    right_buf = state.get("tails_right")

    if left_buf is None or left_buf.size != n:
        left_buf = np.zeros(n, dtype=np.float32)
    if right_buf is None or right_buf.size != n:
        right_buf = np.zeros(n, dtype=np.float32)

    # Loudness drives the tail head
    loud = float(np.max(lv)) if n > 0 else 0.0

    # Decay rate
    decay = state.get("tails_decay", 0.85)   # smoother than 0.92 for centre bloom

    # --- DECAY EXISTING ---
    left_buf *= decay
    right_buf *= decay

    # --- SHIFT LEFT TAIL OUTWARDS ---
    # left side goes: mid → mid-1 → mid-2 → ... → 0
    for i in range(mid - 1, -1, -1):
        left_buf[i] = max(left_buf[i], left_buf[i + 1] * 0.98)

    # --- SHIFT RIGHT TAIL OUTWARDS ---
    for i in range(mid + 1, n):
        right_buf[i] = max(right_buf[i], right_buf[i - 1] * 0.98)

    # --- INJECT NEW HEAD AT CENTER ---
    # If even number of LEDs, bloom from two central LEDs
    if n % 2 == 0:
        left_buf[mid - 1] = max(left_buf[mid - 1], loud)
        right_buf[mid]    = max(right_buf[mid], loud)
    else:
        left_buf[mid]  = max(left_buf[mid], loud)
        right_buf[mid] = max(right_buf[mid], loud)

    # --- MERGE TAILS + OPTIONAL LEVEL FAINT BED ---
    out = np.maximum(left_buf, right_buf)

    # Add a gentle layer of the real spectrum so it never looks disconnected
    out = np.maximum(out, lv * 0.2)

    state["tails_left"] = left_buf
    state["tails_right"] = right_buf

    return out


def illusion(levels, t, state):
    """
    Ripple: alive, water-like travelling wave.

    - Primary big wave travelling across the strip
    - Secondary finer surface ripples
    - Speed and intensity modulated by a smoothed loudness envelope
    - A fading 'tail' so patterns persist briefly instead of being brutally frame-based
    """
    lv = np.asarray(levels, dtype=np.float32)
    n = lv.size
    if n == 0:
        return lv

    if state is None:
        state = {}

    # --- state init ---
    phase = state.get("phase", 0.0)              # main wave phase
    surf_phase = state.get("surf_phase")         # per-band phase for surface detail
    if surf_phase is None or surf_phase.size != n:
        surf_phase = np.random.uniform(0, 2*np.pi, size=n).astype(np.float32)

    env = state.get("env", 0.0)                  # smoothed loudness envelope
    tail = state.get("tail")
    if tail is None or tail.size != n:
        tail = np.zeros(n, dtype=np.float32)

    last_t = state.get("last_t", t)
    dt = max(0.0, t - last_t)

    # --- loudness envelope (slow smoothed) ---
    loud = float(np.max(lv)) if n > 0 else 0.0
    env = env + 0.25 * (loud - env)             # 0.25 controls envelope responsiveness

    # --- phase evolution ---
    base_speed = 2.2                            # baseline travel speed
    phase += base_speed * (0.6 + env) * dt      # faster when louder, but never zero

    surf_speed = 0.7
    surf_phase += surf_speed * dt               # slower evolution for fine ripples

    # --- build waves ---
    idx = np.linspace(0.0, 1.0, n, dtype=np.float32)

    # Primary broad wave
    primary_k = 3.0                             # spatial frequency
    primary = 0.5 + 0.5 * np.sin(idx * primary_k - phase)

    # Secondary finer surface ripple
    surface_k = 9.0
    surface = 0.5 + 0.5 * np.sin(idx * surface_k + surf_phase)

    # Mix them into a single pattern
    wave = primary * 0.7 + surface * 0.3

    # Modulate by envelope: quiet = subtle, loud = fuller
    wave *= (0.25 + 0.75 * env)

    # --- persistence tail ---
    tail_decay = 0.85
    tail *= tail_decay
    tail = np.maximum(tail, wave)

    # --- blend with real spectrum so it still "belongs" to the audio ---
    out = np.maximum(lv * 0.45, tail)

    # --- store state ---
    state["phase"] = phase
    state["surf_phase"] = surf_phase
    state["env"] = env
    state["tail"] = out.copy()
    state["last_t"] = t

    return out


MODES = {0: bounce, 1: aurora, 2: tails, 3: illusion}


def valid_mode(value: object, fallback: int = 0) -> int:
    """Return one of Foxfire's four stable animation indices."""
    try:
        mode = int(value)
    except (TypeError, ValueError):
        mode = int(fallback)
    return mode if mode in MODES else (fallback if fallback in MODES else 0)


def resolve_selector_mode(
    source_selector: int,
    selector_mode_index: int,
    last_tape_mode: int,
    tape_source_position: int,
) -> tuple[int, bool]:
    """Resolve the animation without letting non-Tape sources alter it.

    Mapping is intentionally direct:
      4-way 1 -> Bounce  (index 0)
      4-way 2 -> Stars   (index 1)
      4-way 3 -> Tails   (index 2)
      4-way 4 -> Illusion(index 3)

    Outside Tape, return the remembered tape mode unchanged.
    """
    if int(source_selector) == int(tape_source_position) and int(selector_mode_index) in MODES:
        return int(selector_mode_index), True
    return valid_mode(last_tape_mode), False


# ---------- colour helpers ----------
def _read_ultra_rgba():
    """Read the four /theme/ultra/* RGBA colours from Synapse."""
    def g(key):
       """Evaluate the local interpolation curve."""
       arr = bus.try_get_array(key, length=4, dtype="f32")
       if arr is None:
           return None
       return np.array(arr[:4] if arr and len(arr) >= 4 else [1.0, 1.0, 1.0, 1.0], dtype=np.float32)
    arr = (
        g("/theme/ultra/tl_rgba"),
        g("/theme/ultra/tr_rgba"),
        g("/theme/ultra/bl_rgba"),
        g("/theme/ultra/br_rgba"),
    )
    return arr


def _sort_by_luma(rgb3x):
    """Sort colours darkest→brightest by Rec.709 luminance."""
    luma = rgb3x @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    return rgb3x[np.argsort(luma)]


def write_leds_0to100(values_0_to_1, output_key):
    """Publish simple 0..100 brightness list for debugging."""
    vals = np.rint(np.clip(values_0_to_1, 0.0, 1.0) * 100.0).astype(np.uint8)
    bus.set_array(output_key, vals, "u8")
    return vals


def apply_quartile_palette(bright_0_to_1, rgba_tl, rgba_tr, rgba_bl, rgba_br, out_rgba_key):
    """
    Map brightness values (0..1) into 4 palette tiers (dark→bright)
    and output RGBA float32 [N,4].

    - R,G,B come from whichever palette tier the LED falls into.
    - A = the brightness (0..1).
    """

    # Ensure float32
    b = np.asarray(bright_0_to_1, dtype=np.float32).clip(0.0, 1.0)

    # Build palette, RGB only (ignore palette alpha)
    palette = np.stack(
        [rgba_tl[:3], rgba_tr[:3], rgba_bl[:3], rgba_br[:3]],
        axis=0
    ).astype(np.float32)

    # Sort palette by perceived luma, darkest → brightest
    palette = _sort_by_luma(palette)

    # Determine tier 0..3 from brightness
    pct = np.minimum((b * 100.0).astype(np.int32), 99)
    tier = pct // 25  # integer division

    # Select RGB for each pixel. Brightness lives in alpha and is applied by
    # willo_wisp during RGB→RGBW conversion, so modulating RGB here as well
    # would square the level and make the strip needlessly dim.
    rgb = palette[tier]                  # shape [N,3]

    # Alpha = brightness
    alpha = b[:, None]                   # shape [N,1]

    # RGBA float output
    rgba = np.concatenate([rgb, alpha], axis=1).astype(np.float32)  # [N,4]

    # Write to bus if requested
    if out_rgba_key:
        bus.set_array(out_rgba_key, rgba.ravel(), "f32")

    return rgba


def heartbeat(name: str):
    """Publish the component heartbeat and diagnostic event."""
    now_ms = int(time.monotonic() * 1000)           # single clock domain
    bus.set_int(f"/proc/{name}/heartbeat_ms", now_ms)
    bus.set_int(f"/proc/{name}/hb_seq", bus.get_int(f"/proc/{name}/hb_seq", 0) + 1)
    log_heartbeat(name)

# ---------- main loop ----------
def main():
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    proc = str(cfg["proc_name"])
    leds = int(cfg["led_count"])
    fps = int(cfg["fps"])
    release, attack, decay, rise = map(float, [cfg["release"], cfg["attack"], cfg["decay"], cfg["rise"]])
    gamma = float(cfg["gamma"])
    mode_key = cfg["mode_key"]
    active_mode_key = cfg["active_mode_key"]
    active_mode_seq_key = cfg["active_mode_seq_key"]
    tape_mode_engaged_key = cfg["tape_mode_engaged_key"]
    source_selector_key = cfg["source_selector_key"]
    visualiser_mode_index_key = cfg["visualiser_mode_index_key"]
    tape_source_position = int(cfg.get("tape_source_position", 3))
    default_mode = valid_mode(cfg.get("default_mode", 0))
    gain_key = cfg["gain_key"]
    frame_key = cfg["frame_key"]
    gain_floor = float(cfg.get("gain_floor", 0.08))
    gain_mid = float(cfg.get("gain_mid", 1.00))
    gain_ceiling = float(cfg.get("gain_ceiling", 2.00))
    fmin_hz   = float(cfg.get("fmin_hz", 10.0))
    fmax_hz   = cfg.get("fmax_hz", None)
    fmax_hz   = None if fmax_hz in (None, "null") else float(fmax_hz)
    split_hz  = float(cfg.get("split_hz", 1500.0))
    reducer   = str(cfg.get("reducer_hi", "pctl"))
    pctl_hi   = float(cfg.get("pctl_hi", 90.0))
    pivot_hz  = float(cfg.get("pivot_hz", 1000.0))
    tilt_pow  = float(cfg.get("tilt_power", 0.25))
    tilt_cap  = float(cfg.get("tilt_cap", 3.0))
    fast_mix  = float(cfg.get("fast_mix", 0.25))

    log_info(proc, "started")
    
    # advertise to the hardware driver
    bus.set_int("/leds/main/led_count", leds)
    bus.set_float("/leds/main/fps", fps)
    
    log_event(proc, "init_led_busses", {"led_count": leds, "fps":fps})
    
    def on_stop(signum, frame):
        """Mark the component for an orderly shutdown."""
        nonlocal stopping
        stopping = True
        
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    stopping = False
    
    env = np.full(leds, epsilon, dtype=np.float32)
    peak = np.full(leds, epsilon, dtype=np.float32)
    state = {}

    # This is deliberately RAM-only. A restart simply begins at the configured
    # default until Tape is selected again, while ordinary source changes keep
    # the last tape-selected animation running.
    last_tape_mode = default_mode
    last_published_mode = None
    last_tape_engaged = None

    next_t = time.monotonic()
    last_hb = 0.0

    try:
        while not stopping:
            now = time.monotonic()
            if now < next_t:
                time.sleep(max(0.0, next_t - now))
            next_t += 1.0 / max(1, fps)

            if now - last_hb > 1.0:
                heartbeat(proc)
                last_hb = now
            
            bus.wait_consistent("/audio/seq", "/audio/seq2")

            bins = bus.get_int("/audio/bins", 4096)
            sr = bus.get_float("/audio/sr", 48000.0)
            fftn = bus.get_int("/audio/fft", 4096)
            spec_list = bus.try_get_array("/audio/spectrum", length=bins, dtype="f32")
            if spec_list is None:
                continue
            
            spec = np.array(spec_list, dtype=np.float32)

            bands, centers = downbin_log(spec, sr, fftn, leds,
                                         fmin=fmin_hz, fmax=fmax_hz,
                                         reducer_hi=reducer, p=pctl_hi, split_hz=split_hz)
            
            bands = tilt_gentle(bands, centers, pivot_hz=pivot_hz, power=tilt_pow, cap=tilt_cap)

            prev = state.setdefault("prev_bands", np.zeros_like(bands))
            delta = np.clip(bands - prev,0,None)
            state["prev_bands"] = bands.copy()

            levels, env, peak = apply_envelope_agc(bands, env, peak, release, attack, decay, rise, epsilon)

            source_selector = bus.get_int(source_selector_key, -1)
            selector_mode_index = bus.get_int(visualiser_mode_index_key, -1)
            mode_idx, tape_engaged = resolve_selector_mode(
                source_selector,
                selector_mode_index,
                last_tape_mode,
                tape_source_position,
            )
            if tape_engaged:
                last_tape_mode = mode_idx

            # Preserve the resolved mode for diagnostics and any future UI.
            # Updating only on change avoids needless shared-memory churn at 50 Hz.
            if mode_idx != last_published_mode:
                bus.set_int(mode_key, mode_idx)
                bus.set_int(active_mode_key, mode_idx)
                bus.set_int(active_mode_seq_key, bus.get_int(active_mode_seq_key, 0) + 1)
                last_published_mode = mode_idx
            if tape_engaged != last_tape_engaged:
                bus.set_int(tape_mode_engaged_key, int(tape_engaged))
                last_tape_engaged = tape_engaged

            gain_control = bus.get_float(gain_key, 0.5)
            gain = map_semantic_gain(gain_control, gain_floor, gain_mid, gain_ceiling)

            levels = gamma_and_gain(levels, gain, gamma)
            levels = np.clip(MODES.get(mode_idx, tails)(levels, now, state), 0.0, 1.0)

            if fast_mix > 0.0:
                # The onset boost obeys gain as well, so turning gain down
                # genuinely calms the visualiser instead of leaving bright spikes.
                d = np.clip((delta / (np.max(delta) + epsilon)) * gain, 0.0, 1.0)
                levels = np.clip((1.0 - fast_mix) * levels + fast_mix * d, 0.0, 1.0)

            tl, tr, bl, br = _read_ultra_rgba()
            if tl is None or tr is None or bl is None or br is None:
                continue

            # Aurora already owns hue, saturation and master brightness.
            # Frame alpha remains only the audio/pattern intensity lane.
            apply_quartile_palette(levels, tl, tr, bl, br, out_rgba_key=frame_key)

    finally:
        bus.close_all()
        log_info(proc, "stopped")


if __name__ == "__main__":
    main()
