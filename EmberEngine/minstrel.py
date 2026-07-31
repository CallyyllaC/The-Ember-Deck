#!/usr/bin/env python3
"""
minstrel.py
Plex / Plexamp bridge for EmberEngine.

- Uses plexapi to query Plex Media Server for the Plexamp session.
- Publishes playback state and track info into shared memory via synapse.
- Intended to run under FoundryCore as a supervised process.

Config (YAML) is loaded from CONFIG_PATH and should look roughly like:

  proc_name: minstrel

  url: "http://192.168.X.X:32400"
  token: "YOUR_PLEX_TOKEN"
  username: "your Plex username"      # retained for token refresh fallback
  password: "your Plex password"

  # How to identify the Plexamp client
  plex_client_name: "Plexamp"      # match on player.title (optional)
  plex_client_product: "Plexamp"   # match on player.product (optional)

  poll_interval_s: 0.5             # how often to query sessions

UltraBlur handling is stubbed with a helper you can adapt once you
know exactly how your server exposes those fields.
"""

import os
import sys
import time
import signal
import io
import yaml
import re
import numpy as np
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageOps
except ModuleNotFoundError:  # Artwork remains optional until Pillow is installed.
    Image = None
    ImageOps = None
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.exceptions import PlexApiException

import synapse as bus  # shared memory helper
from whisper_daemon import log_info, log_error, log_media, log_event, log_heartbeat


# --------------- playback enums & keys ----------------

PLAY_STOPPED = 0
PLAY_PLAYING = 1
PLAY_PAUSED  = 2

KEY_PLAY_STATE     = "/plex/play_state"
KEY_POSITION_SEC   = "/plex/position_sec"
KEY_DURATION_SEC   = "/plex/duration_sec"
KEY_TRACK_RK       = "/plex/track_rk"
KEY_TRACK_CHANGE   = "/plex/track_change_seq"

# UI-facing Plex metadata. Minstrel is the only writer for /plex/*, while
# Pawprint owns the generic /media/active_source selector and /bt/* metadata.
MEDIA_TEXT_MAX_CHARS = 192
KEY_PLEX_TITLE_UTF8 = "/plex/title_utf8"
KEY_PLEX_ARTIST_UTF8 = "/plex/artist_utf8"
KEY_PLEX_ALBUM_UTF8 = "/plex/album_utf8"
KEY_PLEX_TRACK_NUMBER = "/plex/track_number"
KEY_PLEX_TRACK_COUNT = "/plex/track_count"
KEY_PLEX_DISC_NUMBER = "/plex/disc_number"
KEY_PLEX_METADATA_SEQ = "/plex/metadata_seq"

# Detail fields are fixed-size UTF-8 buses. A UI should be able to distinguish
# unavailable metadata from stale metadata after a track change.
KEY_PLEX_YEAR_UTF8 = "/plex/year_utf8"
KEY_PLEX_GENRE_UTF8 = "/plex/genre_utf8"
KEY_PLEX_CODEC_UTF8 = "/plex/codec_utf8"
KEY_PLEX_BITRATE_UTF8 = "/plex/bitrate_utf8"
KEY_PLEX_SAMPLE_RATE_UTF8 = "/plex/sample_rate_utf8"
KEY_PLEX_LOUDNESS_UTF8 = "/plex/loudness_utf8"
KEY_PLEX_ADDED_UTF8 = "/plex/added_utf8"
KEY_PLEX_PLAY_COUNT_UTF8 = "/plex/play_count_utf8"
KEY_PLEX_POPULARITY_UTF8 = "/plex/popularity_utf8"
KEY_PLEX_RATING_UTF8 = "/plex/rating_utf8"
KEY_PLEX_BPM_UTF8 = "/plex/bpm_utf8"
KEY_PLEX_MOOD_UTF8 = "/plex/mood_utf8"

# Extended Track Info fields. These are populated defensively from the full
# Plex track's media/part/audio-stream objects, which vary a little by server
# and library agent. "N/A" is preferable to stale or invented metadata.
KEY_PLEX_CONTAINER_UTF8 = "/plex/container_utf8"
KEY_PLEX_FILE_NAME_UTF8 = "/plex/file_name_utf8"
KEY_PLEX_FILE_SIZE_UTF8 = "/plex/file_size_utf8"
KEY_PLEX_CHANNELS_UTF8 = "/plex/channels_utf8"
KEY_PLEX_CHANNEL_LAYOUT_UTF8 = "/plex/channel_layout_utf8"
KEY_PLEX_BIT_DEPTH_UTF8 = "/plex/bit_depth_utf8"
KEY_PLEX_STREAM_TITLE_UTF8 = "/plex/stream_title_utf8"
KEY_PLEX_TRACK_GAIN_UTF8 = "/plex/track_gain_utf8"
KEY_PLEX_TRACK_PEAK_UTF8 = "/plex/track_peak_utf8"
KEY_PLEX_ALBUM_GAIN_UTF8 = "/plex/album_gain_utf8"
KEY_PLEX_ALBUM_PEAK_UTF8 = "/plex/album_peak_utf8"
KEY_PLEX_ALBUM_RANGE_UTF8 = "/plex/album_range_utf8"
KEY_PLEX_LRA_UTF8 = "/plex/lra_utf8"
KEY_PLEX_LAST_PLAYED_UTF8 = "/plex/last_played_utf8"

# Terminal-native album-cover mosaic. The UI reads a small RGB raster from
# shared memory and renders it using coloured half-block characters, so this
# works in LXTerminal without sixel/kitty/iTerm image support.
ART_WIDTH = 48
ART_HEIGHT = 48
KEY_PLEX_ART_VALID = "/plex/art_valid"
KEY_PLEX_ART_SEQ = "/plex/art_seq"
KEY_PLEX_ART_WIDTH = "/plex/art_width"
KEY_PLEX_ART_HEIGHT = "/plex/art_height"
KEY_PLEX_ART_RGB = "/plex/art_rgb"

# Pawprint writes monotonically increasing counters here. Minstrel baselines
# them at launch so a stale shared-memory segment cannot replay an old button
# press after a worker restart.
PLEX_CONTROL_KEYS = {
    "playpause": "/plex/control/playpause_seq",
    "stop": "/plex/control/stop_seq",
    "next": "/plex/control/next_seq",
    "previous": "/plex/control/previous_seq",
}
PLEX_CONTROL_CODES = {
    "playpause": 1,
    "stop": 2,
    "next": 3,
    "previous": 4,
}
KEY_CONTROL_LAST_COMMAND = "/plex/control/last_command"
KEY_CONTROL_LAST_OK = "/plex/control/last_ok"
KEY_CONTROL_LAST_SEQ = "/plex/control/last_seq"

# UltraBlur RGBA keys used by Aurora
KEY_TL_RGBA = "/album/ultra/tl_rgba"
KEY_TR_RGBA = "/album/ultra/tr_rgba"
KEY_BL_RGBA = "/album/ultra/bl_rgba"
KEY_BR_RGBA = "/album/ultra/br_rgba"

# --- lyrics shared memory keys ---

LYRICS_LINE_IDX_KEY = "/lyrics/line_idx"
LYRICS_SEQ_KEY      = "/lyrics/seq"
LYRICS_CURRENT_UTF8 = "/lyrics/current_utf8"  # legacy 256-byte reader compatibility
LYRICS_CURRENT_UTF8_V2 = "/lyrics/current_utf8_v2"
# Context buffers are v2-only. They let the UI render previous/current/next
# timed lines without parsing the LRC again or guessing around a racey index.
LYRICS_PREVIOUS_UTF8_V2 = "/lyrics/previous_utf8_v2"
LYRICS_NEXT_UTF8_V2 = "/lyrics/next_utf8_v2"
LYRICS_MAX_BYTES_LEGACY = 256
LYRICS_MAX_BYTES = 1024

