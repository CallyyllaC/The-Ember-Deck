#!/usr/bin/env python3
"""
BlinkStick Pro RGBW LED driver for EmberEngine.

- Uses Synapse exclusively for runtime data:
    /leds/main/led_count    : int
    /leds/main/fps          : float
    /leds/main/frame_rgba  : float32 array [LED_COUNT * 4] (r,g,b,a in 0..1)

- Config (via CONFIG_PATH, YAML) only covers hardware/power:
    proc_name, pixel_max_ma, usb_current_ma, external_power

- Runs a loop ~FPS, pulls RGBA from Synapse, converts to RGBW, and sends to
  BlinkStick Pro as GRBW bytes using BlinkstickProRGBW.
"""

import os
import sys
import time
import signal

import yaml
import numpy as np
from typing import Any
from pathlib import Path

import synapse as bus  # shared memory helper
from blinkstick_rgbw import BlinkstickProRGBW

from whisper_daemon import log_info, log_error, log_event, log_heartbeat
from runtime_config import ConfigError, require_mapping, validate_willo_wisp
# ----------------- keys ----------------- #

LED_COUNT_KEY   = "/leds/main/led_count"
LED_FPS_KEY     = "/leds/main/fps"
LED_FRAME_KEY   = "/leds/main/frame_rgba"      # float32 [LED_COUNT * 4]

# ----------------- config ----------------- #
DEFAULTS = dict(
    proc_name="willo_wisp",
    pixel_max_ma=50,
    usb_current_ma=400,
    external_power=False,
    data_channel=0,
)
    
def load_cfg() -> dict[str, Any]:
    """Load YAML config for this instance."""
    p = os.environ.get("CONFIG_PATH")
    cfg = {}
    if p and Path(p).exists():
        with open(p, "r", encoding="utf-8") as f:
            cfg = require_mapping("willo_wisp", yaml.safe_load(f))
    out = DEFAULTS.copy()
    out.update(cfg)
    validate_willo_wisp(out)
    return out

# ----------------- RGBA -> RGBW ----------------- #

def rgba_to_rgbw(rgba: np.ndarray) -> np.ndarray:
    """
    Convert RGBA [N,4] floats 0..1 into RGBW [N,4] uint8 0..255.

    - A is treated as per-pixel brightness multiplier.
    - White channel is extracted as min(R,G,B) so neutral parts go into W.
    - Remaining colour is kept in RGB for saturation.
    """
    if rgba.size == 0:
        return np.zeros((0, 4), dtype=np.uint8)

    rgba = np.asarray(rgba, dtype=np.float32)
    rgb = np.clip(rgba[:, 0:3], 0.0, 1.0)        # [N,3]
    alpha = np.clip(rgba[:, 3:4], 0.0, 1.0)      # [N,1]

    # Apply per-pixel brightness from alpha
    rgb = rgb * alpha

    # Extract shared white component
    w = np.min(rgb, axis=1, keepdims=True)       # [N,1]

    # Remove white from RGB to preserve chroma
    rgb_wo_white = np.clip(rgb - w, 0.0, 1.0)
    w = np.clip(w, 0.0, 1.0)

    out = np.concatenate([rgb_wo_white, w], axis=1)  # [N,4]
    out = np.rint(out * 255.0).astype(np.uint8)
    return out


# ----------------- led config via Synapse ----------------- #

def wait_for_led_config(proc_name, should_stop=lambda: False, heartbeat=None):
    """
    Block until LED_COUNT and FPS appear in Synapse.
    Returns (led_count, fps), or None if a stop was requested first.

    The wait keeps heartbeating so Foundry does not restart the whole group
    merely because Foxfire has not advertised its strip yet, and it honours
    SIGTERM instead of waiting for the force-kill.
    """
    log_info(proc_name, "led_waiting")
    heartbeat = bus.Heartbeat(proc_name, on_beat=log_heartbeat) if heartbeat is None else heartbeat
    while not should_stop():
        led_count = bus.get_int(LED_COUNT_KEY, 0)
        fps       = bus.get_float(LED_FPS_KEY, 0.0)

        if led_count > 0 and fps > 0.0:
            return led_count, fps

        heartbeat.tick()
        time.sleep(0.1)
    return None

