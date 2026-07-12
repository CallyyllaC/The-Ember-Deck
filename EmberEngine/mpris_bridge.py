#!/usr/bin/env python3
"""Generic MPRIS bridge for Ember Deck local HDMI-2 applications.

This worker deliberately knows nothing about Shortwave or Pear user interfaces.
It observes one selected local MPRIS player, normalises its metadata into the
Synapse ``/mpris/*`` namespace, and consumes direct transport command counters.
Pawprint remains the owner of physical buttons and route selection.

``playerctl`` is used rather than a Python D-Bus dependency so the bridge works
with the same host-session MPRIS path that was validated for the Flatpak radio
app. The controller limits discovery to the currently selected HDMI-2 source,
which keeps unrelated desktop media players out of the Deck route.
"""
from __future__ import annotations

import io
import math
import os
import re
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import yaml

import synapse as bus
from whisper_daemon import log_error, log_heartbeat, log_info

try:  # Artwork remains optional. Playback and metadata must survive without it.
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - depends on target image install
    Image = None
    ImageOps = None


DEFAULTS = {
    "proc_name": "mpris_bridge",
    "poll_hz": 10.0,
    "refresh_s": 0.60,
    "playerctl_path": "playerctl",
    "gdbus_path": "gdbus",
    "command_timeout_s": 2.0,
    "metadata_buffer_bytes": 512,
    "art_enabled": True,
    "art_width": 48,
    "art_height": 48,
    "art_timeout_s": 4.0,
    "art_max_bytes": 2_000_000,
    # Tiny MPRIS gaps happen while Electron/Flatpak windows settle or playerctl
    # has a little episode. Keep the last good record briefly instead of
    # publishing N/A/default art for one poll.
    "unavailable_grace_s": 2.0,
    "shortwave_players": ["de.haeckerfelix.Shortwave"],
    # Pear's exact bus suffix is intentionally not trusted. The bridge first
    # tries these sensible hints, then chooses a sole non-Shortwave candidate
    # only while HDMI-2 is explicitly on YouTube.
    "youtube_player_hints": ["youtube-music", "youtube music", "pear"],
    "heartbeat_s": 1.0,
}

# Shared source controller contract.
HDMI2_SOURCE_PLEX = 1
HDMI2_SOURCE_RADIO = 2
HDMI2_SOURCE_YOUTUBE = 3
KEY_HDMI2_SELECTED_SOURCE = "/hdmi2/selected_source"

PLAY_STOPPED = 0
PLAY_PLAYING = 1
PLAY_PAUSED = 2

TEXT_DEFAULT = "N/A"
ART_WIDTH = 48
ART_HEIGHT = 48
ART_BYTES = ART_WIDTH * ART_HEIGHT * 3

KEY_AVAILABLE = "/mpris/available"
KEY_PLAY_STATE = "/mpris/play_state"
KEY_POSITION_MS = "/mpris/position_ms"
KEY_DURATION_MS = "/mpris/duration_ms"
KEY_METADATA_SEQ = "/mpris/metadata_seq"
KEY_PLAYER_UTF8 = "/mpris/player_utf8"
KEY_IDENTITY_UTF8 = "/mpris/identity_utf8"
KEY_SOURCE_UTF8 = "/mpris/source_utf8"
KEY_TITLE_UTF8 = "/mpris/title_utf8"
KEY_ARTIST_UTF8 = "/mpris/artist_utf8"
KEY_ALBUM_UTF8 = "/mpris/album_utf8"
KEY_ART_URL_UTF8 = "/mpris/art_url_utf8"
KEY_PROCESS_ID = "/mpris/process_id"
KEY_PROCESS_NAME_UTF8 = "/mpris/process_name_utf8"
KEY_CAN_PLAY = "/mpris/can_play"
KEY_CAN_PAUSE = "/mpris/can_pause"
KEY_CAN_GO_NEXT = "/mpris/can_go_next"
KEY_CAN_GO_PREVIOUS = "/mpris/can_go_previous"
KEY_CAN_SEEK = "/mpris/can_seek"
KEY_ART_VALID = "/mpris/art_valid"
KEY_ART_SEQ = "/mpris/art_seq"
KEY_ART_WIDTH = "/mpris/art_width"
KEY_ART_HEIGHT = "/mpris/art_height"
KEY_ART_RGB = "/mpris/art_rgb"