# LRC timestamp pattern: [mm:ss.xx]
_LRC_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")

# --------------- config + heartbeat -------------------

# ---------- defaults ----------
DEFAULTS = dict(
    server_name=None,
    username=None,
    password=None,
    url=None,
    token=None,
    plex_client_name=None,
    # Optional explicit control target. When unset it deliberately follows
    # plex_client_name, so monitoring and button control stay aimed at the
    # Deck's own Plexamp rather than another phone session.
    plex_control_client_name=None,
    plex_control_client_machine_identifier=None,
    poll_interval_s=1.0,
    lyrics_update_interval_s=0.25,
    clock_seek_threshold_s=18.0,
    clock_fresh_nudge_max_s=1.25,
    clock_fresh_deadband_s=0.35,
    clock_offset_change_epsilon_s=0.001,
    album_art_enabled=True,
    album_art_width=48,
    album_art_height=48,
    album_art_timeout_s=5.0,
)

def load_cfg():
    """Load YAML config for this instance."""
    p = os.environ.get("CONFIG_PATH")
    cfg = {}
    if p and Path(p).exists():
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    out = DEFAULTS.copy()
    out.update(cfg)
    return out


def heartbeat(name: str):
    """Publish a monotonic-ms heartbeat for FoundryCore."""
    now_ms = int(time.monotonic() * 1000)
    bus.set_int(f"/proc/{name}/heartbeat_ms", now_ms)
    seq_key = f"/proc/{name}/hb_seq"
    bus.set_int(seq_key, bus.get_int(seq_key, 0) + 1)
    


# --------------- plex helpers ------------------------

def _is_auth_failure(exc: Exception) -> bool:
    """Return True when Plex rejected the current credentials/token.

    plexapi does not use one consistent exception type for every 401 path, so
    inspect both an attached HTTP response and the message text.  Network
    failures deliberately return False: those should retry the saved token,
    not needlessly force an account login.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in (401, 403):
        return True

    text = str(exc).lower()
    auth_markers = (
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "not authorized",
        "authentication",
        "token is invalid",
        "token invalid",
    )
    return any(marker in text for marker in auth_markers)


def connect_plex(
    servername: str,
    username: str,
    password: str,
    token: str,
    url: str,
    cfg,
    *,
    force_account_login: bool = False,
):
    """Connect to Plex, using credentials to refresh an expired saved token.

    Normal path: try the cached ``url`` + ``token`` first.  Once any
    connection/session attempt fails, the caller sets ``force_account_login``
    and all recovery attempts use the saved account credentials to obtain a
    fresh server connection.  The resulting token and
    URL are persisted, while ``username`` and ``password`` are intentionally
    left untouched in the YAML for the next refresh.
    """
    token_error = None

    if token and url and not force_account_login:
        try:
            plex = PlexServer(url, token)
            # Force an authenticated request now. Constructing PlexServer is
            # lazy, so without this an expired token can look valid until much
            # later in the polling loop.
            plex.library.sections()
            log_event("minstrel", "connected", {
                "server": plex.friendlyName,
                "libraries": str([section.title for section in plex.library.sections()]),
            })
            return plex, token, url
        except Exception as exc:
            token_error = exc
            log_error("minstrel", "Saved Plex token connection failed; trying account login", str(exc))

    if username and password and servername:
        try:
            if force_account_login:
                log_info("minstrel", "refreshing Plex connection via saved username/password")
            account = MyPlexAccount(username, password)
            resource = account.resource(servername)
            if resource is None:
                raise RuntimeError(f"Plex server resource not found: {servername!r}")

            plex = resource.connect()
            # Confirm the returned connection is usable before overwriting the
            # cached token. A half-built PlexServer object is not evidence.
            plex.library.sections()

            fresh_token = plex._token
            fresh_url = plex._baseurl
            if not fresh_token or not fresh_url:
                raise RuntimeError("Account login did not yield a usable server token and URL")

            set_loginInfo(fresh_token, fresh_url, cfg)
            log_event("minstrel", "connected", {
                "server": plex.friendlyName,
                "libraries": str([section.title for section in plex.library.sections()]),
            })
            return plex, fresh_token, fresh_url
        except Exception as exc:
            if token_error is not None:
                raise RuntimeError(
                    f"Saved token failed ({token_error}); account login also failed ({exc})"
                ) from exc
            raise

    if token_error is not None:
        raise RuntimeError(
            "Saved Plex token failed and no username/password fallback is configured"
        ) from token_error

    raise RuntimeError("No usable Plex login method is configured")

def find_plexamp_session(server, client_name: str):
    """Return the matching Plexamp session, or None when no player exists.

    Transport and authentication failures are deliberately allowed to escape.
    Treating a 401 as "no music is playing" was the reason an expired token
    could loop forever without triggering the username/password fallback.
    """
    sessions = server.sessions()

    for sess in sessions:
        player = getattr(sess, "player", None)
        if not player:
            continue

        if client_name and player.title != client_name:
            continue

        return sess

    return None

def map_play_state(player_state: str, sess) -> int:
    """Map Plex player.state string to our enum."""
    if not player_state: 
        return PLAY_STOPPED
    s = player_state.lower()
    if s == "playing":
        return PLAY_PLAYING
    if s == "paused":
        return PLAY_PAUSED
    # "stopped", "idle", "buffering", anything else
    return PLAY_STOPPED


class PlaybackClock:
    """Local playback clock with cautious correction from Plexamp observations.

    Plexamp's ``viewOffset`` can sit unchanged for several polls, then jump in
    a coarse 5- or 10-second step. A repeated value is stale and is ignored.
    When a value changes, it is a fresh reference: small errors nudge the local
    clock, while a clearly large discontinuity is treated as a seek.
    """

    def __init__(
        self,
        seek_threshold_s: float = 18.0,
        fresh_nudge_max_s: float = 1.25,
        fresh_deadband_s: float = 0.35,
        offset_change_epsilon_s: float = 0.001,
    ):
        """Initialize configuration, dependencies, and runtime state."""
        self.seek_threshold_s = max(1.0, float(seek_threshold_s))
        self.fresh_nudge_max_s = max(0.0, float(fresh_nudge_max_s))
        self.fresh_deadband_s = max(0.0, float(fresh_deadband_s))
        self.offset_change_epsilon_s = max(0.0, float(offset_change_epsilon_s))

        self._anchor_pos_s = 0.0
        self._anchor_mono_s = 0.0
        self._play_state = PLAY_STOPPED
        self._ready = False
        self._last_reported_pos_s: Optional[float] = None

    @property
    def ready(self) -> bool:
        """Return the ready result."""
        return self._ready

    @property
    def play_state(self) -> int:
        """Return the play state result."""
        return self._play_state

    def position(self, now: Optional[float] = None) -> float:
        """Return the position result."""
        if not self._ready:
            return 0.0
        if now is None:
            now = time.monotonic()
        if self._play_state == PLAY_PLAYING:
            return max(0.0, self._anchor_pos_s + (now - self._anchor_mono_s))
        return max(0.0, self._anchor_pos_s)

    def _anchor(self, position_s: float, play_state: int, now: float) -> None:
        """Handle the anchor lifecycle step."""
        self._anchor_pos_s = max(0.0, float(position_s))
        self._anchor_mono_s = float(now)
        self._play_state = int(play_state)
        self._ready = True

    def reset(self, position_s: float, play_state: int, now: Optional[float] = None) -> None:
        """Handle the reset lifecycle step."""
        if now is None:
            now = time.monotonic()
        position_s = max(0.0, float(position_s))
        self._anchor(position_s, play_state, now)
        self._last_reported_pos_s = position_s

    def stop(self, now: Optional[float] = None) -> None:
        """Handle the stop lifecycle step."""
        self.reset(0.0, PLAY_STOPPED, now)

    def observe(
        self,
        reported_position_s: float,
        play_state: int,
        *,
        now: Optional[float] = None,
        force_reset: bool = False,
    ) -> str:
        """Fold one Plex observation into the clock.

        Returns one of ``reset``, ``pause``, ``resume``, ``nudge``, ``seek``
        or ``steady``. The caller does not need it for normal operation, but it
        is useful for debugging without making the lyric path depend on logs.
        """
        if now is None:
            now = time.monotonic()

        reported_position_s = max(0.0, float(reported_position_s))
        play_state = int(play_state)

        if force_reset or not self._ready:
            self.reset(reported_position_s, play_state, now)
            return "reset"

        predicted = self.position(now)
        previous_state = self._play_state
        previous_reported = self._last_reported_pos_s
        fresh_offset = (
            previous_reported is None
            or abs(reported_position_s - previous_reported) > self.offset_change_epsilon_s
        )
        self._last_reported_pos_s = reported_position_s

        if play_state == PLAY_STOPPED:
            self.stop(now)
            return "stopped"

        # A real playback-state transition is more trustworthy than a coarse
        # offset. Preserve the local estimate when pausing/resuming.
        if previous_state == PLAY_PLAYING and play_state == PLAY_PAUSED:
            self._anchor(predicted, PLAY_PAUSED, now)
            return "pause"

        if previous_state == PLAY_PAUSED and play_state == PLAY_PLAYING:
            self._anchor(predicted, PLAY_PLAYING, now)
            return "resume"

        if previous_state == PLAY_STOPPED:
            self.reset(reported_position_s, play_state, now)
            return "reset"

        # A paused Deck must never advance merely because a lyric timer ticks.
        # On initial connection ``force_reset`` above anchors to the reported
        # paused offset, so there is no assumed-playing state.
        if play_state == PLAY_PAUSED:
            self._anchor(predicted, PLAY_PAUSED, now)
            return "steady"

        # Same old offset means Plex has not supplied new timing information.
        # Let the monotonic clock remain the sole authority in that interval.
        if not fresh_offset:
            self._anchor(predicted, PLAY_PLAYING, now)
            return "steady"

        error_s = reported_position_s - predicted

        # Fresh, very large jumps are almost certainly seeks. This is not
        # intended to solve every weird Plexamp edge case, merely the normal
        # "I skipped to another part of the song" case.
        if abs(error_s) >= self.seek_threshold_s:
            self.reset(reported_position_s, PLAY_PLAYING, now)
            return "seek"

        # Fresh coarse samples can gently pull the clock back toward reality
        # without throwing lyrics several lines backward in one update.
        if abs(error_s) > self.fresh_deadband_s:
            adjustment = max(-self.fresh_nudge_max_s, min(self.fresh_nudge_max_s, error_s))
            self._anchor(predicted + adjustment, PLAY_PLAYING, now)
            return "nudge"

        self._anchor(predicted, PLAY_PLAYING, now)
        return "steady"

def _ui_text(value: object) -> str:
    """Keep unavailable metadata explicit for the eventual UI."""
    text = str(value or "").strip()
    return text if text else "N/A"


def _utf8_prefix(value: object, max_bytes: int) -> bytes:
    """Return a valid UTF-8 prefix without splitting a multi-byte character."""
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
    """Publish UTF-8 text without corrupting non-ASCII at the fixed bus edge."""
    size = max(8, int(max_chars))
    encoded = _utf8_prefix(value, size - 1)
    buf = np.zeros(size, dtype=np.uint8)
    buf[:len(encoded)] = np.frombuffer(encoded, dtype=np.uint8)
    bus.set_array(key, buf, "u8")


def _safe_nonnegative_int(value: object, default: int = 0) -> int:
    """Return the safe nonnegative int result."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return int(default)


