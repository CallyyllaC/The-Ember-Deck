#!/usr/bin/env python3
"""
- Assumes: BlinkStick Pro driving a single RGBW strip on channel 0
- Strip LEDs use GRBW order on the wire
- Internally we work in (R, G, B, W)
"""

import time
from typing import Tuple, Optional
from blinkstick import blinkstick


class BlinkstickProRGBW:
    """Manage BlinkstickProRGBW state and behaviour."""
    def __init__(
        self,
        led_count: int,
        fps: float = 50.0,
        usb_ma_budget: float = 400.0,
        pixel_max_ma: float = 50.0,
        data_channel: int = 0,
    ) -> None:
        """
        :param led_count: number of LEDs on the RGBW strip
        :param fps: target frames per second (for wait_for_next_frame)
        :param usb_ma_budget: approximate USB current budget for the strip
        :param pixel_max_ma: approx max mA per LED at full white
        :param data_channel: BlinkStick Pro output channel for this strip
        """
        if led_count <= 0:
            raise ValueError("led_count must be > 0")

        self.led_count = led_count
        self.fps = float(fps)
        self.usb_ma_budget = float(usb_ma_budget)
        self.pixel_max_ma = float(pixel_max_ma)
        self.data_channel = int(data_channel)
        if self.data_channel < 0:
            raise ValueError("data_channel must be >= 0")

        # Internal buffer: list of (R, G, B, W), 0..255
        self._pixels = [(0, 0, 0, 0)] * led_count

        # Brightness:
        # usb_limit: safety cap from power budget (0..1)
        # user_brightness: extra scale on top (0..1)
        self.usb_limit = self._compute_usb_limit()
        self.user_brightness = 1.0

        self._dev: Optional[blinkstick.BlinkStick] = None
        self._last_frame_time = time.monotonic()

    # -------------------------------------------------
    # Internal helpers
    # -------------------------------------------------

    def _compute_usb_limit(self) -> float:
        """
        Compute a global brightness cap based on USB power budget.
        Returns a scalar 0..1.
        """
        if self.led_count <= 0 or self.pixel_max_ma <= 0:
            return 1.0

        safe_pct = self.usb_ma_budget / (self.led_count * self.pixel_max_ma) * 100.0
        safe_pct = max(1.0, min(100.0, safe_pct))
        return safe_pct / 100.0

    def _effective_brightness(self) -> float:
        """
        Combined brightness: USB cap * user brightness, clamped 0..1.
        """
        v = self.usb_limit * self.user_brightness
        return max(0.0, min(1.0, v))

    def _ensure_dev(self) -> blinkstick.BlinkStick:
        """Return the ensure dev result."""
        if self._dev is None:
            raise RuntimeError("BlinkstickProRGBW not connected. Call connect() first.")
        return self._dev

    # -------------------------------------------------
    # Public API
    # -------------------------------------------------

    def connect(self) -> bool:
        """
        Connect to the first BlinkStick device and set strip mode if supported.
        """
        dev = blinkstick.find_first()
        if dev is None:
            raise RuntimeError("No BlinkStick device found")
            return False

        self._dev = dev
        
        # Try to put it into individually-addressable mode (ignore if not supported)
        try:
            self._dev.set_mode(2)
        except Exception:
            pass

        # Make sure everything starts off
        #try:
        #    self.clear()
        #    self.show()
        #except Exception:
        #    pass
        return True

    def set_brightness(self, value: float) -> None:
        """
        Set user brightness multiplier (0..1).
        USB safety cap still applies on top.
        """
        self.user_brightness = max(0.0, min(1.0, float(value)))

    def set_pixel(self, index: int, color: Tuple[int, int, int, int]) -> None:
        """
        Set a single pixel in the local buffer (R, G, B, W), 0..255 each.
        Does NOT send to hardware until show() is called.
        """
        if not (0 <= index < self.led_count):
            return

        r, g, b, w = color
        r = max(0, min(255, int(r)))
        g = max(0, min(255, int(g)))
        b = max(0, min(255, int(b)))
        w = max(0, min(255, int(w)))
        self._pixels[index] = (r, g, b, w)

    def fill(self, color: Tuple[int, int, int, int]) -> None:
        """
        Set all pixels in the local buffer to the same (R, G, B, W) colour.
        Does NOT send to hardware until show() is called.
        """
        r, g, b, w = color
        r = max(0, min(255, int(r)))
        g = max(0, min(255, int(g)))
        b = max(0, min(255, int(b)))
        w = max(0, min(255, int(w)))
        self._pixels = [(r, g, b, w)] * self.led_count

    def clear(self) -> None:
        """
        Clear local buffer to black/off. Does NOT send to hardware until show().
        """
        self._pixels = [(0, 0, 0, 0)] * self.led_count

    def show(self) -> None:
        """
        Push the current local buffer to the strip as GRBW via the configured channel.
        """
        dev = self._ensure_dev()
        scale = self._effective_brightness()

        frame: list[int] = []
        for (r, g, b, w) in self._pixels:
            if scale < 1.0:
                r = int(r * scale)
                g = int(g * scale)
                b = int(b * scale)
                w = int(w * scale)

            # Strip expects GRBW byte order
            frame.append(g)
            frame.append(r)
            frame.append(b)
            frame.append(w)

        dev.set_led_data(self.data_channel, frame)
        self._last_frame_time = time.monotonic()

    def wait_for_next_frame(self) -> None:
        """
        Sleep to maintain approximate fps timing.
        Call AFTER show(), before next frame.
        """
        if self.fps <= 0:
            return

        target_dt = 1.0 / self.fps
        now = time.monotonic()
        elapsed = now - self._last_frame_time
        remaining = target_dt - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def off(self) -> None:
        """
        Turn off LEDs and send to hardware immediately.
        """
        self.clear()
        if self._dev is not None:
            frame = [0] * (self.led_count * 4)
            self._dev.set_led_data(self.data_channel, frame)
        self._last_frame_time = time.monotonic()

    def close(self) -> None:
        """
        Optional: turn off and drop reference to device.
        """
        try:
            self.off()
        except Exception:
            pass
        self._dev = None
