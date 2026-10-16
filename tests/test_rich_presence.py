"""Discord Rich Presence worker: gating, presence model, auth lifecycle and IPC.

Discord's SDK is never needed. Worker tests use an in-process FakeBridge; the
IPC tests run tests/fake_discord_bridge.py as a real child process so the
actual BridgeProcess code (pipes, reader thread, timeouts, shutdown) runs.
"""

import io
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ENGINE, FakeBus, capture_whisper, has_module  # noqa: E402

import contracts as C  # noqa: E402
import discord_ipc  # noqa: E402
import presence_model as pm  # noqa: E402
import rich_presence as rp  # noqa: E402
import runtime_config  # noqa: E402
from discord_ipc import Backend, BackendChoice, BridgeError, BridgeProcess  # noqa: E402

HAS_YAML = has_module("yaml")
HAS_CRYPTO = has_module("cryptography")
FAKE_BRIDGE = Path(__file__).resolve().parent / "fake_discord_bridge.py"
APP_ID = "123456789012345678"
ACCESS = "accessTOKENaaaaaaaaaaaaaaaa"
REFRESH = "refreshTOKENbbbbbbbbbbbbbbb"


def text_bytes(text, width=192):
    raw = text.encode("utf-8")[: width - 1]
    return list(raw) + [0] * (width - len(raw))


# --------------------------------------------------------------------------- presence model

class PresenceModelTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()

    def media(self, source, state=C.PLAY_PLAYING, **extra):
        self.bus.values["/media/active_source"] = source
        if source == C.MEDIA_SOURCE_PLEX:
            self.bus.values["/plex/play_state"] = state
        elif source == C.MEDIA_SOURCE_BLUETOOTH:
            self.bus.values["/bt/play_state"] = state
        elif source == C.MEDIA_SOURCE_MPRIS:
            self.bus.values.update({"/mpris/play_state": state, "/mpris/available": 1})
        self.bus.values.update(extra)

    def presence(self, policy=pm.PresencePolicy()):
        return pm.build_presence(pm.read_media_snapshot(self.bus), policy)

    def test_idle(self):
        self.assertEqual(self.presence(), {"type": "listening", "details": "Idle", "large_image": "ember-deck", "large_text": "The Ember Deck"})
        self.assertIsNone(self.presence(pm.PresencePolicy(show_idle=False)))
        for state in (C.PLAY_STOPPED, C.PLAY_PAUSED):
            self.media(C.MEDIA_SOURCE_PLEX, state, **{"/plex/title_utf8": text_bytes("Song"), "/plex/metadata_seq": 1})
            shown = self.presence(pm.PresencePolicy(show_media_metadata=True))
            self.assertEqual(shown, {"type": "listening", "details": "Idle", "large_image": "ember-deck", "large_text": "The Ember Deck"}, "paused/stopped is Idle: no title, icon or timer")

    def test_each_source_by_default_shows_only_the_source(self):
        cases = [
            (C.MEDIA_SOURCE_PLEX, {}, "Plex", "source-plex"),
            (C.MEDIA_SOURCE_BLUETOOTH, {}, "Bluetooth", "source-bluetooth"),
            (C.MEDIA_SOURCE_MPRIS, {"/hdmi2/selected_source": C.HDMI2_SOURCE_RADIO}, "DAB", "source-dab"),
            (C.MEDIA_SOURCE_MPRIS, {"/hdmi2/selected_source": C.HDMI2_SOURCE_YOUTUBE}, "YT Music", "source-ytmusic"),
        ]
        for source, extra, label, icon in cases:
            with self.subTest(label=label):
                self.bus = FakeBus()
                self.media(source, **extra)
                self.assertEqual(self.presence(), {
                    "type": "listening", "details": "Playing music", "state": f"via {label}",
                    "large_image": "ember-deck", "large_text": "The Ember Deck",
                    "small_image": icon, "small_text": label})

    def test_metadata_is_private_by_default_and_never_includes_device_or_server(self):
        self.media(C.MEDIA_SOURCE_BLUETOOTH, **{
            "/bt/title_utf8": text_bytes("Secret Song"), "/bt/artist_utf8": text_bytes("Band"),
            "/bt/device_name_utf8": text_bytes("Alex's iPhone"), "/bt/metadata_seq": 4})
        shown = str(self.presence())
        for private in ("Secret Song", "Band", "iPhone"):
            self.assertNotIn(private, shown)

    def test_metadata_when_enabled_is_track_then_artist_and_album(self):
        self.media(C.MEDIA_SOURCE_PLEX, **{"/plex/title_utf8": text_bytes("Song"),
                                           "/plex/artist_utf8": text_bytes("Band"),
                                           "/plex/album_utf8": text_bytes("Album"), "/plex/metadata_seq": 2})
        shown = self.presence(pm.PresencePolicy(show_media_metadata=True))
        self.assertEqual((shown["details"], shown["state"], shown["large_text"]),
                         ("Song", "Band • Album", "The Ember Deck"))
        self.assertEqual((shown["small_image"], shown["small_text"]), ("source-plex", "Plex"))
        hidden_source = self.presence(pm.PresencePolicy(show_media_metadata=True, show_source=False))
        self.assertNotIn("small_image", hidden_source)
        self.assertNotIn("Plex", str(hidden_source))

    def test_unusable_metadata_is_dropped(self):
        for title in ("N/A", "", "x", "http://plex:32400/x?X-Plex-Token=abc", "/home/pi/Music/a.flac", "a�b"):
            with self.subTest(title=title):
                self.bus = FakeBus()
                self.media(C.MEDIA_SOURCE_PLEX, **{"/plex/title_utf8": text_bytes(title), "/plex/metadata_seq": 1})
                presence = self.presence(pm.PresencePolicy(show_media_metadata=True))
                self.assertEqual(presence["details"], "Playing music")
        self.assertEqual(len(pm.clean_text("y" * 300)), pm.MAX_FIELD_CHARS)

    def test_missing_segments_and_unavailable_plex(self):
        self.media(C.MEDIA_SOURCE_PLEX, **{"/plex/connection_state": C.PLEX_UNAVAILABLE})
        self.assertEqual(self.presence()["details"], "Idle")
        self.media(C.MEDIA_SOURCE_MPRIS, **{"/mpris/available": 0})
        self.assertEqual(self.presence()["details"], "Idle")

    def test_torn_metadata_is_retried_then_never_published(self):
        self.media(C.MEDIA_SOURCE_PLEX, **{"/plex/title_utf8": text_bytes("New Song"),
                                           "/plex/artist_utf8": text_bytes("New Band"), "/plex/metadata_seq": 1})
        reads = {"n": 0}
        real = self.bus.get_int

        def flapping(key, default=0):
            if key == "/plex/metadata_seq":
                reads["n"] += 1
                return reads["n"]  # changes on every read: always torn
            return real(key, default)

        previous = pm.MediaSnapshot("plex", "playing", "Old Song", "Old Band")
        with mock.patch.object(self.bus, "get_int", flapping):
            snapshot = pm.read_media_snapshot(self.bus, previous)
        self.assertEqual(reads["n"], 2 * pm.SEQ_READ_ATTEMPTS)
        self.assertEqual((snapshot.title, snapshot.artist), ("Old Song", "Old Band"))

        settled = {"n": 0}

        def settles(key, default=0):
            if key == "/plex/metadata_seq":
                settled["n"] += 1
                return 5 if settled["n"] > 1 else 4  # torn once, then stable
            return real(key, default)

        with mock.patch.object(self.bus, "get_int", settles):
            snapshot = pm.read_media_snapshot(self.bus)
        self.assertEqual(snapshot.title, "New Song")

    def test_mpris_metadata_uses_the_advertised_width(self):
        self.media(C.MEDIA_SOURCE_MPRIS, **{"/hdmi2/selected_source": C.HDMI2_SOURCE_YOUTUBE, "/mpris/text_bytes": 512,
                                            "/mpris/title_utf8": text_bytes("Long " * 50, 512)})
        snapshot = pm.read_media_snapshot(self.bus)
        self.assertGreater(len(snapshot.title), 100)


class SourceMappingTests(unittest.TestCase):
    """Final activity mapping for the four sources (metadata enabled unless stated)."""

    ON = pm.PresencePolicy(show_media_metadata=True)

    def setUp(self):
        self.bus = FakeBus()

    def mpris(self, selected, label, **fields):
        values = {"/media/active_source": C.MEDIA_SOURCE_MPRIS, "/hdmi2/selected_source": selected,
                  "/mpris/available": 1, "/mpris/play_state": C.PLAY_PLAYING, "/mpris/metadata_seq": 1,
                  "/mpris/source_utf8": text_bytes(label)}
        for key, value in fields.items():
            values[f"/mpris/{key}"] = text_bytes(value) if isinstance(value, str) else value
        self.bus.values.update(values)

    def show(self, policy=ON):
        return pm.build_presence(pm.read_media_snapshot(self.bus), policy)

    def test_plex(self):
        self.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_PLEX, "/plex/play_state": C.PLAY_PLAYING,
                                "/plex/metadata_seq": 3, "/plex/title_utf8": text_bytes("Teardrop"),
                                "/plex/artist_utf8": text_bytes("Massive Attack"),
                                "/plex/album_utf8": text_bytes("Mezzanine"),
                                "/plex/position_sec": 30.0, "/plex/duration_sec": 330.0})
        shown = self.show()
        self.assertEqual((shown["details"], shown["state"], shown["large_text"], shown["small_image"]),
                         ("Teardrop", "Massive Attack • Mezzanine", "The Ember Deck", "source-plex"))
        snapshot = pm.read_media_snapshot(self.bus)
        self.assertEqual((snapshot.position_s, snapshot.duration_s), (30.0, 330.0))

    def test_bluetooth_without_artwork(self):
        self.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_BLUETOOTH, "/bt/play_state": C.PLAY_PLAYING,
                                "/bt/metadata_seq": 1, "/bt/title_utf8": text_bytes("Phone Song"),
                                "/bt/artist_utf8": text_bytes("Phone Artist"), "/bt/album_utf8": text_bytes("N/A"),
                                "/bt/position_ms": 61000, "/bt/duration_ms": 200000})
        shown = self.show()
        self.assertEqual((shown["details"], shown["state"], shown["large_image"], shown["small_image"]),
                         ("Phone Song", "Phone Artist", "ember-deck", "source-bluetooth"))
        self.assertEqual(shown["large_text"], "The Ember Deck", "a placeholder album is never shown")
        self.assertEqual(pm.read_media_snapshot(self.bus).position_s, 61.0)

    def test_dab_station_and_programme(self):
        self.mpris(C.HDMI2_SOURCE_RADIO, "RADIO", title_utf8="Now: Song by Someone", artist_utf8="BBC Radio 6 Music",
                   album_utf8="Afternoon Show", duration_ms=0, position_ms=0)
        shown = self.show()
        self.assertEqual((shown["details"], shown["state"], shown["small_image"], shown["small_text"]),
                         ("Now: Song by Someone", "BBC Radio 6 Music", "source-dab", "DAB"))
        self.assertEqual(shown["large_text"], "The Ember Deck", "DAB album text is programme info, not an album")
        self.mpris(C.HDMI2_SOURCE_RADIO, "RADIO", title_utf8="N/A", album_utf8="Afternoon Show")
        self.assertEqual(self.show()["details"], "Afternoon Show")
        self.mpris(C.HDMI2_SOURCE_RADIO, "RADIO", title_utf8="", album_utf8="")
        shown = self.show()
        self.assertEqual((shown["details"], shown["state"]), ("BBC Radio 6 Music", "via DAB"),
                         "the station is not repeated")

    def test_ytmusic_keeps_the_published_meaning_of_artist(self):
        self.mpris(C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", title_utf8="Official Video", artist_utf8="My Mix Playlist",
                   album_utf8="", duration_ms=240000, position_ms=10000)
        shown = self.show()
        self.assertEqual((shown["details"], shown["state"], shown["small_image"], shown["small_text"]),
                         ("Official Video", "My Mix Playlist", "source-ytmusic", "YT Music"))
        self.assertNotIn("by ", shown["state"])

    def test_placeholders_are_never_rendered(self):
        for placeholder in ("N/A", "None", "Unknown", "unknown artist", "null", "-", ""):
            with self.subTest(placeholder=placeholder):
                self.bus = FakeBus()
                self.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_PLEX, "/plex/play_state": C.PLAY_PLAYING,
                                        "/plex/metadata_seq": 1, "/plex/title_utf8": text_bytes(placeholder),
                                        "/plex/artist_utf8": text_bytes(placeholder),
                                        "/plex/album_utf8": text_bytes(placeholder)})
                shown = self.show()
                self.assertEqual((shown["details"], shown["state"]), ("Playing music", "via Plex"))
                for value in shown.values():
                    self.assertNotIn(str(value).strip().casefold(), {"n/a", "none", "unknown", "null"})

    def test_privacy_hides_titles_artists_albums_and_stations(self):
        self.mpris(C.HDMI2_SOURCE_RADIO, "RADIO", title_utf8="Secret Programme", artist_utf8="Secret Station FM",
                   album_utf8="Secret Album")
        for policy in (pm.PresencePolicy(), pm.PresencePolicy(show_source=False)):
            shown = str(self.show(policy))
            for private in ("Secret Programme", "Secret Station", "Secret Album"):
                self.assertNotIn(private, shown)

    def test_mpris_still_describing_the_previous_player_publishes_nothing(self):
        # HDMI-2 already selected YT Music, but mpris_bridge still reports the
        # radio player: its state/metadata are stale for this source.
        self.mpris(C.HDMI2_SOURCE_YOUTUBE, "RADIO", title_utf8="Old Radio Song", artist_utf8="Old Station")
        self.assertEqual(self.show(), json_like(IDLE_PAYLOAD))
        self.mpris(C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", title_utf8="New Video", artist_utf8="Channel")
        self.assertEqual(self.show()["details"], "New Video")

    def test_unrecognised_or_vanished_players_are_idle(self):
        self.mpris(C.HDMI2_SOURCE_PLEX, "MPRIS", title_utf8="Some Player")
        self.assertEqual(self.show(), json_like(IDLE_PAYLOAD))
        self.mpris(C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", title_utf8="Video", available=0)
        self.assertEqual(self.show(), json_like(IDLE_PAYLOAD))

    def test_source_switch_never_mixes_metadata(self):
        self.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_PLEX, "/plex/play_state": C.PLAY_PLAYING,
                                "/plex/metadata_seq": 1, "/plex/title_utf8": text_bytes("Plex Song")})
        previous = pm.read_media_snapshot(self.bus)
        self.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_BLUETOOTH, "/bt/play_state": C.PLAY_PLAYING,
                                "/bt/metadata_seq": 1, "/bt/title_utf8": text_bytes("Phone Song")})
        snapshot = pm.read_media_snapshot(self.bus, previous)
        self.assertEqual((snapshot.source, snapshot.title), ("bluetooth", "Phone Song"))

    def test_only_registered_assets_can_appear(self):
        self.mpris(C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", title_utf8="Video",
                   art_url_utf8="https://i.ytimg.com/vi/abc/hqdefault.jpg")
        for policy in (self.ON, pm.PresencePolicy(), pm.PresencePolicy(artwork_mode="on")):
            shown = self.show(policy)
            self.assertNotIn("://", str(shown))
            for image in ("large_image", "small_image"):
                if image in shown:
                    self.assertIn(shown[image], pm.REGISTERED_ASSETS)