def _first_present(*values: object) -> object:
    """Return the first non-empty field, preserving 0 when it is meaningful."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _format_number(value: object, *, suffix: str = "", digits: int = 0) -> str:
    """Format number."""
    if value is None or value == "":
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _ui_text(value)
    if digits <= 0:
        return f"{int(round(number))}{suffix}"
    return f"{number:.{digits}f}{suffix}"


def _format_date(value: object) -> str:
    """Format date."""
    if value is None or value == "":
        return "N/A"
    try:
        if hasattr(value, "strftime"):
            return value.strftime("%d %b %Y")
        number = float(value)
        if number > 10_000_000:
            return time.strftime("%d %b %Y", time.localtime(number))
    except Exception:
        pass
    return _ui_text(value)


def _tags_text(value: object) -> str:
    """Return the tags text result."""
    if not value:
        return "N/A"
    try:
        tags = []
        for item in value:
            tag = getattr(item, "tag", None) or getattr(item, "title", None) or str(item)
            if str(tag).strip():
                tags.append(str(tag).strip())
        return ", ".join(tags) if tags else "N/A"
    except TypeError:
        return _ui_text(value)


def _format_bytes(value: object) -> str:
    """Format a Plex byte count compactly for the dense Track Info page."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if amount < 0:
        return "N/A"
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while amount >= 1024.0 and index < len(units) - 1:
        amount /= 1024.0
        index += 1
    if index == 0:
        return f"{int(round(amount))} {units[index]}"
    return f"{amount:.2f} {units[index]}"


def _media_details(item: object) -> dict[str, str]:
    """Extract format and loudness detail from a full track or session object.

    Plex agents expose these fields at different levels depending on library
    type, server version and whether the item is a live session or a full media
    record. Every value is therefore taken from the first available sensible
    source. This function never raises into playback merely because a file is
    missing one optional tag.
    """
    values = {
        "container": "N/A",
        "codec": "N/A",
        "bitrate": "N/A",
        "sample_rate": "N/A",
        "file_name": "N/A",
        "file_size": "N/A",
        "channels": "N/A",
        "channel_layout": "N/A",
        "bit_depth": "N/A",
        "stream_title": "N/A",
        "track_gain": "N/A",
        "track_peak": "N/A",
        "album_gain": "N/A",
        "album_peak": "N/A",
        "album_range": "N/A",
        "lra": "N/A",
        "loudness": "N/A",
    }
    try:
        media_list = getattr(item, "media", None) or []
        media = media_list[0] if media_list else None
        if media is None:
            return values

        parts = getattr(media, "parts", None) or []
        part = parts[0] if parts else None
        streams = (
            getattr(part, "streams", None)
            or getattr(media, "audioStreams", None)
            or getattr(media, "streams", None)
            or []
        )
        audio = next(
            (
                stream for stream in streams
                if int(getattr(stream, "streamType", 0) or 0) == 2
                or str(getattr(stream, "streamType", "")).lower() == "audio"
            ),
            None,
        )

        def pick(*names: str) -> object:
            """Return the pick result."""
            candidates = []
            for name in names:
                for obj in (audio, media, part, item):
                    if obj is not None:
                        candidates.append(getattr(obj, name, None))
            return _first_present(*candidates)

        file_path = _first_present(
            getattr(part, "file", None) if part else None,
            getattr(item, "file", None),
        )
        if file_path not in (None, ""):
            values["file_name"] = Path(str(file_path)).name or "N/A"

        values.update({
            "container": _ui_text(_first_present(
                getattr(part, "container", None) if part else None,
                getattr(media, "container", None),
                getattr(item, "container", None),
            )),
            "codec": _ui_text(_first_present(
                getattr(audio, "codec", None) if audio else None,
                getattr(media, "audioProfile", None),
                getattr(media, "audioCodec", None),
                getattr(media, "container", None),
            )),
            "bitrate": _format_number(_first_present(
                getattr(audio, "bitrate", None) if audio else None,
                getattr(media, "bitrate", None),
                getattr(part, "bitrate", None) if part else None,
            ), suffix=" kbps"),
            "sample_rate": _format_number(_first_present(
                getattr(audio, "samplingRate", None) if audio else None,
                getattr(audio, "sampleRate", None) if audio else None,
                getattr(media, "audioSamplingRate", None),
            ), suffix=" Hz"),
            "file_size": _format_bytes(_first_present(
                getattr(part, "size", None) if part else None,
                getattr(media, "size", None),
            )),
            "channels": _format_number(_first_present(
                getattr(audio, "channels", None) if audio else None,
                getattr(media, "audioChannels", None),
            )),
            "channel_layout": _ui_text(_first_present(
                getattr(audio, "audioChannelLayout", None) if audio else None,
                getattr(audio, "channelLayout", None) if audio else None,
                getattr(media, "audioChannelLayout", None),
            )),
            "bit_depth": _format_number(_first_present(
                getattr(audio, "bitDepth", None) if audio else None,
                getattr(media, "audioBitDepth", None),
                getattr(media, "bitDepth", None),
            ), suffix=" bit"),
            "stream_title": _ui_text(_first_present(
                getattr(audio, "extendedDisplayTitle", None) if audio else None,
                getattr(audio, "displayTitle", None) if audio else None,
                getattr(media, "audioProfile", None),
            )),
            "track_gain": _format_number(pick("gain", "trackGain"), suffix=" dB", digits=2),
            "track_peak": _format_number(pick("peak", "trackPeak"), digits=5),
            "album_gain": _format_number(pick("albumGain"), suffix=" dB", digits=2),
            "album_peak": _format_number(pick("albumPeak"), digits=5),
            "album_range": _format_number(pick("albumRange"), digits=3),
            "lra": _format_number(pick("lra", "loudnessRange"), digits=2),
            "loudness": _format_number(pick("loudnessLUFS", "loudness", "loudnessValue"), suffix=" LUFS", digits=1),
        })
    except Exception:
        # The normal N/A values are a safe and honest fallback.
        pass
    return values


