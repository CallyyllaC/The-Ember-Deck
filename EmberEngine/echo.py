"""
echo.py
Audio capture → spectrum via Synapse keys (per-variable shared memory).

Publishes (each frame):
  /audio/seq, /audio/spectrum (float32 array), /audio/seq2

Publishes (once / rarely):
  /audio/bins (int), /audio/fft (int), /audio/sr (float)

Heartbeat:
  /proc/<proc_name>/heartbeat_ms, /proc/<proc_name>/hb_seq
"""

import os
import time
import signal
from pathlib import Path
from typing import Any

import numpy as np
import yaml
import sounddevice as sd

import synapse as bus  # per-variable shared memory helpers

from whisper_daemon import log_info, log_error, log_event, log_heartbeat

# ---------- defaults ----------

DEFAULTS = dict(
    proc_name="echo",       # heartbeat name
    sample_rate=48000,      # input sample rate
    channels=2,             # input channels; mixed to mono
    smoothness=0.25,        # controls block size (higher = larger blocks)
    fps=50,                 # publish rate (frames per second)
    fft_size=2048,          # FFT size (power of two recommended)
    bins=2048,              # number of rFFT bins to publish (<= fft_size//2 + 1)
    device="default",       # sounddevice input "device" name/id
    # UI scope feed. This is a decimated time-domain window, independent of
    # the FFT spectrum used by Foxfire.
    waveform_samples=160,
    waveform_key="/audio/waveform",
)


def load_cfg() -> dict[str, Any]:
    """
    Load YAML config for this process from CONFIG_PATH and overlay on DEFAULTS.
    Also derive 'period' from fps.
    """
    cfg_path = os.environ.get("CONFIG_PATH")
    cfg: dict[str, Any] = {}
    if cfg_path and Path(cfg_path).exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    out = DEFAULTS.copy()
    out.update(cfg)
    out["period"] = 1.0 / max(1, int(out["fps"]))
    out["waveform_samples"] = max(16, int(out.get("waveform_samples", 160)))
    return out


def decimate_waveform(samples: np.ndarray, output_samples: int) -> np.ndarray:
    """Return a small evenly-spaced time-domain window for the UI scope."""
    count = max(16, int(output_samples))
    source = np.asarray(samples, dtype=np.float32).reshape(-1)
    if source.size == 0:
        return np.zeros(count, dtype=np.float32)
    if source.size == count:
        return source.copy()
    x_old = np.linspace(0.0, 1.0, source.size, dtype=np.float32)
    x_new = np.linspace(0.0, 1.0, count, dtype=np.float32)
    return np.interp(x_new, x_old, source).astype(np.float32)

def mix_to_mono(x: np.ndarray) -> np.ndarray:
    """
    Mix multi-channel audio down to mono float32.
    """
    if x.ndim == 2 and x.shape[1] >= 2:
        return x[:, 0:2].mean(axis=1).astype(np.float32)
    return x.astype(np.float32)


def heartbeat(name: str) -> None:
    """
    Publish a heartbeat in Synapse for FoundryCore.
    """
    now_ms = int(time.monotonic() * 1000)
    key_base = f"/proc/{name}"
    bus.set_int(f"{key_base}/heartbeat_ms", now_ms)
    seq_key = f"{key_base}/hb_seq"
    bus.set_int(seq_key, bus.get_int(seq_key, 0) + 1)
    log_heartbeat(name)


