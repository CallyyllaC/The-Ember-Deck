#!/usr/bin/env python3
"""
pawprint.py

Ember Deck physical I/O owner.

Owns:
  - ADS1115 at 0x48 (A0 colour pot, A1 gain pot)
  - HT16K33 Matrix16x8 at 0x70 (graph, private system lamps, internal REC/front LED)
  - Tape buttons and both selector switches via GPIO
  - Virtual media keys via /dev/uinput
  - Eject hold -> restricted orderly shutdown helper

Publishes:
  /io/in/control/*   semantic controls: gain, brightness, colour, saturation
  /io/in/selector/*  source and visualiser mode selector state
  /io/events/*       selector changes only
  /io/health/*       hardware and virtual-media-key health
  /media/*           UI-facing active-source selector (Pawprint-owned)
  /bt/*              Bluetooth AVRCP metadata and status (Pawprint-owned)

Consumes:
  /io/out/*          graph-bar requests only

REC is intentionally private to Pawprint. It toggles a two-bank control layer:
  REC off: A1 -> gain,       A0 -> colour
  REC on : A1 -> brightness, A0 -> saturation
The front LED is Pawprint-owned feedback for this bank and is not exposed on
an output bus.
"""

from __future__ import annotations

import fcntl
import math
import os
import re
import signal
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

import board
import busio
from gpiozero import Button
from adafruit_ads1x15 import ADS1115, AnalogIn, ads1x15
from adafruit_ht16k33.matrix import Matrix16x8

import synapse as bus
from whisper_daemon import log_error, log_heartbeat, log_info, log_legacy, log_recovery


DEFAULTS = {
    "proc_name": "pawprint",
    "poll_hz": 50,
    "button_debounce_s": 0.05,
    "selector_debounce_s": 0.07,
    "i2c_retry_s": 2.0,
    "ads_address": 0x48,
    "matrix_address": 0x70,
    "ads_gain": 1,
    "pot_input_max_v": 3.30,
    "pot_smoothing": 0.18,
    "gain_deadband": 0.003,
    "colour_deadband": 0.010,
    "pickup_tolerance": 0.045,
    "gain_input_min": 0.0,
    "gain_input_max": 1.0,
    "gain_curve": 1.0,
    # Temporarily disable A1 while the gain pot/wiring is removed or repaired.
    # When false Pawprint does not create or read the ADS1115 A1 channel.
    "gain_enabled": False,
    # A0 currently presents a small, non-linear electrical span. These values
    # map the observed usable span into the semantic 0..1 control range.
    "colour_input_min": 0.020,
    "colour_input_max": 0.140,
    "colour_curve": 1.0,
    # Semantic output ceilings. These are the user-facing 0..1 controls:
    # colour is a full static hue wheel; saturation is grayscale (0) through
    # normal (0.5) to vivid (1.0). A0 calibration remains separate above.
    "gain_output_max": 1.0,
    "brightness_output_max": 1.0,
    "colour_output_max": 1.0,
    "saturation_output_max": 1.0,
    "default_control_value": 0.50,
    "state_save_interval_s": 30.0,
    "state_path": "pawprint_state.yaml",
    "default_indicator_brightness": 0.55,
    # 10-segment physical graph bar. Pawprint owns its normal behaviour;
    # external callers may request an explicit temporary override through
    # /io/out/led/graph_override + /io/out/led/graph_mask.
    "graph_mode": "progress",
    "graph_progress_segments": 10,
    "graph_progress_show_first_segment": True,
    "graph_external_override_enabled": True,
    "heartbeat_period_s": 1.60,
    # Pawprint-owned top status lamps. These are intentionally local and do
    # not consume the public output bus.
    "system_sample_interval_s": 0.50,
    "network_activity_min_bytes": 2048,
    "network_pulse_s": 0.14,
    "disk_activity_min_sectors": 8,
    "disk_pulse_s": 0.14,
    "storage_path": "/",
    "storage_warning_min_free_gb": 2.0,
    "storage_warning_min_free_percent": 10.0,
    "memory_warning_min_available_percent": 10.0,
    # Private tape transport controls. They are intentionally not published
    # on /io; Pawprint injects normal Linux media keys through /dev/uinput.
    "media_keys_enabled": True,
    "uinput_path": "/dev/uinput",
    "media_key_device_name": "Ember Deck Media Keys",
    "media_reconnect_s": 5.0,
    # Transport routing: Plex wins while it is actually playing, then active
    # Bluetooth, then active generic MPRIS. If all are idle Pawprint uses the
    # selected HDMI-2 context before its remembered route; cold boot is Plex.
    "media_source_refresh_s": 0.75,
    "media_command_timeout_s": 2.0,
    "minstrel_heartbeat_stale_s": 5.0,
    "bluetooth_media_enabled": True,
    "bluetoothctl_path": "bluetoothctl",
    "gdbus_path": "gdbus",
    # Bluetooth status is checked frequently for routing; metadata is sampled
    # more gently because each D-Bus property query is a subprocess.
    "bluetooth_metadata_refresh_s": 1.0,
    # Bluetooth phones like to report notification noises as "Playing" for a
    # split second. Only let Bluetooth steal the deck when metadata looks like
    # real media or the playing stream is continuous for long enough.
    "bluetooth_claim_metadata_enabled": True,
    "bluetooth_claim_threshold_s": 2.0,
    "bluetooth_claim_linger_s": 45.0,
    "bluetooth_claim_min_duration_ms": 30000,
    # REC tap still changes pot bank. A deliberate hold owns the ten graph LEDs
    # while it fills and then requests a HDMI-2 source cycle.
    "record_source_hold_s": 3.0,
    "record_source_hold_confirm_s": 0.40,
    "eject_shutdown_hold_s": 5.0,
    "shutdown_helper": "/usr/local/sbin/emberdeck-poweroff",
    "shutdown_blink_hz": 4.0,
    "shutdown_enabled": True,
    "event_capacity": 64,
}

CONTROL_NAMES = ("gain", "brightness", "colour", "saturation")

# Tape transport and REC are Pawprint-private. Only selectors use the event ring.
BUTTON_PINS = {
    "play": 6,
    "stop": 16,
    "rew": 12,
    "ff": 8,
    "eject": 21,
}
RECORD_PIN = 5

SEL3_PINS = (24, 25)  # source selector, electrical bit A/B
SEL4_PINS = (22, 23)  # visualiser selector, electrical bit A/B

SEL3_LOOKUP = {
    0b11: 3,
    0b10: 1,
    0b01: 2,
}
SEL4_LOOKUP = {
    0b11: 1,
    0b10: 2,
    0b01: 3,
    0b00: 4,
}

LED_MAP = {
    "front": (8, 2),
    "top_green_1": (8, 3),
    "top_green_2": (8, 4),
    "top_amber": (8, 5),
    "top_red_1": (8, 6),
    "top_red_2": (8, 7),
}
GRAPH_COORDS = [
    (0, 0), (0, 1), (0, 2), (0, 3), (0, 4),
    (0, 5), (0, 6), (0, 7), (8, 0), (8, 1),
]
TOP_LED_NAMES = ["top_green_1", "top_green_2", "top_amber", "top_red_1", "top_red_2"]

EVENT_DOWN = 1
EVENT_UP = 2
EVENT_SELECTOR = 3
EVENT_CODES = {
    "source_selector": 20,
    "visualiser_mode": 21,
}
EVENT_RECORD_WIDTH = 5  # sequence, monotonic_ms_mod, type, control, value


# Linux input-event constants. Kept here rather than depending on a separate
# Python package; Pawprint only needs four consumer/media key codes.
EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0
BUS_VIRTUAL = 0x06
KEY_STOPCD = 166
KEY_NEXTSONG = 163
KEY_PLAYPAUSE = 164
KEY_PREVIOUSSONG = 165

# uinput ioctl values from linux/uinput.h. The legacy uinput_user_dev setup is
# supported by the Raspberry Pi kernel and avoids a dependency on python-evdev.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE = 0
_IOC_WRITE = 1