def _safe_fetch_track(server, sess):
    """Fetch the full music track once per track change, with session fallback."""
    rating_key = getattr(sess, "ratingKey", None)
    try:
        if server is not None and rating_key is not None:
            return server.fetchItem(int(rating_key))
    except Exception as exc:
        print(f"[minstrel] Full track detail unavailable: {exc!r}", flush=True)
    return sess


def _safe_fetch_parent_album(server, detail, sess):
    """Fetch the parent album only on track change, with gentle fallbacks.

    Plex sessions frequently omit ``year`` even when the album knows it. The
    normal Track.album() route is tried first; the parent metadata key is the
    fallback for sparse session objects.
    """
    for item in (detail, sess):
        if item is None:
            continue
        album_method = getattr(item, "album", None)
        if callable(album_method):
            try:
                album = album_method()
                if album is not None:
                    return album
            except Exception:
                pass

    if server is None:
        return None

    for item in (detail, sess):
        if item is None:
            continue
        parent_key = _first_present(
            getattr(item, "parentRatingKey", None),
            getattr(item, "parentKey", None),
        )
        if parent_key is None:
            continue
        try:
            return server.fetchItem(str(parent_key))
        except Exception:
            # Some Plex servers only accept a numeric rating key here.
            match = re.search(r"(\d+)$", str(parent_key))
            if match:
                try:
                    return server.fetchItem(int(match.group(1)))
                except Exception:
                    pass
    return None


def _year_text(*values: object) -> str:
    """Return a four-digit release year from track/session/album metadata."""
    for value in values:
        if value is None:
            continue
        if hasattr(value, "year"):
            try:
                year = int(value.year)
                if 1000 <= year <= 9999:
                    return str(year)
            except Exception:
                pass
        text = str(value).strip()
        match = re.search(r"(?<!\d)((?:19|20|21)\d{2})(?!\d)", text)
        if match:
            return match.group(1)
    return "N/A"


def publish_plex_metadata(sess, state: dict) -> None:
    """Publish one stable UI-ready record for the active Plex track.

    A live Plexamp session is intentionally sparse, so the main loop retains a
    full track and parent album after each track change. All optional fields use
    explicit ``N/A`` fallbacks instead of leaking stale data from the previous
    song into the Track Info page.
    """
    detail = state.get("plex_full_track") or sess
    album_detail = state.get("plex_parent_album")

    empty_values = {
        "year": "N/A",
        "genre": "N/A",
        "codec": "N/A",
        "bitrate": "N/A",
        "sample_rate": "N/A",
        "loudness": "N/A",
        "added": "N/A",
        "play_count": "N/A",
        "popularity": "N/A",
        "rating": "N/A",
        "bpm": "N/A",
        "mood": "N/A",
        "container": "N/A",
        "file_name": "N/A",
        "file_size": "N/A",
        "channels": "N/A",
        "channel_layout": "N/A",
        "bit_depth": "N/A",
        "stream_title": "N/A",
        "track_gain": "N/A",
        "track_peak": "N/A",
        "album_gain": "N/A",
        "album_peak": "N/A",
        "album_range": "N/A",
        "lra": "N/A",
        "last_played": "N/A",
    }

    if sess is None:
        title = artist = album = "N/A"
        track_number = track_count = disc_number = 0
        values = empty_values
    else:
        title = _ui_text(_first_present(getattr(detail, "title", None), getattr(sess, "title", None)))
        artist = _ui_text(_first_present(
            getattr(detail, "grandparentTitle", None),
            getattr(sess, "grandparentTitle", None),
        ))
        album = _ui_text(_first_present(
            getattr(detail, "parentTitle", None),
            getattr(sess, "parentTitle", None),
        ))
        track_number = _safe_nonnegative_int(_first_present(
            getattr(detail, "index", None), getattr(sess, "index", None),
        ))
        disc_number = _safe_nonnegative_int(_first_present(
            getattr(detail, "parentIndex", None), getattr(sess, "parentIndex", None),
        ))
        track_count = _safe_nonnegative_int(_first_present(
            getattr(detail, "parentLeafCount", None),
            getattr(album_detail, "leafCount", None),
            getattr(detail, "leafCount", None),
            getattr(sess, "parentLeafCount", None),
        ))

        media = _media_details(detail)
        values = {
            **empty_values,
            "year": _year_text(
                getattr(detail, "year", None),
                getattr(sess, "year", None),
                getattr(album_detail, "year", None),
                getattr(album_detail, "originallyAvailableAt", None),
            ),
            "genre": _tags_text(_first_present(
                getattr(detail, "genres", None), getattr(sess, "genres", None),
            )),
            "added": _format_date(_first_present(
                getattr(detail, "addedAt", None), getattr(sess, "addedAt", None),
            )),
            "last_played": _format_date(_first_present(
                getattr(detail, "lastViewedAt", None), getattr(sess, "lastViewedAt", None),
            )),
            "play_count": _format_number(_first_present(
                getattr(detail, "viewCount", None), getattr(sess, "viewCount", None),
            )),
            "popularity": _format_number(_first_present(
                getattr(detail, "popularity", None),
                getattr(detail, "audienceRating", None),
                getattr(sess, "audienceRating", None),
            ), digits=1),
            "rating": _format_number(_first_present(
                getattr(detail, "userRating", None),
                getattr(detail, "rating", None),
                getattr(sess, "userRating", None),
                getattr(sess, "rating", None),
            ), digits=1),
            "bpm": _format_number(_first_present(
                getattr(detail, "bpm", None), getattr(sess, "bpm", None),
            )),
            "mood": _tags_text(_first_present(
                getattr(detail, "moods", None), getattr(sess, "moods", None),
            )),
            **media,
        }

    publish_utf8(KEY_PLEX_TITLE_UTF8, title)
    publish_utf8(KEY_PLEX_ARTIST_UTF8, artist)
    publish_utf8(KEY_PLEX_ALBUM_UTF8, album)
    bus.set_int(KEY_PLEX_TRACK_NUMBER, track_number)
    bus.set_int(KEY_PLEX_TRACK_COUNT, track_count)
    bus.set_int(KEY_PLEX_DISC_NUMBER, disc_number)

    text_keys = {
        "year": KEY_PLEX_YEAR_UTF8,
        "genre": KEY_PLEX_GENRE_UTF8,
        "codec": KEY_PLEX_CODEC_UTF8,
        "bitrate": KEY_PLEX_BITRATE_UTF8,
        "sample_rate": KEY_PLEX_SAMPLE_RATE_UTF8,
        "loudness": KEY_PLEX_LOUDNESS_UTF8,
        "added": KEY_PLEX_ADDED_UTF8,
        "play_count": KEY_PLEX_PLAY_COUNT_UTF8,
        "popularity": KEY_PLEX_POPULARITY_UTF8,
        "rating": KEY_PLEX_RATING_UTF8,
        "bpm": KEY_PLEX_BPM_UTF8,
        "mood": KEY_PLEX_MOOD_UTF8,
        "container": KEY_PLEX_CONTAINER_UTF8,
        "file_name": KEY_PLEX_FILE_NAME_UTF8,
        "file_size": KEY_PLEX_FILE_SIZE_UTF8,
        "channels": KEY_PLEX_CHANNELS_UTF8,
        "channel_layout": KEY_PLEX_CHANNEL_LAYOUT_UTF8,
        "bit_depth": KEY_PLEX_BIT_DEPTH_UTF8,
        "stream_title": KEY_PLEX_STREAM_TITLE_UTF8,
        "track_gain": KEY_PLEX_TRACK_GAIN_UTF8,
        "track_peak": KEY_PLEX_TRACK_PEAK_UTF8,
        "album_gain": KEY_PLEX_ALBUM_GAIN_UTF8,
        "album_peak": KEY_PLEX_ALBUM_PEAK_UTF8,
        "album_range": KEY_PLEX_ALBUM_RANGE_UTF8,
        "lra": KEY_PLEX_LRA_UTF8,
        "last_played": KEY_PLEX_LAST_PLAYED_UTF8,
    }
    for name, key in text_keys.items():
        publish_utf8(key, values[name])

    signature = (
        title, artist, album, track_number, track_count, disc_number,
        *(values[name] for name in sorted(values)),
    )
    if signature != state.get("plex_metadata_signature"):
        state["plex_metadata_signature"] = signature
        bus.set_int(KEY_PLEX_METADATA_SEQ, bus.get_int(KEY_PLEX_METADATA_SEQ, 0) + 1)

