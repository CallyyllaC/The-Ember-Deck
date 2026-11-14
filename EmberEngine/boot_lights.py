#!/usr/bin/env python3
"""Boot running light on the graph and status LEDs, until Pawprint takes over.

Started early in boot by the optional ``emberdeck-boot-lights.service``
(installed by tools/boot_set), long before the Deck's own services:

* a two-segment light runs back and forth along the ten-segment graph;
* the top status lamps sweep in from both ends, meet on the middle (amber)
  lamp, and sweep back out;
* the front red lamp stays on.

The HT16K33 has one owner at a time. As soon as a ``pawprint.py`` process
exists, this clears the LEDs and exits, before Pawprint (still importing its
libraries) touches the chip. It also stops on SIGTERM and after
``TIMEOUT_S`` if the Deck never starts. It reads configs/pawprint.yaml for
the chip address and writes no file.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from typing import Iterable, Tuple

# Same coordinates as pawprint.py (a test keeps them in step).
GRAPH_COORDS = [
    (0, 0), (0, 1), (0, 2), (0, 3), (0, 4),
    (0, 5), (0, 6), (0, 7), (8, 0), (8, 1),
]
TOP_COORDS = [(8, 3), (8, 4), (8, 5), (8, 6), (8, 7)]  # green, green, amber, red, red
FRONT_COORD = (8, 2)

STEP_S = 0.07          # running light speed
TOP_EVERY = 5          # top lamps move once every this many steps
# Outer pair, inner pair, middle lamp, inner pair: in from both sides, meet, back out.
TOP_SWEEP = (0b10001, 0b01010, 0b00100, 0b01010)
RUN_WIDTH = 2
BRIGHTNESS = 0.5
TIMEOUT_S = 300.0
I2C_WAIT_S = 30.0
HANDOFF_PROCESS = "pawprint.py"


def pattern(step: int) -> Tuple[int, int, bool]:
    """(graph mask, top mask, front on) for one animation step."""
    span = len(GRAPH_COORDS) - RUN_WIDTH
    period = span * 2
    pos = step % period
    start = pos if pos <= span else period - pos
    graph = ((1 << RUN_WIDTH) - 1) << start
    top = TOP_SWEEP[(step // TOP_EVERY) % len(TOP_SWEEP)]
    return graph, top, True


def lit(graph: int, top: int, front: bool) -> Iterable[Tuple[int, int]]:
    for index, coord in enumerate(GRAPH_COORDS):
        if graph & (1 << index):
            yield coord
    for index, coord in enumerate(TOP_COORDS):
        if top & (1 << index):
            yield coord
    if front:
        yield FRONT_COORD


def pawprint_running(proc: Path = Path("/proc")) -> bool:
    """Whether any process's command line names pawprint.py."""
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if HANDOFF_PROCESS.encode() in cmdline:
            return True
    return False


def matrix_address(base: Path) -> int:
    try:
        from runtime_config import load_yaml_mapping
        return int(load_yaml_mapping(base / "configs" / "pawprint.yaml", "pawprint").get("matrix_address", 0x70))
    except Exception:
        return 0x70


def open_matrix(address: int, deadline: float):
    """The HT16K33, retrying while I2C comes up; None if it never does."""
    while time.monotonic() < deadline:
        try:
            import board
            import busio
            from adafruit_ht16k33.matrix import Matrix16x8
            matrix = Matrix16x8(busio.I2C(board.SCL, board.SDA), address=address)
            matrix.auto_write = False
            matrix.blink_rate = 0
            matrix.brightness = BRIGHTNESS
            return matrix
        except Exception:
            time.sleep(1.0)
    return None


def clear(matrix) -> None:
    try:
        matrix.fill(0)
        matrix.show()
    except Exception:
        pass


def main() -> int:
    base = Path(__file__).resolve().parent
    sys.path.insert(0, str(base))
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
    if pawprint_running():
        return 0
    started = time.monotonic()
    matrix = open_matrix(matrix_address(base), started + I2C_WAIT_S)
    if matrix is None:
        return 0
    step = 0
    while not stopping and time.monotonic() - started < TIMEOUT_S:
        if pawprint_running():
            break
        try:
            matrix.fill(0)
            for col, row in lit(*pattern(step)):
                matrix[col, row] = 1
            matrix.show()
        except Exception:
            time.sleep(0.5)
        step += 1
        time.sleep(STEP_S)
    clear(matrix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