IDLE_PAYLOAD = {"type": "listening", "details": "Idle", "large_image": "ember-deck", "large_text": "The Ember Deck"}


def json_like(value):
    return dict(value)


YT_URL = "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
YT_GOOGLE_URL = "https://lh3.googleusercontent.com/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-ab=w544-h544-l90-rj"
DAB_URL = "https://www.station.example.org/images/logo-192.png"
PLEX_POSTER = "https://metadata-static.plex.tv/a/people/abcdef0123456789.jpg"
HOSTED_A = "https://artwork.example.org/come-clarity-in-flames.png?v=a3f91c2e0b1d"
HOSTED_B = "https://artwork.example.org/last-fair-deal-gone-down-katatonia.png?v=b71e40f2c3d4"


class ArtworkPolicyTests(unittest.TestCase):
    """artwork_policy.vet_artwork_url: public https artwork yes; private or credential-bearing no."""

    def vet(self, url, source="ytmusic"):
        import artwork_policy
        return artwork_policy.vet_artwork_url(url, source)

    def test_ordinary_public_https_urls_are_accepted(self):
        for url, source in ((YT_URL, "ytmusic"), (YT_GOOGLE_URL, "ytmusic"), (DAB_URL, "dab"),
                            ("https://i.ytimg.com/vi/dQw4w9WgXcQ/hq.jpg?sqp=-oaymwE&rs=AOn4CLC", "ytmusic"),
                            ("https://cdn-profiles.tunein.com/s123/images/logoq.png?t=1", "dab"),
                            ("https://favicons.any-provider.net/station.ico", "dab")):
            with self.subTest(url=url):
                self.assertEqual(self.vet(url, source), (url, "accepted"))

    def test_private_credential_and_unusual_urls_are_rejected(self):
        cases = {
            ("", "ytmusic"): "missing",
            ("N/A", "dab"): "missing",
            (YT_URL, "bluetooth"): "unsupported_source",
            (YT_URL, None): "unsupported_source",
            ("http://radio.example.org/logo.png", "dab"): "not_https",
            ("file:///home/pi/.cache/shortwave/mpris-cover/1.png", "dab"): "not_https",
            ("data:image/png;base64,AAAA", "dab"): "not_https",
            ("https://user:pw@radio.example.org/logo.png", "dab"): "credentials",
            ("https://cdn.example.org/a.png?X-Plex-Token=abc", "dab"): "credentials",
            ("https://cdn.example.org/a.png?access_token=abc", "dab"): "credentials",
            ("https://cdn.example.org/a.png?api_key=abc", "dab"): "credentials",
            ("https://cdn.example.org/a.png?Expires=1&Signature=abc", "dab"): "credentials",
            ("https://cdn.example.org/a.png?X-Amz-Credential=x&X-Amz-Signature=y", "dab"): "credentials",
            ("https://cdn.example.org/a.png?session=abc", "dab"): "credentials",
            ("https://192.168.1.20:32400/photo/x", "dab"): "ip_address",
            ("https://142.250.180.14/a.png", "dab"): "ip_address",
            ("https://[fd00::1]/a.png", "dab"): "ip_address",
            ("https://localhost/a.png", "dab"): "private_network",
            ("https://nas/a.png", "dab"): "private_network",
            ("https://printer.local/a.png", "dab"): "private_network",
            ("https://plex.home.arpa/a.png", "dab"): "private_network",
            ("https://192-168-1-20.0123abcd.plex.direct/photo/x", "dab"): "private_network",
            ("https://radio.example.org:8443/logo.png", "dab"): "port",
            ("https://radio.example.org/logo.png#x", "dab"): "fragment",
            ("https://radio.example.org/lo go.png", "dab"): "invalid",
            ("https://radio.example.org/é.png", "dab"): "invalid",
            ("https://radio.example.org/" + "a" * 300 + ".png", "dab"): "too_long",
        }
        for (url, source), reason in cases.items():
            with self.subTest(url=url[:60], source=source):
                self.assertEqual(self.vet(url, source), (None, reason))

    def test_no_network_activity(self):
        import artwork_policy
        import socket
        with mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("no DNS")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("no fetch")):
            self.assertEqual(artwork_policy.vet_artwork_url(DAB_URL, "dab"), (DAB_URL, "accepted"))