def _clear_album_art() -> None:
    """Mark artwork unavailable and advance the sequence for a UI fallback."""
    bus.set_int(KEY_PLEX_ART_VALID, 0)
    bus.set_int(KEY_PLEX_ART_WIDTH, 0)
    bus.set_int(KEY_PLEX_ART_HEIGHT, 0)
    bus.set_int(KEY_PLEX_ART_SEQ, (bus.get_int(KEY_PLEX_ART_SEQ, 0) + 1) & 0x7FFFFFFF)


def _album_thumb_key(detail, sess) -> Optional[str]:
    """Choose the album image before the track image, with session fallbacks."""
    for item in (detail, sess):
        if item is None:
            continue
        for attr in ("parentThumb", "grandparentThumb", "thumb", "art"):
            value = getattr(item, attr, None)
            if value:
                return str(value)
    return None


def publish_album_art(server, detail, sess, cfg: dict) -> None:
    """Fetch one small cover image on track change and publish its RGB mosaic.

    The cover is deliberately transcoded by Plex to a tiny square first. That
    avoids moving a huge original cover to the Pi and keeps all image work off
    the UI thread. Pillow is optional: without it the rest of Minstrel runs and
    the UI falls back to its palette texture.
    """
    if not bool(cfg.get("album_art_enabled", True)):
        _clear_album_art()
        return
    if Image is None or ImageOps is None:
        print("[minstrel] Album art disabled: Pillow is not installed", flush=True)
        _clear_album_art()
        return
    if server is None:
        _clear_album_art()
        return

    try:
        width = max(12, min(ART_WIDTH, int(cfg.get("album_art_width", ART_WIDTH))))
        height = max(12, min(ART_HEIGHT, int(cfg.get("album_art_height", ART_HEIGHT))))
        timeout = max(1.0, float(cfg.get("album_art_timeout_s", 5.0)))
        thumb_key = _album_thumb_key(detail, sess)
        if not thumb_key:
            _clear_album_art()
            return

        source_url = server.url(thumb_key, includeToken=True)
        image_url = server.transcodeImage(
            source_url,
            height=height,
            width=width,
            minSize=False,
            upscale=True,
            imageFormat="png",
        )
        response = server._session.get(image_url, timeout=timeout)
        response.raise_for_status()

        with Image.open(io.BytesIO(response.content)) as source:
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            rgb = ImageOps.fit(
                source.convert("RGB"),
                (width, height),
                method=resampling,
            )
            pixels = np.asarray(rgb, dtype=np.uint8)

        # The segment's first writer fixes its size. Always publish the stable
        # 48×48 bus capacity, with dimensions describing the valid top-left
        # region. This avoids reallocation and stale-segment size mismatches.
        canvas = np.zeros((ART_HEIGHT, ART_WIDTH, 3), dtype=np.uint8)
        copy_h = min(ART_HEIGHT, pixels.shape[0])
        copy_w = min(ART_WIDTH, pixels.shape[1])
        canvas[:copy_h, :copy_w] = pixels[:copy_h, :copy_w]
        bus.set_array(KEY_PLEX_ART_RGB, canvas.reshape(-1), "u8")
        bus.set_int(KEY_PLEX_ART_WIDTH, copy_w)
        bus.set_int(KEY_PLEX_ART_HEIGHT, copy_h)
        bus.set_int(KEY_PLEX_ART_VALID, 1)
        bus.set_int(KEY_PLEX_ART_SEQ, (bus.get_int(KEY_PLEX_ART_SEQ, 0) + 1) & 0x7FFFFFFF)
    except Exception as exc:
        print(f"[minstrel] Album art unavailable: {exc!r}", flush=True)
        _clear_album_art()

def publish_basic_state(sess, prev_state):
    """Publish Plex state and return the raw timing observation for the clock."""
    player = sess.player
    if not player:
        bus.set_int(KEY_PLAY_STATE, PLAY_STOPPED)
        bus.set_float(KEY_DURATION_SEC, 0.0)
        return 0, PLAY_STOPPED, 0.0

    ps = map_play_state(player.state, sess)
    bus.set_int(KEY_PLAY_STATE, ps)

    if ps != prev_state:
        if ps == PLAY_STOPPED:
            log_media("minstrel", "stop")
        elif ps == PLAY_PAUSED:
            log_media("minstrel", "pause")
        elif ps == PLAY_PLAYING:
            log_media("minstrel", "play", {
                "track": _ui_text(getattr(sess, "title", None)),
                "album": _ui_text(getattr(sess, "parentTitle", None)),
                "artist": _ui_text(getattr(sess, "grandparentTitle", None)),
            })

    view_offset = getattr(sess, "viewOffset", 0) or 0
    duration = getattr(sess, "duration", 0) or 0
    raw_pos_sec = float(view_offset) / 1000.0
    dur_sec = float(duration) / 1000.0
    bus.set_float(KEY_DURATION_SEC, dur_sec)

    rk = sess.ratingKey
    if rk is not None:
        try:
            rk_int = int(rk)
        except (ValueError, TypeError):
            rk_int = 0
    else:
        rk_int = 0
    bus.set_int(KEY_TRACK_RK, rk_int)

    return rk_int, ps, raw_pos_sec

def set_loginInfo(token, url, cfg):
    """Persist a refreshed server token without discarding account credentials."""
    update_config_value("token", token, cfg)
    update_config_value("url", url, cfg)

