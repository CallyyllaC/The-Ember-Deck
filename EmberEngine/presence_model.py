"""Ember Deck's Discord Rich Presence model: what to show, never how to send it.

Everything Ember-specific about presence lives here: which source is active,
what wording to use, media timing, and what the privacy policy allows. The
native Discord bridge only receives the finished, already-filtered fields.

It reuses the state the rest of Ember Deck already publishes rather than
detecting media itself:

* ``/media/active_source`` (Pawprint's routing answer) decides the source;
  MPRIS is DAB or YT Music according to ``/hdmi2/selected_source``, the same
  selection mpris_bridge.py uses to pick its player (and reports back as
  ``/mpris/source_utf8``).
* Each source's own play state, metadata and timing keys (``/plex/*``,
  ``/bt/*``, ``/mpris/*``), the same ones Ember UI displays.

Multi-key metadata is read between two reads of the source's ``*_seq``
counter, retrying if it changed, so a half-updated record is never published.

Activity model (two states):

* Idle: nothing playing, paused, stopped, unavailable or unrecognised.
* Listening: one of Plex, DAB, YT Music or Bluetooth is playing.

Images are the asset keys registered with the Discord application
(``REGISTERED_ASSETS``). Only with ``artwork_mode: on`` (plus metadata and
source display enabled) may the large image be the playing item's own
artwork, and then only a URL already vetted by artwork_policy.py.

Both images open the project page (``presence.project_url``, set in the
shipped rich_presence.yaml) when clicked; the large image's tooltip then
invites the click. An absent or unusable URL turns the links off and nothing
else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from contracts import (
    HDMI2_SOURCE_RADIO,
    HDMI2_SOURCE_YOUTUBE,
    MEDIA_SOURCE_BLUETOOTH,
    MEDIA_SOURCE_MPRIS,
    MEDIA_SOURCE_PLEX,
    PLAY_PAUSED,
    PLAY_PLAYING,
    PLEX_CONNECTED,
)

TEXT_BYTES = 192
MAX_FIELD_CHARS = 128   # Discord's limit for details/state/tooltips
MIN_FIELD_CHARS = 2     # Discord rejects 1-character strings
SEQ_READ_ATTEMPTS = 3
PLEX_ART_URL_BYTES = 512  # minstrel.ARTWORK_URL_BYTES
MAX_LINK_CHARS = 256      # Discord's limit for large_url/small_url

# ---------------------------------------------------------------- sources and assets

SOURCE_PLEX = "plex"
SOURCE_DAB = "dab"
SOURCE_YTMUSIC = "ytmusic"
SOURCE_BLUETOOTH = "bluetooth"

SOURCE_LABELS = {
    SOURCE_PLEX: "Plex",
    SOURCE_DAB: "DAB",
    SOURCE_YTMUSIC: "YT Music",
    SOURCE_BLUETOOTH: "Bluetooth",
}

# Rich Presence art assets registered in the Discord Developer Portal.
ASSET_EMBER_DECK = "ember-deck"
SOURCE_ASSETS = {
    SOURCE_PLEX: "source-plex",
    SOURCE_DAB: "source-dab",
    SOURCE_YTMUSIC: "source-ytmusic",
    SOURCE_BLUETOOTH: "source-bluetooth",
}
REGISTERED_ASSETS = frozenset({ASSET_EMBER_DECK, *SOURCE_ASSETS.values()})

# MPRIS sources, keyed by the HDMI-2 selection, with the label mpris_bridge.py
# publishes once it has switched to the matching player.
_MPRIS_SOURCES = {
    HDMI2_SOURCE_RADIO: (SOURCE_DAB, "RADIO"),
    HDMI2_SOURCE_YOUTUBE: (SOURCE_YTMUSIC, "YOUTUBE"),
}

# ---------------------------------------------------------------- wording
# All user-visible strings in one place. The top line of the presence is the
# registered Discord application's name.
TEXT_IDLE = "Idle"
TEXT_PLAYING = "Playing music"
TEXT_VIA = "via {source}"
TEXT_DECK = "The Ember Deck"
TEXT_EXPLORE = "Explore The Ember Deck"   # large-image tooltip (and third line) while the images link to the project
TEXT_ARTIST_ALBUM = "{artist} • {album}"
MIN_ALBUM_CHARS = 3     # a shortened album keeps at least this much before the "…", else it is dropped

ARTWORK_MODES = ("off", "audit", "on")

# Placeholders publishers use for "no value"; never shown.
_PLACEHOLDERS = frozenset({
    "n/a", "na", "none", "null", "nil", "unknown", "unknown artist", "unknown album", "unknown title",
    "untitled", "-", "--", "—", "?", "(null)", "undefined",
})


@dataclass(frozen=True)
class PresencePolicy:
    show_source: bool = True
    show_media_metadata: bool = False
    show_idle: bool = True
    activity_type: str = "listening"   # "listening" or "playing"
    artwork_mode: str = "audit"        # "off", "audit" or "on" (reserved; static artwork)
    project_url: Optional[str] = None  # vetted link_url() for both images; None: no links

    @classmethod
    def from_cfg(cls, presence: Dict[str, Any]) -> "PresencePolicy":
        return cls(
            show_source=bool(presence.get("show_source", True)),
            show_media_metadata=bool(presence.get("show_media_metadata", False)),
            show_idle=bool(presence.get("show_idle", True)),
            activity_type=str(presence.get("activity_type", "listening")),
            artwork_mode=str(presence.get("artwork_mode", "audit")),
            project_url=link_url(presence.get("project_url")),
        )


def link_url(value: Any) -> Optional[str]:
    """Return ``value`` if it can be a Discord image link, else None (no links).

    https only, at most MAX_LINK_CHARS, printable ASCII without spaces, a
    dotted host name, and no user info. Never raises: a bad URL must only
    turn the links off, never stop presence.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url.startswith("https://") or len(url) > MAX_LINK_CHARS or any(not "!" <= ch <= "~" or ch == "\\" for ch in url):
        return None
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.scheme != "https" or "." not in host.strip(".") or "@" in parts.netloc:
        return None
    return url


