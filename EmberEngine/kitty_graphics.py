"""Pixel images inside the Textual panel through kitty's graphics protocol.

kitty draws an image wherever its *Unicode placeholder* characters appear:
U+10EEEE followed by two combining marks giving the cell's row and column,
with the image id carried in the foreground colour. To Textual these are
ordinary one-cell characters, so its layout, diffing and redraws work as
usual; the pixels arrive separately as graphics commands written through the
Textual driver, in order with Textual's own output.

Protocol reference: https://sw.kovidgoyal.net/kitty/graphics-protocol/
(Unicode placeholders since kitty 0.28; Debian Trixie ships 0.41.1).

Frames travel inside the escape code (zlib + base64, 4096-byte chunks), so a
terminal that ignores them leaves nothing behind, unlike shared memory.
"""

from __future__ import annotations

import base64
import os
import struct
import zlib
from typing import Callable, List, Optional, Tuple

import numpy as np

PLACEHOLDER = chr(0x10EEEE)
ESC = chr(0x1B)

# Row/column marks, in order, from kitty's gen/rowcolumn-diacritics.txt.
_DIACRITIC_HEX = """
    0305 030D 030E 0310 0312 033D 033E 033F 0346 034A 034B 034C 0350 0351 0352 0357
    035B 0363 0364 0365 0366 0367 0368 0369 036A 036B 036C 036D 036E 036F 0483 0484
    0485 0486 0487 0592 0593 0594 0595 0597 0598 0599 059C 059D 059E 059F 05A0 05A1
    05A8 05A9 05AB 05AC 05AF 05C4 0610 0611 0612 0613 0614 0615 0616 0617 0657 0658
    0659 065A 065B 065D 065E 06D6 06D7 06D8 06D9 06DA 06DB 06DC 06DF 06E0 06E1 06E2
    06E4 06E7 06E8 06EB 06EC 0730 0732 0733 0735 0736 073A 073D 073F 0740 0741 0743
    0745 0747 0749 074A 07EB 07EC 07ED 07EE 07EF 07F0 07F1 07F3 0816 0817 0818 0819
    081B 081C 081D 081E 081F 0820 0821 0822 0823 0825 0826 0827 0829 082A 082B 082C
    082D 0951 0953 0954 0F82 0F83 0F86 0F87 135D 135E 135F 17DD 193A 1A17 1A75 1A76
    1A77 1A78 1A79 1A7A 1A7B 1A7C 1B6B 1B6D 1B6E 1B6F 1B70 1B71 1B72 1B73 1CD0 1CD1
    1CD2 1CDA 1CDB 1CE0 1DC0 1DC1 1DC3 1DC4 1DC5 1DC6 1DC7 1DC8 1DC9 1DCB 1DCC 1DD1
    1DD2 1DD3 1DD4 1DD5 1DD6 1DD7 1DD8 1DD9 1DDA 1DDB 1DDC 1DDD 1DDE 1DDF 1DE0 1DE1
    1DE2 1DE3 1DE4 1DE5 1DE6 1DFE 20D0 20D1 20D4 20D5 20D6 20D7 20DB 20DC 20E1 20E7
    20E9 20F0 2CEF 2CF0 2CF1 2DE0 2DE1 2DE2 2DE3 2DE4 2DE5 2DE6 2DE7 2DE8 2DE9 2DEA
    2DEB 2DEC 2DED 2DEE 2DEF 2DF0 2DF1 2DF2 2DF3 2DF4 2DF5 2DF6 2DF7 2DF8 2DF9 2DFA
    2DFB 2DFC 2DFD 2DFE 2DFF A66F A67C A67D A6F0 A6F1 A8E0 A8E1 A8E2 A8E3 A8E4 A8E5
    A8E6 A8E7 A8E8 A8E9 A8EA A8EB A8EC A8ED A8EE A8EF A8F0 A8F1 AAB0 AAB2 AAB3 AAB7
    AAB8 AABE AABF AAC1 FE20 FE21 FE22 FE23 FE24 FE25 FE26 10A0F 10A38 1D185 1D186 1D187
    1D188 1D189 1D1AA 1D1AB 1D1AC 1D1AD 1D242 1D243 1D244
"""
DIACRITICS: Tuple[str, ...] = tuple(chr(int(code, 16)) for code in _DIACRITIC_HEX.split())
MAX_CELLS = len(DIACRITICS)
CHUNK = 4096


