#!/usr/bin/env python3
"""A pixel visualiser inside a Textual app, through kitty's graphics protocol.

The widget's text is kitty *Unicode placeholders* (U+10EEEE plus row/column
marks, image id in the foreground colour). To Textual they are ordinary
one-cell characters, so layout, diffing and redraws are untouched; kitty draws
the image wherever the placeholders are. Frames are pushed separately through
the driver's write(): Textual's own text never changes while the image moves.

Audio data is read, read-only, from Echo's Synapse keys /audio/spectrum and
/audio/waveform on the running Deck. If Echo is not publishing, the panel says
so and draws nothing: no audio is ever simulated.

Run on the Pi inside kitty, with the Deck runtime running:

    kitty --start-as=fullscreen -o font_size=9 -o hide_window_decorations=yes \
        python3 kitty_graphics_demo.py --style bars --update edit --medium shm

Options:
  --style   bars | oscilloscope | spectrogram | radial
  --update  edit        rewrite the image's root frame in place (a=f,r=1)
            retransmit  send a new image, then recreate the virtual placement
  --medium  shm         pixels through POSIX shared memory (t=s)
            direct      pixels base64 inside the escape code (t=d, zlib)
  --fps     target frame rate (default 30)
On quit (q) it prints frames sent, achieved fps and this process's CPU use.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
import zlib
from multiprocessing import shared_memory
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "EmberEngine"))  # repository checkout
sys.path.insert(0, str(Path.home() / "EmberDeck"))         # installed Deck

import numpy as np  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import Static  # noqa: E402

import visualisers as V  # noqa: E402

PLACEHOLDER = "\U0010EEEE"
# First 100 row/column marks from kitty's gen/rowcolumn-diacritics.txt.
DIACRITICS = [chr(int(h, 16)) for h in (
    "0305 030D 030E 0310 0312 033D 033E 033F 0346 034A 034B 034C 0350 0351 0352 0357 035B 0363 0364 0365 "
    "0366 0367 0368 0369 036A 036B 036C 036D 036E 036F 0483 0484 0485 0486 0487 0592 0593 0594 0595 0597 "
    "0598 0599 059C 059D 059E 059F 05A0 05A1 05A8 05A9 05AB 05AC 05AF 05C4 0610 0611 0612 0613 0614 0615 "
    "0616 0617 0657 0658 0659 065A 065B 065D 065E 06D6 06D7 06D8 06D9 06DA 06DB 06DC 06DF 06E0 06E1 06E2 "
    "06E4 06E7 06E8 06EB 06EC 0730 0732 0733 0735 0736 073A 073D 073F 0740 0741 0743 0745 0747 0749 074A"
).split()]
IMAGE_ID = 0xEB0001  # 24-bit, carried in the placeholder's foreground colour
PALETTE = [(70, 20, 40), (210, 80, 60), (240, 150, 70), (255, 236, 205)]


def apc(keys: str, payload: str = "") -> str:
    return f"\x1b_G{keys};{payload}\x1b\\" if payload else f"\x1b_G{keys}\x1b\\"


def placeholder_rows(cols: int, rows: int) -> list:
    return ["".join(PLACEHOLDER + DIACRITICS[r] + DIACRITICS[c] for c in range(cols)) for r in range(rows)]


class Pixels(Static):
    """Kitty image placeholders for a cols x rows area."""

    def __init__(self, cols: int, rows: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cols, self.rows = cols, rows

    def render(self) -> Text:
        r, g, b = (IMAGE_ID >> 16) & 255, (IMAGE_ID >> 8) & 255, IMAGE_ID & 255
        style = Style.parse(f"rgb({r},{g},{b})")
        return Text("\n".join(placeholder_rows(self.cols, self.rows)), style=style)


class Demo(App):
    CSS = """
    Screen { background: #090304; color: #ecd2ae; }
    #status { height: 3; border: round #7d3e26; padding: 0 1; }
    #pixels { border: round #7d3e26; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, args) -> None:
        super().__init__()
        self.args = args
        self.cols, self.rows = args.cols, args.rows
        self.px = (self.cols * args.cell_w, self.rows * args.cell_h)
        self.state = V.State()
        self.sent = 0
        self.started = time.monotonic()
        self.cpu0 = time.process_time()
        self.placed = False
        self.bus = None
        try:
            import synapse
            self.bus = synapse
        except Exception as exc:  # pragma: no cover - depends on the Pi
            self.bus_error = str(exc)

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        pixels = Pixels(self.cols, self.rows, id="pixels")
        pixels.styles.width = self.cols + 2
        pixels.styles.height = self.rows + 2
        yield pixels

    def on_mount(self) -> None:
        self.set_interval(1.0 / self.args.fps, self.frame)

    def _write(self, data: str) -> None:
        self._driver.write(data)

    def _transmit(self, rgb: np.ndarray, action: str) -> None:
        w, h = self.px
        raw = np.ascontiguousarray(rgb, dtype=np.uint8).tobytes()
        common = f"i={IMAGE_ID},f=24,s={w},v={h},q=2"
        if action == "edit":
            common += ",r=1,X=1"
        if self.args.medium == "shm":
            # kitty unlinks the segment after reading it (spec, t=s on POSIX).
            shm = shared_memory.SharedMemory(create=True, size=len(raw), track=False)
            shm.buf[:len(raw)] = raw
            name = shm.name
            shm.close()
            self._write(apc(f"a={'f' if action == 'edit' else 't'},{common},t=s,S={len(raw)}",
                            base64.b64encode(name.encode()).decode()))
        else:
            data = base64.b64encode(zlib.compress(raw, 1)).decode()
            chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)] or [""]
            for i, chunk in enumerate(chunks):
                more = 1 if i < len(chunks) - 1 else 0
                keys = f"a={'f' if action == 'edit' else 't'},{common},t=d,o=z,m={more}" if i == 0 else f"m={more}"
                self._write(apc(keys, chunk))

    def _place(self) -> None:
        self._write(apc(f"a=p,U=1,i={IMAGE_ID},c={self.cols},r={self.rows},q=2"))

    def frame(self) -> None:
        status = self.query_one("#status", Static)
        spectrum = self.bus.try_get_array("/audio/spectrum", 1025, "f32") if self.bus else None
        waveform = self.bus.try_get_array("/audio/waveform", 160, "f32") if self.bus else None
        if spectrum is None or waveform is None:
            status.update("Echo is not publishing /audio/spectrum and /audio/waveform: nothing to draw.")
            return
        renderer = V.PIXEL_RENDERERS[self.args.style]
        rgb = renderer(np.asarray(spectrum, np.float32), np.asarray(waveform, np.float32),
                       self.state, PALETTE, self.px)
        if not self.placed or self.args.update == "retransmit":
            self._transmit(rgb, "transmit")
            self._place()
            self.placed = True
        else:
            self._transmit(rgb, "edit")
        self.sent += 1
        elapsed = time.monotonic() - self.started
        status.update(f"{self.args.style} · {self.args.update} · {self.args.medium} · "
                      f"{self.sent / max(elapsed, 1e-6):.1f} fps · "
                      f"CPU {100 * (time.process_time() - self.cpu0) / max(elapsed, 1e-6):.0f}% of one core")

    def on_unmount(self) -> None:
        self._write(apc(f"a=d,d=I,i={IMAGE_ID},q=2"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--style", choices=sorted(V.PIXEL_RENDERERS), default="bars")
    p.add_argument("--update", choices=("edit", "retransmit"), default="edit")
    p.add_argument("--medium", choices=("shm", "direct"), default="shm")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--cols", type=int, default=74)
    p.add_argument("--rows", type=int, default=15)
    p.add_argument("--cell-w", type=int, default=8, help="cell width in pixels (pi_probe.py reports it)")
    p.add_argument("--cell-h", type=int, default=16)
    args = p.parse_args()
    app = Demo(args)
    app.run()
    elapsed = time.monotonic() - app.started
    print(f"{app.sent} frames in {elapsed:.1f} s = {app.sent / max(elapsed, 1e-6):.1f} fps; "
          f"process CPU {100 * (time.process_time() - app.cpu0) / max(elapsed, 1e-6):.0f}% of one core")


if __name__ == "__main__":
    main()