CONTROL_KEYS = {
    "playpause": "/mpris/control/playpause_seq",
    "stop": "/mpris/control/stop_seq",
    "next": "/mpris/control/next_seq",
    "previous": "/mpris/control/previous_seq",
}

FIELD_SEP = "\x1f"


def load_cfg() -> dict:
    """Load defaults and merge any component-specific YAML configuration."""
    raw = os.environ.get("CONFIG_PATH")
    path = Path(raw) if raw else Path(__file__).resolve().parent / "configs" / "mpris_bridge.yaml"
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


def ui_text(value: object) -> str:
    """Normalize a value for display in the user interface."""
    text = str(value or "").replace("\x00", "").strip()
    return text if text else TEXT_DEFAULT


def utf8_prefix(value: object, size: int) -> bytes:
    """Encode text without splitting a UTF-8 code point at the size limit."""
    raw = ui_text(value).encode("utf-8", errors="replace")
    if len(raw) <= size:
        return raw
    clipped = raw[: max(0, size)]
    while clipped:
        try:
            clipped.decode("utf-8", errors="strict")
            return clipped
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return b""


def publish_text(key: str, value: object, size: int) -> None:
    """Publish text."""
    size = max(16, int(size))
    encoded = utf8_prefix(value, size - 1)
    bus.set_array(key, list(encoded) + [0] * (size - len(encoded)), dtype="u8")


def counter_delta(previous: int, current: int) -> int:
    """Return a bounded delta between wrapping command counters."""
    if current == previous:
        return 0
    delta = (int(current) - int(previous)) & 0x7FFFFFFF
    return 1 if delta <= 0 or delta > 8 else delta


def source_label(selected: int) -> str:
    """Return the source label result."""
    return {
        HDMI2_SOURCE_RADIO: "RADIO",
        HDMI2_SOURCE_YOUTUBE: "YOUTUBE",
    }.get(int(selected), "MPRIS")


def play_state_from_text(value: str) -> int:
    """Return the play state from text result."""
    lowered = str(value or "").strip().lower()
    if lowered == "playing":
        return PLAY_PLAYING
    if lowered in {"paused", "pause"}:
        return PLAY_PAUSED
    return PLAY_STOPPED


@dataclass
class Snapshot:
    """Store a normalized Snapshot record."""
    player: str = ""
    identity: str = TEXT_DEFAULT
    source: str = "MPRIS"
    process_id: int = 0
    process_name: str = TEXT_DEFAULT
    available: bool = False
    play_state: int = PLAY_STOPPED
    title: str = TEXT_DEFAULT
    artist: str = TEXT_DEFAULT
    album: str = TEXT_DEFAULT
    art_url: str = TEXT_DEFAULT
    position_s: float = 0.0
    duration_s: float = 0.0
    sampled_at: float = 0.0
    can_play: bool = False
    can_pause: bool = False
    can_next: bool = False
    can_previous: bool = False
    can_seek: bool = False