@unittest.skipUnless(HAS_YAML, "PyYAML is required for the token store")
class DynamicArtworkTests(unittest.TestCase):
    ON = {"artwork_mode": "on", "show_media_metadata": True}

    def harness(self, presence=None):
        return Harness(self, tokens=tokens(), overrides={"presence": dict(self.ON, **(presence or {}))})

    def play_mpris(self, h, selected, label, art, title="Track", artist="Someone", seq=1):
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_MPRIS, "/hdmi2/selected_source": selected,
                             "/mpris/available": 1, "/mpris/play_state": C.PLAY_PLAYING, "/mpris/metadata_seq": seq,
                             "/mpris/source_utf8": text_bytes(label), "/mpris/title_utf8": text_bytes(title),
                             "/mpris/artist_utf8": text_bytes(artist), "/mpris/art_url_utf8": text_bytes(art)})

    def sent(self, h):
        return [f["presence"] for op, f in h.world.bridge.requests if op == "set_presence"]

    def decisions(self, h):
        return [m.metadata.get("decision") for m in h.messages if m.event == "artwork_selection"]

    def test_ytmusic_uses_its_artwork_and_keeps_the_source_icon(self):
        h = self.harness()
        self.play_mpris(h, C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", YT_URL, title="Song", artist="Playlist")
        h.tick(n=4)
        last = self.sent(h)[-1]
        self.assertEqual((last["large_image"], last["small_image"], last["details"], last["state"]),
                         (YT_URL, "source-ytmusic", "Song", "Playlist"))
        self.assertEqual(dict(h.world.bridge.requests)["init"]["external_artwork"], True)
        self.assertIn("source=ytmusic image=dynamic reason=accepted", self.decisions(h))
        self.assertNotIn("dQw4w9WgXcQ", h.logged_text())

    def test_dab_uses_any_public_station_artwork(self):
        h = self.harness()
        self.play_mpris(h, C.HDMI2_SOURCE_RADIO, "RADIO", DAB_URL, title="Programme A", artist="Station One")
        h.tick(n=4)
        first = self.sent(h)[-1]
        self.assertEqual((first["large_image"], first["small_image"]), (DAB_URL, "source-dab"))
        start = first["start_ms"]
        self.play_mpris(h, C.HDMI2_SOURCE_RADIO, "RADIO", DAB_URL, title="Programme B", artist="Station One", seq=2)
        h.tick(n=8)
        self.assertEqual([p["large_image"] for p in self.sent(h)], [DAB_URL, DAB_URL])
        self.assertEqual(self.sent(h)[-1]["start_ms"], start, "the listening timer is untouched")
        other = "https://another-station.example.net/logo.png?v=2"
        self.play_mpris(h, C.HDMI2_SOURCE_RADIO, "RADIO", other, title="News", artist="Station Two", seq=3)
        h.tick(n=8)
        self.assertEqual(self.sent(h)[-1]["large_image"], other)

    def test_unchanged_artwork_causes_no_updates_and_track_change_does(self):
        h = self.harness()
        self.play_mpris(h, C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", YT_URL, title="One")
        h.tick(n=34)
        self.assertEqual(len(self.sent(h)), 1)
        self.play_mpris(h, C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", YT_GOOGLE_URL, title="Two", seq=2)
        h.tick(n=4)
        self.assertEqual([p["large_image"] for p in self.sent(h)], [YT_URL, YT_GOOGLE_URL])

    def test_rejected_artwork_falls_back_to_the_static_image(self):
        for art, reason in (("https://192.168.1.20:32400/photo/x?X-Plex-Token=tok3n", "ip_address"),
                            ("https://plex.example.org/photo/x?X-Plex-Token=tok3n", "credentials"),
                            ("file:///home/pi/.cache/x.png", "not_https"), ("", "missing")):
            with self.subTest(reason=reason):
                h = self.harness()
                self.play_mpris(h, C.HDMI2_SOURCE_RADIO, "RADIO", art)
                h.tick(n=4)
                self.assertEqual(self.sent(h)[-1]["large_image"], "ember-deck")
                self.assertIn(f"source=dab image=static reason={reason}", self.decisions(h))
                for leak in ("192.168", "tok3n", "/home/pi"):
                    self.assertNotIn(leak, h.logged_text())
                    self.assertNotIn(leak, str(h.world.bridge.requests))

    def test_privacy_and_mode_gates(self):
        for presence in ({"show_media_metadata": False}, {"show_source": False}, {"artwork_mode": "audit"},
                         {"artwork_mode": "off"}):
            with self.subTest(presence=presence):
                h = self.harness(presence=presence)
                self.play_mpris(h, C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", YT_URL)
                h.tick(n=4)
                self.assertNotIn("ytimg", str(self.sent(h)))
                self.assertEqual(self.sent(h)[-1]["large_image"], "ember-deck")

    def play_plex(self, h, public_url, seq=1):
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_PLEX, "/plex/play_state": C.PLAY_PLAYING,
                             "/plex/art_valid": 1, "/plex/metadata_seq": seq,
                             "/plex/title_utf8": text_bytes("Song"),
                             "/plex/art_public_url_utf8": text_bytes(public_url, 512)})

    def test_plex_uses_a_published_public_poster_url(self):
        h = self.harness()
        self.play_plex(h, PLEX_POSTER)
        h.tick(n=4)
        last = self.sent(h)[-1]
        self.assertEqual((last["large_image"], last["small_image"]), (PLEX_POSTER, "source-plex"))
        self.assertIn("source=plex image=dynamic origin=public reason=accepted", self.decisions(h))
        for art in ("", "https://plex.example.org/photo/x?X-Plex-Token=tok3n",
                    "https://192-168-1-20.abc.plex.direct/photo/x"):
            with self.subTest(art=art):
                h2 = self.harness()
                self.play_plex(h2, art)
                h2.tick(n=4)
                self.assertEqual(self.sent(h2)[-1]["large_image"], "ember-deck")
                self.assertNotIn("tok3n", str(h2.world.bridge.requests) + h2.logged_text())
        h3 = self.harness(presence={"show_media_metadata": False})
        self.play_plex(h3, PLEX_POSTER)
        h3.tick(n=4)
        self.assertEqual(self.sent(h3)[-1]["large_image"], "ember-deck")

    def artwork_service(self, h, ready=True, fresh=True):
        stamp = h.bus.monotonic_ms()
        if not fresh:
            stamp = (stamp - 20_000) & h.bus.MS_CLOCK_MASK or 1
        h.bus.values.update({"/artwork/service_ready": int(ready), "/proc/artwork_service/heartbeat_ms": stamp})

    def play_plex_hosted(self, h, hosted, public="", seq=1, title="Song"):
        self.play_plex(h, public, seq=seq)
        h.bus.values.update({"/plex/art_hosted_url_utf8": text_bytes(hosted, 512),
                             "/plex/title_utf8": text_bytes(title)})

    def test_plex_prefers_the_decks_hosted_cover(self):
        h = self.harness()
        self.artwork_service(h)
        self.play_plex_hosted(h, HOSTED_A, public=PLEX_POSTER)
        h.tick(n=4)
        last = self.sent(h)[-1]
        self.assertEqual((last["large_image"], last["small_image"]), (HOSTED_A, "source-plex"))
        self.assertIn("source=plex image=dynamic origin=hosted reason=accepted", self.decisions(h))
        self.assertNotIn("come-clarity", h.logged_text(), "album URLs are not logged")

    def test_hosted_cover_falls_back_when_the_service_is_down(self):
        for ready, fresh in ((False, True), (True, False)):
            with self.subTest(ready=ready, fresh=fresh):
                h = self.harness()
                self.artwork_service(h, ready=ready, fresh=fresh)
                self.play_plex_hosted(h, HOSTED_A, public=PLEX_POSTER)
                h.tick(n=4)
                self.assertEqual(self.sent(h)[-1]["large_image"], PLEX_POSTER, "public poster next")
                h2 = self.harness()
                self.artwork_service(h2, ready=ready, fresh=fresh)
                self.play_plex_hosted(h2, HOSTED_A)
                h2.tick(n=4)
                self.assertEqual(self.sent(h2)[-1]["large_image"], "ember-deck", "then static")

    def test_hosted_cover_respects_privacy_and_mode(self):
        for presence in ({"show_media_metadata": False}, {"show_source": False}, {"artwork_mode": "audit"},
                         {"artwork_mode": "off"}):
            with self.subTest(presence=presence):
                h = self.harness(presence=presence)
                self.artwork_service(h)
                self.play_plex_hosted(h, HOSTED_A)
                h.tick(n=4)
                self.assertEqual(self.sent(h)[-1]["large_image"], "ember-deck")
                self.assertNotIn("awoo", str(h.world.bridge.requests))

    def test_album_changes_switch_covers_and_same_album_adds_no_updates(self):
        h = self.harness()
        self.artwork_service(h)
        self.play_plex_hosted(h, HOSTED_A, title="Track 1")
        h.tick(n=4)
        sent_before = len(self.sent(h))
        self.play_plex_hosted(h, HOSTED_A, seq=2, title="Track 1")  # same track re-published
        for _ in range(10):
            self.artwork_service(h)
            h.tick()
        self.assertEqual(len(self.sent(h)), sent_before, "unchanged cover: no Discord update")
        self.play_plex_hosted(h, HOSTED_B, seq=3, title="Other album song")
        self.artwork_service(h)
        h.tick(n=4)
        self.assertEqual(self.sent(h)[-1]["large_image"], HOSTED_B)

    def test_hosted_cover_survives_reconnect_and_leaves_with_the_source(self):
        h = self.harness()
        self.artwork_service(h)
        self.play_plex_hosted(h, HOSTED_A)
        h.tick(n=4)
        h.world.bridge.events.append({"event": "status", "state": "disconnected", "error": 2, "detail": 1006})
        h.tick()
        self.artwork_service(h)
        h.tick(seconds=6.0)
        self.artwork_service(h)
        h.tick(seconds=2.0)
        self.assertEqual(self.sent(h)[-1]["large_image"], HOSTED_A)
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_BLUETOOTH, "/bt/play_state": C.PLAY_PLAYING})
        h.tick(n=8)
        self.assertEqual(self.sent(h)[-1]["large_image"], "ember-deck")
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_PLEX, "/plex/play_state": C.PLAY_PAUSED})
        h.tick(n=8)
        self.assertEqual(self.sent(h)[-1]["details"], "Idle")

    def test_plex_and_bluetooth_stay_static_and_switches_drop_previous_artwork(self):
        h = self.harness()
        self.play_mpris(h, C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", YT_URL)
        h.tick(n=4)
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_PLEX, "/plex/play_state": C.PLAY_PLAYING,
                             "/plex/art_valid": 1})
        h.tick(n=8)
        self.assertEqual(self.sent(h)[-1]["large_image"], "ember-deck", "no published URL: static")
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_BLUETOOTH, "/bt/play_state": C.PLAY_PLAYING})
        h.tick(n=8)
        self.assertEqual(self.sent(h)[-1]["large_image"], "ember-deck")
        # HDMI-2 switched to DAB but mpris_bridge still describes YT Music: nothing from it.
        self.play_mpris(h, C.HDMI2_SOURCE_RADIO, "YOUTUBE", YT_URL)
        h.tick(n=8)
        self.assertEqual((self.sent(h)[-1]["details"], self.sent(h)[-1]["large_image"]), ("Idle", "ember-deck"))

    def test_pause_and_reconnect(self):
        h = self.harness()
        self.play_mpris(h, C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", YT_URL)
        h.tick(n=4)
        h.world.bridge.events.append({"event": "status", "state": "disconnected", "error": 2, "detail": 1006})
        h.tick()
        h.tick(seconds=6.0)
        h.tick(seconds=2.0)
        self.assertEqual(self.sent(h)[-1]["large_image"], YT_URL, "a reconnect restores the artwork")
        h.bus.values["/mpris/play_state"] = C.PLAY_PAUSED
        h.tick(n=8)
        self.assertEqual(self.sent(h)[-1], {"type": "listening", "details": "Idle", "large_image": "ember-deck",
                                           "large_text": "The Ember Deck"})

    def test_worker_never_forwards_an_unvetted_image(self):
        h = self.harness()
        self.play_mpris(h, C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", YT_URL)
        evil = dict(type="listening", details="x", large_image="https://evil.example/a.png",
                    small_image="https://evil.example/b.png")
        with mock.patch.object(rp, "build_presence", return_value=evil):
            h.tick(n=4)
        last = self.sent(h)[-1]
        self.assertEqual(last["large_image"], "ember-deck")
        self.assertNotIn("small_image", last)

    def test_artwork_errors_never_disturb_presence(self):
        h = self.harness()
        self.play_mpris(h, C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE", YT_URL)
        with mock.patch("artwork_policy.vet_artwork_url", side_effect=RuntimeError("https://secret/?token=t")):
            h.tick(n=4)
        self.assertEqual(self.sent(h)[-1]["large_image"], "ember-deck")
        self.assertNotIn("secret", h.logged_text())


class TimelineTests(unittest.TestCase):
    WALL = 1_800_000_000.0

    def snap(self, source="plex", position=30.0, duration=300.0, title="Song", artist="Band", playback="playing"):
        return pm.MediaSnapshot(source, playback, title, artist, None, position, duration)

    def test_track_progress_from_media_position(self):
        timeline = pm.PresenceTimeline()
        start, end = timeline.update(self.snap(position=30.0, duration=300.0), self.WALL)
        self.assertEqual((start, end), (int((self.WALL - 30) * 1000), int((self.WALL + 270) * 1000)))
        shown = pm.build_presence(self.snap(), pm.PresencePolicy(), (start, end))
        self.assertEqual((shown["start_ms"], shown["end_ms"]), (start, end))

    def test_stable_while_playing_so_no_per_second_updates(self):
        timeline = pm.PresenceTimeline()
        first = timeline.update(self.snap(position=30.0), self.WALL)
        position = 30.0
        for second in range(1, 60):
            # Position samples arrive late or jitter by a couple of seconds.
            position = 30.0 + second - (second % 3)
            self.assertEqual(timeline.update(self.snap(position=position), self.WALL + second), first)
        # A stale (unchanged) sample never re-anchors, however old.
        self.assertEqual(timeline.update(self.snap(position=position), self.WALL + 200), first)

    def test_seek_and_track_change_reanchor(self):
        timeline = pm.PresenceTimeline()
        first = timeline.update(self.snap(position=30.0), self.WALL)
        seek = timeline.update(self.snap(position=200.0), self.WALL + 5)
        self.assertNotEqual(seek, first)
        self.assertEqual(seek[0], int((self.WALL + 5 - 200) * 1000))
        change = timeline.update(self.snap(title="Next", position=1.0), self.WALL + 6)
        self.assertEqual(change[0], int((self.WALL + 5) * 1000))

    def test_pause_resume_recomputes_and_idle_has_no_timer(self):
        timeline = pm.PresenceTimeline()
        timeline.update(self.snap(position=30.0), self.WALL)
        self.assertEqual(timeline.update(self.snap(playback="paused"), self.WALL + 10), (None, None))
        resumed = timeline.update(self.snap(position=30.0), self.WALL + 100)
        self.assertEqual(resumed[0], int((self.WALL + 70) * 1000))
        idle = pm.build_presence(pm.MediaSnapshot(None, "idle"), pm.PresencePolicy(), resumed)
        self.assertNotIn("start_ms", idle)

    def test_dab_elapsed_time_resets_only_on_station_change(self):
        timeline = pm.PresenceTimeline()
        dab = lambda title, station: pm.MediaSnapshot("dab", "playing", title, station, "Show", 0.0, None)
        start, end = timeline.update(dab("Song A", "Station 1"), self.WALL)
        self.assertEqual((start, end), (int(self.WALL * 1000), None))
        self.assertEqual(timeline.update(dab("Song B", "Station 1"), self.WALL + 300), (start, None))
        self.assertEqual(timeline.update(dab("Song B", None), self.WALL + 310), (start, None), "metadata gap")
        self.assertEqual(timeline.update(dab("Song C", "Station 2"), self.WALL + 400)[0], int((self.WALL + 400) * 1000))
        shown = pm.build_presence(dab("Song C", "Station 2"), pm.PresencePolicy(), (start, None))
        self.assertNotIn("end_ms", shown)

    def test_unreliable_progress_falls_back_to_elapsed(self):
        timeline = pm.PresenceTimeline()
        for position, duration in ((None, 300.0), (30.0, None), (500.0, 300.0), (-1.0, 300.0)):
            with self.subTest(position=position, duration=duration):
                timeline.reset()
                self.assertEqual(timeline.update(self.snap(position=position, duration=duration), self.WALL),
                                 (int(self.WALL * 1000), None))


class ArtworkAuditTests(unittest.TestCase):
    SECRETS = ("tok3nVALUE", "192.168.1.20", "plex.home.lan", "/home/pi", "secretpath", "s3cr3tsig", "user:pw")

    def classify(self, url):
        import artwork_audit
        return artwork_audit.classify_url(url)

    def test_classifications(self):
        cases = {
            "": "missing",
            "N/A": "missing",
            "file:///home/pi/.cache/secretpath/logo.png": "local",
            "data:image/png;base64,AAAA": "embedded",
            "https://lh3.googleusercontent.com/abc=w544-h544": "public_candidate",
            "https://i.ytimg.com/vi/x/hqdefault.jpg": "public_candidate",
            "http://cdn.example.org/a.jpg": "public_insecure",
            "http://192.168.1.20:32400/photo/:/transcode?url=x": "private_network",
            "https://plex.home.lan/library/x": "private_network",
            "http://localhost:8080/a.png": "private_network",
            "https://[fd00::1]/a.png": "private_network",
            "https://cdn.example.org/a.jpg?X-Plex-Token=tok3nVALUE": "authenticated",
            "https://cdn.example.org/a.jpg?Signature=s3cr3tsig&Expires=1": "authenticated",
            "https://user:pw@cdn.example.org/a.jpg": "authenticated",
            "ftp://example.org/a.png": "unsupported_scheme",
            "javascript:alert(1)": "unsupported_scheme",
            "https:///nohost": "invalid",
            "x" * 9000: "invalid",
        }
        for url, expected in cases.items():
            with self.subTest(url=url[:40]):
                self.assertEqual(self.classify(url)["classification"], expected)
        self.assertEqual(self.classify("https://i.ytimg.com/vi/x/hq.jpg")["host"], "known_cdn")
        self.assertEqual(self.classify("https://cdn.example.org/a.jpg")["host"], "other")

    def test_diagnostics_never_contain_url_parts(self):
        import artwork_audit
        urls = ["http://192.168.1.20:32400/x?X-Plex-Token=tok3nVALUE", "file:///home/pi/secretpath/a.png",
                "https://user:pw@plex.home.lan/a?Signature=s3cr3tsig", "https://cdn.example.org/secretpath/a.png"]
        for url in urls:
            line = artwork_audit.format_audit({"source": "ytmusic", **artwork_audit.classify_url(url)})
            for secret in self.SECRETS + ("cdn.example.org", "http", "?"):
                if secret == "http":
                    continue
                self.assertNotIn(secret, line)
            self.assertRegex(line, r"^[a-z_=0-9 ]+$", "only fixed words reach the log")

    def test_per_source_inspection(self):
        import artwork_audit
        bus = FakeBus({"/plex/art_valid": 1, "/mpris/text_bytes": 192,
                       "/mpris/art_url_utf8": text_bytes("file:///tmp/x.png")})
        self.assertEqual(artwork_audit.inspect_source(bus, "plex"),
                         {"kind": "plex_server", "classification": "authenticated"})
        self.assertEqual(artwork_audit.inspect_source(bus, "dab")["classification"], "local")
        self.assertEqual(artwork_audit.inspect_source(bus, "bluetooth")["classification"], "missing")
        bus.values["/plex/art_valid"] = 0
        self.assertEqual(artwork_audit.inspect_source(bus, "plex")["classification"], "missing")

    def test_auditor_logs_once_per_change_and_ignores_idle(self):
        import artwork_audit
        logged = []
        auditor = artwork_audit.ArtworkAuditor(logged.append)
        bus = FakeBus({"/mpris/text_bytes": 192, "/mpris/art_url_utf8": text_bytes("https://i.ytimg.com/a.jpg")})
        playing = pm.MediaSnapshot("ytmusic", "playing")
        for _ in range(5):
            auditor.observe(bus, playing)
        auditor.observe(bus, pm.MediaSnapshot(None, "idle"))
        bus.values["/mpris/art_url_utf8"] = text_bytes("file:///x.png")
        auditor.observe(bus, playing)
        self.assertEqual([entry["classification"] for entry in logged], ["public_candidate", "local"])


class SchedulerTests(unittest.TestCase):
    def test_duplicates_are_suppressed_and_changes_debounced(self):
        sched = pm.PresenceScheduler(debounce_s=2.0, min_interval_s=5.0)
        a, b = {"details": "A"}, {"details": "B"}
        self.assertEqual(sched.due(a, 0.0), (False, None))       # waiting for debounce
        self.assertEqual(sched.due(a, 2.0), (True, a))
        sched.sent(a, 2.0)
        self.assertEqual(sched.due(a, 50.0), (False, None))      # identical payload
        self.assertFalse(sched.due(b, 50.0)[0])
        self.assertFalse(sched.due({"details": "C"}, 51.0)[0])   # a newer change restarts the debounce
        self.assertTrue(sched.due({"details": "C"}, 53.0)[0])

    def test_minimum_interval_and_forced_republish(self):
        sched = pm.PresenceScheduler(debounce_s=0.0, min_interval_s=5.0)
        sched.sent({"details": "A"}, 0.0)
        self.assertFalse(sched.due({"details": "B"}, 1.0)[0])
        self.assertTrue(sched.due({"details": "B"}, 5.0)[0])
        sched.sent({"details": "B"}, 5.0)
        sched.mark_unpublished()
        self.assertEqual(sched.due({"details": "B"}, 11.0), (True, {"details": "B"}))

    def test_clear_is_a_payload_too(self):
        sched = pm.PresenceScheduler(debounce_s=0.0, min_interval_s=1.0)
        sched.sent({"details": "A"}, 0.0)
        self.assertEqual(sched.due(None, 2.0), (True, None))


# --------------------------------------------------------------------------- worker harness

class FakeBridge:
    """In-process bridge double following protocol v1 semantics."""

    def __init__(self, world, backend):
        self.world = world
        self.backend = backend
        self.requests = []
        self.events = []
        self.running = True
        self.stopped = False
        world.bridges.append(self)

    def alive(self):
        return self.running

    def exit_status(self):
        return None if self.running else 1

    def request(self, op, timeout_s=5.0, **fields):
        if not self.running:
            raise BridgeError("bridge exited")
        self.requests.append((op, fields))
        handler = self.world.responses.get(op)
        result = handler(fields) if callable(handler) else handler
        if result is None:
            result = "authorizing" if op == "begin_auth" else "success"
        if isinstance(result, Exception):
            raise result
        if op == "set_token" and result == "success" and self.world.auto_ready:
            self.events.append({"event": "status", "state": "ready", "error": 0, "detail": 0})
        return {"id": len(self.requests), "ok": result == "success", "result": result}

    def poll_events(self):
        events, self.events = self.events, []
        return events

    def stop(self):
        self.stopped = True
        self.running = False

    def ops(self):
        return [op for op, _ in self.requests]


class World:
    def __init__(self):
        self.bridges = []
        self.responses = {}
        self.auto_ready = True
        self.backend = BackendChoice(Backend("native", ["discord_bridge"]), "native aarch64 bridge")

    @property
    def bridge(self):
        return self.bridges[-1] if self.bridges else None


class Harness:
    def __init__(self, test, *, tokens=None, overrides=None):
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, True)
        loaded = {
            "discord": {"application_id": APP_ID, "auth_state_path": str(self.tmp / "discord_secrets.yaml"),
                        "retry_initial_s": 5.0, "retry_max_s": 60.0, "auth_timeout_s": 120.0},
            "presence": {"update_debounce_s": 0.0, "min_update_interval_s": 1.0},
        }
        for section, values in (overrides or {}).items():
            if isinstance(values, dict):
                loaded.setdefault(section, {}).update(values)
            else:
                loaded[section] = values
        self.cfg = rp._merge(rp.DEFAULTS, loaded)
        runtime_config.validate_rich_presence(self.cfg)
        self.cfg["_config_dir"] = str(self.tmp)
        self.bus = FakeBus({"/plex/play_state": C.PLAY_PLAYING, "/media/active_source": C.MEDIA_SOURCE_PLEX,
                            "/io/in/control/gain": 0.5})
        self.core_before = dict(self.bus.values)
        patcher = mock.patch.object(rp, "bus", self.bus)
        patcher.start()
        test.addCleanup(patcher.stop)
        whisper = capture_whisper()
        self.messages = whisper.__enter__()
        test.addCleanup(whisper.__exit__, None, None, None)
        self.world = World()
        self.wall = [1_800_000_000]
        self.now = 1000.0
        if tokens is not None:
            self.store().save(tokens)
        self.worker = rp.RichPresenceWorker(
            self.cfg,
            bridge_factory=lambda backend: FakeBridge(self.world, backend),
            backend_selector=lambda discord, base: self.world.backend,
            wall_clock=lambda: self.wall[0],
        )

    def store(self):
        return rp.TokenStore(self.tmp / "discord_secrets.yaml")

    def tick(self, seconds=1.0, n=1):
        for _ in range(n):
            self.now += seconds
            self.worker.tick(self.now)

    def status(self):
        return self.bus.get_int("/discord/status", -1)

    def events(self):
        return [m.event for m in self.messages]

    def logged_text(self):
        return " ".join(f"{m.event} {m.metadata}" for m in self.messages)

    def core_untouched(self):
        return all(self.bus.values.get(k) == v for k, v in self.core_before.items()) and \
            all(key.startswith("/discord/") for key in self.bus.writes)


def tokens(expires_in=7 * 86400, wall=1_800_000_000):
    return rp.DiscordTokens(ACCESS, REFRESH, wall + expires_in, "openid sdk.social_layer_presence")


# --------------------------------------------------------------------------- independence from Awoo supporter status

@unittest.skipUnless(HAS_YAML, "PyYAML is required for the token store")
class SupporterIndependenceTests(unittest.TestCase):
    """Discord Rich Presence is ordinary functionality: an Awoo supporter
    licence, valid or not, present or not, has no effect on it."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.licence = self.home / ".config" / "awoo" / "supporter.licence"
        self.licence.parent.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        # Any use of the supporter library by the Discord worker would show up here.
        import awoo_supporter
        self.supporter_calls = []
        for name in ("verify_licence", "read_licence_file"):
            patcher = mock.patch.object(awoo_supporter, name,
                                        side_effect=lambda *a, _n=name, **k: self.supporter_calls.append(_n))
            patcher.start()
            self.addCleanup(patcher.stop)

    def valid_licence(self):
        from test_awoo_supporter import Signer
        return Signer().licence()

    def run_scenario(self, licence_text):
        if licence_text is None:
            if self.licence.exists():
                self.licence.unlink()
        else:
            self.licence.write_text(licence_text + "\n", encoding="ascii")
        h = Harness(self, tokens=tokens())
        h.tick(n=4)
        discord_state = {k: v for k, v in h.bus.values.items() if k.startswith("/discord/")}
        return h, [b.ops() for b in h.world.bridges], discord_state

    def test_no_licence_links_connects_and_publishes_presence(self):
        h, ops, state = self.run_scenario(None)
        self.assertEqual(ops[0][:2], ["hello", "init"])
        self.assertIn("set_token", ops[0])
        self.assertIn("set_presence", ops[0])
        self.assertEqual(h.status(), C.DISCORD_STATUS_CONNECTED)
        self.assertNotIn("/discord/entitled", state)
        self.assertTrue(h.core_untouched())

    def test_no_licence_and_no_tokens_is_headless_and_not_linked(self):
        h = Harness(self)
        h.tick(n=5)
        self.assertEqual(h.world.bridge.ops(), ["hello", "init"])
        self.assertEqual(h.status(), C.DISCORD_STATUS_NOT_LINKED)

    def test_identical_behaviour_with_missing_invalid_and_valid_licences(self):
        scenarios = [None, "AWOO1.not-a-real-licence", "garbage \u00e9".encode("ascii", "replace").decode()]
        if HAS_CRYPTO:
            scenarios.append(self.valid_licence())
        baseline = None
        for licence_text in scenarios:
            with self.subTest(licence=(licence_text or "none")[:12]):
                _, ops, state = self.run_scenario(licence_text)
                if baseline is None:
                    baseline = (ops, state)
                self.assertEqual((ops, state), baseline)
        self.assertEqual(self.supporter_calls, [], "the Discord worker never consults the supporter library")

    def test_licence_changes_while_running_change_nothing(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=4)
        bridge = h.world.bridge
        ops_before = list(bridge.ops())
        state_before = {k: v for k, v in h.bus.values.items() if k.startswith("/discord/")}
        changes = ["AWOO1.invalid"]
        if HAS_CRYPTO:
            changes.append(self.valid_licence())
        for text in changes:
            self.licence.write_text(text, encoding="ascii")
            h.tick(seconds=31.0, n=3)
        self.licence.unlink()
        h.tick(seconds=31.0, n=3)
        self.assertEqual(len(h.world.bridges), 1, "the bridge is never restarted")
        self.assertFalse(bridge.stopped)
        self.assertNotIn("clear_presence", bridge.ops()[len(ops_before):])
        self.assertEqual({k: v for k, v in h.bus.values.items() if k.startswith("/discord/")}, state_before)
        self.assertEqual(self.supporter_calls, [])


class PiCompatibilityAndRetiredFlowTests(unittest.TestCase):
    BRIDGE = ENGINE / "discord_bridge"

    def code_lines(self, path):
        """Source lines without // comments (C++) or # comments (Python)."""
        marker = "//" if path.suffix in (".cpp", ".hpp") else "#"
        return "\n".join(line.split(marker, 1)[0] for line in path.read_text(encoding="utf-8").splitlines())

    def test_cross_build_allows_undefined_symbols_only_in_the_x86_64_toolchain(self):
        toolchain = (self.BRIDGE / "cmake" / "x86_64-linux-gnu.cmake").read_text(encoding="utf-8")
        self.assertIn('set(CMAKE_EXE_LINKER_FLAGS_INIT "-Wl,--allow-shlib-undefined")', toolchain)
        self.assertIn("set(CMAKE_SYSTEM_PROCESSOR x86_64)", toolchain)
        self.assertNotIn("allow-shlib-undefined", (self.BRIDGE / "CMakeLists.txt").read_text(encoding="utf-8"),
                         "native and development builds keep full link checks")

    def test_device_flow_is_never_used(self):
        sources = [self.BRIDGE / "src" / "main.cpp", ENGINE / "rich_presence.py", ENGINE / "discord_ipc.py",
                   ENGINE / "ember_ui.py"]
        for path in sources:
            code = self.code_lines(path)
            for retired in ("GetTokenFromDevice", "DeviceAuthorizationArgs", "begin_device_auth",
                            "cancel_device_auth", "device_authorization", "auth_user_code", "auth_verification_uri"):
                self.assertNotIn(retired, code, f"{retired} in {path.name}")
        main = self.code_lines(self.BRIDGE / "src" / "main.cpp")
        for api in ("CreateAuthorizationCodeVerifier()", "->Authorize(", "->GetToken(", "SetCodeChallenge(",
                    "->AbortAuthorize()"):
            self.assertIn(api, main)

    def test_pkce_verifier_stays_in_the_bridge(self):
        main = self.code_lines(self.BRIDGE / "src" / "main.cpp")
        self.assertIn("pending_verifier_.set(verifier.Verifier())", main)
        self.assertNotIn('"verifier"', main, "the verifier is never put in a protocol message")
        self.assertNotIn('"code"', main, "the authorization code is never put in a protocol message")
        python = self.code_lines(ENGINE / "rich_presence.py") + self.code_lines(ENGINE / "discord_ipc.py")
        self.assertNotIn("verifier", python)


LAUNCHER = ENGINE / "discord_bridge" / "launcher" / "xdg-open"
SH = shutil.which("sh")

FAKE_OPENER = """#!/bin/sh
# Test stand-in for the system xdg-open: stays alive like a browser launch,
# then records its argv one per line.
sleep 2
for arg in "$@"; do printf '%s\\n' "$arg"; done > "$EMBER_TEST_OUT.tmp"
mv "$EMBER_TEST_OUT.tmp" "$EMBER_TEST_OUT"
"""


@unittest.skipUnless(SH, "a POSIX shell is required")
class DetachedOpenerLauncherTests(unittest.TestCase):
    """discord_bridge/launcher/xdg-open run as a real process."""

    def setUp(self):
        import subprocess
        self.subprocess = subprocess
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = self.tmp / "fake-opener"
        self.fake.write_bytes(FAKE_OPENER.encode("ascii"))
        self.fake.chmod(0o755)
        self.out = self.tmp / "argv.txt"
        # The shell's own spelling of these paths (e.g. /c/... under Git for Windows).
        self.posix_tmp = subprocess.run([SH, "-c", "pwd"], cwd=self.tmp, capture_output=True,
                                        text=True, check=True).stdout.strip()

    def run_launcher(self, args, opener):
        import time
        env = dict(os.environ, EMBER_TEST_OUT=f"{self.posix_tmp}/argv.txt")
        env.pop("EMBERDECK_XDG_OPEN", None)
        if opener is not None:
            env["EMBERDECK_XDG_OPEN"] = opener
        # Arguments travel through a file and a POSIX driver, so no platform
        # command-line parsing (Windows re-quotes and globs argv) sits between
        # the test and the launcher's own "$@" handling.
        args_file = self.tmp / "args.txt"
        args_file.write_bytes("".join(arg + "\n" for arg in args).encode("utf-8"))
        driver = self.tmp / "driver.sh"
        driver.write_bytes(b'set --\nwhile IFS= read -r line; do set -- "$@" "$line"; done < "$ARGS_FILE"\n'
                           b'exec sh "$LAUNCHER" "$@"\n')
        env.update(ARGS_FILE=f"{self.posix_tmp}/args.txt", LAUNCHER=str(LAUNCHER).replace(os.sep, "/"))
        started = time.monotonic()
        result = self.subprocess.run([SH, str(driver)], capture_output=True, env=env, timeout=20)
        return result, time.monotonic() - started

    def wait_for_output(self):
        import time
        deadline = time.monotonic() + 15
        while not self.out.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        return self.out.read_text(encoding="utf-8").splitlines() if self.out.exists() else None

    def test_returns_at_once_and_the_opener_runs_independently_with_literal_args(self):
        args = ["https://discord.com/oauth2/authorize?client_id=1&state=s3cr3tSTATE&code_challenge=CH ALL",
                "$(touch pwned)", "; touch pwned2", "`touch pwned3`", "*", "q'uo\"te", "--", ""]
        result, elapsed = self.run_launcher(args, f"{self.posix_tmp}/fake-opener")
        self.assertEqual(result.returncode, 0)
        self.assertLess(elapsed, 1.5, "the SDK's child must exit before the opener does")
        self.assertFalse(self.out.exists(), "the opener is still running after the launcher returned")
        self.assertEqual((result.stdout, result.stderr), (b"", b""), "nothing (and no URL) is printed")
        self.assertEqual(self.wait_for_output(), args, "argv reaches the real opener unchanged")
        for name in ("pwned", "pwned2", "pwned3"):
            self.assertFalse((self.tmp / name).exists() or Path(name).exists())

    def test_refuses_without_a_usable_real_opener(self):
        for opener in (None, "", "xdg-open", f"{self.posix_tmp}/missing"):
            with self.subTest(opener=opener):
                result, _ = self.run_launcher(["https://discord.com/x?state=s"], opener)
                self.assertEqual(result.returncode, 3)
                self.assertEqual((result.stdout, result.stderr), (b"", b""))

    def test_never_calls_itself(self):
        posix_launcher = self.subprocess.run([SH, "-c", 'cd "$(dirname "$0")" && pwd', str(LAUNCHER)],
                                             capture_output=True, text=True).stdout.strip() + "/xdg-open"
        result, elapsed = self.run_launcher(["https://discord.com/x"], posix_launcher)
        self.assertEqual(result.returncode, 3)
        self.assertLess(elapsed, 5)


class DetachedOpenerEnvironmentTests(unittest.TestCase):
    def which(self, name, path=None):
        return "/usr/bin/xdg-open" if name == "xdg-open" and "/usr/bin" in (path or "").split(os.pathsep) else None

    def test_launcher_script_is_safe_by_construction(self):
        code = "\n".join(line.split("#", 1)[0] for line in LAUNCHER.read_text(encoding="utf-8").splitlines()[1:])
        self.assertTrue(LAUNCHER.read_bytes().startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", LAUNCHER.read_bytes(), "LF line endings (a CRLF shebang would not run)")
        for forbidden in ("eval", "echo", "printf", "logger", "tee", "$*", "systemd-cat"):
            self.assertNotIn(forbidden, code)
        self.assertIn('"$real" "$@" </dev/null >/dev/null 2>&1 &', code)

    def test_environment_puts_the_launcher_first_and_resolves_the_real_opener(self):
        search = os.pathsep.join(["/opt/bin", str(discord_ipc.LAUNCHER_DIR), "/usr/bin"])
        env = discord_ipc.detached_opener_env(path=search, which=self.which)
        self.assertEqual(env["PATH"].split(os.pathsep), [str(discord_ipc.LAUNCHER_DIR), "/opt/bin", "/usr/bin"])
        self.assertEqual(env["EMBERDECK_XDG_OPEN"], os.path.abspath("/usr/bin/xdg-open"))
        self.assertEqual(discord_ipc.detached_opener_env(path="/opt/bin", which=self.which), {},
                         "no system xdg-open: PATH is left alone")

    def test_only_the_box64_backend_gets_the_launcher(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "x86").write_bytes(elf(62))
        (tmp / "arm").write_bytes(elf(183))
        os.chmod(tmp / "arm", 0o755)
        discord = {"backend": "auto", "native_bridge": str(tmp / "arm"), "sdk_library_dir": None,
                   "box64": {"executable": "box64", "bridge": str(tmp / "x86")}}
        before = dict(os.environ)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["/usr/local/bin", "/usr/bin"])}):
            box = discord_ipc._box64(discord, tmp, which=lambda n: "/usr/bin/box64", opener_which=self.which)
            native = discord_ipc._native(discord, tmp, "aarch64")
        env = box.backend.env
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(discord_ipc.LAUNCHER_DIR))
        self.assertEqual(env["EMBERDECK_XDG_OPEN"], os.path.abspath("/usr/bin/xdg-open"))
        self.assertEqual((env["UV_USE_IO_URING"], env["BOX64_NOBANNER"], env["BOX64_LOG"]), ("0", "1", "0"))
        self.assertNotIn("PATH", native.backend.env)
        self.assertNotIn("EMBERDECK_XDG_OPEN", native.backend.env)
        self.assertEqual(dict(os.environ), before, "Ember Deck's own environment is never changed")
        users = [path.name for path in ENGINE.glob("*.py")
                 if "EMBERDECK_XDG_OPEN" in path.read_text(encoding="utf-8")
                 or "launcher" in path.read_text(encoding="utf-8")]
        self.assertEqual(users, ["discord_ipc.py"])


