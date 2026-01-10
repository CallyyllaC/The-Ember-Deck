"""
echo.py
Audio capture → spectrum via Synapse keys (per-variable shared memory).

Publishes (each frame):
  /audio/seq, /audio/spectrum (float32 array), /audio/seq2

Publishes (once / rarely):
  /audio/bins (int), /audio/fft (int), /audio/sr (float)

Heartbeat:
  /proc/echocore/heartbeat
"""

import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml
import synapse as bus  # per-variable shared memory helpers

# sounddevice is required on the target machine
import sounddevice as sd


# ---------- defaults ----------
DEFAULTS = dict(
    proc_name="echocore",   # heartbeat name → /proc/echocore/heartbeat
    sample_rate=48000,      # input sample rate
    channels=2,             # input channels; will be mixed to mono
    smoothness=1.0,         # controls block size (higher = larger blocks)
    fps=50,                 # output frames per second (publish rate)
    fft_size=4096,          # FFT size (power of two recommended)
    bins=4096,              # number of rFFT bins to publish (<= fft_size//2 + 1)
    device="default",       # sounddevice input "device" name/id
)


def load_cfg() -> dict[str, Any]:
    """Load YAML config for this process from CONFIG_PATH, overlay on DEFAULTS."""
    p = os.environ.get("CONFIG_PATH")
    cfg = {}
    if p and Path(p).exists():
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    out = DEFAULTS.copy()
    out.update(cfg)
    out["period"] = 1.0 / max(1, int(out["fps"]))
    return out


def init_audio(sample_rate: int, channels: int, block: int, device: str) -> sd.InputStream:
    """
    Create and start a sounddevice InputStream.
    - samplerate: Hz
    - channels: input channels (we mix to mono later)
    - dtype: float32 for simple numpy interop
    - blocksize: frames read per call to stream.read()
    """
    s = sd.InputStream(
        samplerate=sample_rate,
        channels=channels,
        dtype="float32",
        blocksize=block,
        device=device,
    )
    s.start()
    return s


def read_exact_mono(stream: sd.InputStream, n: int, block: int, channels: int) -> np.ndarray:
    """
    Read at least n mono samples from the stream, using block as a minimum chunk.
    - Mixes first two channels to mono if stereo is available, else squeezes.
    - Pads with zeros if short.
    """
    out = np.empty(n, dtype=np.float32)
    got = 0
    while got < n:
        need = n - got
        frames = max(block, need)
        data, _ = stream.read(frames)        # data shape: (frames, channels) float32
        if data.size == 0:
            break
        if data.ndim == 2 and data.shape[1] >= 2:
            mono = data[:, 0:2].mean(axis=1, dtype=np.float32)
        else:
            mono = data.squeeze()
        take = min(need, mono.shape[0])
        out[got:got + take] = mono[:take]
        got += take
    if got < n:
        out[got:] = 0.0
    return out


def heartbeat(name: str) -> None:
    """Publish a float epoch timestamp for FoundryCore to watch."""
    bus.set_float(f"/proc/{name}/heartbeat", time.time())


def main() -> None:
    cfg = load_cfg()

    sr: int = int(cfg["sample_rate"])
    ch: int = int(cfg["channels"])
    fps: int = int(cfg["fps"])
    fftn: int = int(cfg["fft_size"])
    bins: int = int(cfg["bins"])
    dev: str = str(cfg["device"])
    proc: str = str(cfg["proc_name"])

    # Publish static meta so readers know what to expect
    bus.set_int("/audio/bins", bins)
    bus.set_int("/audio/fft", fftn)
    bus.set_float("/audio/sr", float(sr))

    # Derive a sensible block size from "smoothness"
    block = int(1024 * max(0.25, float(cfg["smoothness"])))
    stream = init_audio(sr, ch, block, dev)

    next_t = time.monotonic()
    last_hb = 0.0
    seq = 0

    try:
        while True:
            # Simple frame pacing
            now = time.monotonic()
            if now < next_t:
                time.sleep(max(0.0, next_t - now))
            next_t += (1.0 / max(1, fps))

            # Read exactly fftn mono samples
            frame = read_exact_mono(stream, fftn, block, ch)

            # rFFT magnitude (float32)
            spec = np.abs(np.fft.rfft(frame, n=fftn)).astype(np.float32)
            # Ensure published length matches /audio/bins
            if spec.shape[0] != bins:
                if spec.shape[0] > bins:
                    spec = spec[:bins]
                else:
                    spec = np.pad(spec, (0, bins - spec.shape[0]))

            # Double-stamp protocol for a consistent frame
            seq = (seq + 1) & 0x7FFFFFFF
            bus.set_int("/audio/seq", seq)                  # begin frame
            bus.set_array("/audio/spectrum", spec, "f32")   # payload
            bus.set_int("/audio/seq2", seq)                 # end frame

            # Heartbeat once per second
            if now - last_hb > 1.0:
                heartbeat(proc)
                last_hb = now

    except KeyboardInterrupt:
        pass
    finally:
        # Stop audio and release SHM handles
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        bus.close_all()


if __name__ == "__main__":
    main()