def publishTrackInfo(track):
    """Log a track change and refresh artwork colours from the richest object."""
    log_media("minstrel", "track_change", {
        "track": _ui_text(getattr(track, "title", None)),
        "album": _ui_text(getattr(track, "parentTitle", None)),
        "artist": _ui_text(getattr(track, "grandparentTitle", None)),
    })
    try_publish_ultrablur(track)

def try_publish_ultrablur(track) -> bool:
    """Publish album UltraBlur corner colours when Plex exposes them.

    Plex session and track objects vary by server/client version. A missing
    album palette is normal and must never bring down Minstrel on a track
    change. ``/album/ultra/valid`` prevents Aurora from reusing stale colours
    from the previous track.
    """
    bus.set_int("/album/ultra/valid", 0)
    if track is None:
        return False

    try:
        album_method = getattr(track, "album", None)
        album = album_method() if callable(album_method) else None
        ubc = getattr(album, "ultraBlurColors", None)
        if ubc is None:
            return False

        encoded_corners = (
            getattr(ubc, "topLeft", None),
            getattr(ubc, "topRight", None),
            getattr(ubc, "bottomLeft", None),
            getattr(ubc, "bottomRight", None),
        )
        if not all(_valid_hex_colour(value) for value in encoded_corners):
            return False
        corners = tuple(hex_to_rgba_f32(value) for value in encoded_corners)

        for key, rgba in zip((KEY_TL_RGBA, KEY_TR_RGBA, KEY_BL_RGBA, KEY_BR_RGBA), corners):
            bus.set_array(key, tidy_ultrablur(rgba), "f32")
        bus.set_int("/album/ultra/valid", 1)
        return True
    except Exception as exc:
        print(f"[minstrel] UltraBlur unavailable: {exc!r}", flush=True)
        return False


def tidy_ultrablur(rgba, sat_min=None, val_min=None, val_max=None):
    """Return raw, bounded UltraBlur RGBA for Aurora's colour policy."""
    _ = (sat_min, val_min, val_max)
    r, g, b, a = rgba
    return [max(0.0, min(1.0, float(value))) for value in (r, g, b, a)]


def _valid_hex_colour(value) -> bool:
    """Return whether Plex supplied a complete RGB or RGBA hex colour."""
    if not isinstance(value, str):
        return False
    encoded = value.strip().removeprefix("#")
    if len(encoded) not in (6, 8):
        return False
    try:
        int(encoded, 16)
        return True
    except ValueError:
        return False


def hex_to_rgba_f32(h: str):
    """
    Convert #RRGGBB or RRGGBB[AA] into [r, g, b, a] floats 0..1.
    Defaults alpha to 1.0 if not present.
    """
    if not h:
        return [0.0, 0.0, 0.0, 1.0]

    h = h.strip()
    if h.startswith("#"):
        h = h[1:]

    if len(h) not in (6, 8):
        # unexpected format, be defensive
        return [0.0, 0.0, 0.0, 1.0]

    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    if len(h) == 8:
        a = int(h[6:8], 16)
    else:
        a = 255

    s = 1.0 / 255.0
    return [r * s, g * s, b * s, a * s]

def _is_lrc_stream(stream) -> bool:
    """Return True for streams that are explicitly LRC/synchronised candidates.

    Plex's ``timed`` flag is not dependable for external LRC files: Plexamp can
    render them as timed lyrics while the API advertises ``timed=False``. The
    format/codec is therefore the primary clue; the parsed payload is the final
    authority.
    """
    fmt = str(getattr(stream, "format", "") or "").strip().lower()
    codec = str(getattr(stream, "codec", "") or "").strip().lower()
    return fmt == "lrc" or codec == "lrc"


def _collect_lyric_stream_candidates(sess, server):
    """Collect and rank lyric streams without trusting Plex's timed metadata."""
    sources = [sess]

    # Session metadata normally contains lyric streams. A full library item is
    # a harmless fallback for the cases where Plex only includes them there.
    rating_key = getattr(sess, "ratingKey", None)
    if rating_key is not None:
        try:
            full_item = server.fetchItem(int(rating_key))
            if full_item is not None:
                sources.append(full_item)
        except Exception:
            pass

    seen = set()
    ranked = []
    for source in sources:
        try:
            streams = source.lyricStreams()
        except Exception:
            continue

        for stream in streams or []:
            key = getattr(stream, "key", None)
            if not key or key in seen:
                continue
            seen.add(key)

            is_lrc = _is_lrc_stream(stream)
            timed = bool(getattr(stream, "timed", False))
            if not (is_lrc or timed):
                continue

            # Prefer actual LRC streams. A timed non-LRC stream is still worth
            # trying as a fallback because some servers label the format oddly.
            priority = 0 if (is_lrc and timed) else 1 if is_lrc else 2
            ranked.append((priority, stream))

    ranked.sort(key=lambda item: item[0])
    return [stream for _, stream in ranked]


def _decode_lyric_payload(raw: bytes) -> str:
    """Decode LRC bytes without trusting Requests' default ISO-8859-1 guess.

    Plex commonly serves external LRC files without a useful charset header.
    Requests then reports ISO-8859-1, which turns valid UTF-8 Japanese lyrics
    into mojibake before they ever reach the UI. Prefer BOM-aware UTF-8, then
    fall back to the common Japanese legacy encodings only when UTF-8 fails.
    """
    if not raw:
        return ""

    # UTF-16 LRC files exist in the wild and are unambiguous when BOM-marked.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")

    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            pass

    for encoding in ("cp932", "shift_jis", "euc_jp"):
        try:
            return raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            pass

    # Preserve every byte as a last resort rather than crashing the lyric loop.
    return raw.decode("utf-8", errors="replace")


def _fetch_lyric_stream_text(stream, server) -> str:
    """Fetch one lyric stream as correctly decoded text."""
    url = server.url(stream.key, includeToken=True)
    resp = server._session.get(url, headers=server._headers(), timeout=5)
    resp.raise_for_status()
    return _decode_lyric_payload(resp.content)


def fetch_timed_lyrics_from_plex(sess, server):
    """Fetch the first lyric stream that genuinely parses as timestamped LRC.

    We deliberately do not filter solely on ``stream.timed``. Plex may mark an
    external `.lrc` stream as `timed=False` even when Plexamp mobile renders it
    correctly. The test is instead: is it an LRC/synchronised candidate, and do
    its contents produce timestamped entries?
    """
    candidates = _collect_lyric_stream_candidates(sess, server)
    if not candidates:
        return []

    for stream in candidates:
        try:
            text = _fetch_lyric_stream_text(stream, server)
            entries = parse_lrc_text(text)
        except Exception:
            continue

        if entries:
            log_event("minstrel", "lyrics_found", {"track": sess.title})
            return entries

    return []

def _write_lyric_buffer(key: str, text: str, size: int) -> None:
    """Publish a valid UTF-8 lyric line into one fixed-size Synapse segment."""
    encoded = _utf8_prefix(text or "", size - 1)
    buf = np.zeros(size, dtype=np.uint8)
    buf[:len(encoded)] = np.frombuffer(encoded, dtype=np.uint8)
    bus.set_array(key, buf, "u8")


def publish_lyrics_context(previous: str, current: str, next_line: str, idx: int, state: dict) -> None:
    """Publish previous/current/next timed lyrics as one coherent context.

    The UI does not attempt to infer neighbouring lines from an index that may
    have changed between shared-memory reads. All three lines are published
    together whenever the timed lyric index changes.
    """
    seq = int(state.get("lyrics_seq", 0)) + 1
    state["lyrics_seq"] = seq

    _write_lyric_buffer(LYRICS_CURRENT_UTF8_V2, current, LYRICS_MAX_BYTES)
    _write_lyric_buffer(LYRICS_PREVIOUS_UTF8_V2, previous, LYRICS_MAX_BYTES)
    _write_lyric_buffer(LYRICS_NEXT_UTF8_V2, next_line, LYRICS_MAX_BYTES)
    # Keep the old single-line reader alive for any utility scripts.
    _write_lyric_buffer(LYRICS_CURRENT_UTF8, current, LYRICS_MAX_BYTES_LEGACY)

    bus.set_int(LYRICS_LINE_IDX_KEY, int(idx))
    bus.set_int(LYRICS_SEQ_KEY, seq)