class NoSupporterCouplingTests(unittest.TestCase):
    def test_worker_and_config_carry_no_supporter_coupling(self):
        source = (ENGINE / "rich_presence.py").read_text(encoding="utf-8").lower()
        for marker in ("awoo_supporter", "supporter", "licence", "entitle"):
            self.assertNotIn(marker, source)
        with self.assertRaisesRegex(runtime_config.ConfigError, "unknown option"):
            runtime_config.validate_rich_presence({"supporter_licence_path": "~/.config/awoo/supporter.licence"})
        self.assertFalse(hasattr(C, "DISCORD_STATUS_LICENCE_REQUIRED"))


class FeatureSwitchTests(unittest.TestCase):
    def test_disabled_and_unconfigured_never_start_the_bridge(self):
        for overrides, status in (({"enabled": False}, C.DISCORD_STATUS_DISABLED),
                                  ({"discord": {"application_id": None}}, C.DISCORD_STATUS_NOT_CONFIGURED)):
            with self.subTest(status=status):
                h = Harness(self, overrides=overrides)
                h.tick(n=3)
                self.assertEqual(h.world.bridges, [])
                self.assertEqual(h.status(), status)

    def test_missing_backend_is_reported_and_retried_with_backoff(self):
        h = Harness(self)
        h.world.backend = BackendChoice(None, "native bridge not built; Box64 is not installed")
        calls = []
        h.worker.backend_selector = lambda d, b: calls.append(h.now) or h.world.backend
        h.tick(n=40)
        self.assertEqual(h.status(), C.DISCORD_STATUS_SDK_UNAVAILABLE)
        self.assertEqual([round(c - calls[0]) for c in calls], [0, 5, 15, 35])
        self.assertEqual(h.events().count("discord_backend_unavailable"), 1)
        self.assertTrue(h.core_untouched())


