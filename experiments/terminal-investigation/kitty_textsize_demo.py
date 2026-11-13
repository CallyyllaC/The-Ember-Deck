#!/usr/bin/env python3
"""Textual + kitty large text (OSC 66) through the placeholder driver filter.

Run on the Pi inside kitty, full screen on the 640 x 480 display:

    kitty --start-as=fullscreen -o font_size=9 -o hide_window_decorations=yes \
        python3 kitty_textsize_demo.py

What to watch for (the Pi checklist in README.md has the full list):
* The title and the current lyric draw at twice the size and stay sharp.
* Every 2 s the lyric changes (longer, then shorter): no leftovers, no text
  pushed sideways in the clock panel beside it.
* Every 1 s the clock beside the large text updates without disturbing it.
* Press h to hide and show the lyric panel, r to force a full repaint, q to quit.

Investigation code; uses only Textual (already installed for the Deck UI).
"""

from __future__ import annotations

import sys
import time
from itertools import cycle
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Horizontal  # noqa: E402
from textual.drivers.linux_driver import LinuxDriver  # noqa: E402
from textual.widgets import Static  # noqa: E402

import textsize_filter as tf  # noqa: E402

LYRICS = [
    "Ever on and on",
    "I continue circling",
    "With nothing but my hate",
    "Hate",
    "In a carousel of agony",
]


class TextSizeDriver(LinuxDriver):
    """Textual's Linux driver with placeholder → OSC 66 translation."""

    def write(self, data: str) -> None:
        super().write(tf.translate(data))


class Large(Static):
    """One line drawn at ``scale`` (placeholders), plus normal lines below."""

    def __init__(self, line: str, below: str = "", scale: int = 2, **kwargs) -> None:
        super().__init__(**kwargs)
        self.line, self.below, self.scale = line, below, scale

    def set_line(self, line: str, below: str = "") -> None:
        self.line, self.below = line, below
        self.refresh()

    def render(self) -> Text:
        width = max(1, self.size.width // self.scale)
        line = self.line[:width]
        rows = tf.REGISTRY.placeholder_rows(line, self.scale) if line else []
        return Text("\n".join([*rows, self.below]), style="bold")


class Demo(App):
    CSS = """
    Screen { background: #090304; color: #ecd2ae; }
    #title { height: 4; border: round #7d3e26; padding: 0 1; }
    #row { height: 6; }
    #lyric { width: 3fr; border: round #7d3e26; padding: 0 1; color: #fae2c4; }
    #clock { width: 1fr; border: round #7d3e26; padding: 0 1; }
    #log { height: 1fr; border: round #7d3e26; padding: 0 1; color: #96705f; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("h", "toggle", "Hide lyric"), ("r", "repaint", "Repaint")]

    def compose(self) -> ComposeResult:
        yield Large("Bad Apple!!", "Lollia · LEVEL 01", id="title")
        with Horizontal(id="row"):
            yield Large(LYRICS[0], "next: " + LYRICS[1], id="lyric")
            yield Static("", id="clock")
        yield Static("", id="log")

    def on_mount(self) -> None:
        self._lyrics = cycle(LYRICS)
        self._log = []
        self.set_interval(2.0, self.next_lyric)
        self.set_interval(1.0, self.tick)

    def next_lyric(self) -> None:
        line = next(self._lyrics)
        self.query_one("#lyric", Large).set_line(line, "normal-size line under the large one")
        self._log.append(f"{time.strftime('%H:%M:%S')} lyric -> {line!r}")
        self.query_one("#log", Static).update("\n".join(self._log[-12:]))

    def tick(self) -> None:
        self.query_one("#clock", Static).update(time.strftime("%H:%M:%S\nbeside the\nlarge text"))

    def action_toggle(self) -> None:
        lyric = self.query_one("#lyric")
        lyric.display = not lyric.display

    def action_repaint(self) -> None:
        self.screen.refresh(repaint=True)


if __name__ == "__main__":
    Demo(driver_class=TextSizeDriver).run()
