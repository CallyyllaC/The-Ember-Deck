#!/usr/bin/env python3
"""Ask the terminal this runs in what it really supports, then draw samples.

Run on the Pi inside each candidate terminal, full screen on the 640 x 480
display:   python3 pi_probe.py | tee probe-<terminal>.txt

Investigation tool only. It writes nothing outside the current directory and
changes no configuration. Queries use standard replies (CPR, DA1, XTWINOPS,
kitty's a=q); a terminal that ignores a query simply times out.
"""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import time
import tty

ESC = "\x1b"


def query(seq: str, until: str, timeout: float = 0.6) -> str:
    """Send seq and read the reply up to the regex ``until`` (or timeout)."""
    sys.stdout.write(seq)
    sys.stdout.flush()
    buf, end = "", time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([sys.stdin], [], [], max(0.0, end - time.monotonic()))
        if not ready:
            break
        buf += os.read(sys.stdin.fileno(), 4096).decode("latin-1")
        if re.search(until, buf):
            break
    return buf


def cpr() -> tuple:
    reply = query(f"{ESC}[6n", r"\x1b\[\d+;\d+R")
    m = re.search(r"\x1b\[(\d+);(\d+)R", reply)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def probe() -> dict:
    found = {}
    # Text sizing (kitty's documented CPR method).
    sys.stdout.write("\r")
    a = cpr()
    sys.stdout.write(f"{ESC}]66;w=2; \a")
    b = cpr()
    sys.stdout.write(f"{ESC}]66;s=2; \a")
    c = cpr()
    found["osc66_width"] = b[1] - a[1] == 2
    found["osc66_scale"] = c[1] - b[1] == 2
    sys.stdout.write("\r\n\n\n")
    # Kitty graphics: a query action answered with OK before the DA1 reply.
    reply = query(f"{ESC}_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA{ESC}\\{ESC}[c", r"\x1b\[\?[\d;]*c")
    found["kitty_graphics"] = "_Gi=31;OK" in reply
    da1 = re.search(r"\x1b\[\?([\d;]*)c", reply)
    found["da1"] = da1.group(1) if da1 else ""
    found["sixel"] = bool(da1 and "4" in da1.group(1).split(";"))
    # Cell and window size in pixels (XTWINOPS 16 and 14).
    m = re.search(r"\x1b\[6;(\d+);(\d+)t", query(f"{ESC}[16t", r"\x1b\[6;\d+;\d+t"))
    found["cell_px"] = f"{m.group(2)}x{m.group(1)}" if m else "no reply"
    m = re.search(r"\x1b\[4;(\d+);(\d+)t", query(f"{ESC}[14t", r"\x1b\[4;\d+;\d+t"))
    found["window_px"] = f"{m.group(2)}x{m.group(1)}" if m else "no reply"
    size = os.get_terminal_size()
    found["grid"] = f"{size.columns}x{size.lines}"
    found["TERM"] = os.environ.get("TERM", "")
    found["COLORTERM"] = os.environ.get("COLORTERM", "")
    return found


def samples() -> None:
    w = sys.stdout.write
    w("\x1b[2J\x1b[H")
    w("1. Normal text  The quick brown fox  0123456789\r\n")
    w("\x1b[1m2. Bold text\x1b[0m  \x1b[3m3. Italic text\x1b[0m\r\n")
    w(f"4. OSC 66 s=2: {ESC}]66;s=2;Ever on and on\a\r\n\n")
    w(f"5. OSC 66 s=2:n=3:d=4 (1.5x): {ESC}]66;s=2:n=3:d=4;Lollia\a\r\n\n")
    w(f"6. OSC 66 s=3: {ESC}]66;s=3;BIG\a\r\n\n\n")
    w(f"{ESC}#3 7. DECDHL top half\r\n{ESC}#4 7. DECDHL bottom half\r\n")
    w("8. Eighth blocks: ▁▂▃▄▅▆▇█  Half/quadrant: ▀▄▌▐▖▗▘▝▚▞  Braille: ⣿⡇⠿⣀⠉⢸\r\n")
    w("9. True colour: ")
    for i in range(48):
        w(f"\x1b[48;2;{i * 5};{40 + i * 3};{200 - i * 4}m \x1b[0m")
    w("\r\n10. CJK and emoji: 君の名は  ❤ ✓\r\n")
    sys.stdout.flush()


def main() -> None:
    if not sys.stdin.isatty():
        sys.exit("Run this in the terminal being tested, not through a pipe for input.")
    old = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        results = probe()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    samples()
    print("\r\nProbe results:")
    for key, value in results.items():
        print(f"  {key:16s} {value}")
    print("\nCheck by eye: does line 4 show large text? Are lines 7 double height?")


if __name__ == "__main__":
    main()