# --------------------------------------------------------------------------- authentication

@unittest.skipUnless(HAS_YAML, "PyYAML is required for the token store")
class AuthenticationLifecycleTests(unittest.TestCase):
    def test_unlinked_service_never_opens_a_browser(self):
        # Headless systemd worker: no begin_auth (which opens a browser),
        # however long it runs, after crashes or after an unlink.
        h = Harness(self)
        h.tick(seconds=60.0, n=30)
        h.world.bridge.running = False
        h.tick(seconds=60.0, n=5)
        for bridge in h.world.bridges:
            self.assertNotIn("begin_auth", bridge.ops())
        self.assertEqual(h.status(), C.DISCORD_STATUS_NOT_LINKED)
        self.assertEqual(h.bus.get_int("/discord/auth_state"), C.DISCORD_AUTH_UNLINKED)

    def test_refreshed_tokens_are_saved_before_use(self):
        h = Harness(self, tokens=tokens(expires_in=-10))
        h.tick()
        bridge = h.world.bridge
        ops_at_save = []
        real_save = rp.TokenStore.save
        def save(store, tokens_):
            ops_at_save.append(list(bridge.ops()))
            real_save(store, tokens_)
        bridge.events.append({"event": "tokens", "source": "refresh", "access_token": "newACCESStokenxxxxxx",
                              "refresh_token": "newREFRESHtokenyyyyy", "expires_in": 604800, "scope": "openid"})
        with mock.patch.object(rp.TokenStore, "save", save):
            h.tick(n=2)
        self.assertNotIn("set_token", ops_at_save[0], "tokens are persisted before they are used")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(h.store().path.stat().st_mode), 0o600)
        self.assertNotIn("newACCESStokenxxxxxx", h.logged_text())

    def test_no_synapse_key_carries_authorisation_material(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=3)
        keys = {k for k in h.bus.values if k.startswith("/discord/")}
        self.assertEqual(keys, {"/discord/enabled", "/discord/backend", "/discord/backend_available",
                                "/discord/auth_state", "/discord/connection_state", "/discord/status",
                                "/discord/status_seq"})
        for value in h.bus.values.values():
            self.assertNotIn(ACCESS, str(value))
            self.assertNotIn(REFRESH, str(value))

    def test_restart_with_stored_tokens_needs_no_authorisation(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=2)
        self.assertNotIn("begin_auth", h.world.bridge.ops())
        self.assertEqual(h.status(), C.DISCORD_STATUS_CONNECTED)

    def test_expired_access_token_is_refreshed_and_persisted(self):
        h = Harness(self, tokens=tokens(expires_in=-10))
        h.tick()
        bridge = h.world.bridge
        self.assertIn(("refresh_token", {"refresh_token": REFRESH}), bridge.requests)
        self.assertNotIn("set_token", bridge.ops(), "an expired token is not used")
        self.assertEqual(h.bus.get_int("/discord/auth_state"), C.DISCORD_AUTH_REFRESHING)
        bridge.events.append({"event": "tokens", "source": "refresh", "access_token": "newACCESStokenxxxxxx",
                              "refresh_token": "newREFRESHtokenyyyyy", "expires_in": 604800, "scope": "openid"})
        h.tick(n=2)
        self.assertEqual(h.store().load().refresh_token, "newREFRESHtokenyyyyy")
        self.assertIn(("set_token", {"access_token": "newACCESStokenxxxxxx"}), bridge.requests)
        self.assertEqual(h.status(), C.DISCORD_STATUS_CONNECTED)

    def test_token_near_expiry_is_renewed_while_still_connected(self):
        h = Harness(self, tokens=tokens(expires_in=3600))  # inside the default 24 h margin
        h.tick()
        self.assertIn("refresh_token", h.world.bridge.ops())
        self.assertIn("set_token", h.world.bridge.ops())

    def test_revoked_refresh_token_unlinks_without_opening_a_browser(self):
        h = Harness(self, tokens=tokens(expires_in=-10))
        h.tick()
        h.world.bridge.events.append({"event": "auth_failed", "source": "refresh", "result": "unauthenticated"})
        h.tick()
        self.assertIsNone(h.store().load())
        self.assertEqual(h.status(), C.DISCORD_STATUS_NOT_LINKED)
        h.tick(seconds=60.0, n=5)
        self.assertNotIn("begin_auth", h.world.bridge.ops())

    def test_transient_refresh_failure_keeps_the_link(self):
        h = Harness(self, tokens=tokens(expires_in=-10))
        h.tick()
        h.world.bridge.events.append({"event": "auth_failed", "source": "refresh", "result": "transient"})
        h.tick()
        self.assertIsNotNone(h.store().load())

    def test_non_rejection_refresh_failure_keeps_the_link_and_retries(self):
        # Only an explicit rejection unlinks; anything else (for example an
        # SDK validation or unknown error) must not cost the user their link.
        h = Harness(self, tokens=tokens(expires_in=-10))
        h.tick()
        h.world.bridge.events.append({"event": "auth_failed", "source": "refresh", "result": "permanent"})
        h.tick()
        self.assertIsNotNone(h.store().load())
        h.tick(seconds=6.0)
        self.assertEqual(h.world.bridge.ops().count("refresh_token"), 2)

    def link(self, h, **kwargs):
        options = dict(bridge_factory=lambda backend: FakeBridge(h.world, backend),
                       backend_selector=lambda d, b: h.world.backend, sleep=lambda s: None,
                       wall_clock=lambda: h.wall[0], environ={"DISPLAY": ":0"})
        options.update(kwargs)
        return rp.link(h.cfg, **options)

    def test_link_command_stores_tokens_and_the_worker_connects(self):
        h = Harness(self)
        h.tick()
        worker_bridge = h.worker.bridge
        token_event = {"event": "tokens", "source": "authorize", "access_token": ACCESS, "refresh_token": REFRESH,
                       "expires_in": 604800, "scope": "openid"}
        h.world.responses["begin_auth"] = lambda fields: h.world.bridge.events.append(token_event) or "authorizing"
        self.assertEqual(self.link(h), "linked")
        self.assertTrue(h.world.bridge.stopped)
        self.assertEqual(h.store().load().access_token, ACCESS)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(h.store().path.stat().st_mode), 0o600)
        h.tick(n=3)
        self.assertIn(("set_token", {"access_token": ACCESS}), worker_bridge.requests)
        self.assertEqual(h.status(), C.DISCORD_STATUS_CONNECTED)
        self.assertIn("set_presence", worker_bridge.ops())
        self.assertNotIn(ACCESS, h.logged_text())

    def test_link_requires_a_graphical_session_on_linux(self):
        h = Harness(self)
        with mock.patch.object(rp.sys, "platform", "linux"):
            outcome = self.link(h, environ={})
        self.assertIn("no graphical session", outcome)
        self.assertEqual(h.world.bridges, [], "no bridge (and so no browser) is started")
        with mock.patch.object(rp.sys, "platform", "linux"):
            h.world.responses["begin_auth"] = lambda fields: h.world.bridge.events.append(
                {"event": "auth_failed", "source": "authorize", "result": "cancelled"}) or "authorizing"
            self.assertEqual(self.link(h, environ={"WAYLAND_DISPLAY": "wayland-0"}),
                             "linking did not complete (cancelled)")

    def test_link_bridge_crash_is_reported_once_without_retry(self):
        h = Harness(self)
        def crash(fields):
            h.world.bridge.running = False
            return "authorizing"
        h.world.responses["begin_auth"] = crash
        outcome = self.link(h)
        self.assertIn("bridge stopped", outcome)
        self.assertEqual(len(h.world.bridges), 1)
        self.assertIsNone(h.store().load())

    def test_link_command_reports_failure_and_timeout(self):
        h = Harness(self)
        for result in ("cancelled", "unauthenticated", "transient"):
            h.world.responses["begin_auth"] = lambda fields, r=result: h.world.bridge.events.append(
                {"event": "auth_failed", "source": "authorize", "result": r}) or "authorizing"
            self.assertEqual(self.link(h), f"linking did not complete ({result})")
        h.world.responses["begin_auth"] = "authorizing"
        clock = iter(range(0, 10000, 50))
        self.assertEqual(self.link(h, clock=lambda: next(clock)), "not approved within 120 s")
        self.assertIn("cancel_auth", h.world.bridge.ops())
        self.assertIsNone(h.store().load())

    def test_malformed_token_response_is_rejected(self):
        h = Harness(self)
        h.tick()
        h.world.bridge.events.append({"event": "tokens", "access_token": "short", "refresh_token": REFRESH,
                                      "expires_in": "soon"})
        h.tick()
        self.assertIsNone(h.store().load())
        self.assertIn("discord_token_response_invalid", h.events())

    def test_persistence_failure_still_connects(self):
        h = Harness(self)
        h.tick()
        h.world.bridge.events.append({"event": "tokens", "access_token": ACCESS, "refresh_token": REFRESH,
                                      "expires_in": 604800, "scope": "openid"})
        with mock.patch.object(rp.TokenStore, "save", side_effect=OSError(30, "Read-only file system")):
            h.tick(n=2)
        self.assertIn("discord_token_write_failed", h.events())
        self.assertEqual(h.status(), C.DISCORD_STATUS_CONNECTED)

    def test_corrupt_token_file_is_ignored_safely(self):
        h = Harness(self)
        h.store().path.write_text(f"cache:\n  access_token: [{ACCESS}\n", encoding="utf-8")
        h.tick()
        self.assertIn("discord_token_file_invalid", h.events())
        self.assertNotIn(ACCESS, h.logged_text())
        self.assertNotIn("begin_auth", h.world.bridge.ops())
        self.assertEqual(h.status(), C.DISCORD_STATUS_NOT_LINKED)

    def test_unlink_clears_tokens_and_the_running_worker_notices(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=2)
        first = h.world.bridge
        outcome = rp.unlink(h.cfg, bridge_factory=lambda backend: FakeBridge(h.world, backend),
                            backend_selector=lambda d, b: h.world.backend)
        self.assertEqual(outcome, "revoked")
        self.assertIn(("revoke", {"token": REFRESH}), h.world.bridge.requests)
        self.assertIsNone(h.store().load())
        h.tick()
        self.assertTrue(first.stopped)
        self.assertIn("clear_presence", first.ops())