def _publish_blank_lyric(state: dict) -> None:
    """Clear lyrics once, including the text buffer and change sequence."""
    if state.get("lyrics_last_idx") == -1:
        return
    state["lyrics_last_idx"] = -1
    publish_lyrics_context("", "", "", -1, state)


def update_timed_lyrics_line(position_s: float, state: dict):
    """Update the active LRC line from the Deck's smooth local clock."""
    entries = state.get("lyrics_lrc") or []
    if not entries:
        _publish_blank_lyric(state)
        return

    pos_ms = max(0.0, float(position_s)) * 1000.0
    times = state.get("lyrics_times")
    if not times or len(times) != len(entries):
        times = [t for (t, _) in entries]
        state["lyrics_times"] = times

    import bisect
    idx = bisect.bisect_right(times, pos_ms) - 1

    # Before the first timestamp, show the first lyric line immediately as the
    # current line. Some tracks have long intros; an empty lyric panel until the
    # first sung word makes the Deck look broken even when timed lyrics are
    # already loaded.
    if idx < 0:
        if state.get("lyrics_last_idx") != -2:
            state["lyrics_last_idx"] = -2
            first_text = entries[0][1] if entries else ""
            second_text = entries[1][1] if len(entries) > 1 else ""
            publish_lyrics_context("", first_text, second_text, -1, state)
        return

    if idx >= len(entries):
        idx = len(entries) - 1

    if idx == state.get("lyrics_last_idx"):
        return

    state["lyrics_last_idx"] = idx
    previous_text = entries[idx - 1][1] if idx > 0 else ""
    _, lyric_text = entries[idx]
    next_text = entries[idx + 1][1] if idx + 1 < len(entries) else ""
    publish_lyrics_context(previous_text, lyric_text, next_text, idx, state)

def parse_lrc_text(text: str):
    """
    Parse LRC-style timed lyrics into [(time_ms, text), ...].
    """
    entries = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        tags = _LRC_TS.findall(line)
        if not tags:
            continue

        # Remove all [mm:ss.xx] tags from the line
        lyric = _LRC_TS.sub("", line).strip()
        if not lyric:
            continue

        for mm, ss in tags:
            try:
                m = int(mm)
                s = float(ss)
            except ValueError:
                continue
            t_ms = int((m * 60 + s) * 1000)
            entries.append((t_ms, lyric))

    entries.sort(key=lambda x: x[0])
    return entries

def update_config_value(key, value, cfg):
    """
    Update a single top-level key in Aurora's config file and in-memory cfg.

    key      : string key in the YAML (e.g. "base_colour", "dynamicColour")
    value    : new value to write
    cfg      : current in-memory config dict
    cfg_path : optional explicit path; defaults to $CONFIG_PATH

    Returns the updated value.
    """
    cfg_path = os.environ.get("CONFIG_PATH")
    if not cfg_path:
        return

    # update in-memory view
    cfg[key] = value

    # load full file so we preserve other keys
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}

    # update on-disk dict
    data[key] = value

    # write back
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    return value
def _status_note(proc_name: str, message: str) -> None:
    """Operational logs must not depend on Whisper's decorative templates."""
    print(f"[{proc_name}] {message}", flush=True)


def find_plex_control_client(server, cfg):
    """Return the Deck's own Plexamp client for remote-control commands.

    The server can also list phones and browsers. Prefer an explicit machine
    identifier when configured; otherwise use the same title Minstrel already
    uses to identify the Deck session. Commands are proxied through the Plex
    server, avoiding a second local client connection or any extra token owner.
    """
    wanted_id = str(cfg.get("plex_control_client_machine_identifier") or "").strip()
    wanted_name = str(cfg.get("plex_control_client_name") or cfg.get("plex_client_name") or "").strip()

    for client in server.clients():
        client_id = str(getattr(client, "machineIdentifier", "") or "")
        client_name = str(getattr(client, "title", "") or "")
        if wanted_id and client_id != wanted_id:
            continue
        if not wanted_id and wanted_name and client_name != wanted_name:
            continue
        client.proxyThroughServer()
        return client
    return None


def send_plex_control_command(server, cfg, command: str, play_state: int) -> None:
    """Execute one physical transport request against the Deck's Plexamp.

    Plex has no single ``playpause`` endpoint. Pawprint's Play key therefore
    toggles from Minstrel's current Deck-only state, while the remaining tape
    controls map directly to Plex's music transport calls.
    """
    client = find_plex_control_client(server, cfg)
    if client is None:
        target = cfg.get("plex_control_client_machine_identifier") or cfg.get("plex_control_client_name") or cfg.get("plex_client_name")
        raise RuntimeError(f"Plex control client not found: {target!r}")

    if command == "playpause":
        if int(play_state) == PLAY_PLAYING:
            client.pause(mtype="music")
        else:
            client.play(mtype="music")
    elif command == "stop":
        client.stop(mtype="music")
    elif command == "next":
        client.skipNext(mtype="music")
    elif command == "previous":
        client.skipPrevious(mtype="music")
    else:
        raise ValueError(f"Unknown Plex control command: {command}")


def _counter_delta(previous: int, current: int) -> int:
    """Return a bounded count of unseen counter increments.

    Normal presses increase by one. A short burst can be processed as multiple
    commands, while a strange stale/reset value is treated as one request
    rather than replaying an entire archaeological dig of old shared memory.
    """
    if current == previous:
        return 0
    delta = (int(current) - int(previous)) & 0x7FFFFFFF
    if delta <= 0 or delta > 8:
        return 1
    return delta


def consume_plex_control_requests(server, cfg, seen: dict, proc_name: str) -> None:
    """Consume Pawprint counters once per worker tick, not once per Plex poll.

    A failed command is intentionally consumed rather than retried forever: a
    physical Next press should not suddenly fire ten seconds later when an
    unrelated reconnect succeeds. The touchscreen remains the deliberate
    override for those rare out-of-sync moments.
    """
    for command, key in PLEX_CONTROL_KEYS.items():
        current = bus.get_int(key, 0)
        previous = seen.get(command, current)
        if current == previous:
            continue
        seen[command] = current

        requests = _counter_delta(previous, current)
        for _ in range(requests):
            try:
                send_plex_control_command(
                    server,
                    cfg,
                    command,
                    bus.get_int(KEY_PLAY_STATE, PLAY_STOPPED),
                )
                bus.set_int(KEY_CONTROL_LAST_COMMAND, PLEX_CONTROL_CODES[command])
                bus.set_int(KEY_CONTROL_LAST_OK, 1)
                bus.set_int(KEY_CONTROL_LAST_SEQ, current)
                _status_note(proc_name, f"Plex control: {command}")
            except Exception as exc:
                bus.set_int(KEY_CONTROL_LAST_COMMAND, PLEX_CONTROL_CODES[command])
                bus.set_int(KEY_CONTROL_LAST_OK, 0)
                bus.set_int(KEY_CONTROL_LAST_SEQ, current)
                log_error(proc_name, "Plex control command failed", f"{command}: {exc}")
                break


# --------------- main loop ---------------------------

def _clear_lyrics(state: dict) -> None:
    """Clear timed-lyrics state so an old song cannot survive session loss."""
    state["lyrics_lrc"] = []
    state["lyrics_times"] = []
    state["lyrics_source"] = "none"
    _publish_blank_lyric(state)

