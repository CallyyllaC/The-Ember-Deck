#!/usr/bin/env python3
"""Own the one local graphical-media slot on Ember Deck HDMI-2.

The source controller does not decide transport routing or UI metadata. It only
chooses which local context occupies the HDMI-2 app slot:

    PLEX -> RADIO (Shortwave) -> YOUTUBE (Pear) -> PLEX

Pawprint requests a cycle through a shared counter.  The controller closes the
old guest app, stops Plex when leaving it, launches the chosen guest app in the
Wayland session, and installs narrow Labwc window rules that move it to HDMI-2
before making it fullscreen.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

import synapse as bus
from whisper_daemon import log_error, log_heartbeat, log_info

DEFAULTS = {
    "proc_name": "hdmi2_source_controller",
    "poll_hz": 10.0,
    "command_timeout_s": 4.0,
    "startup_settle_s": 1.0,
    "flatpak_path": "flatpak",
    "shortwave_app_id": "de.haeckerfelix.Shortwave",
    "pear_appimage": "~/apps/pear/Pear.AppImage",
    "pear_args": ["--ozone-platform=wayland"],
    "pear_process_match": "Pear.AppImage",
    "labwc_rules_enabled": True,
    "labwc_rc_path": "~/.config/labwc/rc.xml",
    # Verify once with `wlr-randr`; HDMI-A-2 is the normal wlroots connector
    # spelling for the second physical HDMI output, not an X11 display label.
    "labwc_hdmi2_output": "HDMI-A-2",
    "labwc_path": "labwc",
    "heartbeat_s": 1.0,
}

HDMI2_SOURCE_PLEX = 1
HDMI2_SOURCE_RADIO = 2
HDMI2_SOURCE_YOUTUBE = 3
SOURCE_ORDER = (HDMI2_SOURCE_PLEX, HDMI2_SOURCE_RADIO, HDMI2_SOURCE_YOUTUBE)
SOURCE_NAMES = {
    HDMI2_SOURCE_PLEX: "PLEX",
    HDMI2_SOURCE_RADIO: "RADIO",
    HDMI2_SOURCE_YOUTUBE: "YOUTUBE",
}

KEY_CYCLE_SEQ = "/hdmi2/control/cycle_seq"
KEY_SELECTED_SOURCE = "/hdmi2/selected_source"
KEY_SELECTED_SEQ = "/hdmi2/selected_source_seq"
KEY_STATUS = "/hdmi2/status"
KEY_STATUS_SEQ = "/hdmi2/status_seq"

STATUS_IDLE = 0
STATUS_STARTING = 1
STATUS_ACTIVE = 2
STATUS_ERROR = 3


def load_cfg() -> dict:
    """Load defaults and merge any component-specific YAML configuration."""
    raw = os.environ.get("CONFIG_PATH")
    path = Path(raw) if raw else Path(__file__).resolve().parent / "configs" / "hdmi2_source_controller.yaml"
    loaded: dict = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = DEFAULTS.copy()
    cfg.update(loaded)
    cfg["_config_path"] = str(path)
    return cfg


def heartbeat(proc_name: str) -> None:
    """Publish the component heartbeat and diagnostic event."""
    now_ms = int(time.monotonic() * 1000)
    bus.set_int(f"/proc/{proc_name}/heartbeat_ms", now_ms)
    seq_key = f"/proc/{proc_name}/hb_seq"
    bus.set_int(seq_key, bus.get_int(seq_key, 0) + 1)
    log_heartbeat(proc_name)


def counter_delta(previous: int, current: int) -> int:
    """Return a bounded delta between wrapping command counters."""
    if current == previous:
        return 0
    delta = (int(current) - int(previous)) & 0x7FFFFFFF
    return 1 if delta <= 0 or delta > 8 else delta


class Hdmi2SourceController:
    """Manage Hdmi2SourceController state and behaviour."""
    def __init__(self, cfg: dict):
        """Initialize configuration, dependencies, and runtime state."""
        self.cfg = cfg
        self.proc_name = str(cfg.get("proc_name", "hdmi2_source_controller"))
        self.running = True
        self.current = HDMI2_SOURCE_PLEX
        self.seen_cycle_seq = bus.get_int(KEY_CYCLE_SEQ, 0)
        self.last_heartbeat_at = 0.0
        # Foundry restarts its worker group as one appliance. Do not let an old
        # graphical guest survive that reset while the new controller believes
        # the cold-boot source is Plex.
        self.startup_transition_pending = True
        self._publish_selected(self.current, STATUS_IDLE)
        self._install_labwc_rules()

    def _session_env(self) -> Dict[str, str]:
        """Supply the graphical-session basics when Foundry was started by a service.

        Labwc publishes these into the user activation environment, but the
        defaults below also cover the normal single-user Pi desktop session.
        """
        env = os.environ.copy()
        uid = os.getuid()
        runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
        env.setdefault("XDG_RUNTIME_DIR", runtime)
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
        env.setdefault("XDG_SESSION_TYPE", "wayland")
        return env

    def _run(self, argv: Sequence[str], timeout: Optional[float] = None) -> Tuple[bool, str]:
        """Run a child command and return its success flag and output."""
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout or max(0.3, float(self.cfg.get("command_timeout_s", 4.0))),
                check=False,
                env=self._session_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if completed.returncode != 0:
            return False, (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
        return True, completed.stdout

    def _publish_selected(self, source: int, status: int) -> None:
        """Publish selected."""
        source = int(source)
        changed = source != bus.get_int(KEY_SELECTED_SOURCE, HDMI2_SOURCE_PLEX)
        status_changed = status != bus.get_int(KEY_STATUS, STATUS_IDLE)
        bus.set_int(KEY_SELECTED_SOURCE, source)
        bus.set_int(KEY_STATUS, int(status))
        if changed:
            bus.set_int(KEY_SELECTED_SEQ, bus.get_int(KEY_SELECTED_SEQ, 0) + 1)
        if status_changed:
            bus.set_int(KEY_STATUS_SEQ, bus.get_int(KEY_STATUS_SEQ, 0) + 1)

    def _plex_stop(self) -> None:
        """Handle the plex stop lifecycle step."""
        key = "/plex/control/stop_seq"
        next_seq = (bus.get_int(key, 0) + 1) & 0x7FFFFFFF
        bus.set_int(key, 1 if next_seq == 0 else next_seq)

    def _is_running(self, pattern: str) -> bool:
        """Return whether running."""
        if not pattern:
            return False
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _stop_shortwave(self) -> None:
        """Stop shortwave."""
        self._run([str(self.cfg["flatpak_path"]), "kill", str(self.cfg["shortwave_app_id"])])

    def _stop_pear(self) -> None:
        """Stop pear."""
        pattern = str(self.cfg.get("pear_process_match", "Pear.AppImage")).strip()
        if pattern:
            self._run(["pkill", "-TERM", "-f", pattern])

    def _close_guest_apps(self) -> None:
        """Close guest apps."""
        self._stop_shortwave()
        self._stop_pear()
        # Give Wayland a breath to unmap the old surface before the replacement
        # is launched. This keeps Labwc's first-map rules deterministic.
        time.sleep(0.20)

    def _launch(self, argv: Sequence[str]) -> bool:
        """Return the launch result."""
        env = self._session_env()
        runtime = Path(env["XDG_RUNTIME_DIR"])
        wayland_socket = runtime / env["WAYLAND_DISPLAY"]
        if not wayland_socket.exists():
            log_error(self.proc_name, "graphical session unavailable", str(wayland_socket))
            return False
        try:
            subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            return True
        except OSError as exc:
            log_error(self.proc_name, "guest launch failed", repr(exc))
            return False

    def _launch_shortwave(self) -> bool:
        """Launch shortwave."""
        app_id = str(self.cfg["shortwave_app_id"])
        if self._is_running(app_id):
            return True
        return self._launch([str(self.cfg["flatpak_path"]), "run", app_id])

    def _launch_pear(self) -> bool:
        """Launch pear."""
        appimage = Path(os.path.expanduser(str(self.cfg["pear_appimage"])))
        pattern = str(self.cfg.get("pear_process_match", appimage.name))
        if self._is_running(pattern):
            return True
        if not appimage.is_file():
            log_error(self.proc_name, "Pear AppImage missing", str(appimage))
            return False
        if not os.access(appimage, os.X_OK):
            log_error(self.proc_name, "Pear AppImage is not executable", str(appimage))
            return False
        return self._launch([str(appimage), *[str(item) for item in self.cfg.get("pear_args", [])]])

    @staticmethod
    def _rule_matches(rule: ET.Element, identifier: str) -> bool:
        """Return the rule matches result."""
        if rule.attrib.get("identifier") != identifier:
            return False
        actions = rule.findall("action")
        return [action.attrib.get("name") for action in actions] == ["MoveToOutput", "ToggleFullscreen"]

    def _reconfigure_labwc(self) -> None:
        """Reload Labwc rules even when Foundry lacks LABWC_PID in its env."""
        ok, _ = self._run([str(self.cfg.get("labwc_path", "labwc")), "--reconfigure"])
        if ok:
            return
        # A user service launched before/away from the desktop may not inherit
        # LABWC_PID. Fall back to the current user's compositor process rather
        # than leaving the newly written rules dormant until the next reboot.
        try:
            result = subprocess.run(
                ["pgrep", "-u", str(os.getuid()), "-x", "labwc"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
                check=False,
            )
            for raw_pid in result.stdout.splitlines():
                try:
                    os.kill(int(raw_pid.strip()), signal.SIGHUP)
                    return
                except (ValueError, OSError):
                    continue
        except (OSError, subprocess.SubprocessError):
            pass

    def _install_labwc_rules(self) -> None:
        """Install/update only Ember's two deterministic HDMI-2 window rules.

        Labwc executes matching window-rule actions when a window maps. The
        order is deliberate: move while the client is not fullscreen, then make
        it fullscreen on the target output. No generic focus/resize hacks or
        X11-only window managers are involved.
        """
        if not bool(self.cfg.get("labwc_rules_enabled", True)):
            return
        rc_path = Path(os.path.expanduser(str(self.cfg["labwc_rc_path"])))
        output = str(self.cfg.get("labwc_hdmi2_output", "")).strip()
        if not output:
            log_error(self.proc_name, "Labwc HDMI-2 output is blank", "set labwc_hdmi2_output")
            return
        identifiers = [
            str(self.cfg.get("shortwave_app_id", "de.haeckerfelix.Shortwave")),
            # Pear declares product/window class YouTube Music. Wildcards are
            # case-insensitive in Labwc, covering package/name variants.
            "*youtube*music*",
        ]
        try:
            if rc_path.exists():
                tree = ET.parse(rc_path)
                root = tree.getroot()
            else:
                root = ET.Element("labwc_config")
                tree = ET.ElementTree(root)
            rules = root.find("windowRules")
            if rules is None:
                rules = ET.SubElement(root, "windowRules")

            changed = False
            for identifier in identifiers:
                owned = next((rule for rule in rules.findall("windowRule") if self._rule_matches(rule, identifier)), None)
                if owned is None:
                    owned = ET.SubElement(rules, "windowRule", {"identifier": identifier})
                    ET.SubElement(owned, "action", {"name": "MoveToOutput", "output": output})
                    ET.SubElement(owned, "action", {"name": "ToggleFullscreen"})
                    changed = True
                else:
                    move = owned.find("action")
                    if move is not None and move.attrib.get("output") != output:
                        move.set("output", output)
                        changed = True

            if not rc_path.exists() or changed:
                rc_path.parent.mkdir(parents=True, exist_ok=True)
                ET.indent(tree, space="  ")
                tree.write(rc_path, encoding="utf-8", xml_declaration=True)
                self._reconfigure_labwc()
                print(f"[{self.proc_name}] Labwc HDMI-2 rules ready: {output}", flush=True)
        except Exception as exc:
            log_error(self.proc_name, "Labwc rule install failed", repr(exc))

    def _select(self, source: int) -> None:
        """Handle the select lifecycle step."""
        source = int(source)
        self._publish_selected(source, STATUS_STARTING)
        if source == HDMI2_SOURCE_PLEX:
            self._close_guest_apps()
            self.current = source
            self._publish_selected(source, STATUS_ACTIVE)
            print(f"[{self.proc_name}] HDMI-2 source: PLEX", flush=True)
            return

        # Leaving Plex is intentionally decisive. It removes the existing
        # priority winner before Radio/Pear has time to report MPRIS state.
        self._plex_stop()
        self._close_guest_apps()
        launched = self._launch_shortwave() if source == HDMI2_SOURCE_RADIO else self._launch_pear()
        if launched:
            self.current = source
            self._publish_selected(source, STATUS_ACTIVE)
            print(f"[{self.proc_name}] HDMI-2 source: {SOURCE_NAMES[source]}", flush=True)
        else:
            self._publish_selected(source, STATUS_ERROR)

    def _consume_cycles(self) -> None:
        """Consume cycles."""
        current = bus.get_int(KEY_CYCLE_SEQ, 0)
        previous = self.seen_cycle_seq
        if current == previous:
            return
        self.seen_cycle_seq = current
        for _ in range(counter_delta(previous, current)):
            index = SOURCE_ORDER.index(self.current)
            self._select(SOURCE_ORDER[(index + 1) % len(SOURCE_ORDER)])

    def run(self) -> None:
        """Run the component until shutdown."""
        log_info(self.proc_name, "started")
        period = 1.0 / max(1.0, float(self.cfg.get("poll_hz", 10.0)))
        next_tick = time.monotonic() + max(0.0, float(self.cfg.get("startup_settle_s", 1.0)))
        while self.running:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
                now = time.monotonic()
            next_tick = max(next_tick + period, now)
            if self.startup_transition_pending:
                self.startup_transition_pending = False
                self._select(HDMI2_SOURCE_PLEX)
            self._consume_cycles()
            if now - self.last_heartbeat_at >= max(0.25, float(self.cfg.get("heartbeat_s", 1.0))):
                heartbeat(self.proc_name)
                self.last_heartbeat_at = now
        log_info(self.proc_name, "stopped")


def main() -> None:
    """Configure and run the component until shutdown."""
    app = Hdmi2SourceController(load_cfg())

    def stop_handler(signum, frame):
        """Mark the component for an orderly shutdown."""
        app.running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        app.run()
    finally:
        try:
            bus.close_all()
        except Exception:
            pass


if __name__ == "__main__":
    main()
