"""CRT switch-off / switch-on page transition, drawn by kitty above the UI.

A supporter feature (see AWOO-SUPPORTER-SYSTEM.md). When the panel changes
page, an overlay image above Textual's text plays an old television turning
off: the picture collapses to a bright line, brightening as it goes, the line
shrinks to a dot and fades; the page changes behind the black screen; then it
switches back on in reverse and the overlay is removed.

The overlay frames do not depend on what is on screen, so they are rendered
once (at half the window's pixel size; kitty scales them up, which also
softens the glow) and uploaded to kitty as separate images. Playing a
transition then only sends tiny "place image n" commands, which stays smooth
on the Pi. Images placed directly (not through placeholders) live in their
own layer; Textual's text output never erases them.
"""

from __future__ import annotations

import base64
import zlib
from typing import Callable, List, Optional, Tuple

import numpy as np

import kitty_graphics

FIRST_IMAGE_ID = 0xEB7000      # one id per frame, upwards from here
OVERLAY_Z = 1000                # above text (kitty draws z >= 0 over cells)
OFF_STEPS = 9
ON_STEPS = 8
FRAME_S = 0.026                 # about 38 frames a second
HOLD_S = 0.09                   # black screen while the page is swapped
GLOW: Tuple[int, int, int] = (236, 242, 255)


def _ease_in(x: np.ndarray | float):
    return x * x


def _frame(width: int, height: int, vertical: float, horizontal: float, glow: float) -> np.ndarray:
    """One RGBA overlay.

    ``vertical`` is the open fraction of the picture's height (1 = full,
    0 = a line), ``horizontal`` the open fraction of the line's width once
    collapsed, ``glow`` the brightness washed over what is still visible.
    """
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    cy, cx = (height - 1) / 2.0, (width - 1) / 2.0
    half_h = max(0.7, vertical * height / 2.0)
    half_w = max(0.7, horizontal * width / 2.0)
    dy = np.abs(yy - cy)
    dx = np.abs(xx - cx)

    inside = (dy <= half_h) & (dx <= half_w)
    # Soft bright rim on the edges of the open area (the "electron beam").
    rim = np.exp(-np.square(np.maximum(dy - half_h, 0) / 1.6)) * np.exp(-np.square(np.maximum(dx - half_w, 0) / 1.6))
    rim = np.where(inside, 0.0, rim)
    edge = np.exp(-np.square((half_h - dy) / 1.2)) * (dx <= half_w)

    alpha = np.where(inside, glow, 1.0)
    brightness = np.where(inside, 1.0, 0.0) * glow + rim * (0.4 + glow) + edge * glow * 0.8
    brightness = np.clip(brightness, 0.0, 1.0)
    alpha = np.clip(np.maximum(alpha, brightness), 0.0, 1.0)

    rgba = np.empty((height, width, 4), np.uint8)
    for channel, value in enumerate(GLOW):
        rgba[:, :, channel] = (brightness * value).astype(np.uint8)
    rgba[:, :, 3] = (alpha * 255).astype(np.uint8)
    return rgba


def frames(width: int, height: int) -> Tuple[List[np.ndarray], int]:
    """Switch-off frames, a black frame, switch-on frames; and the black frame's index."""
    out: List[np.ndarray] = []
    for k in range(1, OFF_STEPS + 1):
        p = k / OFF_STEPS
        if p <= 0.7:
            q = p / 0.7
            out.append(_frame(width, height, 1.0 - _ease_in(q), 1.0, 0.12 + 0.6 * q))
        else:
            q = (p - 0.7) / 0.3
            out.append(_frame(width, height, 0.0, 1.0 - _ease_in(q), 0.75 * (1.0 - q) + 0.25))
    black = np.zeros((height, width, 4), np.uint8)
    black[:, :, 3] = 255
    hold = len(out)
    out.append(black)
    for k in range(1, ON_STEPS + 1):
        p = k / ON_STEPS
        if p <= 0.3:
            q = p / 0.3
            out.append(_frame(width, height, 0.0, _ease_in(q), 0.8))
        else:
            q = (p - 0.3) / 0.7
            out.append(_frame(width, height, 1.0 - _ease_in(1.0 - q), 1.0, 0.7 * (1.0 - q)))
    return out, hold


class CrtTransition:
    """Uploads the frames once and plays them on demand."""

    def __init__(self, write: Callable[[str], None], first_id: int = FIRST_IMAGE_ID) -> None:
        self.write = write
        self.first_id = first_id
        self.count = 0
        self.hold = 0
        self.cells: Optional[Tuple[int, int]] = None

    def prepare(self, cols: int, rows: int, cell_px: Tuple[int, int]) -> None:
        """Render and upload the frames for this window size (once per size)."""
        if self.cells == (cols, rows):
            return
        self.release()
        width = max(16, cols * cell_px[0] // 2)
        height = max(16, rows * cell_px[1] // 2)
        images, self.hold = frames(width, height)
        payload = []
        for index, rgba in enumerate(images):
            data = base64.b64encode(zlib.compress(rgba.tobytes(), 6)).decode("ascii")
            payload.append(kitty_graphics._chunked(
                f"a=t,i={self.first_id + index},f=32,s={width},v={height},o=z,q=2", data))
        self.write("".join(payload))
        self.count = len(images)
        self.cells = (cols, rows)

    def place(self, index: int) -> str:
        """Commands that show frame ``index`` over the whole window."""
        cols, rows = self.cells or (1, 1)
        image_id = self.first_id + index
        # Save the cursor, place at the top-left cell without moving it, restore.
        return (f"{kitty_graphics.ESC}7{kitty_graphics.ESC}[1;1H"
                + kitty_graphics.command(f"a=p,i={image_id},p=1,c={cols},r={rows},z={OVERLAY_Z},C=1,q=2")
                + f"{kitty_graphics.ESC}8")

    def hide(self, index: int) -> str:
        """Commands that remove frame ``index`` from the screen (its data stays)."""
        return kitty_graphics.command(f"a=d,d=i,i={self.first_id + index},q=2")

    def release(self) -> None:
        """Delete every uploaded frame from the terminal."""
        for index in range(self.count):
            self.write(kitty_graphics.command(f"a=d,d=I,i={self.first_id + index},q=2"))
        self.count = 0
        self.cells = None