@dataclass(frozen=True)
class MediaSnapshot:
    """The facts presence needs. Contains no device names, servers or URLs."""

    source: Optional[str]             # SOURCE_* key, or None when idle/unrecognised
    playback: str                     # "playing", "paused" or "idle"
    title: Optional[str] = None
    artist: Optional[str] = None      # DAB: the station; YT Music: may be a playlist
    album: Optional[str] = None
    position_s: Optional[float] = None
    duration_s: Optional[float] = None
    # Raw artwork reference read in the same metadata window: MPRIS artUrl
    # (DAB / YT Music) or Minstrel's vetted public poster URL (Plex). Untrusted:
    # artwork_policy decides whether it is usable; never logged or shown.
    art_url: Optional[str] = field(default=None, repr=False)
    # Plex only: this Deck's own public URL for the current cover, served by
    # artwork_service.py (Cloudflare Tunnel). Preferred while that service is up.
    hosted_art_url: Optional[str] = field(default=None, repr=False)


# ---------------------------------------------------------------- Synapse reads

_SOURCE_KEYS = {
    MEDIA_SOURCE_PLEX: ("/plex/metadata_seq", "/plex/title_utf8", "/plex/artist_utf8", "/plex/album_utf8"),
    MEDIA_SOURCE_BLUETOOTH: ("/bt/metadata_seq", "/bt/title_utf8", "/bt/artist_utf8", "/bt/album_utf8"),
    MEDIA_SOURCE_MPRIS: ("/mpris/metadata_seq", "/mpris/title_utf8", "/mpris/artist_utf8", "/mpris/album_utf8"),
}


def read_raw_text(bus, key: str, width: int) -> Optional[str]:
    """Read a NUL-terminated UTF-8 Synapse text array, unfiltered (None if absent)."""
    try:
        raw = bus.try_get_array(key, length=width, dtype="u8")
    except Exception:  # missing or stale segment
        return None
    if raw is None:
        return None
    return bytes(int(b) & 0xFF for b in raw).split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def mpris_text_width(bus) -> int:
    advertised = bus.get_int("/mpris/text_bytes", TEXT_BYTES)
    return advertised if 16 <= advertised <= 65536 else TEXT_BYTES


def _read_text(bus, key: str, width: int) -> Optional[str]:
    return clean_text(read_raw_text(bus, key, width))


def clean_text(value: Optional[str]) -> Optional[str]:
    """Return human-facing text, or None for placeholder, unsafe or empty values."""
    if value is None:
        return None
    text = "".join(ch for ch in str(value) if ch.isprintable()).strip()
    if not text or text.casefold() in _PLACEHOLDERS or "�" in text:
        return None
    lowered = text.lower()
    # Never forward anything that looks like a URL, path or credential.
    if "://" in lowered or "x-plex-token" in lowered or "token=" in lowered or text.startswith(("/", "~")):
        return None
    if len(text) > MAX_FIELD_CHARS:
        text = text[: MAX_FIELD_CHARS - 1].rstrip() + "…"
    return text if len(text) >= MIN_FIELD_CHARS else None


