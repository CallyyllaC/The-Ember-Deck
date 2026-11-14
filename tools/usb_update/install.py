#!/usr/bin/env python3
"""Ember Deck USB update: move ~/EmberDeck onto the code in ./payload.

Run through install.sh, which installs kitty first. This script:

1. Checks the payload's checksums, the existing install, the Python
   environment, and that your current configuration files validate against
   the new code (on a copy, in a staging folder).
2. Stops the Deck's services and backs up ~/EmberDeck (without .venv and
   logs) to ~/EmberDeck-backups.
3. Copies the new program files in. It never changes an existing file under
   ~/EmberDeck/configs; a config file the new code expects but you do not
   have is added. Nothing outside ~/EmberDeck is touched apart from the
   backup folder.
4. Starts the services that were running and watches them. If the runtime or
   the kitty panel does not come up healthy, it restores the backup and
   restarts the old install by itself.

``install.py --rollback`` restores the most recent backup.
Python standard library only; works with the Pi's system python3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

UNITS = ("emberdeck.service", "ember-ui.service", "ember-browser.service")
BACKUP_EXCLUDE = {".venv", "logs", "__pycache__"}
EXECUTABLES = {"system/emberdeck-poweroff", "discord_bridge/launcher/xdg-open"}
REQUIRED_MODULES = ("numpy", "PIL", "yaml", "rich", "textual")
# Test hooks: the real commands on the Pi, fakes in the update's own tests.
SYSTEMCTL = os.environ.get("EMBERDECK_UPDATE_SYSTEMCTL", "systemctl").split()
PGREP = os.environ.get("EMBERDECK_UPDATE_PGREP", "pgrep").split()
HEALTH_SECONDS = float(os.environ.get("EMBERDECK_UPDATE_HEALTH_SECONDS", "25"))


class UpdateError(Exception):
    """Stop the update with a message for the person running it."""


class Log:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.handle = path.open("a", encoding="utf-8")

    def __call__(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {message}"
        print(message, flush=True)
        self.handle.write(line + "\n")
        self.handle.flush()


def run(cmd: Sequence[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=timeout)


# --------------------------------------------------------------------------- payload
def payload_files(root: Path) -> List[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def verify_manifest(usb: Path) -> None:
    manifest = usb / "MANIFEST.sha256"
    if not manifest.is_file():
        raise UpdateError("MANIFEST.sha256 is missing from the update folder; copy the whole folder again.")
    bad = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        path = usb / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            bad.append(name)
    if bad:
        raise UpdateError(f"{len(bad)} update file(s) are damaged or missing (first: {bad[0]}). "
                          "Copy the update folder to the USB stick again.")


# --------------------------------------------------------------------------- services
def systemctl(*args: str) -> subprocess.CompletedProcess:
    return run([*SYSTEMCTL, "--user", *args])


def active_units() -> List[str]:
    return [unit for unit in UNITS if systemctl("is-active", unit).stdout.strip() == "active"]


def unit_state(unit: str) -> Tuple[str, int]:
    out = systemctl("show", "-p", "ActiveState", "-p", "NRestarts", unit).stdout
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    try:
        restarts = int(values.get("NRestarts", "0") or 0)
    except ValueError:
        restarts = 0
    return values.get("ActiveState", "unknown"), restarts


def stop_units(log: Log) -> None:
    # Stop the panel and browser first; both are PartOf the runtime anyway.
    for unit in reversed(UNITS):
        systemctl("stop", unit)
    log("Stopped the Ember Deck services.")


def start_units(units: Sequence[str], log: Log) -> None:
    for unit in UNITS:
        if unit in units:
            result = systemctl("start", unit)
            if result.returncode != 0:
                log(f"  {unit} did not start: {result.stdout.strip()[:200]}")
    log("Started: " + (", ".join(units) if units else "nothing (no service was running before)"))


def process_running(pattern: str) -> bool:
    return run([*PGREP, "-f", pattern]).returncode == 0


def health_check(units: Sequence[str], log: Log) -> Optional[str]:
    """None when everything that was started stays healthy, else the reason."""
    if not units:
        return None
    baseline = {unit: unit_state(unit)[1] for unit in units}
    deadline = time.monotonic() + HEALTH_SECONDS
    log(f"Watching the services for {HEALTH_SECONDS:.0f} s...")
    while True:
        for unit in units:
            state, restarts = unit_state(unit)
            if state == "failed":
                return f"{unit} failed"
            if restarts > baseline[unit]:
                return f"{unit} restarted after the update"
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    for unit in units:
        state, _ = unit_state(unit)
        if state != "active":
            return f"{unit} is {state}, not active"
    if "ember-ui.service" in units:
        if not process_running("kitty-panel.conf"):
            return "the panel's kitty window is not running"
        if not process_running("ember_ui.py"):
            return "the panel UI (ember_ui.py) is not running inside kitty"
    return None


# --------------------------------------------------------------------------- checks
def preflight(target: Path, usb: Path, staging: Path, log: Log) -> None:
    payload = usb / "payload" / "EmberDeck"
    for name in ("FoundryCore.py", "run_foundry.sh", "configs"):
        if not (target / name).exists():
            raise UpdateError(f"{target} does not look like an Ember Deck install ({name} is missing).")
    python = Path(os.environ.get("EMBERDECK_UPDATE_PYTHON", target / ".venv" / "bin" / "python"))
    if not python.exists():
        raise UpdateError(f"{python} is missing; the update reuses the existing Python environment.")
    probe = run([str(python), "-c", "import " + ", ".join(REQUIRED_MODULES)])
    if probe.returncode != 0:
        raise UpdateError("The Deck's Python environment is missing a package the new UI needs "
                          f"({', '.join(REQUIRED_MODULES)}):\n{probe.stdout.strip()[-400:]}")
    log("Existing install and Python environment look right.")

    # Validate the Deck's own configuration against the new code, on copies.
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(payload, staging)
    shutil.copytree(target / "configs", staging / "configs")
    shipped = usb / "payload" / "configs-shipped"
    for path in shipped.iterdir():
        if not (staging / "configs" / path.name).exists():
            shutil.copy2(path, staging / "configs" / path.name)
    check = run([str(python), str(staging / "runtime_config.py"), str(staging / "configs")], timeout=120)
    if check.returncode != 0:
        raise UpdateError("Your configuration files do not validate against the new code, so nothing was "
                          "changed. Details:\n" + check.stdout.strip())
    log("Your configuration files validate against the new code (checked on a copy).")

    need = sum(p.stat().st_size for p in target.rglob("*")
               if p.is_file() and not BACKUP_EXCLUDE.intersection(p.relative_to(target).parts))
    free = shutil.disk_usage(target.parent).free
    if free < need * 2 + 200_000_000:
        raise UpdateError(f"Not enough free disk space for a backup ({free // 1_000_000} MB free).")


# --------------------------------------------------------------------------- backup and restore
def _backup_filter(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    parts = Path(info.name).parts[1:]  # strip the leading "EmberDeck"
    return None if BACKUP_EXCLUDE.intersection(parts) else info


def make_backup(target: Path, backups: Path, stamp: str, usb: Path, units: Sequence[str],
                version: str, log: Log) -> Path:
    backups.mkdir(parents=True, exist_ok=True)
    archive = backups / f"EmberDeck-before-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(target, arcname="EmberDeck", filter=_backup_filter)
    payload = usb / "payload" / "EmberDeck"
    new_files = [rel for rel in payload_files(payload) if not (target / rel).exists()]
    new_files += [f"configs/{p.name}" for p in sorted((usb / "payload" / "configs-shipped").iterdir())
                  if not (target / "configs" / p.name).exists()]
    meta = {
        "archive": archive.name,
        "created": stamp,
        "target": str(target),
        "new_files": new_files,
        "active_units": list(units),
        "installed_version": version,
    }
    meta_path = backups / f"EmberDeck-before-{stamp}.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log(f"Backed up {target} to {archive}")
    return meta_path


def restore(meta_path: Path, log: Log) -> None:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    target = Path(meta["target"])
    archive = meta_path.parent / meta["archive"]
    log(f"Restoring {target} from {archive.name}...")
    for rel in meta.get("new_files", []):
        path = target / rel
        if path.is_file():
            path.unlink()
    with tarfile.open(archive, "r:gz") as tar:
        # configs/ is left alone: the update never changes an existing config
        # file, and the Deck may have written to one since (Minstrel's token cache).
        members = [m for m in tar.getmembers()
                   if (m.name == "EmberDeck" or m.name.startswith("EmberDeck/"))
                   and not m.name.startswith("EmberDeck/configs/") and m.name != "EmberDeck/configs"]
        for member in members:
            member.name = member.name[len("EmberDeck"):].lstrip("/") or "."
        if sys.version_info >= (3, 12):
            tar.extractall(target, members=members, filter="data")
        else:  # pragma: no cover - the Pi has 3.13
            tar.extractall(target, members=members)
    log("Restored the previous install.")


def latest_backup(backups: Path) -> Path:
    found = sorted(backups.glob("EmberDeck-before-*.json"))
    if not found:
        raise UpdateError(f"No backup found in {backups}.")
    return found[-1]


# --------------------------------------------------------------------------- install
def boot_history(usb: Path) -> Dict[str, set]:
    """Every shipped version (sha256) of each boot file, from the payload."""
    history: Dict[str, set] = {}
    path = usb / "payload" / "boot-history.sha256"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            digest, _, rel = line.partition("  ")
            if rel:
                history.setdefault(rel, set()).add(digest)
    return history


def changed_on_this_deck(dest: Path, source: Path, shipped: set) -> bool:
    """A boot file that matches neither this version nor any shipped one."""
    if not dest.is_file():
        return False
    data = dest.read_bytes()
    return data != source.read_bytes() and hashlib.sha256(data).hexdigest() not in shipped


def install_files(target: Path, usb: Path, log: Log, keep_dir: Optional[Path] = None) -> List[str]:
    payload = usb / "payload" / "EmberDeck"
    history = boot_history(usb)
    kept = []
    written = 0
    for rel in payload_files(payload):
        source, dest = payload / rel, target / rel
        # The boot theme is the one place a Deck owner adjusts files for their
        # own hardware (images flipped for an upside-down screen, the script's
        # display geometry). Keep those; save this version beside the backup.
        if rel.startswith("boot/") and changed_on_this_deck(dest, source, history.get(rel, set())):
            if keep_dir is not None:
                (keep_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, keep_dir / rel)
            kept.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(dest.name + ".update-tmp")
        shutil.copyfile(source, temp)
        executable = rel.endswith(".sh") or rel in EXECUTABLES
        os.chmod(temp, 0o755 if executable else 0o644)
        os.replace(temp, dest)
        written += 1
    added = []
    for path in sorted((usb / "payload" / "configs-shipped").iterdir()):
        dest = target / "configs" / path.name
        if not dest.exists():
            shutil.copy2(path, dest)
            added.append(path.name)
    log(f"Installed {written} program files.")
    if kept:
        where = f" (this version's copies: {keep_dir})" if keep_dir is not None else ""
        log("Kept boot theme files you changed on this Deck: " + ", ".join(kept) + where)
    if added:
        log("Added config files you did not have yet: " + ", ".join(added))
    else:
        log("No config file was changed or added.")
    return added


def config_notes(target: Path) -> List[str]:
    notes = []
    pawprint = target / "configs" / "pawprint.yaml"
    try:
        text = pawprint.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        if line.split("#", 1)[0].replace(" ", "").lower() == "media_keys_enabled:true":
            notes.append("configs/pawprint.yaml still has media_keys_enabled: true (left as you set it). "
                         "The new default is false; change it yourself if you do not use media keys.")
    return notes


# --------------------------------------------------------------------------- main
def update(usb: Path, home: Path) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups = home / "EmberDeck-backups"
    log = Log(backups / f"update-{stamp}.log")
    target = home / "EmberDeck"
    version = (usb / "VERSION").read_text(encoding="utf-8").strip() if (usb / "VERSION").is_file() else "unknown"
    log(f"Ember Deck update {version}")
    staging = home / ".emberdeck-update-staging"
    try:
        verify_manifest(usb)
        preflight(target, usb, staging, log)
    except UpdateError as exc:
        log(f"\nUpdate stopped before changing anything:\n{exc}")
        return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    units = active_units()
    log("Running before the update: " + (", ".join(units) or "none"))
    stop_units(log)
    try:
        meta = make_backup(target, backups, stamp, usb, units, version, log)
    except Exception as exc:
        log(f"\nBackup failed ({exc}); nothing was changed. Restarting the services.")
        start_units(units, log)
        return 1
    try:
        install_files(target, usb, log, keep_dir=backups / f"new-boot-files-{stamp}")
    except Exception as exc:
        log(f"\nCopying the new files failed ({exc}). Restoring the backup.")
        restore(meta, log)
        start_units(units, log)
        return 1

    start_units(units, log)
    problem = health_check(units, log)
    if problem:
        log(f"\nThe new version did not come up healthy: {problem}.")
        log("Rolling back to the previous install...")
        stop_units(log)
        restore(meta, log)
        start_units(units, log)
        log(f"\nRolled back. kitty stays installed. The full log is {log.path}")
        return 1

    log("\nUpdate complete.")
    log(f"  Backup:     {backups / json.loads(meta.read_text())['archive']}")
    log(f"  Log:        {log.path}")
    log(f"  Roll back:  bash {usb / 'rollback.sh'}")
    for note in config_notes(target):
        log(f"  Note: {note}")
    return 0


def rollback(home: Path, backup: Optional[str]) -> int:
    backups = home / "EmberDeck-backups"
    log = Log(backups / f"rollback-{time.strftime('%Y%m%d-%H%M%S')}.log")
    try:
        meta = Path(backup) if backup else latest_backup(backups)
        units = json.loads(meta.read_text(encoding="utf-8")).get("active_units", list(UNITS))
    except (UpdateError, OSError, ValueError) as exc:
        log(str(exc))
        return 1
    stop_units(log)
    restore(meta, log)
    start_units(units, log)
    log("Rollback complete.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ember Deck USB update")
    parser.add_argument("--rollback", action="store_true", help="restore the latest backup")
    parser.add_argument("--backup", help="a specific backup .json to restore")
    parser.add_argument("--home", default=str(Path.home()), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    home = Path(args.home)
    if args.rollback:
        return rollback(home, args.backup)
    return update(Path(__file__).resolve().parent, home)


if __name__ == "__main__":
    sys.exit(main())