def _ioc(direction: int, type_: int, number: int, size: int) -> int:
    """Return the ioc result."""
    return (
        (direction << _IOC_DIRSHIFT)
        | (type_ << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _io(type_: int, number: int) -> int:
    """Return the io result."""
    return _ioc(_IOC_NONE, type_, number, 0)


def _iow(type_: int, number: int, size: int) -> int:
    """Return the iow result."""
    return _ioc(_IOC_WRITE, type_, number, size)


UI_SET_EVBIT = _iow(ord("U"), 100, struct.calcsize("i"))
UI_SET_KEYBIT = _iow(ord("U"), 101, struct.calcsize("i"))
UI_DEV_CREATE = _io(ord("U"), 1)
UI_DEV_DESTROY = _io(ord("U"), 2)


# Pawprint keeps this intentionally tiny. Values are published as ints so a
# future UI can display the route without teaching Synapse about strings.
MEDIA_TARGET_NONE = 0
MEDIA_TARGET_PLEX = 1
MEDIA_TARGET_BLUETOOTH = 2
MEDIA_TARGET_MPRIS = 3
# Compatibility alias. The old numeric local route is now explicitly generic
# MPRIS, but keeping the symbol avoids breaking an out-of-tree helper.
MEDIA_TARGET_LOCAL = MEDIA_TARGET_MPRIS
PLEX_PLAYING = 1

MEDIA_TARGET_NAMES = {
    MEDIA_TARGET_NONE: "none",
    MEDIA_TARGET_PLEX: "plex",
    MEDIA_TARGET_BLUETOOTH: "bluetooth",
    MEDIA_TARGET_MPRIS: "mpris",
}

# Shared UI contract.  Source codes intentionally match the media-router
# target codes above so a future UI can choose the matching /plex/* or /bt/*
# namespace without a translation table.  Strings are fixed-size UTF-8 buffers
# because Synapse segments cannot resize after their first writer creates them.
MEDIA_TEXT_MAX_CHARS = 192
KEY_MEDIA_ACTIVE_SOURCE = "/media/active_source"
KEY_MEDIA_ACTIVE_SOURCE_SEQ = "/media/active_source_seq"
KEY_BT_CONNECTED = "/bt/connected"
KEY_BT_PLAY_STATE = "/bt/play_state"
KEY_BT_POSITION_MS = "/bt/position_ms"
KEY_BT_DURATION_MS = "/bt/duration_ms"
KEY_BT_TRACK_NUMBER = "/bt/track_number"
KEY_BT_TRACK_COUNT = "/bt/track_count"
KEY_BT_PHONE_BATTERY_PCT = "/bt/phone_battery_pct"
KEY_BT_METADATA_SEQ = "/bt/metadata_seq"
KEY_BT_TITLE_UTF8 = "/bt/title_utf8"
KEY_BT_ARTIST_UTF8 = "/bt/artist_utf8"
KEY_BT_ALBUM_UTF8 = "/bt/album_utf8"
KEY_BT_PLAYER_NAME_UTF8 = "/bt/player_name_utf8"
KEY_BT_DEVICE_NAME_UTF8 = "/bt/device_name_utf8"

# The generic local-app MPRIS bridge owns this namespace. Pawprint only reads
# its availability/playback state and emits control counters back to it.
KEY_MPRIS_AVAILABLE = "/mpris/available"
KEY_MPRIS_PLAY_STATE = "/mpris/play_state"
KEY_MPRIS_POSITION_MS = "/mpris/position_ms"
KEY_MPRIS_DURATION_MS = "/mpris/duration_ms"
KEY_HDMI2_SELECTED_SOURCE = "/hdmi2/selected_source"
KEY_HDMI2_CYCLE_SEQ = "/hdmi2/control/cycle_seq"
HDMI2_SOURCE_PLEX = 1
HDMI2_SOURCE_RADIO = 2
HDMI2_SOURCE_YOUTUBE = 3

MPRIS_CONTROL_KEYS = {
    "play": "/mpris/control/playpause_seq",
    "stop": "/mpris/control/stop_seq",
    "ff": "/mpris/control/next_seq",
    "rew": "/mpris/control/previous_seq",
}

BT_PLAY_STOPPED = 0
BT_PLAY_PLAYING = 1
BT_PLAY_PAUSED = 2

PLEX_CONTROL_KEYS = {
    "play": "/plex/control/playpause_seq",
    "stop": "/plex/control/stop_seq",
    "ff": "/plex/control/next_seq",
    "rew": "/plex/control/previous_seq",
}


@dataclass
class BluetoothMediaState:
    """One connected AVRCP-capable phone/player, including UI metadata."""

    connected: bool = False
    playing: bool = False
    device_path: str = ""
    player_path: str = ""
    status: str = ""
    device_name: str = "N/A"
    player_name: str = "N/A"
    title: str = "N/A"
    artist: str = "N/A"
    album: str = "N/A"
    position_ms: int = 0
    duration_ms: int = 0
    track_number: int = 0
    track_count: int = 0
    phone_battery_pct: int = -1


class BluetoothMediaController:
    """Small subprocess bridge to BlueZ's already-present D-Bus API.

    We deliberately use ``gdbus`` rather than adding a Python D-Bus dependency
    to Pawprint. The phone's player path is discovered afresh during periodic
    status refreshes, so reconnects and a different paired phone do not need a
    hard-coded MAC address.
    """

    _MAC_LINE = re.compile(r"^Device\s+([0-9A-Fa-f:]{17})(?:\s+(.*))?$")
    _BLUEZ_PATH = re.compile(r"(/org/bluez/[A-Za-z0-9_./-]+)")
    _QUOTED = re.compile(r"'((?:\\'|[^'])*)'")
    _TRACK_TEXT = re.compile(r"'(Title|Artist|Album)'\s*:\s*<\s*'((?:\\'|[^'])*)'\s*>")
    _TRACK_UINT = re.compile(r"'(Duration|TrackNumber|NumberOfTracks)'\s*:\s*<\s*(?:uint32\s+)?(\d+)\s*>")
    _UINT = re.compile(r"(?:uint32\s+)?(-?\d+)")

    def __init__(self, cfg: dict):
        """Initialize configuration, dependencies, and runtime state."""
        self.cfg = cfg
        self.state = BluetoothMediaState()
        self._last_metadata_refresh_at = 0.0

    def _run(self, argv: List[str]) -> Tuple[bool, str]:
        """Run a child command and return its success flag and output."""
        timeout = max(0.2, float(self.cfg.get("media_command_timeout_s", 2.0)))
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
            return False, message
        return True, completed.stdout

    def _get_property(self, object_path: str, interface: str, prop: str) -> Tuple[bool, str]:
        """Return property."""
        return self._run([
            str(self.cfg.get("gdbus_path", "gdbus")),
            "call",
            "--system",
            "--dest", "org.bluez",
            "--object-path", object_path,
            "--method", "org.freedesktop.DBus.Properties.Get",
            interface,
            prop,
        ])

    @staticmethod
    def _decode_variant_text(raw: str) -> str:
        """Return the decode variant text result."""
        match = BluetoothMediaController._QUOTED.search(raw or "")
        if match is None:
            return "N/A"
        # gdbus quotes apostrophes as \' and backslashes as \\. Only undo
        # the two escapes that are relevant to a title/artist display.
        return _ui_text(match.group(1).replace("\\'", "'").replace("\\\\", "\\"))

    @staticmethod
    def _decode_variant_uint(raw: str, default: int = 0) -> int:
        """Return the decode variant uint result."""
        match = BluetoothMediaController._UINT.search(raw or "")
        if match is None:
            return int(default)
        try:
            return int(match.group(1))
        except ValueError:
            return int(default)

    @staticmethod
    def _parse_track(raw: str) -> Dict[str, object]:
        """Parse track."""
        values: Dict[str, object] = {
            "Title": "N/A",
            "Artist": "N/A",
            "Album": "N/A",
            "Duration": 0,
            "TrackNumber": 0,
            "NumberOfTracks": 0,
        }
        for key, value in BluetoothMediaController._TRACK_TEXT.findall(raw or ""):
            values[key] = _ui_text(value.replace("\\'", "'").replace("\\\\", "\\"))
        for key, value in BluetoothMediaController._TRACK_UINT.findall(raw or ""):
            try:
                values[key] = int(value)
            except ValueError:
                pass
        return values

    def refresh(self) -> BluetoothMediaState:
        """Refresh object."""
        if not bool(self.cfg.get("bluetooth_media_enabled", True)):
            self.state = BluetoothMediaState()
            return self.state

        ok, output = self._run([str(self.cfg.get("bluetoothctl_path", "bluetoothctl")), "devices", "Connected"])
        if not ok:
            self.state = BluetoothMediaState()
            return self.state

        previous = self.state
        now = time.monotonic()
        for line in output.splitlines():
            match = self._MAC_LINE.match(line.strip())
            if not match:
                continue
            mac = match.group(1).upper()
            device_name = _ui_text(match.group(2))
            device_path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

            ok, player_raw = self._get_property(device_path, "org.bluez.MediaControl1", "Player")
            if not ok:
                continue
            path_match = self._BLUEZ_PATH.search(player_raw)
            if path_match is None:
                continue
            player_path = path_match.group(1)

            ok, status_raw = self._get_property(player_path, "org.bluez.MediaPlayer1", "Status")
            status = self._decode_variant_text(status_raw).lower() if ok else ""
            metadata_due = (
                previous.player_path != player_path
                or now - self._last_metadata_refresh_at >= max(0.25, float(self.cfg.get("bluetooth_metadata_refresh_s", 1.0)))
            )

            if metadata_due:
                ok, track_raw = self._get_property(player_path, "org.bluez.MediaPlayer1", "Track")
                track = self._parse_track(track_raw) if ok else {}
                ok, position_raw = self._get_property(player_path, "org.bluez.MediaPlayer1", "Position")
                position_ms = self._decode_variant_uint(position_raw, 0) if ok else 0
                ok, player_name_raw = self._get_property(player_path, "org.bluez.MediaPlayer1", "Name")
                player_name = self._decode_variant_text(player_name_raw) if ok else "N/A"
                ok, battery_raw = self._get_property(device_path, "org.bluez.Battery1", "Percentage")
                battery_pct = self._decode_variant_uint(battery_raw, -1) if ok else -1
                self._last_metadata_refresh_at = now
            else:
                track = {
                    "Title": previous.title,
                    "Artist": previous.artist,
                    "Album": previous.album,
                    "Duration": previous.duration_ms,
                    "TrackNumber": previous.track_number,
                    "NumberOfTracks": previous.track_count,
                }
                position_ms = previous.position_ms
                player_name = previous.player_name
                battery_pct = previous.phone_battery_pct

            self.state = BluetoothMediaState(
                connected=True,
                playing=status == "playing",
                device_path=device_path,
                player_path=player_path,
                status=status,
                device_name=device_name,
                player_name=_ui_text(player_name),
                title=_ui_text(track.get("Title")),
                artist=_ui_text(track.get("Artist")),
                album=_ui_text(track.get("Album")),
                position_ms=max(0, int(position_ms)),
                duration_ms=max(0, int(track.get("Duration", 0) or 0)),
                track_number=max(0, int(track.get("TrackNumber", 0) or 0)),
                track_count=max(0, int(track.get("NumberOfTracks", 0) or 0)),
                phone_battery_pct=max(-1, min(100, int(battery_pct))),
            )
            return self.state

        self.state = BluetoothMediaState()
        return self.state

    def send(self, action: str) -> Tuple[bool, str]:
        """Return the send result."""
        methods = {
            "play": "Play",
            "pause": "Pause",
            "stop": "Stop",
            "next": "Next",
            "previous": "Previous",
        }
        method = methods.get(action)
        if not method or not self.state.player_path:
            return False, "no Bluetooth media player is available"
        return self._run([
            str(self.cfg.get("gdbus_path", "gdbus")),
            "call",
            "--system",
            "--dest", "org.bluez",
            "--object-path", self.state.player_path,
            "--method", f"org.bluez.MediaPlayer1.{method}",
        ])


class VirtualMediaKeys:
    """A tiny self-contained /dev/uinput consumer-control keyboard."""

    def __init__(self, path: str, name: str):
        """Initialize configuration, dependencies, and runtime state."""
        self.path = path
        self.name = name
        self.fd: Optional[int] = None

    @property
    def online(self) -> bool:
        """Return the online result."""
        return self.fd is not None

    def open(self) -> None:
        """Handle the open lifecycle step."""
        if self.fd is not None:
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
            for code in (KEY_PLAYPAUSE, KEY_STOPCD, KEY_NEXTSONG, KEY_PREVIOUSSONG):
                fcntl.ioctl(fd, UI_SET_KEYBIT, code)

            # struct uinput_user_dev: name[80], input_id, ff_effects_max,
            # then four 64-int absolute-axis arrays. We do not expose axes.
            name = self.name.encode("utf-8")[:79]
            descriptor = struct.pack("80sHHHHI", name, BUS_VIRTUAL, 0x454D, 0x4244, 1, 0)
            descriptor += b"\x00" * (64 * 4 * 4)
            os.write(fd, descriptor)
            fcntl.ioctl(fd, UI_DEV_CREATE)
            self.fd = fd
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def close(self) -> None:
        """Handle the close lifecycle step."""
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _emit(self, event_type: int, code: int, value: int) -> None:
        """Handle the emit lifecycle step."""
        if self.fd is None:
            raise RuntimeError("virtual media device is offline")
        seconds = int(time.time())
        useconds = int((time.time() - seconds) * 1_000_000)
        # Native ABI layout matches struct input_event on this 64-bit Pi.
        os.write(self.fd, struct.pack("@llHHi", seconds, useconds, event_type, code, value))

    def press(self, code: int) -> None:
        """Handle the press lifecycle step."""
        try:
            self._emit(EV_KEY, int(code), 1)
            self._emit(EV_SYN, SYN_REPORT, 0)
            self._emit(EV_KEY, int(code), 0)
            self._emit(EV_SYN, SYN_REPORT, 0)
        except OSError:
            self.close()
            raise


@dataclass
class StableValue:
    """Store a normalized StableValue record."""
    stable: Optional[int] = None
    candidate: Optional[int] = None
    changed_at: float = 0.0


@dataclass
class PickupState:
    """Store a normalized PickupState record."""
    acquired: bool = False
    previous_value: Optional[float] = None


def load_cfg() -> dict:
    """Load defaults and merge any component-specific YAML configuration."""
    raw_config_path = os.environ.get("CONFIG_PATH")
    config_path = Path(raw_config_path) if raw_config_path else None
    loaded = {}
    if config_path is not None and config_path.is_file():
        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}

    cfg = DEFAULTS.copy()
    cfg.update(loaded)

    if config_path is not None and config_path.is_file():
        cfg_dir = config_path.parent
    else:
        cfg_dir = Path.cwd() / "configs"

    state_path = Path(str(cfg.get("state_path") or DEFAULTS["state_path"]))
    if not state_path.is_absolute():
        state_path = cfg_dir / state_path
    cfg["state_path"] = str(state_path)
    cfg["_config_path"] = str(config_path) if config_path is not None else ""
    return cfg


def clamp01(value: float) -> float:
    """Clamp a numeric value to the inclusive range from zero to one."""
    return max(0.0, min(1.0, float(value)))


def _ui_text(value: object) -> str:
    """Return a stable human-facing fallback for missing metadata."""
    text = str(value or "").strip()
    return text if text else "N/A"


def _utf8_prefix(value: object, max_bytes: int) -> bytes:
    """Return a valid UTF-8 prefix without splitting a multi-byte glyph."""
    raw = _ui_text(value).encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return raw
    clipped = raw[:max(0, int(max_bytes))]
    while clipped:
        try:
            clipped.decode("utf-8", errors="strict")
            return clipped
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return b""


def publish_utf8(key: str, value: object, max_chars: int = MEDIA_TEXT_MAX_CHARS) -> None:
    """Publish a fixed-size NUL-padded UTF-8 text buffer without byte-splitting."""
    size = max(8, int(max_chars))
    encoded = _utf8_prefix(value, size - 1)
    payload = list(encoded) + [0] * (size - len(encoded))
    bus.set_array(key, payload, dtype="u8")


def finite_control(value: object, fallback: float) -> float:
    """Return the finite control result."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return clamp01(parsed) if math.isfinite(parsed) else fallback


def heartbeat(proc_name: str) -> None:
    """Publish the component heartbeat and diagnostic event."""
    now_ms = int(time.monotonic() * 1000)
    bus.set_int(f"/proc/{proc_name}/heartbeat_ms", now_ms)
    key = f"/proc/{proc_name}/hb_seq"
    bus.set_int(key, bus.get_int(key, 0) + 1)
    bus.set_int("/io/health/heartbeat_seq", bus.get_int("/io/health/heartbeat_seq", 0) + 1)
    log_heartbeat(proc_name)


def status_note(proc_name: str, message: str) -> None:
    """Compatibility adapter for notes awaiting a machine-readable event."""
    log_legacy(proc_name, f"[{proc_name}] {message}")


class ControlStateStore:
    """Small atomic YAML backup for the four semantic controls."""

    def __init__(self, path: Path, default_value: float, caps: Dict[str, float]):
        """Initialize configuration, dependencies, and runtime state."""
        self.path = path
        self.default_value = clamp01(default_value)
        self.caps = {name: clamp01(caps.get(name, 1.0)) for name in CONTROL_NAMES}
        self.dirty = False
        self.last_save_at = time.monotonic()

    def clamp_control(self, name: str, value: object, fallback: float) -> float:
        """Return the clamp control result."""
        return min(self.caps.get(name, 1.0), finite_control(value, fallback))

    def defaults(self) -> Dict[str, float]:
        """Return the defaults result."""
        return {name: min(self.default_value, self.caps.get(name, 1.0)) for name in CONTROL_NAMES}

    def load(self) -> Dict[str, float]:
        """Return the load result."""
        values = self.defaults()
        if not self.path.exists():
            return values
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                payload = yaml.safe_load(fh) or {}
            controls = payload.get("controls", {}) if isinstance(payload, dict) else {}
            if not isinstance(controls, dict):
                raise ValueError("controls mapping is missing")
            for name in CONTROL_NAMES:
                values[name] = self.clamp_control(name, controls.get(name), values[name])
            return values
        except Exception as exc:
            log_error("pawprint", "control state load failed", repr(exc))
            return values

    def mark_dirty(self) -> None:
        """Handle the mark dirty lifecycle step."""
        self.dirty = True

    def save_if_due(self, values: Dict[str, float], interval_s: float, now: float) -> None:
        """Handle the save if due lifecycle step."""
        if self.dirty and now - self.last_save_at >= max(1.0, float(interval_s)):
            self.save(values, now)

    def save(self, values: Dict[str, float], now: Optional[float] = None) -> None:
        """Handle the save lifecycle step."""
        clean = {name: round(self.clamp_control(name, values[name], self.default_value), 6) for name in CONTROL_NAMES}
        payload = {
            "version": 1,
            "controls": clean,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    yaml.safe_dump(payload, fh, sort_keys=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            self.dirty = False
            self.last_save_at = time.monotonic() if now is None else now
        except Exception as exc:
            log_error("pawprint", "control state save failed", repr(exc))


class EventRing:
    """Fixed-size ring. Pawprint is the only writer; consumers track sequence."""

    def __init__(self, capacity: int):
        """Initialize configuration, dependencies, and runtime state."""
        self.capacity = max(8, int(capacity))
        self.seq = bus.get_int("/io/events/seq2", 0)
        self.records = [0] * (self.capacity * EVENT_RECORD_WIDTH)
        previous = bus.try_get_array("/io/events/ring", len(self.records), dtype="i32")
        if previous is not None and len(previous) == len(self.records):
            self.records = list(previous)
        else:
            bus.set_array("/io/events/ring", self.records, dtype="i32")
            bus.set_int("/io/events/capacity", self.capacity)
            bus.set_int("/io/events/record_width", EVENT_RECORD_WIDTH)
            bus.set_int("/io/events/seq", self.seq)
            bus.set_int("/io/events/seq2", self.seq)

    def push(self, event_type: int, control: int, value: int) -> None:
        """Handle the push lifecycle step."""
        self.seq = (self.seq + 1) & 0x7FFFFFFF
        if self.seq == 0:
            self.seq = 1
        index = self.seq % self.capacity
        offset = index * EVENT_RECORD_WIDTH
        now_ms_mod = int(time.monotonic() * 1000) & 0x7FFFFFFF
        bus.set_int("/io/events/seq", self.seq)
        self.records[offset:offset + EVENT_RECORD_WIDTH] = [
            self.seq,
            now_ms_mod,
            int(event_type),
            int(control),
            int(value),
        ]
        bus.set_array("/io/events/ring", self.records, dtype="i32")
        bus.set_int("/io/events/seq2", self.seq)


class PawprintHardware:
    """Owns I²C peripherals. GPIO stays independent if I²C flakes."""

    def __init__(self, cfg: dict):
        """Initialize configuration, dependencies, and runtime state."""
        self.cfg = cfg
        self.i2c = None
        self.ads = None
        self.matrix = None
        self.colour_chan = None
        self.gain_chan = None
        self.last_connect_attempt = 0.0
        self.last_matrix_frame = None
        self.active_i2c_fault_keys: set[str] = set()

    @property
    def online(self) -> bool:
        """Return the online result."""
        required = (self.ads, self.matrix, self.colour_chan)
        if not all(required):
            return False
        return self.gain_chan is not None or not bool(self.cfg.get("gain_enabled", True))

    @staticmethod
    def _scan_addresses(i2c) -> List[int]:
        """Return the scan addresses result."""
        while not i2c.try_lock():
            time.sleep(0.01)
        try:
            return list(i2c.scan())
        finally:
            i2c.unlock()

    def disconnect(self) -> None:
        """Handle the disconnect lifecycle step."""
        self.ads = None
        self.matrix = None
        self.colour_chan = None
        self.gain_chan = None
        self.last_matrix_frame = None
        if self.i2c is not None:
            try:
                self.i2c.deinit()
            except Exception:
                pass
        self.i2c = None
        bus.set_int("/io/health/i2c_ok", 0)
        bus.set_int("/io/health/ads_ok", 0)
        bus.set_int("/io/health/matrix_ok", 0)

    def connect_if_due(self, now: float) -> bool:
        """Return the connect if due result."""
        if self.online:
            return True
        if now - self.last_connect_attempt < float(self.cfg["i2c_retry_s"]):
            return False

        self.last_connect_attempt = now
        self.disconnect()
        try:
            self.i2c = busio.I2C(board.SCL, board.SDA)
            addresses = self._scan_addresses(self.i2c)
            ads_address = int(self.cfg["ads_address"])
            matrix_address = int(self.cfg["matrix_address"])
            if ads_address not in addresses or matrix_address not in addresses:
                missing = []
                if ads_address not in addresses:
                    missing.append(f"ADS1115 0x{ads_address:02X}")
                if matrix_address not in addresses:
                    missing.append(f"HT16K33 0x{matrix_address:02X}")
                fault_key = f"{self.cfg['proc_name']}:i2c:peripherals"
                self.active_i2c_fault_keys.add(fault_key)
                log_error(
                    str(self.cfg["proc_name"]),
                    "I2C peripheral missing",
                    ", ".join(missing),
                    event="i2c_peripheral_missing",
                    dedupe_key=fault_key,
                    metadata={"device": "i2c", "missing": missing},
                )
                self.disconnect()
                return False

            self.ads = ADS1115(self.i2c, address=ads_address)
            self.ads.gain = float(self.cfg["ads_gain"])
            self.colour_chan = AnalogIn(self.ads, ads1x15.Pin.A0)
            self.gain_chan = AnalogIn(self.ads, ads1x15.Pin.A1) if bool(self.cfg.get("gain_enabled", True)) else None

            self.matrix = Matrix16x8(self.i2c, address=matrix_address)
            self.matrix.auto_write = False
            self.matrix.blink_rate = 0
            self.matrix.fill(0)
            self.matrix.show()

            bus.set_int("/io/health/i2c_ok", 1)
            bus.set_int("/io/health/ads_ok", 1)
            bus.set_int("/io/health/matrix_ok", 1)
            for fault_key in sorted(self.active_i2c_fault_keys):
                log_recovery(
                    str(self.cfg["proc_name"]),
                    "i2c_connection_recovered",
                    recovers_key=fault_key,
                    metadata={"device": "i2c"},
                )
            self.active_i2c_fault_keys.clear()
            return True
        except Exception as exc:
            fault_key = f"{self.cfg['proc_name']}:i2c:connection"
            self.active_i2c_fault_keys.add(fault_key)
            log_error(
                str(self.cfg["proc_name"]),
                "I2C connection failed",
                repr(exc),
                event="i2c_connection_failed",
                dedupe_key=fault_key,
                metadata={"device": "i2c"},
            )
            self.disconnect()
            return False

    def read_pots(self) -> Tuple[float, Optional[float]]:
        """Read pots."""
        if not self.online:
            raise RuntimeError("I2C hardware is offline")
        max_v = max(0.1, float(self.cfg["pot_input_max_v"]))
        colour = clamp01(float(self.colour_chan.voltage) / max_v)
        gain = None
        if self.gain_chan is not None:
            gain = clamp01(float(self.gain_chan.voltage) / max_v)
        return colour, gain

    def write_matrix(self, graph_mask: int, top_mask: int, front_on: bool, brightness: float) -> None:
        """Write matrix."""
        if not self.online:
            return
        frame = (int(graph_mask) & 0x3FF, int(top_mask) & 0x1F, bool(front_on), round(clamp01(brightness), 3))
        if frame == self.last_matrix_frame:
            return
        try:
            self.matrix.brightness = frame[3]
            self.matrix.fill(0)
            for index, (col, row) in enumerate(GRAPH_COORDS):
                if frame[0] & (1 << index):
                    self.matrix[col, row] = 1
            for index, led_name in enumerate(TOP_LED_NAMES):
                if frame[1] & (1 << index):
                    col, row = LED_MAP[led_name]
                    self.matrix[col, row] = 1
            if frame[2]:
                col, row = LED_MAP["front"]
                self.matrix[col, row] = 1
            self.matrix.show()
            self.last_matrix_frame = frame
        except Exception as exc:
            log_error(str(self.cfg["proc_name"]), "I2C indicator write failed", repr(exc))
            self.disconnect()


class Pawprint:
    """Manage Pawprint state and behaviour."""
    def __init__(self, cfg: dict):
        """Initialize configuration, dependencies, and runtime state."""
        self.cfg = cfg
        self.proc_name = str(cfg["proc_name"])
        self.hardware = PawprintHardware(cfg)
        self.events = EventRing(int(cfg["event_capacity"]))
        self.running = True

        debounce = float(cfg["button_debounce_s"])
        self.buttons: Dict[str, Button] = {
            name: Button(pin, pull_up=True, bounce_time=debounce)
            for name, pin in BUTTON_PINS.items()
        }
        self.record_button = Button(RECORD_PIN, pull_up=True, bounce_time=debounce)
        self.sel3_a = Button(SEL3_PINS[0], pull_up=True, bounce_time=debounce)
        self.sel3_b = Button(SEL3_PINS[1], pull_up=True, bounce_time=debounce)
        self.sel4_a = Button(SEL4_PINS[0], pull_up=True, bounce_time=debounce)
        self.sel4_b = Button(SEL4_PINS[1], pull_up=True, bounce_time=debounce)

        self.button_stable: Dict[str, StableValue] = {name: StableValue() for name in BUTTON_PINS}
        self.record_stable = StableValue()
        self.selector_stable: Dict[str, StableValue] = {
            "source_selector": StableValue(),
            "visualiser_mode": StableValue(),
        }

        control_caps = {name: float(cfg.get(f"{name}_output_max", 1.0)) for name in CONTROL_NAMES}
        self.control_caps = {name: clamp01(value) for name, value in control_caps.items()}
        self.state_store = ControlStateStore(
            Path(str(cfg["state_path"])),
            float(cfg["default_control_value"]),
            self.control_caps,
        )
        self.controls = self.state_store.load()
        self.rec_bank = False  # always starts in the primary (REC-off) bank

        # Logical physical-pot values after calibration/curve/smoothing.
        self.pot_values: Dict[str, Optional[float]] = {"colour": None, "gain": None}
        self.pickups: Dict[bool, Dict[str, PickupState]] = {
            False: {"colour": PickupState(), "gain": PickupState()},
            True: {"colour": PickupState(), "gain": PickupState()},
        }
        self.last_hb = 0.0
        self.ads_read_fault_active = False

        # Pawprint-owned system status lamps.  The top five indicators are
        # deliberately not externally commanded; they reflect local host state.
        self.last_system_sample_at = 0.0
        self.last_network_total: Optional[int] = None
        self.last_disk_total: Optional[int] = None
        self.network_pulse_until = 0.0
        self.disk_pulse_until = 0.0
        self.storage_warning = False
        self.memory_warning = False

        # Private tape transport and shutdown hold state.
        self.media_keys = VirtualMediaKeys(
            str(cfg["uinput_path"]),
            str(cfg["media_key_device_name"]),
        )
        self.last_media_connect_attempt = 0.0
        self.bluetooth_media = BluetoothMediaController(cfg)
        self.last_media_source_refresh_at = 0.0
        self.last_media_target = MEDIA_TARGET_NONE
        self.last_media_activity_at = 0.0
        self._bt_raw_playing_since: Optional[float] = None
        self._bt_claim_until = 0.0
        self._bt_metadata_signature: Optional[Tuple[object, ...]] = None
        self.eject_pressed_at: Optional[float] = None
        self.eject_shutdown_fired = False
        self.record_pressed_at: Optional[float] = None
        self.record_source_cycle_fired = False
        self.record_source_cycle_fired_at: Optional[float] = None

        for name, value in self.controls.items():
            bus.set_float(f"/io/in/control/{name}", value)
        bus.set_int("/io/health/online", 0)
        bus.set_int("/io/health/media_keys_ok", 0)
        bus.set_int("/io/health/bluetooth_media_ok", 0)
        bus.set_int("/io/health/bluetooth_media_playing", 0)
        bus.set_int("/io/health/bluetooth_media_claimed", 0)
        bus.set_int("/io/health/plex_media_available", 0)
        bus.set_int("/io/health/mpris_media_available", 0)
        bus.set_int("/io/health/mpris_media_playing", 0)
        bus.set_int("/io/health/rec_source_hold", 0)
        bus.set_int("/io/health/rec_source_hold_mask", 0)
        bus.set_int("/io/health/media_target", MEDIA_TARGET_NONE)
        bus.set_int("/io/health/media_target_seq", 0)
        bus.set_int("/io/health/media_last_activity_ms", 0)

        # UI-facing active source mirrors the existing router's chosen target.
        # It is deliberately separate from /io/health so UI code has a stable
        # media namespace. Cold boot intentionally presents Plex as selected.
        bus.set_int(KEY_MEDIA_ACTIVE_SOURCE, MEDIA_TARGET_PLEX)
        bus.set_int(KEY_MEDIA_ACTIVE_SOURCE_SEQ, 1)
        bus.set_int(KEY_BT_METADATA_SEQ, 0)
        self._publish_bluetooth_metadata(BluetoothMediaState())

        # Pawprint owns output bus existence. Graph only is public; front and
        # top system/status lamps are intentionally Pawprint-private.
        bus.set_int("/io/out/valid", 0)
        bus.set_int("/io/out/led/graph_mask", 0)
        # Explicit override is deliberately opt-in. Normal operation is the
        # Pawprint-owned track-progress bar below.
        bus.set_int("/io/out/led/graph_override", 0)
        bus.set_float("/io/out/led/brightness", float(cfg["default_indicator_brightness"]))
        # Pawprint is the sole producer of long-hold source-cycle requests.
        # Initialising rather than resetting preserves a live controller if
        # Pawprint itself restarts.
        bus.set_int(KEY_HDMI2_CYCLE_SEQ, bus.get_int(KEY_HDMI2_CYCLE_SEQ, 0))
        bus.set_int("/io/health/output_bus_ready", 1)

    @staticmethod
    def _bits(a_button: Button, b_button: Button) -> int:
        # gpiozero is_pressed means LOW. Existing electrical map uses HIGH=1.
        """Return the bits result."""
        a = 0 if a_button.is_pressed else 1
        b = 0 if b_button.is_pressed else 1
        return (b << 1) | a

    def _read_selector(self, which: str) -> Tuple[int, Optional[int]]:
        """Return the read selector result."""
        if which == "source_selector":
            bits = self._bits(self.sel3_a, self.sel3_b)
            return bits, SEL3_LOOKUP.get(bits)
        bits = self._bits(self.sel4_a, self.sel4_b)
        return bits, SEL4_LOOKUP.get(bits)

    def _ensure_media_keys(self, now: float) -> None:
        """Handle the ensure media keys lifecycle step."""
        if not bool(self.cfg.get("media_keys_enabled", True)):
            bus.set_int("/io/health/media_keys_ok", 0)
            return
        if self.media_keys.online:
            bus.set_int("/io/health/media_keys_ok", 1)
            return
        if now - self.last_media_connect_attempt < max(1.0, float(self.cfg["media_reconnect_s"])):
            return
        self.last_media_connect_attempt = now
        try:
            self.media_keys.open()
            bus.set_int("/io/health/media_keys_ok", 1)
            status_note(self.proc_name, "virtual media keys ready")
        except Exception as exc:
            bus.set_int("/io/health/media_keys_ok", 0)
            log_error(self.proc_name, "virtual media key setup failed", repr(exc))

    def _send_media_key(self, name: str) -> None:
        """Send media key."""
        codes = {
            "play": KEY_PLAYPAUSE,
            "stop": KEY_STOPCD,
            "ff": KEY_NEXTSONG,
            "rew": KEY_PREVIOUSSONG,
        }
        code = codes.get(name)
        if code is None:
            return
        if not self.media_keys.online:
            log_error(self.proc_name, "media key ignored: virtual device offline", name)
            return
        try:
            self.media_keys.press(code)
            status_note(self.proc_name, f"media key sent: {name}")
        except Exception as exc:
            bus.set_int("/io/health/media_keys_ok", 0)
            log_error(self.proc_name, "virtual media key write failed", repr(exc))

    def _request_shutdown(self) -> None:
        """Handle the request shutdown lifecycle step."""
        if not bool(self.cfg.get("shutdown_enabled", True)):
            status_note(self.proc_name, "shutdown requested but disabled")
            return
        helper = str(self.cfg["shutdown_helper"])
        try:
            subprocess.Popen(
                ["sudo", "-n", helper],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            status_note(self.proc_name, "orderly shutdown requested")
        except Exception as exc:
            log_error(self.proc_name, "orderly shutdown request failed", repr(exc))

    def _plex_is_available(self, now: float) -> bool:
        """Return the plex is available result."""
        heartbeat_ms = bus.get_int("/proc/minstrel/heartbeat_ms", 0)
        if heartbeat_ms <= 0:
            return False
        now_ms = int(now * 1000)
        age_ms = now_ms - heartbeat_ms
        max_age_ms = int(max(1.0, float(self.cfg.get("minstrel_heartbeat_stale_s", 5.0))) * 1000)
        return 0 <= age_ms <= max_age_ms

    def _publish_bluetooth_metadata(self, state: BluetoothMediaState) -> None:
        """Publish Bluetooth data without ever leaving stale track text behind.

        Pawprint is the sole writer for /bt/*. Missing AVRCP fields are explicit
        ``N/A`` strings (or 0/-1 for numeric fields), so the eventual UI never
        has to guess whether an old title belongs to a disconnected phone.
        """
        if state.status == "playing":
            play_state = BT_PLAY_PLAYING
        elif state.status == "paused":
            play_state = BT_PLAY_PAUSED
        else:
            play_state = BT_PLAY_STOPPED

        bus.set_int(KEY_BT_CONNECTED, int(state.connected))
        bus.set_int(KEY_BT_PLAY_STATE, play_state)
        bus.set_int(KEY_BT_POSITION_MS, int(state.position_ms))
        bus.set_int(KEY_BT_DURATION_MS, int(state.duration_ms))
        bus.set_int(KEY_BT_TRACK_NUMBER, int(state.track_number))
        bus.set_int(KEY_BT_TRACK_COUNT, int(state.track_count))
        bus.set_int(KEY_BT_PHONE_BATTERY_PCT, int(state.phone_battery_pct))
        publish_utf8(KEY_BT_TITLE_UTF8, state.title)
        publish_utf8(KEY_BT_ARTIST_UTF8, state.artist)
        publish_utf8(KEY_BT_ALBUM_UTF8, state.album)
        publish_utf8(KEY_BT_PLAYER_NAME_UTF8, state.player_name)
        publish_utf8(KEY_BT_DEVICE_NAME_UTF8, state.device_name)

        signature = (
            state.connected, state.status, state.device_name, state.player_name,
            state.title, state.artist, state.album, state.duration_ms,
            state.track_number, state.track_count, state.phone_battery_pct,
        )
        if signature != self._bt_metadata_signature:
            self._bt_metadata_signature = signature
            bus.set_int(KEY_BT_METADATA_SEQ, bus.get_int(KEY_BT_METADATA_SEQ, 0) + 1)

    @staticmethod
    def _real_bt_text(value: object) -> bool:
        """Return the real bt text result."""
        text = str(value or "").strip()
        return bool(text and text.upper() != "N/A")

    def _bluetooth_metadata_looks_like_media(self, state: BluetoothMediaState) -> bool:
        """Return the bluetooth metadata looks like media result."""
        if not bool(self.cfg.get("bluetooth_claim_metadata_enabled", True)):
            return False
        has_title = self._real_bt_text(state.title)
        has_artist_or_album = self._real_bt_text(state.artist) or self._real_bt_text(state.album)
        duration_ok = state.duration_ms >= int(self.cfg.get("bluetooth_claim_min_duration_ms", 30000))
        return bool(has_title and (has_artist_or_album or duration_ok))

    def _bluetooth_should_claim(self, state: BluetoothMediaState, now: float) -> bool:
        """Return the bluetooth should claim result."""
        if not state.connected:
            self._bt_raw_playing_since = None
            self._bt_claim_until = 0.0
            return False

        if state.playing:
            if self._bt_raw_playing_since is None:
                self._bt_raw_playing_since = now
            continuous_s = now - self._bt_raw_playing_since
            continuous_ok = continuous_s >= max(0.0, float(self.cfg.get("bluetooth_claim_threshold_s", 2.0)))
            if self._bluetooth_metadata_looks_like_media(state) or continuous_ok:
                self._bt_claim_until = now + max(0.0, float(self.cfg.get("bluetooth_claim_linger_s", 45.0)))
                return True
            return False

        self._bt_raw_playing_since = None
        return now < self._bt_claim_until

    def _set_last_media_target(self, target: int, now: float, reason: str) -> None:
        """Set last media target."""
        target = int(target)
        changed = target != self.last_media_target
        self.last_media_target = target
        self.last_media_activity_at = now
        bus.set_int("/io/health/media_target", target)
        bus.set_int("/io/health/media_last_activity_ms", int(now * 1000))
        bus.set_int(KEY_MEDIA_ACTIVE_SOURCE, target)
        if changed:
            bus.set_int("/io/health/media_target_seq", bus.get_int("/io/health/media_target_seq", 0) + 1)
            bus.set_int(KEY_MEDIA_ACTIVE_SOURCE_SEQ, bus.get_int(KEY_MEDIA_ACTIVE_SOURCE_SEQ, 0) + 1)
            status_note(self.proc_name, f"media target: {MEDIA_TARGET_NAMES.get(target, 'unknown')} ({reason})")

    def _update_media_sources(self, now: float, *, force: bool = False) -> None:
        """Update media sources."""
        interval = max(0.10, float(self.cfg.get("media_source_refresh_s", 0.75)))
        if not force and now - self.last_media_source_refresh_at < interval:
            return
        self.last_media_source_refresh_at = now

        bt_state = self.bluetooth_media.refresh()
        self._publish_bluetooth_metadata(bt_state)
        bt_claim = self._bluetooth_should_claim(bt_state, now)
        plex_available = self._plex_is_available(now)
        plex_playing = plex_available and bus.get_int("/plex/play_state", PLEX_PLAYING) == PLEX_PLAYING

        bus.set_int("/io/health/bluetooth_media_ok", int(bt_state.connected))
        bus.set_int("/io/health/bluetooth_media_playing", int(bt_state.playing))
        bus.set_int("/io/health/bluetooth_media_claimed", int(bt_claim))
        bus.set_int("/io/health/plex_media_available", int(plex_available))

        # Live playback outranks remembered preference. This is how starting a
        # phone stream teaches Pawprint that Bluetooth is now the active source.
        mpris_available = bool(bus.get_int(KEY_MPRIS_AVAILABLE, 0))
        mpris_playing = mpris_available and bus.get_int(KEY_MPRIS_PLAY_STATE, BT_PLAY_STOPPED) == BT_PLAY_PLAYING
        hdmi2_source = bus.get_int(KEY_HDMI2_SELECTED_SOURCE, HDMI2_SOURCE_PLEX)

        bus.set_int("/io/health/mpris_media_available", int(mpris_available))
        bus.set_int("/io/health/mpris_media_playing", int(mpris_playing))

        if plex_playing:
            self._set_last_media_target(MEDIA_TARGET_PLEX, now, "Plex playing")
        elif bt_claim:
            self._set_last_media_target(MEDIA_TARGET_BLUETOOTH, now, "Bluetooth claimed")
        elif mpris_playing:
            self._set_last_media_target(MEDIA_TARGET_MPRIS, now, "MPRIS playing")
        elif hdmi2_source in (HDMI2_SOURCE_RADIO, HDMI2_SOURCE_YOUTUBE):
            # Selecting a local HDMI-2 context must immediately route a future
            # physical Play press to MPRIS, even while the guest app is still
            # launching or paused. Bluetooth still wins if it is actually live.
            self._set_last_media_target(MEDIA_TARGET_MPRIS, now, "HDMI-2 local selected")
        elif hdmi2_source == HDMI2_SOURCE_PLEX and self.last_media_target == MEDIA_TARGET_MPRIS:
            self._set_last_media_target(MEDIA_TARGET_PLEX, now, "HDMI-2 Plex selected")

    def _resolve_media_target(self, now: float) -> int:
        """Choose one destination, never broadcast controls to every source.

        Live playback wins; otherwise use the last successful/observed target.
        On a cold boot, where there is no remembered target yet, Plex is the
        intentional default. This is a physical jukebox, not a committee.
        """
        self._update_media_sources(now, force=True)
        bt_state = self.bluetooth_media.state
        bt_claim = self._bluetooth_should_claim(bt_state, now)
        plex_available = self._plex_is_available(now)
        plex_playing = plex_available and bus.get_int("/plex/play_state", PLEX_PLAYING) == PLEX_PLAYING
        mpris_available = bool(bus.get_int(KEY_MPRIS_AVAILABLE, 0))
        mpris_playing = mpris_available and bus.get_int(KEY_MPRIS_PLAY_STATE, BT_PLAY_STOPPED) == BT_PLAY_PLAYING
        hdmi2_source = bus.get_int(KEY_HDMI2_SELECTED_SOURCE, HDMI2_SOURCE_PLEX)

        if plex_playing:
            target = MEDIA_TARGET_PLEX
            reason = "Plex playing"
        elif bt_claim:
            target = MEDIA_TARGET_BLUETOOTH
            reason = "Bluetooth claimed"
        elif mpris_playing:
            target = MEDIA_TARGET_MPRIS
            reason = "MPRIS playing"
        elif hdmi2_source in (HDMI2_SOURCE_RADIO, HDMI2_SOURCE_YOUTUBE):
            target = MEDIA_TARGET_MPRIS
            reason = "selected HDMI-2 local"
        elif self.last_media_target == MEDIA_TARGET_BLUETOOTH and bt_claim:
            target = MEDIA_TARGET_BLUETOOTH
            reason = "remembered Bluetooth"
        elif self.last_media_target == MEDIA_TARGET_PLEX and plex_available:
            target = MEDIA_TARGET_PLEX
            reason = "remembered Plex"
        elif self.last_media_target == MEDIA_TARGET_MPRIS and mpris_available:
            target = MEDIA_TARGET_MPRIS
            reason = "remembered MPRIS"
        elif self.last_media_target == MEDIA_TARGET_NONE:
            # Cold boot: assume the Deck's own Plexamp is intended. If the
            # workers are still booting, the touchscreen remains the manual
            # override until Minstrel is alive again.
            target = MEDIA_TARGET_PLEX
            reason = "cold boot default"
        elif plex_available:
            target = MEDIA_TARGET_PLEX
            reason = "Plex fallback"
        elif bt_claim:
            target = MEDIA_TARGET_BLUETOOTH
            reason = "Bluetooth fallback"
        else:
            target = MEDIA_TARGET_MPRIS
            reason = "MPRIS fallback"

        self._set_last_media_target(target, now, reason)
        return target

    def _send_plex_request(self, name: str) -> bool:
        """Send plex request."""
        key = PLEX_CONTROL_KEYS.get(name)
        if key is None:
            return False
        seq = (bus.get_int(key, 0) + 1) & 0x7FFFFFFF
        if seq == 0:
            seq = 1
        bus.set_int(key, seq)
        return True

    def _send_mpris_request(self, name: str) -> bool:
        """Send mpris request."""
        key = MPRIS_CONTROL_KEYS.get(name)
        if key is None:
            return False
        seq = (bus.get_int(key, 0) + 1) & 0x7FFFFFFF
        bus.set_int(key, 1 if seq == 0 else seq)
        return True

    def _send_bluetooth_media(self, name: str) -> Tuple[bool, str]:
        """Send bluetooth media."""
        if name == "play":
            action = "pause" if self.bluetooth_media.state.playing else "play"
        elif name == "stop":
            action = "stop"
        elif name == "ff":
            action = "next"
        elif name == "rew":
            action = "previous"
        else:
            return False, "unknown transport command"
        return self.bluetooth_media.send(action)

    def _route_transport_command(self, name: str, now: float) -> None:
        """Handle the route transport command lifecycle step."""
        target = self._resolve_media_target(now)
        if target == MEDIA_TARGET_PLEX:
            if self._send_plex_request(name):
                status_note(self.proc_name, f"media command routed to Plex: {name}")
            else:
                log_error(self.proc_name, "Plex media command rejected", name)
            return

        if target == MEDIA_TARGET_BLUETOOTH:
            ok, detail = self._send_bluetooth_media(name)
            if ok:
                status_note(self.proc_name, f"media command routed to Bluetooth: {name}")
            else:
                log_error(self.proc_name, "Bluetooth media command failed", f"{name}: {detail}")
            return

        if target == MEDIA_TARGET_MPRIS:
            if self._send_mpris_request(name):
                status_note(self.proc_name, f"media command routed to MPRIS: {name}")
            else:
                log_error(self.proc_name, "MPRIS media command rejected", name)
            return

        self._send_media_key(name)

    def _on_transport_edge(self, name: str, pressed: int, now: float) -> None:
        """Handle the on transport edge lifecycle step."""
        if name == "eject":
            if pressed:
                self.eject_pressed_at = now
                self.eject_shutdown_fired = False
            else:
                self.eject_pressed_at = None
            return
        if pressed:
            self._route_transport_command(name, now)

    def _debounce_button(self, name: str, pressed: int, now: float) -> None:
        """Consume tape transport locally; do not publish it on the I/O bus."""
        state = self.button_stable[name]
        if state.candidate != pressed:
            state.candidate = pressed
            state.changed_at = now
        if state.stable is None:
            # Do not issue a media command simply because a button was held
            # while the Deck booted.
            state.stable = pressed
            return
        if state.stable != state.candidate and now - state.changed_at >= float(self.cfg["button_debounce_s"]):
            state.stable = int(state.candidate)
            self._on_transport_edge(name, state.stable, now)

    def _update_eject_hold(self, now: float) -> None:
        """Update eject hold."""
        if self.eject_shutdown_fired or self.eject_pressed_at is None:
            return
        held_for = now - self.eject_pressed_at
        if held_for >= max(1.0, float(self.cfg["eject_shutdown_hold_s"])):
            self.eject_shutdown_fired = True
            self._request_shutdown()

    def _toggle_rec_bank(self) -> None:
        """Handle the toggle rec bank lifecycle step."""
        self.rec_bank = not self.rec_bank
        # A bank switch must never instantly jump a restored value to a pot's
        # unrelated physical position. Require pickup again.
        self.pickups[self.rec_bank] = {"colour": PickupState(), "gain": PickupState()}

    def _request_hdmi2_cycle(self) -> None:
        """Handle the request hdmi2 cycle lifecycle step."""
        seq = (bus.get_int(KEY_HDMI2_CYCLE_SEQ, 0) + 1) & 0x7FFFFFFF
        bus.set_int(KEY_HDMI2_CYCLE_SEQ, 1 if seq == 0 else seq)
        status_note(self.proc_name, "HDMI-2 source cycle requested")

    def _debounce_record_toggle(self, pressed: int, now: float) -> None:
        """REC tap changes pot bank; a long hold cycles the HDMI-2 context."""
        state = self.record_stable
        if state.candidate != pressed:
            state.candidate = pressed
            state.changed_at = now
        if state.stable is None:
            state.stable = pressed
            return
        if state.stable != state.candidate and now - state.changed_at >= float(self.cfg["button_debounce_s"]):
            state.stable = int(state.candidate)
            if state.stable:
                self.record_pressed_at = now
                self.record_source_cycle_fired = False
                self.record_source_cycle_fired_at = None
            else:
                if not self.record_source_cycle_fired:
                    self._toggle_rec_bank()
                    self.record_pressed_at = None
                    self.record_source_cycle_fired_at = None
                else:
                    # Keep the all-lit confirmation for a small, controlled
                    # moment after release. The next press remains blocked only
                    # until this feedback expires.
                    self.record_pressed_at = None

    def _update_record_source_hold(self, now: float) -> None:
        """Update record source hold."""
        if self.record_pressed_at is None or self.record_source_cycle_fired:
            return
        if now - self.record_pressed_at >= max(1.0, float(self.cfg["record_source_hold_s"])):
            self.record_source_cycle_fired = True
            self.record_source_cycle_fired_at = now
            self._request_hdmi2_cycle()

    def _debounce_selector(self, name: str, position: Optional[int], raw_bits: int, now: float) -> None:
        """Handle the debounce selector lifecycle step."""
        bus.set_int(f"/io/in/selector/{name}_bits", raw_bits)
        candidate = -1 if position is None else int(position)
        state = self.selector_stable[name]
        if state.candidate != candidate:
            state.candidate = candidate
            state.changed_at = now
        if state.stable is None:
            state.stable = candidate
            bus.set_int(f"/io/in/selector/{name}", candidate)
            if name == "visualiser_mode":
                bus.set_int("/io/in/selector/visualiser_mode_index", candidate - 1 if candidate > 0 else -1)
            return
        if state.stable != state.candidate and now - state.changed_at >= float(self.cfg["selector_debounce_s"]):
            state.stable = int(state.candidate)
            bus.set_int(f"/io/in/selector/{name}", state.stable)
            if name == "visualiser_mode":
                bus.set_int("/io/in/selector/visualiser_mode_index", state.stable - 1 if state.stable > 0 else -1)
            self.events.push(EVENT_SELECTOR, EVENT_CODES[name], state.stable)

    def _remap_pot(self, raw: float, physical: str) -> float:
        """Return the remap pot result."""
        lo = float(self.cfg[f"{physical}_input_min"])
        hi = float(self.cfg[f"{physical}_input_max"])
        curve = max(0.05, float(self.cfg[f"{physical}_curve"]))
        if hi <= lo:
            return clamp01(raw)
        normalised = clamp01((raw - lo) / (hi - lo))
        return clamp01(normalised ** curve)

    def _target_for(self, physical: str) -> str:
        """Return the target for result."""
        if physical == "gain":
            return "brightness" if self.rec_bank else "gain"
        return "saturation" if self.rec_bank else "colour"

    def _deadband_for(self, physical: str) -> float:
        """Return the deadband for result."""
        return max(0.0, float(self.cfg[f"{physical}_deadband"]))

    def _pickup_reached(self, pickup: PickupState, pot_value: float, target_value: float) -> bool:
        """Return the pickup reached result."""
        tolerance = max(0.0, float(self.cfg["pickup_tolerance"]))
        if abs(pot_value - target_value) <= tolerance:
            return True
        previous = pickup.previous_value
        if previous is not None:
            before = previous - target_value
            after = pot_value - target_value
            if before == 0.0 or after == 0.0 or (before < 0.0 < after) or (after < 0.0 < before):
                return True
        return False

    def _apply_pot(self, physical: str, value: float) -> None:
        """Handle the apply pot lifecycle step."""
        target = self._target_for(physical)
        target_cap = self.control_caps.get(target, 1.0)
        value = min(target_cap, clamp01(value))
        pickup = self.pickups[self.rec_bank][physical]
        current = min(target_cap, self.controls[target])

        if not pickup.acquired:
            if self._pickup_reached(pickup, value, current):
                pickup.acquired = True
            pickup.previous_value = value
            if not pickup.acquired:
                return

        pickup.previous_value = value
        if abs(value - current) < self._deadband_for(physical):
            return
        self.controls[target] = value
        bus.set_float(f"/io/in/control/{target}", value)
        self.state_store.mark_dirty()

    def _publish_pots(self) -> None:
        """Publish pots."""
        if not self.hardware.online:
            return
        try:
            raw_colour, raw_gain = self.hardware.read_pots()
        except Exception as exc:
            log_error(
                self.proc_name,
                "ADS1115 read failed",
                repr(exc),
                event="ads1115_read_failed",
                dedupe_key=f"{self.proc_name}:ads1115:read",
                metadata={"device": "ads1115"},
            )
            self.ads_read_fault_active = True
            self.hardware.disconnect()
            return

        if self.ads_read_fault_active:
            log_recovery(
                self.proc_name,
                "ads1115_read_recovered",
                recovers_key=f"{self.proc_name}:ads1115:read",
                metadata={"device": "ads1115"},
            )
            self.ads_read_fault_active = False

        alpha = clamp01(float(self.cfg["pot_smoothing"]))
        logical_samples = {
            "colour": self._remap_pot(raw_colour, "colour"),
        }
        if raw_gain is not None:
            logical_samples["gain"] = self._remap_pot(raw_gain, "gain")
        for physical, raw in logical_samples.items():
            previous = self.pot_values[physical]
            filtered = raw if previous is None else previous + alpha * (raw - previous)
            self.pot_values[physical] = filtered
            self._apply_pot(physical, filtered)

    @staticmethod
    def _read_network_total_bytes() -> int:
        """Return RX+TX bytes across non-loopback interfaces."""
        total = 0
        try:
            lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
            for line in lines:
                if ":" not in line:
                    continue
                interface, payload = line.split(":", 1)
                if interface.strip() == "lo":
                    continue
                fields = payload.split()
                if len(fields) >= 9:
                    total += int(fields[0]) + int(fields[8])
        except (OSError, ValueError):
            pass
        return total

    @staticmethod
    def _read_disk_total_sectors() -> int:
        """Return a coarse read+write sector counter for real block devices."""
        total = 0
        try:
            for line in Path("/proc/diskstats").read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) < 10:
                    continue
                name = fields[2]
                if name.startswith(("loop", "ram", "zram", "fd", "sr")):
                    continue
                # Field positions follow Linux /proc/diskstats: sectors read
                # and sectors written. Counting partitions as well is harmless
                # here because this is a pulse indicator, not an accounting tool.
                total += int(fields[5]) + int(fields[9])
        except (OSError, ValueError):
            pass
        return total

    @staticmethod
    def _read_mem_available_percent() -> Optional[float]:
        """Return the read mem available percent result."""
        total_kb = None
        available_kb = None
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, remainder = line.partition(":")
                value = remainder.strip().split()
                if not value:
                    continue
                if key == "MemTotal":
                    total_kb = int(value[0])
                elif key == "MemAvailable":
                    available_kb = int(value[0])
        except (OSError, ValueError):
            return None
        if not total_kb or available_kb is None:
            return None
        return 100.0 * available_kb / total_kb

    def _update_system_status(self, now: float) -> None:
        """Update private top-lamp state from local OS counters.

        The lamps intentionally read local state directly.  Nobody else has to
        maintain a fragile indicator protocol just to say that the Deck is
        alive, moving packets, or running out of resources.
        """
        interval = max(0.10, float(self.cfg["system_sample_interval_s"]))
        if now - self.last_system_sample_at < interval:
            return
        self.last_system_sample_at = now

        network_total = self._read_network_total_bytes()
        if self.last_network_total is not None:
            network_delta = max(0, network_total - self.last_network_total)
            if network_delta >= max(1, int(self.cfg["network_activity_min_bytes"])):
                self.network_pulse_until = max(
                    self.network_pulse_until,
                    now + max(0.04, float(self.cfg["network_pulse_s"])),
                )
        self.last_network_total = network_total

        disk_total = self._read_disk_total_sectors()
        if self.last_disk_total is not None:
            disk_delta = max(0, disk_total - self.last_disk_total)
            if disk_delta >= max(1, int(self.cfg["disk_activity_min_sectors"])):
                self.disk_pulse_until = max(
                    self.disk_pulse_until,
                    now + max(0.04, float(self.cfg["disk_pulse_s"])),
                )
        self.last_disk_total = disk_total

        try:
            usage = shutil.disk_usage(str(self.cfg["storage_path"]))
            free_percent = 100.0 * usage.free / usage.total if usage.total else 0.0
            min_free_bytes = max(0.0, float(self.cfg["storage_warning_min_free_gb"])) * (1024 ** 3)
            min_free_percent = max(0.0, float(self.cfg["storage_warning_min_free_percent"]))
            self.storage_warning = usage.free < min_free_bytes or free_percent < min_free_percent
        except OSError:
            # An unreadable root filesystem is bad enough to deserve the warning.
            self.storage_warning = True

        mem_available_percent = self._read_mem_available_percent()
        if mem_available_percent is not None:
            self.memory_warning = mem_available_percent < max(0.0, float(self.cfg["memory_warning_min_available_percent"]))

    def _heartbeat_level(self, now: float) -> bool:
        # Green 1: double heartbeat, then a quiet pause.
        """Return the heartbeat level result."""
        period = max(0.40, float(self.cfg["heartbeat_period_s"]))
        phase = (now % period) / period
        return phase < 0.10 or 0.18 <= phase < 0.30

    def _system_top_mask(self, now: float) -> int:
        """Return the system top mask result."""
        mask = 0
        if self._heartbeat_level(now):
            mask |= 1 << 0  # Green 1: Pawprint/Deck heartbeat
        if now < self.network_pulse_until:
            mask |= 1 << 1  # Green 2: network activity
        if now < self.disk_pulse_until:
            mask |= 1 << 2  # Amber: storage activity
        if self.storage_warning:
            mask |= 1 << 3  # Red 1: low/unreadable storage
        if self.memory_warning:
            mask |= 1 << 4  # Red 2: low available RAM
        return mask

    def _front_led_level(self, now: float) -> bool:
        """REC lamp, temporarily overridden by the Eject shutdown hold."""
        if self.eject_pressed_at is not None and not self.eject_shutdown_fired:
            # Rapid visual countdown while Eject is held. The poweroff helper
            # fires only after the configured hold duration.
            hz = max(1.0, float(self.cfg["shutdown_blink_hz"]))
            return int(now * hz * 2.0) % 2 == 0
        return self.rec_bank

    def _track_progress_mask(self) -> int:
        """Return the Pawprint-owned 10-segment track-progress fill.

        The analogue VU already owns loudness. These LEDs instead answer the
        useful physical question: how far through the current track are we?
        Plex publishes a smooth seconds clock; Bluetooth publishes AVRCP
        millisecond snapshots. Paused tracks deliberately retain their fill.
        """
        if str(self.cfg.get("graph_mode", "progress")).strip().lower() != "progress":
            return 0

        segments = max(1, min(len(GRAPH_COORDS), int(self.cfg.get("graph_progress_segments", len(GRAPH_COORDS)))))
        source = bus.get_int(KEY_MEDIA_ACTIVE_SOURCE, MEDIA_TARGET_NONE)

        if source == MEDIA_TARGET_PLEX:
            state = bus.get_int("/plex/play_state", 0)
            position = bus.get_float("/plex/position_sec", 0.0)
            duration = bus.get_float("/plex/duration_sec", 0.0)
        elif source == MEDIA_TARGET_BLUETOOTH:
            state = bus.get_int(KEY_BT_PLAY_STATE, BT_PLAY_STOPPED)
            position = bus.get_int(KEY_BT_POSITION_MS, 0) / 1000.0
            duration = bus.get_int(KEY_BT_DURATION_MS, 0) / 1000.0
        elif source == MEDIA_TARGET_MPRIS:
            state = bus.get_int(KEY_MPRIS_PLAY_STATE, BT_PLAY_STOPPED)
            position = bus.get_int(KEY_MPRIS_POSITION_MS, 0) / 1000.0
            duration = bus.get_int(KEY_MPRIS_DURATION_MS, 0) / 1000.0
        else:
            return 0

        if state not in (PLEX_PLAYING, BT_PLAY_PAUSED) or duration <= 0.0:
            return 0

        fraction = max(0.0, min(1.0, float(position) / float(duration)))
        filled = int(math.floor(fraction * segments + 1e-9))
        if fraction > 0.0 and bool(self.cfg.get("graph_progress_show_first_segment", True)):
            filled = max(1, filled)
        if fraction >= 0.995:
            filled = segments

        return (1 << max(0, min(segments, filled))) - 1

    def _record_hold_graph_mask(self, now: float) -> Optional[int]:
        """Return REC source-hold progress, or ``None`` when REC is normal.

        This has deliberate priority over both track progress and external graph
        effects. A source change is a physical context operation, so the user
        deserves an unmistakable five-second fill rather than a silent mode
        switch hidden behind a track-progress bar.
        """
        if self.record_pressed_at is None:
            confirm_s = max(0.0, float(self.cfg.get("record_source_hold_confirm_s", 0.0)))
            fired_at = self.record_source_cycle_fired_at
            if self.record_source_cycle_fired and fired_at is not None and now - fired_at < confirm_s:
                mask = (1 << len(GRAPH_COORDS)) - 1
                bus.set_int("/io/health/rec_source_hold", 1)
                bus.set_int("/io/health/rec_source_hold_mask", mask)
                return mask
            self.record_source_cycle_fired = False
            self.record_source_cycle_fired_at = None
            bus.set_int("/io/health/rec_source_hold", 0)
            bus.set_int("/io/health/rec_source_hold_mask", 0)
            return None
        hold_s = max(1.0, float(self.cfg["record_source_hold_s"]))
        held = max(0.0, now - self.record_pressed_at)
        if self.record_source_cycle_fired:
            held = hold_s
        fraction = max(0.0, min(1.0, held / hold_s))
        # Floor, not ceil: with ten LEDs, ceil lit the final segment at
        # 4.5 s of a 5.0 s hold. That made the bar look complete before the
        # source-cycle threshold had actually fired. The final LED now comes
        # on only when the threshold is reached (or once fired above clamps
        # ``held`` to ``hold_s``).
        filled = int(math.floor(fraction * len(GRAPH_COORDS) + 1e-9))
        mask = (1 << max(0, min(len(GRAPH_COORDS), filled))) - 1
        bus.set_int("/io/health/rec_source_hold", 1)
        bus.set_int("/io/health/rec_source_hold_mask", mask)
        return mask

    def _render_outputs(self, now: float) -> None:
        """Draw graph, including the REC source-cycle hold takeover."""
        hold_mask = self._record_hold_graph_mask(now)
        override_enabled = bool(self.cfg.get("graph_external_override_enabled", True))
        override_active = override_enabled and bool(bus.get_int("/io/out/led/graph_override", 0))

        if hold_mask is not None:
            graph_mask = hold_mask
            brightness = float(self.cfg["default_indicator_brightness"])
        elif override_active:
            graph_mask = bus.get_int("/io/out/led/graph_mask", 0)
            brightness = bus.get_float("/io/out/led/brightness", float(self.cfg["default_indicator_brightness"]))
        else:
            graph_mask = self._track_progress_mask()
            brightness = float(self.cfg["default_indicator_brightness"])

        bus.set_int("/io/health/graph_progress_mask", int(graph_mask) & 0x3FF)
        self.hardware.write_matrix(
            graph_mask=graph_mask,
            top_mask=self._system_top_mask(now),
            # Front red: REC bank, unless Eject is being held for shutdown.
            front_on=self._front_led_level(now),
            brightness=brightness,
        )

    def _publish_status(self) -> None:
        """Publish status."""
        online = int(self.hardware.online)
        bus.set_int("/io/health/online", online)
        bus.set_int("/io/health/media_keys_ok", int(self.media_keys.online))
        if not online:
            bus.set_int("/io/health/i2c_ok", 0)
            bus.set_int("/io/health/ads_ok", 0)
            bus.set_int("/io/health/matrix_ok", 0)

    def run(self) -> None:
        """Run the component until shutdown."""
        log_info(self.proc_name, "started")
        log_info(self.proc_name, "enter_main_loop")
        period = 1.0 / max(1, int(self.cfg["poll_hz"]))
        next_tick = time.monotonic()

        while self.running:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
                now = time.monotonic()
            next_tick = max(next_tick + period, now)

            self.hardware.connect_if_due(now)
            self._ensure_media_keys(now)
            self._publish_status()
            self._update_media_sources(now)

            for name, button in self.buttons.items():
                self._debounce_button(name, int(button.is_pressed), now)
            self._update_eject_hold(now)
            self._debounce_record_toggle(int(self.record_button.is_pressed), now)
            self._update_record_source_hold(now)

            source_bits, source_position = self._read_selector("source_selector")
            self._debounce_selector("source_selector", source_position, source_bits, now)
            mode_bits, mode_position = self._read_selector("visualiser_mode")
            self._debounce_selector("visualiser_mode", mode_position, mode_bits, now)

            self._publish_pots()
            self._update_system_status(now)
            self._render_outputs(now)
            self.state_store.save_if_due(self.controls, float(self.cfg["state_save_interval_s"]), now)

            if now - self.last_hb >= 1.0:
                heartbeat(self.proc_name)
                self.last_hb = now

        self.state_store.save(self.controls)
        self.media_keys.close()
        bus.set_int("/io/health/media_keys_ok", 0)
        self.hardware.disconnect()
        log_info(self.proc_name, "stopped")


def main() -> None:
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    app = Pawprint(cfg)

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
