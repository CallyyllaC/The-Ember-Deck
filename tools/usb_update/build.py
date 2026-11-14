#!/usr/bin/env python3
"""Build the Ember Deck USB update folder from this repository checkout.

    python tools/usb_update/build.py [output-folder]

The folder holds install.sh, install.py, rollback.sh, README.txt, VERSION,
CHANGELOG.md, AWOO-SUPPORTER-SYSTEM.md, MANIFEST.sha256 and payload/:

* payload/EmberDeck        every file under EmberEngine/ that git tracks or
                           would track (ignored files such as secrets,
                           *.local.yaml, logs and builds are left out),
                           except configs/
* payload/configs-shipped  the shipped config templates from
                           EmberEngine/configs, used only to validate and to
                           add files a Deck does not have yet
* boot-set/                the optional boot set installer (run separately)

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


# Boot files the update shipped before they were committed (sha256 of the
# file as installed). Any version in git history is found automatically.
EARLIER_BOOT_FILES = {
    "boot/plymouth/emberdeck/emberdeck.script": {
        "cfd27c1ee384c62cf4b37793ae09baa8f234267e18b1e9931596c8e4908a3fd7",  # 2026-11-13 build
    },
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE, text=True).stdout


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout


def boot_history(payload: Path) -> str:
    """sha256 of every version of each boot file ever shipped, one per line.

    The update replaces a boot file on a Deck only if it is one of these;
    any other content was changed on that Deck and is kept.
    """
    lines = []
    for path in sorted((payload / "EmberDeck" / "boot").rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(payload / "EmberDeck").as_posix()
        hashes = set(EARLIER_BOOT_FILES.get(rel, set()))
        hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        for commit in git("log", "--format=%H", "--", f"EmberEngine/{rel}").split():
            try:
                data = git_bytes("show", f"{commit}:EmberEngine/{rel}")
            except subprocess.CalledProcessError:
                continue  # the file was deleted in that commit
            if b"\0" not in data:
                data = data.replace(b"\r\n", b"\n")
            hashes.add(hashlib.sha256(data).hexdigest())
        lines += [f"{digest}  {rel}" for digest in sorted(hashes)]
    return "\n".join(lines) + "\n"


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
    for name in ("install.sh", "install.py", "rollback.sh", "README.txt"):
        copy_lf(HERE / name, out / name)
    copy_lf(ROOT / "tools" / "boot_set" / "install-boot-set.sh", out / "boot-set" / "install-boot-set.sh")
    for name in ("CHANGELOG.md", "AWOO-SUPPORTER-SYSTEM.md"):   # what changed; supporter help
        copy_lf(ROOT / name, out / name)
    (payload / "boot-history.sha256").write_text(boot_history(payload), encoding="utf-8", newline="\n")

    # The release tag when there is one (v3.0, or v3.0-4-g1a2b3c4 for later
    # commits), otherwise the commit id.
    head = git("describe", "--tags", "--always").strip()
    dirty = bool(git("status", "--porcelain", "--", "EmberEngine").strip())
    version = f"{head}{' + uncommitted changes' if dirty else ''} (built {time.strftime('%Y-%m-%d %H:%M')})"
    (out / "VERSION").write_text(version + "\n", encoding="utf-8", newline="\n")

    lines = []
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = path.relative_to(out).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}")
    (out / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{out}\n  {program} program files, {shipped} config templates\n  version {version}")
    return out


if __name__ == "__main__":
    default = ROOT.parent / f"EmberDeck-update-{time.strftime('%Y-%m-%d')}"
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else default)