def read_stable_metadata(bus, source: int) -> Optional[Tuple[Optional[str], ...]]:
    """Return ``(title, artist, album, art_url, hosted_art_url)`` read in one unchanged ``*_seq`` window.

    ``art_url`` is the raw MPRIS artwork reference (None for other sources).

    Returns None if the record kept changing for SEQ_READ_ATTEMPTS reads;
    callers then keep their previous metadata instead of publishing a torn one.
    """
    keys = _SOURCE_KEYS.get(source)
    if keys is None:
        return (None, None, None, None, None)
    seq_key, title_key, artist_key, album_key = keys
    width = mpris_text_width(bus) if source == MEDIA_SOURCE_MPRIS else TEXT_BYTES
    for _ in range(SEQ_READ_ATTEMPTS):
        before = bus.get_int(seq_key, -1)
        title = _read_text(bus, title_key, width)
        artist = _read_text(bus, artist_key, width)
        album = _read_text(bus, album_key, width)
        art_url = (read_raw_text(bus, "/mpris/art_url_utf8", width) if source == MEDIA_SOURCE_MPRIS else
                   read_raw_text(bus, "/plex/art_public_url_utf8", PLEX_ART_URL_BYTES)
                   if source == MEDIA_SOURCE_PLEX else None)
        hosted = (read_raw_text(bus, "/plex/art_hosted_url_utf8", PLEX_ART_URL_BYTES)
                  if source == MEDIA_SOURCE_PLEX else None)
        if bus.get_int(seq_key, -1) == before:
            return (title, artist, album, art_url, hosted)
    return None