# --------------------------------------------------------------------------- presence publishing and crashes

@unittest.skipUnless(HAS_YAML, "PyYAML is required for the token store")
class PresencePublishingTests(unittest.TestCase):
    def test_source_change_updates_and_duplicates_are_not_resent(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=3)
        bridge = h.world.bridge
        published = [f["presence"] for op, f in bridge.requests if op == "set_presence"]
        self.assertEqual(len(published), 1)
        self.assertEqual((published[0]["state"], published[0]["small_image"]), ("via Plex", "source-plex"))
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_BLUETOOTH, "/bt/play_state": C.PLAY_PLAYING})
        h.tick(n=3)
        h.tick(n=20)
        published = [f["presence"]["state"] for op, f in bridge.requests if op == "set_presence"]
        self.assertEqual(published, ["via Plex", "via Bluetooth"])

    def test_playback_timestamps_do_not_cause_extra_updates(self):
        h = Harness(self, tokens=tokens())
        h.bus.values.update({"/plex/position_sec": 10.0, "/plex/duration_sec": 300.0})
        h.tick(n=3)
        for second in range(60):
            h.wall[0] += 1
            h.bus.values["/plex/position_sec"] = 11.0 + second
            h.tick()
        sent = [f["presence"] for op, f in h.world.bridge.requests if op == "set_presence"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["end_ms"] - sent[0]["start_ms"], 300_000)

    def test_artwork_urls_never_reach_the_bridge_in_any_mode(self):
        for mode in ("off", "audit", "on"):
            with self.subTest(mode=mode):
                h = Harness(self, tokens=tokens(), overrides={"presence": {"artwork_mode": mode,
                                                                            "show_media_metadata": True}})
                h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_MPRIS,
                                     "/hdmi2/selected_source": C.HDMI2_SOURCE_YOUTUBE, "/mpris/available": 1,
                                     "/mpris/play_state": C.PLAY_PLAYING, "/mpris/metadata_seq": 1,
                                     "/mpris/source_utf8": text_bytes("YOUTUBE"),
                                     "/mpris/title_utf8": text_bytes("Video"),
                                     "/mpris/art_url_utf8": text_bytes("https://i.ytimg.com/vi/SECRETID/hq.jpg?sig=tok3n")})
                h.tick(n=4)
                presence_requests = str([f for op, f in h.world.bridge.requests if op == "set_presence"])
                self.assertIn("ember-deck", presence_requests)
                for leak in ("://", "ytimg", "SECRETID", "tok3n"):
                    self.assertNotIn(leak, presence_requests, "a signed URL is rejected even in mode on")
                init = dict(h.world.bridge.requests)["init"]
                self.assertEqual(init["external_artwork"], mode == "on")
                logged = h.logged_text()
                for leak in ("ytimg", "SECRETID", "tok3n", "sig="):
                    self.assertNotIn(leak, logged)
                audits = [m for m in h.messages if m.event == "artwork_audit"]
                self.assertEqual(len(audits), 1 if mode == "audit" else 0)
                if mode == "audit":
                    self.assertIn("classification=authenticated", str(audits[0].metadata))
                if mode == "on":
                    selections = [m for m in h.messages if m.event == "artwork_selection"]
                    self.assertEqual([m.metadata["decision"] for m in selections],
                                     ["source=ytmusic image=static reason=credentials"])

    def test_artwork_audit_failure_cannot_disrupt_presence(self):
        h = Harness(self, tokens=tokens())
        with mock.patch("artwork_audit.inspect_source", side_effect=RuntimeError("https://secret/x?token=t")):
            h.tick(n=3)
        self.assertIn("set_presence", h.world.bridge.ops())
        self.assertNotIn("secret", h.logged_text())

    def test_bridge_crash_restarts_only_the_bridge_and_republishes(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=2)
        h.world.bridge.running = False
        h.tick()
        self.assertEqual(h.status(), C.DISCORD_STATUS_RECONNECTING)
        h.tick(seconds=6.0, n=2)
        self.assertEqual(len(h.world.bridges), 2)
        self.assertIn("set_presence", h.world.bridge.ops())
        self.assertIn("discord_bridge_restarted", h.events())
        self.assertTrue(h.core_untouched())

    def test_repeated_bridge_failures_back_off(self):
        h = Harness(self)
        h.world.responses["hello"] = BridgeError("bridge did not answer in time")
        starts = []
        real_factory = h.worker.bridge_factory
        h.worker.bridge_factory = lambda backend: starts.append(h.now) or real_factory(backend)
        h.tick(n=80)
        self.assertEqual([round(s - starts[0]) for s in starts], [0, 5, 15, 35, 75])
        self.assertTrue(all(b.stopped for b in h.world.bridges))

    def test_reconnect_republishes_presence(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=2)
        bridge = h.world.bridge
        bridge.events.append({"event": "status", "state": "disconnected", "error": 2, "detail": 1006})
        h.tick()
        self.assertEqual(h.status(), C.DISCORD_STATUS_RECONNECTING)
        h.tick(seconds=6.0)  # token re-sent; fake reports ready again
        h.tick(seconds=2.0)
        self.assertEqual(bridge.ops().count("set_presence"), 2)

    def test_rejected_access_token_triggers_refresh(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=2)
        h.world.bridge.events.append({"event": "status", "state": "disconnected", "result": "unauthenticated"})
        h.tick(seconds=6.0)
        self.assertIn("refresh_token", h.world.bridge.ops())

    def test_bridge_start_failure_reports_redacted_stderr_tail(self):
        h = Harness(self, tokens=tokens())
        h.world.backend = BackendChoice(Backend("native", [sys.executable, str(FAKE_BRIDGE), "loader"]), "test")
        h.worker.bridge_factory = BridgeProcess
        with mock.patch.dict(os.environ, {"FAKE_BRIDGE_SECRET": ACCESS}):
            h.tick()
        failures = [m for m in h.messages if m.event == "discord_bridge_failed"]
        self.assertEqual(len(failures), 1)
        text = " ".join(repr(m) for m in h.messages)
        self.assertIn("libpulse.so.0", text)
        self.assertIn("bridge stderr", text)
        for secret in (ACCESS, "Zq81Lm0x", "eyJ2IjoxfQ", "c2lnbmF0dXJl"):
            self.assertNotIn(secret, text)
        self.assertNotIn("libpulse", " ".join(str(v) for v in h.bus.values.values()))

    def test_successful_bridge_reports_no_stderr(self):
        h = Harness(self, tokens=tokens())
        h.world.backend = BackendChoice(Backend("native", [sys.executable, str(FAKE_BRIDGE), "noisy"]), "test")
        h.worker.bridge_factory = BridgeProcess
        h.tick(n=2)
        self.addCleanup(h.worker.shutdown)
        self.assertNotIn("noise line", " ".join(repr(m) for m in h.messages))

    def test_shutdown_clears_presence_and_stops_the_bridge(self):
        h = Harness(self, tokens=tokens())
        h.tick(n=2)
        bridge = h.world.bridge
        h.worker.shutdown()
        self.assertEqual(bridge.ops()[-1], "clear_presence")
        self.assertTrue(bridge.stopped)
        self.assertEqual(h.bus.get_int("/discord/connection_state"), C.DISCORD_CONNECTION_DISCONNECTED)


# --------------------------------------------------------------------------- real child-process IPC

# --------------------------------------------------------------------------- image links to the project page

PROJECT_URL = "https://awoo.ltd/projects/ember-deck"
LINK_FIELDS = ("large_url", "small_url", "large_text")


class ProjectLinkModelTests(unittest.TestCase):
    """Both images open presence.project_url; only the large image's tooltip changes."""

    LINKED = pm.PresencePolicy(show_media_metadata=True, project_url=PROJECT_URL)
    UNLINKED = pm.PresencePolicy(show_media_metadata=True)
    LABELS = {"plex": "Plex", "bluetooth": "Bluetooth", "RADIO": "DAB", "YOUTUBE": "YT Music"}

    def play(self, source, **values):
        self.bus = FakeBus()
        self.bus.values.update({"/media/active_source": source, **values})
        return pm.read_media_snapshot(self.bus)

    def cases(self):
        yield "plex", C.MEDIA_SOURCE_PLEX, {"/plex/play_state": C.PLAY_PLAYING, "/plex/metadata_seq": 1,
                                            "/plex/title_utf8": text_bytes("Teardrop"),
                                            "/plex/artist_utf8": text_bytes("Massive Attack"),
                                            "/plex/album_utf8": text_bytes("Mezzanine"),
                                            "/plex/position_sec": 30.0, "/plex/duration_sec": 330.0}
        yield "bluetooth", C.MEDIA_SOURCE_BLUETOOTH, {"/bt/play_state": C.PLAY_PLAYING, "/bt/metadata_seq": 1,
                                                      "/bt/title_utf8": text_bytes("Phone Song"),
                                                      "/bt/album_utf8": text_bytes("Phone Album")}
        for selected, label in ((C.HDMI2_SOURCE_RADIO, "RADIO"), (C.HDMI2_SOURCE_YOUTUBE, "YOUTUBE")):
            yield label, C.MEDIA_SOURCE_MPRIS, {
                "/hdmi2/selected_source": selected, "/mpris/available": 1, "/mpris/play_state": C.PLAY_PLAYING,
                "/mpris/metadata_seq": 1, "/mpris/source_utf8": text_bytes(label),
                "/mpris/title_utf8": text_bytes("Track"), "/mpris/artist_utf8": text_bytes("Station"),
                "/mpris/album_utf8": text_bytes("Record")}

    def test_both_images_link_to_the_same_configured_page(self):
        policy = pm.PresencePolicy.from_cfg({"project_url": "https://example.org/deck"})
        for name, source, values in self.cases():
            with self.subTest(source=name):
                shown = pm.build_presence(self.play(source, **values), policy)
                self.assertEqual((shown["large_url"], shown["small_url"]),
                                 ("https://example.org/deck", "https://example.org/deck"))
        self.assertEqual(pm.PresencePolicy.from_cfg({"project_url": PROJECT_URL}).project_url, PROJECT_URL)

    def test_large_tooltip_invites_the_click_and_small_tooltip_is_unchanged(self):
        for name, source, values in self.cases():
            with self.subTest(source=name):
                snapshot = self.play(source, **values)
                linked, unlinked = pm.build_presence(snapshot, self.LINKED), pm.build_presence(snapshot, self.UNLINKED)
                self.assertEqual(linked["large_text"], "Explore The Ember Deck")
                self.assertEqual(linked["small_text"], self.LABELS[name])
                self.assertEqual((linked["small_image"], linked["small_text"]),
                                 (unlinked["small_image"], unlinked["small_text"]))
        idle = pm.build_presence(pm.MediaSnapshot(source=None, playback="idle"), self.LINKED)
        self.assertEqual(idle, {"type": "listening", "details": "Idle", "large_image": "ember-deck",
                                "large_text": "Explore The Ember Deck", "large_url": PROJECT_URL})

    def test_music_metadata_and_timing_are_unchanged(self):
        timing = (1_800_000_000_000, 1_800_000_300_000)
        for name, source, values in self.cases():
            with self.subTest(source=name):
                snapshot = self.play(source, **values)
                linked = pm.build_presence(snapshot, self.LINKED, timing)
                unlinked = pm.build_presence(snapshot, self.UNLINKED, timing)
                strip = lambda shown: {k: v for k, v in shown.items() if k not in LINK_FIELDS}  # noqa: E731
                self.assertEqual(strip(linked), strip(unlinked))
                self.assertEqual((linked["start_ms"], linked["end_ms"]), timing)
        _, source, values = next(self.cases())
        shown = pm.build_presence(self.play(source, **values), self.LINKED)
        self.assertEqual((shown["details"], shown["state"], shown["large_text"]),
                         ("Teardrop", "Massive Attack • Mezzanine", "Explore The Ember Deck"))

    def test_dynamic_artwork_keeps_its_image_and_gains_the_link(self):
        policy = pm.PresencePolicy(show_media_metadata=True, artwork_mode="on", project_url=PROJECT_URL)
        shown = pm.build_presence(pm.MediaSnapshot("ytmusic", "playing", "Song", "Artist"), policy, artwork=YT_URL)
        self.assertEqual((shown["large_image"], shown["large_url"], shown["small_url"]),
                         (YT_URL, PROJECT_URL, PROJECT_URL))

    def test_unusable_or_absent_urls_mean_no_links(self):
        for value in (None, "", "   ", 42, True, ["https://awoo.ltd/"], "awoo.ltd/projects/ember-deck",
                      "http://awoo.ltd/projects/ember-deck", "HTTPS://awoo.ltd/x", "javascript:alert(1)",
                      "https://user:pw@awoo.ltd/", "https://awoo/", "https://.ltd/", "https://awoo.ltd:99999/",
                      "https://awoo.ltd/a b", "https://awoo.ltd/é", "https://awoo.ltd/" + "a" * 240):
            with self.subTest(value=value):
                policy = pm.PresencePolicy.from_cfg({"project_url": value, "show_media_metadata": True})
                self.assertIsNone(policy.project_url)
                shown = pm.build_presence(self.play(C.MEDIA_SOURCE_PLEX, **{"/plex/play_state": C.PLAY_PLAYING}),
                                          policy)
                self.assertNotIn("large_url", shown)
                self.assertNotIn("small_url", shown)
                self.assertEqual((shown["large_text"], shown["small_text"]), ("The Ember Deck", "Plex"))
        self.assertEqual(pm.link_url("  https://awoo.ltd/projects/ember-deck \n"), PROJECT_URL)
        self.assertEqual(len(pm.link_url("https://awoo.ltd/" + "a" * 239)), pm.MAX_LINK_CHARS)
        self.assertIsNone(rp.DEFAULTS["presence"]["project_url"], "the URL comes from configuration, not code")
        self.assertIsNone(pm.PresencePolicy.from_cfg({}).project_url, "a config without the key has no links")