# ----------------- main loop ----------------- #

def main():
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    proc_name = str(cfg.get("proc_name", "led_driver"))
    log_info(proc_name, "started")
    
    stopping = False

    def on_stop(signum, frame):
        """Mark the component for an orderly shutdown."""
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    strip = None
    heartbeat = bus.Heartbeat(proc_name, on_beat=log_heartbeat)

    try:
        led_config = wait_for_led_config(proc_name, lambda: stopping, heartbeat)
        if led_config is None:
            return
        led_count, fps = led_config

        pixel_max_ma = float(cfg.get("pixel_max_ma", 50.0))
        usb_ma       = float(cfg.get("usb_current_ma", 400.0))
        external     = bool(cfg.get("external_power", False))
        data_channel = int(cfg.get("data_channel", 0))

        # If externally powered, the BlinkStick's USB connection is data-only
        # and must not dim the separately powered strip.
        usb_ma_for_strip = 1e9 if external else usb_ma

        strip = BlinkstickProRGBW(
            led_count=led_count,
            fps=fps,
            usb_ma_budget=usb_ma_for_strip,
            pixel_max_ma=pixel_max_ma,
            data_channel=data_channel,
        )
        connected = False
        connection_fault_active = False
        while not connected and not stopping:
            time.sleep(1)
            try:
                connected = strip.connect()
            except Exception as exc:
                connected = False
                if not connection_fault_active:
                    log_error(proc_name, "BlinkStick connection failed", str(exc))
            if not connected and not connection_fault_active:
                log_error(proc_name, "BlinkStick unavailable", "USB controller not found")
            connection_fault_active = not connected
            bus.set_int("/io/health/led_strip_ok", int(connected))
            time.sleep(1)
            heartbeat.tick()
        if stopping:
            return

        strip.set_brightness(1.0)

        log_event(proc_name, "led_connected", {
            "led_count": led_count,
            "fps": fps,
            "usb_ma": usb_ma,
            "pixel_max_ma": pixel_max_ma,
            "external_power": external,
            "data_channel": data_channel,
        })

        frame_len = led_count * 4
        frame_interval = 1.0 / fps

        while not stopping:
            t0 = time.monotonic()

            # Heartbeat before any "no frame yet" skip below; otherwise a
            # missing Foxfire frame would starve the heartbeat and restart the group.
            heartbeat.tick(t0)

            # get RGBA frame from synapse
            arr = bus.try_get_array(LED_FRAME_KEY, length=frame_len, dtype="f32")
            if arr is None or len(arr) < frame_len:
                time.sleep(0.002)
                continue

            rgba = np.asarray(arr, dtype=np.float32).reshape(-1, 4)
            if rgba.shape[0] != led_count:
                # broken or mismatched frame, skip
                time.sleep(0.002)
                continue

            # Convert RGBA (0..1) to RGBW uint8 (0..255)
            rgbw = rgba_to_rgbw(rgba)

            # Push frame into local buffer
            for idx in range(led_count):
                r, g, b, w = map(int, rgbw[idx])
                strip.set_pixel(idx, (r, g, b, w))

            try:
                strip.show()
            except Exception as exc:
                bus.set_int("/io/health/led_strip_ok", 0)
                log_error(proc_name, "BlinkStick frame write failed", str(exc))
                strip.close()
                connected = False
                while not connected and not stopping:
                    time.sleep(1)
                    try:
                        connected = strip.connect()
                    except Exception:
                        connected = False
                    heartbeat.tick()
                bus.set_int("/io/health/led_strip_ok", int(connected))
                if stopping:
                    break
                log_event(proc_name, "led_reconnected", {"data_channel": data_channel})

            now = time.monotonic()

            # frame pacing – you can also just rely on strip.wait_for_next_frame()
            elapsed = now - t0
            sleep_for = frame_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    finally:
        bus.set_int("/io/health/led_strip_ok", 0)
        try:
            if strip is not None:
                strip.off()
                strip.close()
        except Exception as e:
            log_error(proc_name, "Error during led strip shutdown", str(e))
        try:
            bus.close_all()
        except Exception:
            pass        
        log_info(proc_name, "stopped")


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(2) from None