def _timing(bus, active: int) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(position_s, duration_s)``; None where the source reports nothing usable."""
    if active == MEDIA_SOURCE_PLEX:
        position, duration = bus.get_float("/plex/position_sec", -1.0), bus.get_float("/plex/duration_sec", 0.0)
    elif active == MEDIA_SOURCE_BLUETOOTH:
        position, duration = bus.get_int("/bt/position_ms", -1) / 1000.0, bus.get_int("/bt/duration_ms", 0) / 1000.0
    else:
        position, duration = bus.get_int("/mpris/position_ms", -1) / 1000.0, bus.get_int("/mpris/duration_ms", 0) / 1000.0
    return (position if position >= 0 else None, duration if duration > 0 else None)


def read_media_snapshot(bus, previous: Optional[MediaSnapshot] = None) -> MediaSnapshot:
    """Read the active source, its playback state, metadata and timing from Synapse."""
    active = bus.get_int("/media/active_source", 0)
    if active == MEDIA_SOURCE_PLEX:
        if bus.get_int("/plex/connection_state", PLEX_CONNECTED) != PLEX_CONNECTED:
            return MediaSnapshot(source=None, playback="idle")
        name, state = SOURCE_PLEX, bus.get_int("/plex/play_state", 0)
    elif active == MEDIA_SOURCE_BLUETOOTH:
        name, state = SOURCE_BLUETOOTH, bus.get_int("/bt/play_state", 0)
    elif active == MEDIA_SOURCE_MPRIS:
        mapped = _MPRIS_SOURCES.get(bus.get_int("/hdmi2/selected_source", 0))
        if mapped is None or not bus.get_int("/mpris/available", 0):
            return MediaSnapshot(source=None, playback="idle")  # unrecognised or no player
        name, expected_label = mapped
        label = read_raw_text(bus, "/mpris/source_utf8", mpris_text_width(bus))
        if label and label.strip() and label.strip().upper() != expected_label:
            # mpris_bridge has not switched players yet: its state and metadata
            # still describe the previous source, so publish nothing from it.
            return MediaSnapshot(source=None, playback="idle")
        state = bus.get_int("/mpris/play_state", 0)
    else:
        return MediaSnapshot(source=None, playback="idle")

    if state == PLAY_PAUSED:
        return MediaSnapshot(source=name, playback="paused")
    if state != PLAY_PLAYING:
        return MediaSnapshot(source=None, playback="idle")

    metadata = read_stable_metadata(bus, active)
    if metadata is None:  # torn: reuse what we had for the same source, never another source's
        if previous is not None and previous.source == name:
            metadata = (previous.title, previous.artist, previous.album, previous.art_url,
                        previous.hosted_art_url)
        else:
            metadata = (None, None, None, None, None)
    position, duration = _timing(bus, active)
    return MediaSnapshot(source=name, playback="playing", title=metadata[0], artist=metadata[1],
                         album=metadata[2], position_s=position, duration_s=duration, art_url=metadata[3],
                         hosted_art_url=metadata[4])


# ---------------------------------------------------------------- media timing

@dataclass
class PresenceTimeline:
    """Discord timestamps from media timing, stable enough not to cause updates.

    * Plex, Bluetooth and YT Music with a usable position and duration show
      track progress: start = now - position, end = start + duration.
    * DAB (live radio) and anything without a usable duration show elapsed
      listening time from when playback started; for DAB the timer resets only
      when the station (MPRIS artist) changes, not on programme/song updates.
    * Timestamps are anchored once per track or station and kept while fresh
      position samples agree within ``seek_tolerance_s``, so the payload does
      not change every second; a seek re-anchors. Idle/paused resets.
    """

    seek_tolerance_s: float = 5.0
    _key: Optional[Tuple[Any, ...]] = None
    _mode: Optional[str] = None
    _start_ms: Optional[int] = None
    _end_ms: Optional[int] = None
    _last_position: Optional[float] = None

    def reset(self) -> None:
        self._key = self._mode = self._start_ms = self._end_ms = self._last_position = None

    def update(self, snapshot: MediaSnapshot, wall_now: float) -> Tuple[Optional[int], Optional[int]]:
        if snapshot.playback != "playing" or snapshot.source is None:
            self.reset()
            return (None, None)
        if snapshot.source == SOURCE_DAB:
            station = snapshot.artist
            if station is None and self._key is not None and self._key[0] == SOURCE_DAB:
                station = self._key[1]  # metadata gap: same station
            key: Tuple[Any, ...] = (SOURCE_DAB, station)
        else:
            key = (snapshot.source, snapshot.title, snapshot.artist, snapshot.album)
        position, duration = snapshot.position_s, snapshot.duration_s
        progress = (snapshot.source != SOURCE_DAB and duration is not None and duration >= 1.0
                    and position is not None and 0.0 <= position <= duration + 2.0)
        if progress:
            start_ms = int(round((wall_now - position) * 1000.0))
            same = key == self._key and self._mode == "progress" and self._start_ms is not None
            if same:
                fresh = position != self._last_position
                if fresh and abs(start_ms - self._start_ms) > self.seek_tolerance_s * 1000.0:
                    self._start_ms = start_ms  # seek or resync
            else:
                self._start_ms = start_ms
            self._end_ms = self._start_ms + int(round(duration * 1000.0))
            self._last_position = position
            self._mode = "progress"
        else:
            if key != self._key or self._mode != "elapsed" or self._start_ms is None:
                self._start_ms = int(round(wall_now * 1000.0))
            self._end_ms = None
            self._last_position = None
            self._mode = "elapsed"
        self._key = key
        return (self._start_ms, self._end_ms)


# ---------------------------------------------------------------- presence construction

# Sources whose MPRIS artwork may become the large image (artwork_policy.py).
DYNAMIC_ARTWORK_SOURCES = frozenset({SOURCE_DAB, SOURCE_YTMUSIC, SOURCE_PLEX})


def dynamic_artwork_allowed(snapshot: MediaSnapshot, policy: PresencePolicy) -> bool:
    """Privacy gate: own artwork reveals what is playing, so it needs every switch on."""
    return (policy.artwork_mode == "on" and policy.show_media_metadata and policy.show_source
            and snapshot.playback == "playing" and snapshot.source in DYNAMIC_ARTWORK_SOURCES)


def artist_album_line(artist: Optional[str], album: Optional[str], title: Optional[str] = None) -> Optional[str]:
    """Return the second line, "Artist • Album", either one alone, or None.

    Takes clean_text() values. The album is left out when it repeats the
    artist or the title (case-insensitively). If the joined line would pass
    MAX_FIELD_CHARS the album is shortened with "…" and the artist kept
    whole; an album that would keep fewer than MIN_ALBUM_CHARS is dropped.
    """
    if album and album.casefold() in {(artist or "").casefold(), (title or "").casefold()}:
        album = None
    if not artist or not album:
        return artist or album
    line = TEXT_ARTIST_ALBUM.format(artist=artist, album=album)
    if len(line) <= MAX_FIELD_CHARS:
        return line
    room = MAX_FIELD_CHARS - len(TEXT_ARTIST_ALBUM.format(artist=artist, album="")) - 1
    shortened = album[:max(room, 0)].rstrip()
    if len(shortened) < MIN_ALBUM_CHARS:
        return artist
    return TEXT_ARTIST_ALBUM.format(artist=artist, album=shortened + "…")


def build_presence(snapshot: MediaSnapshot, policy: PresencePolicy,
                   timing: Tuple[Optional[int], Optional[int]] = (None, None),
                   artwork: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the presence fields to publish, or None to clear presence.

    The large image is the Ember Deck asset, or ``artwork`` (a URL already
    vetted by artwork_policy.py) when dynamic_artwork_allowed(); the small
    image is the source's asset during playback (when the source may be
    shown). Titles, artists, albums and stations appear only with
    ``show_media_metadata``; outside DAB the album shares the second line
    with the artist (artist_album_line). With ``policy.project_url`` both
    images link to it and the large image's tooltip is TEXT_EXPLORE, which
    Discord also renders as the activity's third line.
    """
    activity_type = policy.activity_type if policy.activity_type in ("listening", "playing") else "listening"
    link = policy.project_url
    large: Dict[str, Any] = {"large_image": ASSET_EMBER_DECK, "large_text": TEXT_EXPLORE if link else TEXT_DECK}
    if link:
        large["large_url"] = link
    if snapshot.playback != "playing" or snapshot.source not in SOURCE_LABELS:
        if not policy.show_idle:
            return None
        return {"type": activity_type, "details": TEXT_IDLE, **large}

    source = snapshot.source
    label = SOURCE_LABELS[source]
    presence: Dict[str, Any] = {"type": activity_type, **large}
    if policy.show_source:
        presence["small_image"] = SOURCE_ASSETS[source]
        presence["small_text"] = label
        if link:
            presence["small_url"] = link

    details: Optional[str] = None
    state: Optional[str] = None
    if policy.show_media_metadata:
        title, artist, album = clean_text(snapshot.title), clean_text(snapshot.artist), clean_text(snapshot.album)
        if source == SOURCE_DAB:
            # MPRIS artist is the station; title/album carry programme or song text.
            details = title or album or artist
            state = artist if artist and artist != details else None
        else:
            # YT Music's "artist" may be a playlist: shown as published, not relabelled.
            details = title
            state = artist_album_line(artist, album, title)
    if not details:
        details = TEXT_PLAYING
    if not state and policy.show_source:
        state = TEXT_VIA.format(source=label)
    presence["details"] = details
    if state:
        presence["state"] = state

    if artwork and dynamic_artwork_allowed(snapshot, policy):
        presence["large_image"] = artwork

    start_ms, end_ms = timing
    if start_ms is not None:
        presence["start_ms"] = int(start_ms)
        if end_ms is not None and end_ms > start_ms:
            presence["end_ms"] = int(end_ms)
    return presence