class ArtistAlbumLineTests(unittest.TestCase):
    """Second line "Artist • Album"; the third line (large tooltip) stays "Explore The Ember Deck"."""

    LINKED = pm.PresencePolicy(show_media_metadata=True, project_url=PROJECT_URL)

    def show(self, source="plex", title="Not Alone", artist="In Flames", album="Colony (Deluxe Edition)",
             policy=LINKED):
        return pm.build_presence(pm.MediaSnapshot(source, "playing", title, artist, album), policy)

    def assert_unchanged_images(self, shown, label="Plex", icon="source-plex"):
        self.assertEqual((shown["large_image"], shown["large_text"], shown["large_url"]),
                         ("ember-deck", "Explore The Ember Deck", PROJECT_URL))
        self.assertEqual((shown["small_image"], shown["small_text"], shown["small_url"]), (icon, label, PROJECT_URL))

    def test_artist_and_album_share_the_second_line(self):
        shown = self.show()
        self.assertEqual({k: v for k, v in shown.items() if k not in LINK_FIELDS}, {
            "type": "listening", "details": "Not Alone", "state": "In Flames • Colony (Deluxe Edition)",
            "large_image": "ember-deck", "small_image": "source-plex", "small_text": "Plex"})
        self.assert_unchanged_images(shown)
        for source, label in (("bluetooth", "Bluetooth"), ("ytmusic", "YT Music")):
            with self.subTest(source=source):
                shown = self.show(source)
                self.assertEqual(shown["state"], "In Flames • Colony (Deluxe Edition)")
                self.assert_unchanged_images(shown, label, f"source-{source}")

    def test_artist_only_and_album_only(self):
        self.assertEqual(self.show(album=None)["state"], "In Flames")
        self.assertEqual(self.show(artist=None)["state"], "Colony (Deluxe Edition)")
        self.assertEqual(self.show(artist="N/A", album="Unknown Album")["state"], "via Plex", "placeholders are absent")
        for shown in (self.show(album=None), self.show(artist=None)):
            self.assert_unchanged_images(shown)

    def test_missing_metadata_keeps_the_existing_fallback(self):
        shown = self.show(title=None, artist=None, album=None)
        self.assertEqual((shown["details"], shown["state"]), ("Playing music", "via Plex"))
        self.assert_unchanged_images(shown)
        hidden = self.show(title=None, artist=None, album=None,
                           policy=pm.PresencePolicy(show_media_metadata=True, show_source=False, project_url=PROJECT_URL))
        self.assertNotIn("state", hidden)
        private = self.show(policy=pm.PresencePolicy(project_url=PROJECT_URL))
        self.assertEqual((private["details"], private["state"]), ("Playing music", "via Plex"))
        self.assertNotIn("Colony", str(private), "albums still need show_media_metadata")

    def test_duplicates_are_not_repeated(self):
        self.assertEqual(self.show(artist="Weezer", album="Weezer")["state"], "Weezer")
        self.assertEqual(self.show(artist="Weezer", album="WEEZER")["state"], "Weezer")
        self.assertEqual(self.show(title="Not Alone", album="not alone")["state"], "In Flames",
                         "a single's album repeats the title on the first line")
        self.assertEqual(self.show(title="Not Alone", artist=None, album="Not Alone")["state"], "via Plex")

    def test_long_metadata_is_shortened_keeping_the_artist(self):
        artist, album = "A" * 60, "B" * 200
        state = self.show(artist=artist, album=album)["state"]
        self.assertEqual(len(state), pm.MAX_FIELD_CHARS)
        self.assertTrue(state.startswith(artist + " • B"))
        self.assertTrue(state.endswith("B…"))
        self.assertEqual(self.show(artist="A" * 121, album=album)["state"], "A" * 121 + " • BBB…")
        self.assertEqual(self.show(artist="A" * 122, album=album)["state"], "A" * 122, "too little album left")
        self.assertEqual(self.show(artist="A" * 300, album=album)["state"], "A" * 127 + "…")
        cjk = self.show(artist="日本" * 40, album="音楽" * 40)["state"]
        self.assertLessEqual(len(cjk), pm.MAX_FIELD_CHARS)
        self.assertTrue(cjk.startswith("日本" * 40 + " • 音"))
        shown = self.show(title="T" * 300, artist="A" * 300, album="B" * 300)
        for key in ("details", "state", "large_text", "small_text"):
            self.assertLessEqual(len(shown[key]), pm.MAX_FIELD_CHARS, key)
            self.assertGreaterEqual(len(shown[key]), pm.MIN_FIELD_CHARS, key)
        self.assert_unchanged_images(shown)

    def test_dab_station_line_is_unchanged(self):
        shown = self.show("dab", title="Now: Song", artist="BBC Radio 6 Music", album="Afternoon Show")
        self.assertEqual((shown["details"], shown["state"]), ("Now: Song", "BBC Radio 6 Music"),
                         "DAB album text is programme info, not an album")
        self.assert_unchanged_images(shown, "DAB", "source-dab")

    def test_only_the_presentation_changes(self):
        snapshot = pm.MediaSnapshot("plex", "playing", "Not Alone", "In Flames", "Colony (Deluxe Edition)", 50.0, 244.0)
        timing = (1_800_000_000_000, 1_800_000_244_000)
        shown = pm.build_presence(snapshot, self.LINKED, timing)
        self.assertEqual((snapshot.artist, snapshot.album), ("In Flames", "Colony (Deluxe Edition)"))
        self.assertEqual((shown["start_ms"], shown["end_ms"]), timing)
        artwork = pm.PresencePolicy(show_media_metadata=True, artwork_mode="on", project_url=PROJECT_URL)
        shown = pm.build_presence(snapshot, artwork, artwork=HOSTED_A)
        self.assertEqual((shown["large_image"], shown["state"]), (HOSTED_A, "In Flames • Colony (Deluxe Edition)"))
        self.assert_unchanged_images({**shown, "large_image": "ember-deck"})

    def test_tracks_and_sources_update_the_line(self):
        sequence = [
            (pm.MediaSnapshot("plex", "playing", "Not Alone", "In Flames", "Colony (Deluxe Edition)"),
             "In Flames • Colony (Deluxe Edition)"),
            (pm.MediaSnapshot("plex", "playing", "Teardrop", "Massive Attack", "Mezzanine"), "Massive Attack • Mezzanine"),
            (pm.MediaSnapshot("bluetooth", "playing", "Phone Song", "Phone Artist"), "Phone Artist"),
            (pm.MediaSnapshot("ytmusic", "playing", "Video", None, "Some Album"), "Some Album"),
            (pm.MediaSnapshot("dab", "playing", "Programme", "Station FM", "Show"), "Station FM"),
        ]
        for snapshot, state in sequence:
            with self.subTest(source=snapshot.source, title=snapshot.title):
                shown = pm.build_presence(snapshot, self.LINKED)
                self.assertEqual((shown["details"], shown["state"], shown["large_text"]),
                                 (snapshot.title, state, "Explore The Ember Deck"))


@unittest.skipUnless(HAS_YAML, "PyYAML is required for the token store")
class ProjectLinkWorkerTests(unittest.TestCase):
    def test_artist_album_line_follows_tracks_and_sources(self):
        h = Harness(self, tokens=tokens(), overrides={"presence": {"show_media_metadata": True,
                                                                   "project_url": PROJECT_URL}})
        h.bus.values.update({"/plex/metadata_seq": 1, "/plex/title_utf8": text_bytes("Not Alone"),
                             "/plex/artist_utf8": text_bytes("In Flames"),
                             "/plex/album_utf8": text_bytes("Colony (Deluxe Edition)"),
                             "/plex/position_sec": 50.0, "/plex/duration_sec": 244.0})
        h.tick(n=3)
        h.bus.values.update({"/plex/metadata_seq": 2, "/plex/title_utf8": text_bytes("Teardrop"),
                             "/plex/artist_utf8": text_bytes("Massive Attack"),
                             "/plex/album_utf8": text_bytes("Mezzanine")})
        h.tick(n=3)
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_BLUETOOTH, "/bt/play_state": C.PLAY_PLAYING,
                             "/bt/metadata_seq": 1, "/bt/title_utf8": text_bytes("Phone Song"),
                             "/bt/album_utf8": text_bytes("Phone Album")})
        h.tick(n=3)
        sent = self.sent(h)
        self.assertEqual([(p["details"], p["state"]) for p in sent],
                         [("Not Alone", "In Flames • Colony (Deluxe Edition)"),
                          ("Teardrop", "Massive Attack • Mezzanine"), ("Phone Song", "Phone Album")])
        for payload in sent:
            self.assertEqual((payload["large_text"], payload["large_url"], payload["small_url"]),
                             ("Explore The Ember Deck", PROJECT_URL, PROJECT_URL))
        self.assertEqual(sent[0]["end_ms"] - sent[0]["start_ms"], 244_000)

    def sent(self, h):
        return [f["presence"] for op, f in h.world.bridge.requests if op == "set_presence"]

    def test_links_survive_track_changes_source_changes_reconnect_and_pause(self):
        h = Harness(self, tokens=tokens(), overrides={"presence": {"show_media_metadata": True,
                                                                   "project_url": PROJECT_URL}})
        h.bus.values.update({"/plex/metadata_seq": 1, "/plex/title_utf8": text_bytes("One"),
                             "/plex/position_sec": 1.0, "/plex/duration_sec": 200.0})
        h.tick(n=3)
        for seq, title in ((2, "Two"), (3, "Three")):
            h.bus.values.update({"/plex/metadata_seq": seq, "/plex/title_utf8": text_bytes(title)})
            h.tick(n=3)
        h.bus.values.update({"/media/active_source": C.MEDIA_SOURCE_BLUETOOTH, "/bt/play_state": C.PLAY_PLAYING})
        h.tick(n=3)
        h.world.bridge.events.append({"event": "status", "state": "disconnected", "error": 2, "detail": 1006})
        h.tick()
        h.tick(seconds=6.0)
        h.tick(seconds=2.0)
        h.bus.values["/bt/play_state"] = C.PLAY_PAUSED
        h.tick(n=3)
        sent = self.sent(h)
        self.assertEqual([p["details"] for p in sent],
                         ["One", "Two", "Three", "Playing music", "Playing music", "Idle"])
        for payload in sent:
            self.assertEqual((payload["large_url"], payload["large_text"]), (PROJECT_URL, "Explore The Ember Deck"))
            if "small_image" in payload:
                self.assertEqual(payload["small_url"], PROJECT_URL)
        self.assertEqual(sum("small_url" in p for p in sent), 5)

    def test_configured_url_is_used_for_both_images(self):
        h = Harness(self, tokens=tokens(), overrides={"presence": {"project_url": "https://example.org/my-deck"}})
        h.tick(n=3)
        payload = self.sent(h)[-1]
        self.assertEqual((payload["large_url"], payload["small_url"]),
                         ("https://example.org/my-deck", "https://example.org/my-deck"))
        self.assertNotIn("presence_links_disabled", h.events())

    def test_invalid_or_absent_url_never_interrupts_presence(self):
        for value, warned in ((None, False), ("", False), ("http://awoo.ltd/x", True),
                              ("https://user:hunter2@example.org/", True), (17, True)):
            with self.subTest(value=value):
                h = Harness(self, tokens=tokens(), overrides={"presence": {"project_url": value}})
                h.tick(n=3)
                payload = self.sent(h)[-1]
                self.assertEqual((payload["state"], payload["small_image"], payload["small_text"]),
                                 ("via Plex", "source-plex", "Plex"))
                self.assertEqual(payload["large_text"], "The Ember Deck")
                self.assertNotIn("large_url", payload)
                self.assertNotIn("small_url", payload)
                self.assertEqual("presence_links_disabled" in h.events(), warned)
                self.assertNotIn("hunter2", h.logged_text())
                self.assertEqual(h.status(), C.DISCORD_STATUS_CONNECTED)


