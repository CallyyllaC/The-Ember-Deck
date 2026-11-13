#!/usr/bin/env python3
"""Build the Ember Deck USB update folder from this repository checkout.

    python tools/usb_update/build.py [output-folder]

The folder holds install.sh, install.py, rollback.sh, README.txt, VERSION,
MANIFEST.sha256 and payload/:

* payload/EmberDeck        every file under EmberEngine/ that git tracks or
                           would track (ignored files such as secrets,
                           *.local.yaml, logs and builds are left out),
                           except configs/
* payload/configs-shipped  the shipped config templates from
                           EmberEngine/configs, used only to validate and to
                           add files a Deck does not have yet
* payload/terminal-tests   the terminal investigation's test kit

Text files are written with LF line endings whatever the checkout uses.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TEST_KIT = ROOT / "experiments" / "terminal-investigation"
TEST_KIT_FILES = ("README.md", "pi_probe.py", "kitty_textsize_demo.py", "kitty_graphics_demo.py",
                  "textsize_filter.py", "visualisers.py", "verify_textsize_model.py")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE, text=True).stdout


def copy_lf(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    if b"\0" not in data:  # text: normalise line endings for the Pi
        data = data.replace(b"\r\n", b"\n")
    dest.write_bytes(data)


def build(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    payload = out / "payload"
    files = [line for line in git("ls-files", "-co", "--exclude-standard", "--", "EmberEngine").splitlines() if line]
    program = shipped = 0
    for rel in files:
        source = ROOT / rel
        if not source.is_file() or "__pycache__" in rel:
            continue
        inner = rel[len("EmberEngine/"):]
        if inner.startswith("configs/"):
            copy_lf(source, payload / "configs-shipped" / inner[len("configs/"):])
            shipped += 1
        else:
            copy_lf(source, payload / "EmberDeck" / inner)
            program += 1
    for name in TEST_KIT_FILES:
        copy_lf(TEST_KIT / name, payload / "terminal-tests" / name)
    for name in ("install.sh", "install.py", "rollback.sh", "README.txt"):
        copy_lf(HERE / name, out / name)

    head = git("rev-parse", "--short", "HEAD").strip()
    dirty = bool(git("status", "--porcelain", "--", "EmberEngine").strip())
    version = f"{head}{' + uncommitted changes' if dirty else ''} (built {time.strftime('%Y-%m-%d %H:%M')})"
    (out / "VERSION").write_text(version + "\n", encoding="utf-8", newline="\n")

    lines = []
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = path.relative_to(out).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}")
    (out / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{out}\n  {program} program files, {shipped} config templates, "
          f"{len(TEST_KIT_FILES)} test-kit files\n  version {version}")
    return out


if __name__ == "__main__":
    default = ROOT.parent / f"EmberDeck-update-{time.strftime('%Y-%m-%d')}"
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else default)
