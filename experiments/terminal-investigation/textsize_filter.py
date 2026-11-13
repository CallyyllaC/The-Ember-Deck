"""Proof of concept: kitty large text (OSC 66) inside a Textual app.

Not production code. Investigation only, see README.md in this folder.

Why a filter: Textual measures every character as a terminal cell and Rich
strips the BEL that ends an OSC sequence, so a widget cannot put an OSC 66
escape in its own text. Instead a widget draws *placeholder* characters
(Unicode private use, one per terminal cell the large text will cover) and
this filter, sitting in the Textual driver's write(), turns them into kitty
escapes:

* ANCHOR(id) at the top-left cell  -> ``ESC ] 66 ; s=<scale> ; <text> BEL``.
  kitty draws the text at that scale and moves the cursor right by its width,
  which is exactly the cells Textual thinks the following SKIP characters use.
* SKIP on the top row               -> nothing (kitty's cursor is already past).
* SKIP on a lower row               -> ``ESC [ C`` (cursor forward). kitty's
  spec says *printing* on a lower row of a multicell skips past the block,
  which would shift the rest of the line; moving the cursor does not.

Textual positions the cursor absolutely (CUP) at the start of every span it
redraws, so the filter needs no knowledge of the screen. It only needs to know
which row of a block a SKIP is on, which the placeholder encodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

from rich.cells import cell_len

ANCHOR_BASE = 0xF0000   # + run id (0..0xFFF)
SKIP_TOP = "\U000F8000"
SKIP_LOWER = "\U000F8001"
_PLACEHOLDER = re.compile("[\U000F0000-\U000F0FFF\U000F8000\U000F8001]+")


@dataclass
class Run:
    text: str
    scale: int

    @property
    def width(self) -> int:
        return cell_len(self.text) * self.scale


class Registry:
    """Large-text runs by id. Widgets register; the driver filter looks up."""

    def __init__(self) -> None:
        self.runs: Dict[int, Run] = {}
        self._ids: Dict[tuple, int] = {}

    def register(self, text: str, scale: int) -> int:
        key = (text, scale)
        if key not in self._ids:
            self._ids[key] = len(self._ids) % 0x1000
            self.runs[self._ids[key]] = Run(text, scale)
        return self._ids[key]

    def placeholder_rows(self, text: str, scale: int) -> List[str]:
        """The rows of placeholder characters a widget draws for one run."""
        run_id = self.register(text, scale)
        width = self.runs[run_id].width
        top = chr(ANCHOR_BASE + run_id) + SKIP_TOP * (width - 1)
        return [top] + [SKIP_LOWER * width for _ in range(scale - 1)]


REGISTRY = Registry()


def translate(data: str, registry: Registry = REGISTRY) -> str:
    """Rewrite one chunk of Textual output, replacing placeholders."""

    def replace(match: re.Match) -> str:
        out = []
        lower = 0
        for char in match.group(0):
            if char == SKIP_TOP:
                continue
            if char == SKIP_LOWER:
                lower += 1
                continue
            if lower:
                out.append(f"\x1b[{lower}C")
                lower = 0
            run = registry.runs.get(ord(char) - ANCHOR_BASE)
            if run is not None:
                out.append(f"\x1b]66;s={run.scale};{run.text}\x07")
        if lower:
            out.append(f"\x1b[{lower}C")
        return "".join(out)

    return _PLACEHOLDER.sub(replace, data)