# ---------------------------------------------------------------- update scheduling

@dataclass
class PresenceScheduler:
    """Decide when a desired presence should actually be sent to Discord.

    * identical payloads are never re-sent;
    * a change must stay stable for ``debounce_s`` before it is sent;
    * updates are at least ``min_interval_s`` apart (Discord rate-limits presence);
    * ``mark_unpublished()`` (after a reconnect) forces the current payload out again.
    """

    debounce_s: float = 2.0
    min_interval_s: float = 5.0
    published: Optional[Dict[str, str]] = None
    has_published: bool = False
    _candidate: Optional[Dict[str, str]] = None
    _candidate_since: Optional[float] = None
    _last_sent_at: Optional[float] = None
    _force: bool = field(default=False)

    def mark_unpublished(self) -> None:
        self.has_published = False
        self._force = True

    def due(self, desired: Optional[Dict[str, str]], now: float) -> Tuple[bool, Optional[Dict[str, str]]]:
        """Return ``(send, payload)``; payload None means clear presence."""
        if self.has_published and desired == self.published and not self._force:
            self._candidate, self._candidate_since = None, None
            return False, None
        if desired != self._candidate or self._candidate_since is None:
            self._candidate, self._candidate_since = desired, now
        if not self._force and now - self._candidate_since < self.debounce_s:
            return False, None
        if self._last_sent_at is not None and now - self._last_sent_at < self.min_interval_s:
            return False, None
        return True, desired

    def sent(self, payload: Optional[Dict[str, str]], now: float) -> None:
        self.published = payload
        self.has_published = True
        self._last_sent_at = now
        self._force = False
        self._candidate, self._candidate_since = None, None

    def reset(self) -> None:
        self.published = None
        self.has_published = False
        self._candidate = None
        self._candidate_since = None
        self._force = False

