"""Check the OSC 66 placeholder filter against Textual's real output.

Runs a small Textual app headlessly, records the exact bytes Textual writes,
passes them through textsize_filter.translate(), and replays them into a
model of kitty's documented multicell rules
(https://sw.kovidgoyal.net/kitty/text-sizing-protocol/). After every frame
the model screen is compared with what Textual believes it drew.

This is evidence, not proof: the model implements the spec, not kitty. The
same app runs in a real kitty with kitty_textsize_demo.py on the Pi.

Usage:  python verify_textsize_model.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.drivers.headless_driver import HeadlessDriver  # noqa: E402
from textual.widgets import Static  # noqa: E402

import textsize_filter as tf  # noqa: E402

FRAMES: List[str] = []
HeadlessDriver.write = lambda self, data: FRAMES.append(data)
HeadlessDriver.is_headless = property(lambda self: False)


# --------------------------------------------------------------------------- model of kitty
@dataclass
class Multicell:
    x: int
    y: int
    w: int
    h: int
    text: str


class KittyModel:
    """Screen model following the text sizing protocol's overwrite rules."""

    TOKEN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.|.", re.S)

    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        self.cells: List[List[str]] = [[" "] * width for _ in range(height)]
        self.owner: List[List[Optional[int]]] = [[None] * width for _ in range(height)]
        self.multicells: Dict[int, Multicell] = {}
        self.next_id = 0
        self.x = self.y = 0
        self.shifted_writes = 0  # writes displaced by kitty's lower-row skip rule

    def _erase(self, mc_id: int) -> None:
        mc = self.multicells.pop(mc_id)
        for yy in range(mc.y, mc.y + mc.h):
            for xx in range(mc.x, mc.x + mc.w):
                if 0 <= yy < self.h and 0 <= xx < self.w and self.owner[yy][xx] == mc_id:
                    self.owner[yy][xx] = None
                    self.cells[yy][xx] = " "

    def _print(self, char: str) -> None:
        if not (0 <= self.y < self.h):
            return
        while 0 <= self.x < self.w and self.owner[self.y][self.x] is not None:
            mc = self.multicells[self.owner[self.y][self.x]]
            if self.y == mc.y:
                self._erase(self.owner[self.y][self.x])  # top row: replaced
                break
            self.x = mc.x + mc.w  # lower row: cursor skips past the block
            self.shifted_writes += 1
        if 0 <= self.x < self.w:
            self.cells[self.y][self.x] = char
        self.x += 1

    def _multicell(self, scale: int, text: str) -> None:
        w, h = len(text) * scale, scale
        for yy in range(self.y, min(self.h, self.y + h)):
            for xx in range(self.x, min(self.w, self.x + w)):
                if self.owner[yy][xx] is not None and self.owner[yy][xx] in self.multicells:
                    self._erase(self.owner[yy][xx])
        mc_id = self.next_id
        self.next_id += 1
        self.multicells[mc_id] = Multicell(self.x, self.y, w, h, text)
        for yy in range(self.y, min(self.h, self.y + h)):
            for xx in range(self.x, min(self.w, self.x + w)):
                self.owner[yy][xx] = mc_id
                self.cells[yy][xx] = " "
        self.x += w

    def feed(self, data: str) -> None:
        for tok in self.TOKEN.findall(data):
            if tok.startswith("\x1b["):
                final, params = tok[-1], tok[2:-1]
                if final == "H":
                    row, _, col = params.partition(";")
                    self.y, self.x = int(row or 1) - 1, int(col or 1) - 1
                elif final == "C":
                    self.x += int(params or 1)
                elif final in "JK":
                    raise AssertionError(f"Textual emitted an erase: {tok!r}")
            elif tok.startswith("\x1b]66;"):
                meta, _, text = tok[5:].rstrip("\x07").partition(";")
                scale = int(dict(kv.split("=") for kv in meta.split(":"))["s"])
                self._multicell(scale, text)
            elif tok.startswith("\x1b"):
                continue
            elif tok == "\n":
                self.y += 1
            elif tok == "\r":
                self.x = 0
            else:
                self._print(tok)