def main():
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    proc_name = str(cfg.get("proc_name", "minstrel"))
    server_name = cfg.get("server_name")
    username = cfg.get("username")
    password = cfg.get("password")
    token = cfg.get("token")
    url = cfg.get("url")

    log_info(proc_name, "started")

    has_token_login = bool(url and token)
    has_account_login = bool(server_name and username and password)
    if not (has_token_login or has_account_login):
        log_error(
            proc_name,
            "no_login",
            "Configure token+url, username+password+server_name, or preferably both.",
        )
        sys.exit(1)

    if has_token_login:
        log_info(proc_name, "url_found")
    if has_account_login:
        log_info(proc_name, "user_found")

    client_name = cfg.get("plex_client_name")
    poll_interval = max(0.1, float(cfg.get("poll_interval_s", 1.0)))
    lyrics_interval = max(0.05, float(cfg.get("lyrics_update_interval_s", 0.25)))
    clock = PlaybackClock(
        seek_threshold_s=cfg.get("clock_seek_threshold_s", 18.0),
        fresh_nudge_max_s=cfg.get("clock_fresh_nudge_max_s", 1.25),
        fresh_deadband_s=cfg.get("clock_fresh_deadband_s", 0.35),
        offset_change_epsilon_s=cfg.get("clock_offset_change_epsilon_s", 0.001),
    )

    server = None
    force_account_login = False
    prev_state = None
    last_track_rk = None
    track_change_seq = 0
    last_hb = 0.0
    next_poll = 0.0
    next_lyrics_tick = 0.0

    backoff = 1.0
    backoff_max = 30.0
    next_connect_attempt = 0.0

    state = {}
    # Baseline any pre-existing shared-memory values. This prevents an old
    # Pawprint command from replaying merely because Minstrel restarted.
    control_seen = {command: bus.get_int(key, 0) for command, key in PLEX_CONTROL_KEYS.items()}
    bus.set_int(KEY_CONTROL_LAST_COMMAND, 0)
    bus.set_int(KEY_CONTROL_LAST_OK, 1)
    bus.set_int(KEY_CONTROL_LAST_SEQ, 0)
    bus.set_int(KEY_PLEX_METADATA_SEQ, 0)
    _clear_album_art()
    publish_plex_metadata(None, state)
    stopping = False

    def on_stop(signum, frame):
        """Mark the component for an orderly shutdown."""
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    try:
        while not stopping:
            now = time.monotonic()

            # Poll Plex independently from lyric updates. The API is a coarse
            # reference; the local clock supplies the smooth position between
            # these observations.
            if now >= next_poll:
                next_poll = now + poll_interval

                if server is None and now >= next_connect_attempt:
                    try:
                        server, token, url = connect_plex(
                            server_name,
                            username,
                            password,
                            token,
                            url,
                            cfg,
                            force_account_login=force_account_login,
                        )
                        # A successful credential refresh gives us a new token;
                        # future reconnects may use that cache normally again.
                        force_account_login = False
                        backoff = 1.0
                    except Exception as e:
                        # One failed connection attempt is enough to retire the
                        # cached token for this recovery cycle. Credentials are
                        # deliberately retained in the YAML, so every retry
                        # after a failure uses them to obtain a fresh token
                        # instead of repeatedly knocking on Plex with the same
                        # dead one.
                        if has_account_login:
                            force_account_login = True
                            log_error(
                                proc_name,
                                "Plex connection failed; next attempt will use saved username/password",
                                str(e),
                            )
                        else:
                            log_error(proc_name, "Failed to connect to Plex", str(e))
                        server = None
                        next_connect_attempt = now + backoff
                        backoff = min(backoff * 2.0, backoff_max)

                if server is not None:
                    try:
                        sess = find_plexamp_session(server, client_name)
                    except Exception as e:
                        # Do not try to classify the failure and then keep
                        # retrying an old cached token. PlexAPI exposes 401s
                        # inconsistently, and the practical rule for this Deck
                        # is simple: one failed session query retires the token
                        # for this recovery cycle. Use the saved username and
                        # password on the next attempt to obtain a new one.
                        if has_account_login:
                            force_account_login = True
                            log_error(
                                proc_name,
                                "Plex session query failed; refreshing via saved username/password",
                                str(e),
                            )
                        else:
                            log_error(proc_name, "Error querying Plex", str(e))

                        server = None
                        sess = None
                        next_connect_attempt = now

                    if sess is None:
                        prev_state = PLAY_STOPPED
                        last_track_rk = None
                        clock.stop(now)
                        bus.set_int(KEY_PLAY_STATE, PLAY_STOPPED)
                        bus.set_float(KEY_POSITION_SEC, 0.0)
                        bus.set_float(KEY_DURATION_SEC, 0.0)
                        state["plex_full_track"] = None
                        state["plex_parent_album"] = None
                        bus.set_int("/album/ultra/valid", 0)
                        _clear_album_art()
                        publish_plex_metadata(None, state)
                        _clear_lyrics(state)
                    else:
                        rk_int, play_state, raw_pos_s = publish_basic_state(sess, prev_state)
                        track_changed = last_track_rk is None or rk_int != last_track_rk
                        clock.observe(
                            raw_pos_s,
                            play_state,
                            now=now,
                            force_reset=track_changed,
                        )

                        if track_changed:
                            last_track_rk = rk_int
                            track_change_seq = (track_change_seq + 1) & 0x7FFFFFFF
                            bus.set_int(KEY_TRACK_CHANGE, track_change_seq)

                            # Session responses are intentionally thin. Fetch
                            # the full track once per change for the Track Info
                            # page, while retaining the session as a safe fallback.
                            state["plex_full_track"] = _safe_fetch_track(server, sess)
                            state["plex_parent_album"] = _safe_fetch_parent_album(
                                server, state["plex_full_track"], sess
                            )

                            state["lyrics_lrc"] = fetch_timed_lyrics_from_plex(sess, server)
                            state["lyrics_times"] = [t for (t, _) in state["lyrics_lrc"]]
                            state["lyrics_last_idx"] = None
                            if state["lyrics_lrc"]:
                                state["lyrics_source"] = "timed"
                                # Publish the initial lyric context immediately on
                                # track change instead of waiting for the next timed
                                # lyric tick. Long intros deserve better than an
                                # empty box wearing a fake moustache.
                                first_text = state["lyrics_lrc"][0][1]
                                second_text = state["lyrics_lrc"][1][1] if len(state["lyrics_lrc"]) > 1 else ""
                                state["lyrics_last_idx"] = -2
                                publish_lyrics_context("", first_text, second_text, -1, state)
                            else:
                                state["lyrics_source"] = "none"
                                _publish_blank_lyric(state)

                            publishTrackInfo(state["plex_full_track"])
                            publish_album_art(server, state["plex_full_track"], sess, cfg)

                        publish_plex_metadata(sess, state)
                        prev_state = play_state

            # Transport requests are independent of the 1 Hz Plex session poll
            # so physical buttons remain responsive. They target the Deck's
            # configured Plexamp client even while that client is idle.
            if server is not None:
                consume_plex_control_requests(server, cfg, control_seen, proc_name)

            # Publish a smooth position for the eventual UI even though Plex is
            # only polled once per second.
            bus.set_float(KEY_POSITION_SEC, clock.position(now))

            if now >= next_lyrics_tick:
                while next_lyrics_tick <= now:
                    next_lyrics_tick += lyrics_interval
                if state.get("lyrics_source") == "timed":
                    update_timed_lyrics_line(clock.position(now), state)

            if now - last_hb > 1.0:
                heartbeat(proc_name)
                log_heartbeat(proc_name)
                last_hb = now

            next_due = min(next_poll, next_lyrics_tick, last_hb + 1.0)
            time.sleep(max(0.01, min(0.05, next_due - time.monotonic())))

    finally:
        bus.close_all()
        log_info(proc_name, "stopped")


if __name__ == "__main__":
    main()