class MprisBridge:
    """Manage MprisBridge state and behaviour."""
    def __init__(self, cfg: dict):
        """Initialize configuration, dependencies, and runtime state."""
        self.cfg = cfg
        self.proc_name = str(cfg.get("proc_name", "mpris_bridge"))
        self.text_size = max(64, int(cfg.get("metadata_buffer_bytes", 512)))
        self.snapshot = Snapshot()
        self.running = True
        self.last_refresh_at = 0.0
        self.last_heartbeat_at = 0.0
        self.last_metadata_signature: Optional[Tuple[object, ...]] = None
        self.last_art_url = ""
        self.last_good_seen_at = 0.0
        self.control_seen: Dict[str, int] = {name: bus.get_int(key, 0) for name, key in CONTROL_KEYS.items()}
        self._init_bus()

    def _init_bus(self) -> None:
        """Handle the init bus lifecycle step."""
        self._publish(Snapshot())
        for key in CONTROL_KEYS.values():
            bus.set_int(key, bus.get_int(key, 0))
        self._clear_art()
        bus.set_int("/io/health/mpris_bridge_ok", 0)

    def _run(self, argv: Sequence[str], timeout: Optional[float] = None) -> Tuple[bool, str]:
        """Run a child command and return its success flag and output."""
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout or max(0.2, float(self.cfg.get("command_timeout_s", 2.0))),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if completed.returncode != 0:
            return False, (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
        return True, completed.stdout

    def _list_players(self) -> List[str]:
        """Return the list players result."""
        ok, output = self._run([str(self.cfg["playerctl_path"]), "--list-all"])
        if not ok:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _is_shortwave(self, player: str) -> bool:
        """Return whether shortwave."""
        lowered = player.casefold()
        return any(lowered == str(item).casefold() for item in self.cfg.get("shortwave_players", []))

    def _select_player(self, selected_source: int, players: Iterable[str]) -> str:
        """Select player."""
        candidates = list(players)
        if selected_source == HDMI2_SOURCE_RADIO:
            for configured in self.cfg.get("shortwave_players", []):
                for candidate in candidates:
                    if candidate.casefold() == str(configured).casefold():
                        return candidate
            return ""

        if selected_source != HDMI2_SOURCE_YOUTUBE:
            return ""

        non_radio = [item for item in candidates if not self._is_shortwave(item)]
        hints = [str(item).casefold() for item in self.cfg.get("youtube_player_hints", [])]
        process_hints = [str(item).casefold() for item in self.cfg.get("youtube_process_hints", [])]
        for candidate in non_radio:
            folded = candidate.casefold()
            if any(hint in folded for hint in hints):
                return candidate
            if process_hints:
                pid, proc_name = self._process_for_player(candidate)
                process_blob = f"{pid} {proc_name}".casefold()
                if any(hint in process_blob for hint in process_hints):
                    return candidate

        # Pear's bus suffix has not been pinned. A sole remaining candidate is
        # a safe dynamic answer because HDMI-2 is explicitly on YouTube.
        if len(non_radio) == 1:
            return non_radio[0]

        # Prefer an actually active candidate if an unrelated, idle MPRIS app
        # happens to exist in the desktop session.
        for candidate in non_radio:
            status = self._player_status(candidate)
            if status in {PLAY_PLAYING, PLAY_PAUSED}:
                return candidate
        return ""

    def _player_status(self, player: str) -> int:
        """Return the player status result."""
        ok, output = self._run([str(self.cfg["playerctl_path"]), f"--player={player}", "status"])
        return play_state_from_text(output) if ok else PLAY_STOPPED

    def _metadata(self, player: str) -> Tuple[str, str, str, float, str]:
        """Return the metadata result."""
        template = FIELD_SEP.join([
            "{{xesam:title}}",
            "{{xesam:artist}}",
            "{{xesam:album}}",
            "{{mpris:length}}",
            "{{mpris:artUrl}}",
        ])
        ok, output = self._run([
            str(self.cfg["playerctl_path"]),
            f"--player={player}",
            "metadata",
            "--format",
            template,
        ])
        if not ok:
            return TEXT_DEFAULT, TEXT_DEFAULT, TEXT_DEFAULT, 0.0, TEXT_DEFAULT
        parts = output.rstrip("\r\n").split(FIELD_SEP)
        parts += [""] * (5 - len(parts))
        try:
            duration_s = max(0.0, float(parts[3].strip() or 0.0) / 1_000_000.0)
        except ValueError:
            duration_s = 0.0
        return ui_text(parts[0]), ui_text(parts[1]), ui_text(parts[2]), duration_s, ui_text(parts[4])

    def _position(self, player: str) -> float:
        """Return the position result."""
        ok, output = self._run([str(self.cfg["playerctl_path"]), f"--player={player}", "position"])
        if not ok:
            return 0.0
        try:
            value = float(output.strip())
            return max(0.0, value) if math.isfinite(value) else 0.0
        except ValueError:
            return 0.0

    def _process_for_player(self, player: str) -> Tuple[int, str]:
        """Return the process for player result."""
        destination = f"org.mpris.MediaPlayer2.{player}"
        ok, raw = self._run([
            str(self.cfg["gdbus_path"]), "call", "--session",
            "--dest", "org.freedesktop.DBus",
            "--object-path", "/org/freedesktop/DBus",
            "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID",
            destination,
        ], timeout=0.5)
        if not ok:
            return 0, TEXT_DEFAULT
        match = re.search(r"(?:uint32\s+)?(\d+)", raw or "")
        if match is None:
            return 0, TEXT_DEFAULT
        pid = int(match.group(1))
        name = TEXT_DEFAULT
        try:
            comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
            if comm:
                name = ui_text(comm)
        except OSError:
            pass
        if name == TEXT_DEFAULT:
            try:
                raw_cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                if raw_cmd:
                    name = ui_text(raw_cmd[:180])
            except OSError:
                pass
        return pid, name

    def _identity_and_caps(self, player: str) -> Tuple[str, Dict[str, bool]]:
        # Playerctl intentionally hides most root/player properties. One small
        # GetAll call on player change gets the capability truth without adding
        # a permanent Python D-Bus dependency.
        """Return the identity and caps result."""
        destination = f"org.mpris.MediaPlayer2.{player}"
        ok, root_raw = self._run([
            str(self.cfg["gdbus_path"]), "call", "--session",
            "--dest", destination,
            "--object-path", "/org/mpris/MediaPlayer2",
            "--method", "org.freedesktop.DBus.Properties.Get",
            "org.mpris.MediaPlayer2", "Identity",
        ])
        identity = ui_text(root_raw)
        match = re.search(r"<'(.*)'>", root_raw or "")
        if match:
            identity = ui_text(match.group(1).replace("\\'", "'"))
        elif not ok:
            identity = ui_text(player)

        ok, raw = self._run([
            str(self.cfg["gdbus_path"]), "call", "--session",
            "--dest", destination,
            "--object-path", "/org/mpris/MediaPlayer2",
            "--method", "org.freedesktop.DBus.Properties.GetAll",
            "org.mpris.MediaPlayer2.Player",
        ])
        def prop(name: str) -> bool:
            """Return the prop result."""
            if not ok:
                return False
            pattern = rf"['\"]{re.escape(name)}['\"]\s*:\s*<\s*(true|false)\s*>"
            found = re.search(pattern, raw or "", flags=re.IGNORECASE)
            return bool(found and found.group(1).lower() == "true")

        return identity, {
            "play": prop("CanPlay"),
            "pause": prop("CanPause"),
            "next": prop("CanGoNext"),
            "previous": prop("CanGoPrevious"),
            "seek": prop("CanSeek"),
        }

    def _refresh(self, now: float) -> None:
        """Refresh object."""
        selected_source = bus.get_int(KEY_HDMI2_SELECTED_SOURCE, HDMI2_SOURCE_PLEX)
        player = self._select_player(selected_source, self._list_players())
        if not player:
            grace = max(0.0, float(self.cfg.get("unavailable_grace_s", 2.0)))
            if self.snapshot.available and now - self.last_good_seen_at < grace:
                self._publish(self.snapshot)
                return
            self.snapshot = Snapshot(source=source_label(selected_source), sampled_at=now)
            self._publish(self.snapshot)
            return

        previous = self.snapshot
        identity, caps = self._identity_and_caps(player) if player != previous.player else (
            previous.identity,
            {
                "play": previous.can_play,
                "pause": previous.can_pause,
                "next": previous.can_next,
                "previous": previous.can_previous,
                "seek": previous.can_seek,
            },
        )
        process_id, process_name = self._process_for_player(player) if player != previous.player else (previous.process_id, previous.process_name)
        title, artist, album, duration_s, art_url = self._metadata(player)
        position_s = self._position(player)
        snapshot = Snapshot(
            player=player,
            identity=identity,
            source=source_label(selected_source),
            process_id=process_id,
            process_name=process_name,
            available=True,
            play_state=self._player_status(player),
            title=title,
            artist=artist,
            album=album,
            art_url=art_url,
            position_s=position_s,
            duration_s=duration_s,
            sampled_at=now,
            can_play=caps["play"],
            can_pause=caps["pause"],
            can_next=caps["next"],
            can_previous=caps["previous"],
            can_seek=caps["seek"],
        )
        self.snapshot = snapshot
        self.last_good_seen_at = now
        self._publish(snapshot)

    def _estimated_position(self, snap: Snapshot, now: Optional[float] = None) -> float:
        """Return the estimated position result."""
        if not snap.available:
            return 0.0
        value = snap.position_s
        if snap.play_state == PLAY_PLAYING:
            value += max(0.0, (time.monotonic() if now is None else now) - snap.sampled_at)
        if snap.duration_s > 0.0:
            value = min(value, snap.duration_s)
        return max(0.0, value)

    def _publish(self, snap: Snapshot) -> None:
        """Handle the publish lifecycle step."""
        position_ms = int(round(self._estimated_position(snap) * 1000.0))
        duration_ms = int(round(max(0.0, snap.duration_s) * 1000.0))
        bus.set_int(KEY_AVAILABLE, int(snap.available))
        bus.set_int(KEY_PLAY_STATE, int(snap.play_state))
        bus.set_int(KEY_POSITION_MS, position_ms)
        bus.set_int(KEY_DURATION_MS, duration_ms)
        bus.set_int(KEY_CAN_PLAY, int(snap.can_play))
        bus.set_int(KEY_CAN_PAUSE, int(snap.can_pause))
        bus.set_int(KEY_CAN_GO_NEXT, int(snap.can_next))
        bus.set_int(KEY_CAN_GO_PREVIOUS, int(snap.can_previous))
        bus.set_int(KEY_CAN_SEEK, int(snap.can_seek))
        publish_text(KEY_PLAYER_UTF8, snap.player, self.text_size)
        publish_text(KEY_IDENTITY_UTF8, snap.identity, self.text_size)
        publish_text(KEY_SOURCE_UTF8, snap.source, self.text_size)
        bus.set_int(KEY_PROCESS_ID, int(max(0, snap.process_id)))
        publish_text(KEY_PROCESS_NAME_UTF8, snap.process_name, self.text_size)
        publish_text(KEY_TITLE_UTF8, snap.title, self.text_size)
        publish_text(KEY_ARTIST_UTF8, snap.artist, self.text_size)
        publish_text(KEY_ALBUM_UTF8, snap.album, self.text_size)
        publish_text(KEY_ART_URL_UTF8, snap.art_url, self.text_size)

        signature = (
            snap.player, snap.identity, snap.source, snap.process_id, snap.process_name, snap.available, snap.play_state,
            snap.title, snap.artist, snap.album, snap.duration_s, snap.art_url,
            snap.can_play, snap.can_pause, snap.can_next, snap.can_previous, snap.can_seek,
        )
        if signature != self.last_metadata_signature:
            self.last_metadata_signature = signature
            bus.set_int(KEY_METADATA_SEQ, bus.get_int(KEY_METADATA_SEQ, 0) + 1)
            self._update_art(snap.art_url if snap.available else "")

    def _clear_art(self) -> None:
        """Clear art."""
        bus.set_int(KEY_ART_VALID, 0)
        bus.set_int(KEY_ART_WIDTH, 0)
        bus.set_int(KEY_ART_HEIGHT, 0)
        bus.set_int(KEY_ART_SEQ, (bus.get_int(KEY_ART_SEQ, 0) + 1) & 0x7FFFFFFF)

    def _read_art_bytes(self, art_url: str) -> bytes:
        """Return the read art bytes result."""
        maximum = max(16_384, int(self.cfg.get("art_max_bytes", 2_000_000)))
        parsed = urllib.parse.urlparse(art_url)
        if parsed.scheme == "file":
            path = Path(urllib.request.url2pathname(parsed.path))
            with path.open("rb") as fh:
                payload = fh.read(maximum + 1)
        elif parsed.scheme in {"http", "https"}:
            request = urllib.request.Request(art_url, headers={"User-Agent": "EmberDeck/1.0"})
            with urllib.request.urlopen(request, timeout=max(0.5, float(self.cfg.get("art_timeout_s", 4.0)))) as response:
                payload = response.read(maximum + 1)
        else:
            return b""
        return b"" if len(payload) > maximum else payload

    def _update_art(self, art_url: str) -> None:
        """Update art."""
        art_url = "" if art_url == TEXT_DEFAULT else str(art_url or "").strip()
        if art_url == self.last_art_url:
            return
        self.last_art_url = art_url
        if not art_url or not bool(self.cfg.get("art_enabled", True)) or Image is None or ImageOps is None:
            self._clear_art()
            return
        try:
            payload = self._read_art_bytes(art_url)
            if not payload:
                self._clear_art()
                return
            width = max(12, min(ART_WIDTH, int(self.cfg.get("art_width", ART_WIDTH))))
            height = max(12, min(ART_HEIGHT, int(self.cfg.get("art_height", ART_HEIGHT))))
            with Image.open(io.BytesIO(payload)) as source:
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                fitted = ImageOps.fit(source.convert("RGB"), (width, height), method=resampling)
                pixels = np.asarray(fitted, dtype=np.uint8)
            canvas = np.zeros((ART_HEIGHT, ART_WIDTH, 3), dtype=np.uint8)
            canvas[: pixels.shape[0], : pixels.shape[1]] = pixels
            bus.set_array(KEY_ART_RGB, canvas.reshape(-1), dtype="u8")
            bus.set_int(KEY_ART_WIDTH, int(pixels.shape[1]))
            bus.set_int(KEY_ART_HEIGHT, int(pixels.shape[0]))
            bus.set_int(KEY_ART_VALID, 1)
            bus.set_int(KEY_ART_SEQ, (bus.get_int(KEY_ART_SEQ, 0) + 1) & 0x7FFFFFFF)
        except Exception as exc:
            log_error(self.proc_name, "MPRIS artwork unavailable", repr(exc))
            self._clear_art()

    def _consume_controls(self) -> None:
        """Consume controls."""
        player = self.snapshot.player if self.snapshot.available else ""
        commands = {
            "playpause": "play-pause",
            "stop": "stop",
            "next": "next",
            "previous": "previous",
        }
        for name, key in CONTROL_KEYS.items():
            current = bus.get_int(key, 0)
            previous = self.control_seen.get(name, current)
            if current == previous:
                continue
            self.control_seen[name] = current
            requests = counter_delta(previous, current)
            for _ in range(requests):
                if not player:
                    log_error(self.proc_name, "MPRIS command rejected", f"{name}: no selected player")
                    break
                ok, detail = self._run([
                    str(self.cfg["playerctl_path"]),
                    f"--player={player}",
                    commands[name],
                ])
                if ok:
                    print(f"[{self.proc_name}] MPRIS control: {name}", flush=True)
                else:
                    log_error(self.proc_name, "MPRIS control command failed", f"{name}: {detail}")
                    break

    def run(self) -> None:
        """Run the component until shutdown."""
        log_info(self.proc_name, "started")
        period = 1.0 / max(1.0, float(self.cfg.get("poll_hz", 10.0)))
        next_tick = time.monotonic()
        while self.running:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
                now = time.monotonic()
            next_tick = max(next_tick + period, now)

            if now - self.last_refresh_at >= max(0.15, float(self.cfg.get("refresh_s", 0.60))):
                self.last_refresh_at = now
                self._refresh(now)
            else:
                # Keep progress smooth between MPRIS polling snapshots.
                self._publish(self.snapshot)
            self._consume_controls()
            bus.set_int("/io/health/mpris_bridge_ok", 1)
            if now - self.last_heartbeat_at >= max(0.25, float(self.cfg.get("heartbeat_s", 1.0))):
                heartbeat(self.proc_name)
                self.last_heartbeat_at = now

        bus.set_int("/io/health/mpris_bridge_ok", 0)
        log_info(self.proc_name, "stopped")


def main() -> None:
    """Configure and run the component until shutdown."""
    app = MprisBridge(load_cfg())

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
