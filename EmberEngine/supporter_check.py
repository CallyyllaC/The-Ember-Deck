#!/usr/bin/env python3
"""Check or install the Awoo supporter licence for The Ember Deck.

    ~/EmberDeck/.venv/bin/python ~/EmberDeck/supporter_check.py
        Report whether the installed licence is accepted, or why not.

    ~/EmberDeck/.venv/bin/python ~/EmberDeck/supporter_check.py --install
        Ask for the supporter code (pasted at a hidden prompt, so it never
        reaches the shell history), check it, and only then save it to
        ~/.config/awoo/supporter.licence, readable by you alone.

The licence path follows ``supporter_licence_path`` in ember_ui.yaml (and its
.local overlay) when set. The code itself is never printed. See
AWOO-SUPPORTER-SYSTEM.md for how to get a code.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import awoo_supporter  # noqa: E402

DEFAULT_PATH = "~/.config/awoo/supporter.licence"
RESTART = "systemctl --user restart ember-ui.service"


def licence_path(config_dir: Path = HERE / "configs") -> Path:
    """The configured licence location, falling back to the default."""
    value = DEFAULT_PATH
    try:
        from runtime_config import load_yaml_mapping
        value = str(load_yaml_mapping(config_dir / "ember_ui.yaml", "ember_ui").get("supporter_licence_path", value))
    except Exception:
        pass
    return Path(os.path.expanduser(value))


def check(path: Path) -> int:
    if not path.is_file():
        print(f"No supporter licence at {path}.")
        print("Install one with: supporter_check.py --install")
        return 1
    try:
        licence = awoo_supporter.verify_licence(awoo_supporter.read_licence_file(path))
    except awoo_supporter.LicenceError as exc:
        print(f"The supporter licence at {path} is not accepted: {awoo_supporter.describe(exc.reason)}.")
        if exc.reason == "crypto_unavailable":
            print("Install it with: ~/EmberDeck/.venv/bin/pip install cryptography")
        return 1
    print(f"Valid supporter licence (signing key {licence.kid}). Thank you!")
    print(f"Page transitions are on once the panel restarts: {RESTART}")
    return 0


def install(path: Path, code: Optional[str] = None) -> int:
    if code is None:
        code = getpass.getpass("Paste your supporter code (it will not be shown), then press Enter: ")
    code = code.strip()
    try:
        awoo_supporter.verify_licence(code)
    except awoo_supporter.LicenceError as exc:
        print(f"That code is not accepted: {awoo_supporter.describe(exc.reason)}. Nothing was saved.")
        if exc.reason == "crypto_unavailable":
            print("Install it with: ~/EmberDeck/.venv/bin/pip install cryptography")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(code + "\n")
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"Saved to {path} (readable by you only).")
    print(f"Restart the panel to turn page transitions on: {RESTART}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check or install the Awoo supporter licence.")
    parser.add_argument("--install", action="store_true", help="paste a supporter code, check it and save it")
    parser.add_argument("--path", help="licence file (default: from ember_ui.yaml, else " + DEFAULT_PATH + ")")
    args = parser.parse_args(argv)
    path = Path(os.path.expanduser(args.path)) if args.path else licence_path()
    return install(path) if args.install else check(path)


if __name__ == "__main__":
    sys.exit(main())