def main() -> None:
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    sr: int = int(cfg["sample_rate"])
    ch: int = int(cfg["channels"])
    fps: int = int(cfg["fps"])
    fftn: int = int(cfg["fft_size"])
    requested_bins = cfg.get("bins")
    native_bins = fftn // 2 + 1
    # rFFT returns exactly fft_size//2 + 1 real bins. Publishing any other
    # length used to pad zeros into the upper spectrum, which distorted the
    # LED frequency mapping. Keep the bus truthful and self-consistent.
    if requested_bins in (None, "", 0):
        bins = native_bins
    else:
        bins = int(requested_bins)
        if bins != native_bins:
            print(f"[echo] FFT bins corrected: requested={bins}, "
            f"native={native_bins}, fft_size={fftn}",
            flush=True,)
            bins = native_bins
    dev: str = str(cfg["device"])
    proc: str = str(cfg["proc_name"])
    waveform_samples = int(cfg.get("waveform_samples", 160))
    waveform_key = str(cfg.get("waveform_key", "/audio/waveform"))
    
    period = 1.0 / max(1, fps)

    log_info(proc, "started")
    
    # rolling buffer for latest audio window
    buf = np.zeros(fftn, dtype=np.float32)
    window = np.hanning(fftn).astype(np.float32)

    # Shared state between callback and main loop
    state = {
        "buf": buf,
        "fft_size": fftn,
        "have_audio": False,
    }

    # ---- publish static meta once ----
    bus.set_int("/audio/bins", bins)
    bus.set_int("/audio/fft", fftn)
    bus.set_float("/audio/sr", float(sr))

    # initialise spectrum and seq
    bus.set_array("/audio/spectrum", np.zeros(bins, dtype=np.float32), "f32")
    bus.set_array(waveform_key, np.zeros(waveform_samples, dtype=np.float32), "f32")
    bus.set_int("/audio/seq", 0)
    bus.set_int("/audio/seq2", 0)
    
    log_event(proc, "init_audio_busses", {"bins":bins, "fftn":fftn, "sr":sr})

    # ---- audio callback ----

    def audio_callback(indata, frames, time_info, status):
        """
        Pipe new audio samples into the rolling buffer.
        This runs in sounddevice's internal thread; keep it light.
        """
        if status:
            # Optional: print or track overruns/underruns
            #print("[echo] audio status:", status)
            pass

        mono = mix_to_mono(indata)
        n = mono.shape[0]

        # roll in-place on the shared buffer
        b = state["buf"]
        if n >= b.shape[0]:
            b[:] = mono[-b.shape[0]:]
        else:
            b[:-n] = b[n:]
            b[-n:] = mono

        state["have_audio"] = True

    # derive blocksize from smoothness; smaller block = lower latency, more CPU
    blocksize = int(1024 * max(0.25, float(cfg["smoothness"])))

    stream = sd.InputStream(
        samplerate=sr,
        channels=ch,
        dtype="float32",
        blocksize=blocksize,
        device=dev,
        callback=audio_callback,
    )
    
    
    log_event(proc, "audio_stream_connected", {"sr":sr, "channels":ch, "blocksize":blocksize, "device":dev})

    stopping = False

    def on_stop(signum, frame):
        """Mark the component for an orderly shutdown."""
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    seq = 0
    last_hb = 0.0
    next_pub = time.monotonic()

    try:
        stream.start()
        log_info(proc, "audio_stream_started")

        while not stopping:
            now = time.monotonic()

            # publish spectrum at fixed FPS
            if now >= next_pub:
                next_pub += period

                if state["have_audio"]:
                    # snapshot current buffer so callback can keep writing
                    buf_snap = state["buf"].copy()
                else:
                    buf_snap = np.zeros_like(buf)

                windowed = buf_snap * window
                spec = np.abs(np.fft.rfft(windowed, n=fftn)).astype(np.float32)
                waveform = decimate_waveform(buf_snap, waveform_samples)

                # bins is kept equal to rFFT's native output length above.
                # Do not pad or truncate here: both alter the frequency axis.

                seq = (seq + 1) & 0x7FFFFFFF
                bus.set_int("/audio/seq", seq)
                bus.set_array("/audio/spectrum", spec, "f32")
                bus.set_array(waveform_key, waveform, "f32")
                bus.set_int("/audio/seq2", seq)

            # heartbeat ~1 Hz
            if now - last_hb > 1.0:
                heartbeat(proc)
                last_hb = now

            # small sleep to avoid busy loop
            time.sleep(0.001)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        bus.close_all()
        log_info(proc, "stopped")


if __name__ == "__main__":
    main()