class BridgeProcessTests(unittest.TestCase):
    def bridge(self, mode="normal"):
        process = BridgeProcess(Backend("native", [sys.executable, str(FAKE_BRIDGE), mode]))
        self.addCleanup(process.stop, 0.5)
        return process

    def test_handshake_ids_and_status_events(self):
        b = self.bridge()
        hello = b.request("hello", 5.0, protocol=1)
        self.assertEqual((hello["result"], hello["protocol"], hello["id"]), ("success", 1, 1))
        second = b.request("init", 5.0, application_id=APP_ID)
        self.assertEqual((second["id"], second["echo_op"]), (2, "init"))
        b.request("set_token", 5.0, access_token=ACCESS)
        deadline = __import__("time").monotonic() + 5
        events = []
        while not events and __import__("time").monotonic() < deadline:
            events = b.poll_events()
        self.assertEqual(events[0]["state"], "ready")

    def test_malformed_lines_are_counted_and_ignored(self):
        b = self.bridge("garbage")
        self.assertEqual(b.request("hello", 5.0)["result"], "success")
        self.assertGreaterEqual(b.malformed_lines, 3)

    def test_timeout(self):
        b = self.bridge("silent")
        with self.assertRaisesRegex(BridgeError, "did not answer"):
            b.request("hello", 0.3)

    def test_unexpected_exit(self):
        b = self.bridge("exit")
        b.request("hello", 5.0)
        b.process.wait(5)
        self.assertFalse(b.alive())
        with self.assertRaises(BridgeError):
            b.request("init", 1.0)

    def test_clean_shutdown_and_forced_stop(self):
        b = self.bridge()
        b.request("hello", 5.0)
        b.stop(2.0)
        self.assertIsNotNone(b.process.poll())
        stubborn = self.bridge("stubborn")
        stubborn.request("hello", 5.0)
        stubborn.stop(0.5)
        self.assertIsNotNone(stubborn.process.poll(), "a bridge ignoring shutdown is still killed")

    def test_stderr_tail_is_bounded_and_kept_after_exit(self):
        b = self.bridge("noisy")
        b.request("hello", 5.0)
        deadline = __import__("time").monotonic() + 5
        while "last line" not in b.stderr_tail() and __import__("time").monotonic() < deadline:
            pass
        tail = b.stderr_tail()
        self.assertTrue(tail.endswith("last line"))
        self.assertLessEqual(len(tail.encode()), discord_ipc.STDERR_TAIL_BYTES)
        self.assertFalse(tail.startswith("x"), "the partial first line is dropped")
        loader = self.bridge("loader")
        loader.process.wait(5)
        self.assertIn("libpulse.so.0", loader.stderr_tail())

    def test_diagnostic_redaction(self):
        r = discord_ipc.redact_diagnostics
        self.assertEqual(r("Bearer abc"), "Bearer [REDACTED]")
        self.assertNotIn("xyz", r('{"access_token": "xyz", "refresh_token": "xyz"}'))
        self.assertNotIn("s3cr3t", r("client_secret=s3cr3t"))
        self.assertNotIn("payload", r("licence AWOO1.awoo-2026-01.payload.sig"))
        self.assertEqual(r("tok Ab3dEf5gHi7jKl9mNo1pQr3sTu5v"), "tok [REDACTED]")
        loader = "libdiscord_partner_sdk.so: wrong ELF class: ELFCLASS64 (libpulse.so.0, GLIBC_2.29)"
        self.assertEqual(r(loader), loader)
        self.assertEqual(r("a\x1b[31mb\x00c"), "a[31mbc")

    def test_ipc_writes_nothing_to_our_stdout(self):
        output = io.StringIO()
        with redirect_stdout(output):
            b = self.bridge()
            b.request("set_token", 5.0, access_token=ACCESS)
            b.stop(2.0)
        self.assertNotIn(ACCESS, output.getvalue())


# --------------------------------------------------------------------------- backend selection

def elf(machine: int) -> bytes:
    return b"\x7fELF" + bytes([2, 1, 1]) + bytes(11) + struct.pack("<H", machine) + bytes(44)


class BackendSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.native = self.tmp / "discord_bridge"
        self.x86 = self.tmp / "discord_bridge_x86_64"

    def write(self, path, machine):
        path.write_bytes(elf(machine))
        path.chmod(0o755)

    def discord(self, **overrides):
        cfg = {"backend": "auto", "native_bridge": str(self.native), "sdk_library_dir": None,
               "box64": {"executable": "box64", "bridge": str(self.x86)}}
        cfg.update(overrides)
        return cfg

    def test_elf_machine_detection(self):
        self.write(self.native, 183)
        self.assertEqual(discord_ipc.elf_machine(self.native), "aarch64")
        self.write(self.x86, 62)
        self.assertEqual(discord_ipc.elf_machine(self.x86), "x86_64")
        script = self.tmp / "script.sh"
        script.write_text("#!/bin/sh\n")
        self.assertIsNone(discord_ipc.elf_machine(script))

    def test_auto_prefers_a_matching_native_bridge(self):
        self.write(self.native, 183)
        self.write(self.x86, 62)
        choice = discord_ipc.select_backend(self.discord(), self.tmp, host="aarch64", which=lambda n: "/usr/bin/box64")
        self.assertEqual(choice.backend.kind, "native")

    def test_box64_backend_disables_io_uring_and_native_does_not(self):
        # Pi 5 + Box64 + SDK 1.10.19337: client init busy-looped on io_uring
        # until UV_USE_IO_URING=0 was set for the bridge.
        self.write(self.native, 183)
        self.write(self.x86, 62)
        before = os.environ.get("UV_USE_IO_URING")
        box = discord_ipc.select_backend(self.discord(backend="box64"), self.tmp, host="aarch64",
                                         which=lambda n: "/usr/bin/box64")
        self.assertEqual(box.backend.env["UV_USE_IO_URING"], "0")
        self.assertEqual(box.backend.env["BOX64_NOBANNER"], "1")
        self.assertEqual(box.backend.env["BOX64_LOG"], "0")
        native = discord_ipc.select_backend(self.discord(sdk_library_dir="/opt/sdk/lib"), self.tmp, host="aarch64",
                                            which=lambda n: "/usr/bin/box64")
        self.assertEqual(native.backend.kind, "native")
        self.assertNotIn("UV_USE_IO_URING", native.backend.env)
        self.assertEqual(os.environ.get("UV_USE_IO_URING"), before, "never set for Ember Deck as a whole")
        uses = [path.name for path in ENGINE.glob("*.py") if "UV_USE_IO_URING" in path.read_text(encoding="utf-8")]
        self.assertEqual(uses, ["discord_ipc.py"])

    def test_auto_falls_back_to_box64_for_an_x86_64_sdk(self):
        self.write(self.x86, 62)
        choice = discord_ipc.select_backend(self.discord(sdk_library_dir="/opt/sdk/lib"), self.tmp, host="aarch64",
                                            which=lambda n: "/usr/bin/box64")
        self.assertEqual(choice.backend.kind, "box64")
        self.assertEqual(choice.backend.argv, ["/usr/bin/box64", str(self.x86)])
        self.assertEqual(choice.backend.env["BOX64_LD_LIBRARY_PATH"],
                         str(discord_ipc._expand("/opt/sdk/lib", self.tmp)))

    def test_wrong_architecture_and_missing_box64_are_unavailable(self):
        self.write(self.native, 62)  # an x86_64 build configured as "native" on an ARM64 host
        self.write(self.x86, 62)
        choice = discord_ipc.select_backend(self.discord(), self.tmp, host="aarch64", which=lambda n: None)
        self.assertIsNone(choice.backend)
        self.assertIn("host is aarch64", choice.reason)
        self.assertIn("Box64 is not installed", choice.reason)
        choice = discord_ipc.select_backend(self.discord(backend="box64", box64={"executable": "box64",
                                                                                 "bridge": str(self.native)}),
                                            self.tmp, host="aarch64", which=lambda n: "/usr/bin/box64")
        self.assertEqual(choice.backend.kind, "box64")  # explicit box64 mode takes the x86_64 bridge

    def test_nothing_built_is_unavailable_not_an_error(self):
        choice = discord_ipc.select_backend(self.discord(), self.tmp, host="aarch64", which=lambda n: None)
        self.assertIsNone(choice.backend)


# --------------------------------------------------------------------------- Ember UI status area

class UiStatusTests(unittest.TestCase):
    def setUp(self):
        from _support import import_engine_module
        self.ui = import_engine_module("ember_ui")
        self.bus = FakeBus()
        patcher = mock.patch.object(self.ui, "bus", self.bus)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_is_shown_before_the_worker_publishes(self):
        self.assertEqual(self.ui.discord_status_lines(), [])

    def test_states_and_pending_authorisation(self):
        self.bus.values["/discord/status"] = C.DISCORD_STATUS_DISABLED
        self.assertEqual(self.ui.discord_status_lines(), ["RP    : Disabled"])
        self.bus.values["/discord/status"] = C.DISCORD_STATUS_AUTHORIZING
        self.assertEqual(self.ui.discord_status_lines(), ["RP    : Authorising Discord"])
        self.bus.values["/discord/status"] = C.DISCORD_STATUS_ERROR
        self.assertEqual(self.ui.discord_status_lines(), ["RP    : Error"])
        self.bus.values["/discord/status"] = C.DISCORD_STATUS_CONNECTED
        self.assertEqual(self.ui.discord_status_lines(), ["RP    : Connected"])


# --------------------------------------------------------------------------- configuration and Foundry

class ConfigurationAndFoundryTests(unittest.TestCase):
    def test_defaults_and_shipped_yaml_validate(self):
        runtime_config.validate_rich_presence(rp.DEFAULTS)
        if HAS_YAML:
            loaded = runtime_config.load_yaml_mapping(ENGINE / "configs" / "rich_presence.yaml", "rich_presence")
            runtime_config.validate_rich_presence(rp._merge(rp.DEFAULTS, loaded))
            # The operator may set their own (non-secret) application ID; the
            # code itself never invents one.
            self.assertIsNone(rp.DEFAULTS["discord"]["application_id"], "no application ID is invented")
            self.assertIn(str(loaded["discord"]["application_id"]) if loaded["discord"]["application_id"] else None,
                          (None, rp.application_id(rp._merge(rp.DEFAULTS, loaded))))
            # The working YAML may be customised (hardware testing); the code default stays private.
            self.assertFalse(rp.DEFAULTS["presence"]["show_media_metadata"], "metadata is private by default")

    def test_invalid_options_are_rejected(self):
        cases = [
            ({"surprise": 1}, "unknown option"),
            ({"discord": {"backend": "wine"}}, "backend must be one of"),
            ({"discord": {"application_id": "my-app"}}, "application_id"),
            ({"discord": {"client_secret": "x"}}, "unknown option"),
            ({"discord": {"box64": {"path": "x"}}}, "unknown option"),
            ({"presence": {"activity_type": "streaming"}}, "activity_type"),
            ({"presence": {"show_media_metadata": "yes"}}, "true or false"),
            ({"enabled": "true"}, "true or false"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                cfg = rp._merge(rp.DEFAULTS, overrides)
                with self.assertRaisesRegex(runtime_config.ConfigError, message):
                    runtime_config.validate_rich_presence(cfg)
        cfg = rp._merge(rp.DEFAULTS, {"discord": {"application_id": 123456789012345678}})
        runtime_config.validate_rich_presence(cfg)  # a numeric YAML value is fine

    @unittest.skipUnless(HAS_YAML, "PyYAML is required")
    def test_unquoted_yaml_on_and_off_are_understood(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for raw, expected in (("on", "on"), ("off", "off"), ('"on"', "on"), ("audit", "audit")):
            with self.subTest(raw=raw):
                path = tmp / "rich_presence.yaml"
                path.write_text("proc_name: rich_presence" + chr(10) + "presence:" + chr(10)
                                + "  artwork_mode: " + raw + chr(10), encoding="utf-8")
                with mock.patch.dict(os.environ, {"CONFIG_PATH": str(path)}):
                    self.assertEqual(rp.load_cfg()["presence"]["artwork_mode"], expected)
        self.assertEqual(rp.DEFAULTS["presence"]["artwork_mode"], "audit", "defaults are never mutated")

    def test_rich_presence_is_the_last_worker_and_worker_scoped(self):
        import FoundryCore
        last = FoundryCore.WORKERS[-1]
        self.assertEqual(last["name"], "rich_presence")
        self.assertEqual(FoundryCore.restart_scope(last), FoundryCore.RESTART_SCOPE_WORKER)
        self.assertTrue((ENGINE / last["script"]).is_file())
        self.assertTrue((ENGINE / "configs" / last["config"]).is_file())
        others = [w for w in FoundryCore.WORKERS[:-1] if w["name"] != "artwork_service"]
        self.assertTrue(all(FoundryCore.restart_scope(w) == FoundryCore.RESTART_SCOPE_GROUP for w in others))

    def test_worker_imports_no_hardware_or_sdk_module(self):
        source = (ENGINE / "rich_presence.py").read_text(encoding="utf-8")
        for module in ("gpiozero", "board", "busio", "sounddevice", "blinkstick", "plexapi", "ctypes.CDLL(\"libdiscord"):
            self.assertNotIn(f"import {module}", source)

    def test_secret_and_sdk_paths_are_gitignored(self):
        if shutil.which("git") is None or not (ENGINE.parent / ".git").exists():
            self.skipTest("git checkout required")
        import subprocess
        for path in ("EmberEngine/configs/discord_secrets.yaml", "EmberEngine/third_party/discord_social_sdk/lib/x.so",
                     "EmberEngine/discord_bridge/build/discord_bridge"):
            with self.subTest(path=path):
                self.assertEqual(subprocess.run(["git", "check-ignore", "-q", path], cwd=ENGINE.parent).returncode, 0)


if __name__ == "__main__":
    unittest.main()
