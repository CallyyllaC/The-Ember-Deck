#!/usr/bin/env python3
"""Work out the kitty command-line options for the panel UI.

launch_ember_ui.sh runs this and passes its output to kitty. It only *reads*
existing configuration, so moving the panel from LXTerminal to kitty changes
no file:

* Font: the family and size from the ``emberdeck`` LXTerminal profile
  (``~/.config/lxterminal/lxterminal-emberdeck.conf``, key ``fontname``), so
  the panel keeps the same text size and therefore the same layout grid.
* Window rules: if a Labwc rule in ``~/.config/labwc/rc.xml`` was written for
  the LXTerminal panel (identifier ``lxterminal`` or title ``EMBER DECK``),
  kitty identifies itself the same way so that rule keeps applying. If that
  rule already makes the window fullscreen or maximised, kitty does not
  also start fullscreen (``ToggleFullscreen`` would then undo it).

Usage: ``panel_terminal.py --args`` prints one kitty argument per line.
Everything falls back to safe defaults if a file is missing or unreadable.
"""

from __future__ import annotations

import configparser
import fnmatch
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple

PANEL_TITLE = "EMBER DECK"
LXTERMINAL_APP_ID = "lxterminal"
KITTY_APP_ID = "emberdeck-panel"
FULLSCREEN_ACTIONS = {"togglefullscreen", "togglemaximize", "maximize"}


def lxterminal_font(path: Path) -> Optional[Tuple[str, float]]:
    """(family, size) from an LXTerminal profile's ``fontname = Family Size``."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
        value = parser.get("general", "fontname", fallback="").strip()
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    family, _, size = value.rpartition(" ")
    try:
        points = float(size)
    except ValueError:
        return None
    family = family.strip()
    if not family or not 4.0 <= points <= 72.0:
        return None
    return family, points


def _matches(pattern: Optional[str], value: str) -> bool:
    return bool(pattern) and fnmatch.fnmatch(value.lower(), pattern.lower())


def panel_rules(path: Path) -> Tuple[bool, bool]:
    """(a rule targets the panel by identifier, a panel rule handles fullscreen)."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False, False
    by_identifier = False
    fullscreen = False
    for rule in root.iter():
        if not str(rule.tag).lower().endswith("windowrule"):
            continue
        identifier = rule.attrib.get("identifier")
        title = rule.attrib.get("title")
        hits_id = _matches(identifier, LXTERMINAL_APP_ID)
        hits_title = _matches(title, PANEL_TITLE)
        if not (hits_id or hits_title):
            continue
        by_identifier = by_identifier or hits_id
        for action in rule.iter():
            if str(action.tag).lower().endswith("action"):
                if action.attrib.get("name", "").lower() in FULLSCREEN_ACTIONS:
                    fullscreen = True
    return by_identifier, fullscreen


def kitty_args(home: Path) -> List[str]:
    """kitty options for the panel window, from the files described above."""
    args: List[str] = []
    font = lxterminal_font(home / ".config" / "lxterminal" / "lxterminal-emberdeck.conf")
    if font is not None:
        family, size = font
        args += ["-o", f"font_family={family}", "-o", f"font_size={size:g}"]
    by_identifier, rule_fullscreen = panel_rules(home / ".config" / "labwc" / "rc.xml")
    args += ["--class", LXTERMINAL_APP_ID if by_identifier else KITTY_APP_ID]
    if not rule_fullscreen:
        args.append("--start-as=fullscreen")
    return args


def main(argv: List[str]) -> int:
    if argv[1:] != ["--args"]:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    for arg in kitty_args(Path.home()):
        print(arg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