# --------------------------------------------------------------------------- the app
class Big(Static):
    """Draws its text as scale-2 placeholders plus one normal line below."""

    def __init__(self, big: str, small: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.big, self.small = big, small

    def set_lines(self, big: str, small: str) -> None:
        self.big, self.small = big, small
        self.refresh()

    def render(self) -> Text:
        rows = tf.REGISTRY.placeholder_rows(self.big, 2) if self.big else []
        return Text("\n".join([*rows, self.small]))


class Demo(App):
    CSS = """
    Screen { layout: vertical; }
    #row { height: 4; layout: horizontal; }
    #lyric { width: 50; height: 3; }
    #clock { width: 1fr; height: 3; }
    #log { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal
        yield Static("EMBER DECK  header", id="head")
        with Horizontal(id="row"):
            yield Big("EVER ON", "and on, I continue circling", id="lyric")
            yield Static("12:00\nbeside", id="clock")
        yield Static("log line", id="log")


def expected_screen(app: App) -> List[str]:
    strips = app.screen._compositor.render_strips()
    return ["".join(seg.text for seg in strip) for strip in strips]


def check(app: App, model: KittyModel, label: str) -> List[str]:
    problems = []
    expected = expected_screen(app)
    anchors = {}
    for y, line in enumerate(expected):
        for x, char in enumerate(line):
            code = ord(char)
            is_anchor = tf.ANCHOR_BASE <= code < tf.ANCHOR_BASE + 0x1000
            if is_anchor:
                anchors[(x, y)] = tf.REGISTRY.runs[code - tf.ANCHOR_BASE].text
            if is_anchor or char in (tf.SKIP_TOP, tf.SKIP_LOWER):
                if model.owner[y][x] is None:
                    problems.append(f"{label}: large text missing at {x},{y}")
            elif model.owner[y][x] is not None:
                problems.append(f"{label}: stray large text over {x},{y}")
            elif model.cells[y][x] != char:
                problems.append(f"{label}: cell {x},{y} is {model.cells[y][x]!r}, Textual drew {char!r}")
    for (x, y), text in anchors.items():
        found = [mc for mc in model.multicells.values() if (mc.x, mc.y, mc.text) == (x, y, text)]
        if not found:
            problems.append(f"{label}: no multicell {text!r} anchored at {x},{y}")
    return problems


async def scenario(translate) -> Tuple[List[str], int]:
    FRAMES.clear()
    app = Demo()
    model = KittyModel(80, 24)
    problems: List[str] = []

    async def step(label: str) -> None:
        await pilot.pause(0.15)
        for frame in FRAMES:
            model.feed(translate(frame))
        FRAMES.clear()
        problems.extend(check(app, model, label))

    async with app.run_test(size=(80, 24)) as pilot:
        await step("first frame")
        lyric = app.query_one("#lyric", Big)
        clock = app.query_one("#clock", Static)
        lyric.set_lines("WITH NOTHING", "but my hate in a carousel of agony")
        await step("longer large line")
        clock.update("12:01\nbeside, updated")
        await step("neighbour on the lower row changes")
        lyric.set_lines("HATE", "shorter large line")
        await step("shorter large line")
        app.query_one("#log", Static).update("a log line below changes")
        await step("unrelated widget changes")
        app.screen.refresh(repaint=True)
        await step("full repaint")
        lyric.display = False
        await step("lyric hidden")
        lyric.display = True
        await step("lyric shown again")
    return problems, model.shifted_writes


def naive(data: str) -> str:
    """What a widget gets if it simply prints spaces under the large text."""
    out = tf.translate(data)
    return re.sub(r"\x1b\[(\d+)C", lambda m: " " * int(m.group(1)), out)


def main() -> None:
    for name, fn in (("placeholder filter", tf.translate), ("spaces under the block", naive)):
        problems, shifted = asyncio.run(scenario(fn))
        print(f"{name}: {len(problems)} mismatches, {shifted} writes pushed sideways by kitty's skip rule")
        for line in problems[:6]:
            print("   ", line)


if __name__ == "__main__":
    main()