def is_kitty(environ=None) -> bool:
    """Whether the UI is running inside kitty (which sets both of these)."""
    env = os.environ if environ is None else environ
    return env.get("TERM") == "xterm-kitty" or "KITTY_WINDOW_ID" in env


def id_colour(image_id: int) -> Tuple[int, int, int]:
    """The 24-bit foreground colour that names ``image_id`` to kitty."""
    return ((image_id >> 16) & 255, (image_id >> 8) & 255, image_id & 255)


def placeholder_rows(cols: int, rows: int) -> List[str]:
    """One string per row: a placeholder per cell, each naming its row and column."""
    cols = max(0, min(int(cols), MAX_CELLS))
    rows = max(0, min(int(rows), MAX_CELLS))
    return ["".join(PLACEHOLDER + DIACRITICS[r] + DIACRITICS[c] for c in range(cols)) for r in range(rows)]


def cell_pixels(fd: Optional[int] = None) -> Optional[Tuple[int, int]]:
    """Cell size in pixels from the terminal's window size report, if it gives one."""
    try:
        import fcntl
        import termios
        if fd is None:
            import sys
            fd = sys.__stdout__.fileno()
        rows, cols, xpix, ypix = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))
    except Exception:
        return None
    if rows and cols and xpix and ypix:
        return (xpix // cols, ypix // rows)
    return None


def command(keys: str, payload: str = "") -> str:
    """One graphics command (an APC escape sequence)."""
    body = f"{keys};{payload}" if payload else keys
    return f"{ESC}_G{body}{ESC}\\"


def _chunked(keys: str, data: str) -> str:
    """Split base64 data into the protocol's 4096-byte chunks (m=1 means more follow)."""
    pieces = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [""]
    out = []
    for index, piece in enumerate(pieces):
        more = 1 if index < len(pieces) - 1 else 0
        out.append(command(f"{keys},m={more}" if index == 0 else f"m={more}", piece))
    return "".join(out)


class ImageStream:
    """Sends RGB frames for one image id and keeps its placeholder placement.

    ``mode="replace"`` sends each frame as a new image with the same id and
    recreates the virtual placement (the spec says re-sending an id deletes
    its placements). ``mode="edit"`` sends the first frame that way and then
    rewrites the image's root frame in place; it is lighter but the spec does
    not say placeholder placements show the edit, so it is opt-in.
    """

    def __init__(self, write: Callable[[str], None], image_id: int, mode: str = "replace") -> None:
        self.write = write
        self.image_id = int(image_id)
        self.mode = mode if mode in ("replace", "edit") else "replace"
        self._placed: Optional[Tuple[int, int, int, int]] = None
        self.frames_sent = 0

    def send(self, rgb: np.ndarray, cols: int, rows: int) -> None:
        height, width = rgb.shape[:2]
        raw = np.ascontiguousarray(rgb[:, :, :3], dtype=np.uint8).tobytes()
        data = base64.b64encode(zlib.compress(raw, 1)).decode("ascii")
        shape = (width, height, int(cols), int(rows))
        base = f"i={self.image_id},f=24,s={width},v={height},o=z,q=2"
        if self.mode == "edit" and self._placed == shape:
            self.write(_chunked(f"a=f,r=1,X=1,{base}", data))
        else:
            self.write(_chunked(f"a=t,{base}", data)
                       + command(f"a=p,U=1,i={self.image_id},c={int(cols)},r={int(rows)},q=2"))
            self._placed = shape
        self.frames_sent += 1

    def reset(self) -> None:
        """Forget the placement so the next frame recreates it (after a resize)."""
        self._placed = None

    def delete(self) -> None:
        """Remove the image and its data from the terminal."""
        self.write(command(f"a=d,d=I,i={self.image_id},q=2"))
        self._placed = None
