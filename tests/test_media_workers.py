"""Media-source worker contracts without Plex, D-Bus, Flatpak or a desktop.

Minstrel, the MPRIS bridge and the HDMI-2 controller all talk to external
services. These tests replace those services with small fakes and check the
Synapse contracts other workers rely on, especially when a service is down.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import FakeBus, capture_whisper, has_module, import_engine_module  # noqa: E402

minstrel = import_engine_module("minstrel")
mpris_bridge = import_engine_module("mpris_bridge")
hdmi2 = import_engine_module("hdmi2_source_controller")


def use_bus(test: unittest.TestCase, *modules) -> FakeBus:
    bus = FakeBus()
    for module in modules:
        patcher = mock.patch.object(module, "bus", bus)
        patcher.start()
        test.addCleanup(patcher.stop)
    whisper = capture_whisper()
    test.messages = whisper.__enter__()
    test.addCleanup(whisper.__exit__, None, None, None)
    return bus


# --------------------------------------------------------------------------- Minstrel


class PlaybackClockTests(unittest.TestCase):
    def setUp(self):
        self.clock = minstrel.PlaybackClock(seek_threshold_s=18.0, fresh_nudge_max_s=1.25, fresh_deadband_s=0.35)

    def test_first_observation_anchors_and_playing_advances(self):
        self.assertEqual(self.clock.observe(10.0, minstrel.PLAY_PLAYING, now=100.0), "reset")
        self.assertAlmostEqual(self.clock.position(103.0), 13.0)

    def test_repeated_stale_offset_does_not_drag_the_clock_back(self):
        self.clock.observe(10.0, minstrel.PLAY_PLAYING, now=100.0)
        self.assertEqual(self.clock.observe(10.0, minstrel.PLAY_PLAYING, now=104.0), "steady")
        self.assertAlmostEqual(self.clock.position(104.0), 14.0)

    def test_large_fresh_jump_is_a_seek_and_small_error_is_nudged(self):
        self.clock.observe(10.0, minstrel.PLAY_PLAYING, now=100.0)
        self.assertEqual(self.clock.observe(60.0, minstrel.PLAY_PLAYING, now=101.0), "seek")
        self.assertAlmostEqual(self.clock.position(101.0), 60.0)
        self.assertEqual(self.clock.observe(65.0, minstrel.PLAY_PLAYING, now=103.0), "nudge")
        self.assertAlmostEqual(self.clock.position(103.0), 62.0 + 1.25)

    def test_pause_freezes_and_stop_resets(self):
        self.clock.observe(10.0, minstrel.PLAY_PLAYING, now=100.0)
        self.assertEqual(self.clock.observe(10.0, minstrel.PLAY_PAUSED, now=102.0), "pause")
        self.assertAlmostEqual(self.clock.position(150.0), 12.0)
        self.assertEqual(self.clock.observe(0.0, minstrel.PLAY_STOPPED, now=151.0), "stopped")
        self.assertEqual(self.clock.position(160.0), 0.0)


class LyricsTests(unittest.TestCase):
    def test_lrc_parsing_expands_multi_stamps_and_sorts(self):
        entries = minstrel.parse_lrc_text("[00:05.00][00:01.50]chorus\n[00:03]verse\nno stamp\n[00:09.00]")
        self.assertEqual(entries, [(1500, "chorus"), (3000, "verse"), (5000, "chorus")])

    def test_lyric_payload_decoding_prefers_utf8_then_japanese_legacy(self):
        self.assertEqual(minstrel._decode_lyric_payload("夜".encode("utf-8")), "夜")
        self.assertEqual(minstrel._decode_lyric_payload("夜".encode("cp932")), "夜")
        self.assertEqual(minstrel._decode_lyric_payload(b""), "")

    def test_timed_lyrics_publish_coherent_context_once_per_line(self):
        bus = use_bus(self, minstrel)
        state = {"lyrics_lrc": [(1000, "one"), (2000, "two"), (3000, "three")]}
        minstrel.update_timed_lyrics_line(2.5, state)
        self.assertEqual(
            (bus.text("/lyrics/previous_utf8_v2"), bus.text("/lyrics/current_utf8_v2"), bus.text("/lyrics/next_utf8_v2")),
            ("one", "two", "three"),
        )
        self.assertEqual(bus.text("/lyrics/current_utf8"), "two")
        seq = bus.get_int("/lyrics/seq")
        minstrel.update_timed_lyrics_line(2.6, state)
        self.assertEqual(bus.get_int("/lyrics/seq"), seq)


class PlexControlTests(unittest.TestCase):
    def setUp(self):
        self.bus = use_bus(self, minstrel)
        self.cfg = {"plex_client_name": "Ember Deck"}

    def server_with(self, client):
        server = mock.Mock()
        server.clients.return_value = [client]
        return server

    def deck_client(self, title="Ember Deck"):
        client = mock.Mock()
        client.title = title
        client.machineIdentifier = "abc"
        return client

    def test_counter_delta_is_bounded_and_wraps(self):
        self.assertEqual(minstrel._counter_delta(5, 5), 0)
        self.assertEqual(minstrel._counter_delta(5, 7), 2)
        self.assertEqual(minstrel._counter_delta(0x7FFFFFFE, 0x7FFFFFFF), 1)
        # A stale or reset counter is one request, not a replay of old presses.
        self.assertEqual(minstrel._counter_delta(5, 500), 1)
        self.assertEqual(minstrel._counter_delta(500, 5), 1)

    def test_play_key_toggles_from_current_plex_state(self):
        client = self.deck_client()
        self.bus.values["/plex/play_state"] = minstrel.PLAY_PLAYING
        self.bus.values["/plex/control/playpause_seq"] = 1
        seen = {"playpause": 0, "stop": 0, "next": 0, "previous": 0}
        minstrel.consume_plex_control_requests(self.server_with(client), self.cfg, seen, "minstrel")
        client.pause.assert_called_once_with(mtype="music")
        self.assertEqual(self.bus.get_int("/plex/control/last_ok"), 1)

    def test_commands_only_target_the_decks_own_client(self):
        phone = self.deck_client(title="Phone")
        self.bus.values["/plex/control/next_seq"] = 1
        seen = {"playpause": 0, "stop": 0, "next": 0, "previous": 0}
        with redirect_stdout(io.StringIO()):
            minstrel.consume_plex_control_requests(self.server_with(phone), self.cfg, seen, "minstrel")
        phone.skipNext.assert_not_called()
        self.assertEqual(self.bus.get_int("/plex/control/last_ok"), 0)

    def test_failed_command_is_consumed_not_replayed_later(self):
        client = self.deck_client()
        client.skipNext.side_effect = ConnectionError("server unreachable")
        self.bus.values["/plex/control/next_seq"] = 1
        seen = {"playpause": 0, "stop": 0, "next": 0, "previous": 0}
        server = self.server_with(client)
        minstrel.consume_plex_control_requests(server, self.cfg, seen, "minstrel")
        self.assertEqual(self.bus.get_int("/plex/control/last_ok"), 0)
        self.assertEqual([m.event for m in self.messages], ["plex_control_command_failed"])
        client.skipNext.side_effect = None
        minstrel.consume_plex_control_requests(server, self.cfg, seen, "minstrel")
        self.assertEqual(client.skipNext.call_count, 1)

    def test_session_lookup_ignores_other_household_players(self):
        phone = mock.Mock(player=mock.Mock(title="Phone"))
        deck = mock.Mock(player=mock.Mock(title="Ember Deck"))
        server = mock.Mock()
        server.sessions.return_value = [phone, deck]
        self.assertIs(minstrel.find_plexamp_session(server, "Ember Deck"), deck)
        server.sessions.return_value = [phone]
        self.assertIsNone(minstrel.find_plexamp_session(server, "Ember Deck"))

    def test_session_query_failures_propagate_to_trigger_reconnect(self):
        server = mock.Mock()
        server.sessions.side_effect = RuntimeError("(401) unauthorized")
        with self.assertRaises(RuntimeError):
            minstrel.find_plexamp_session(server, "Ember Deck")


class FakePosterServer:
    """Answers /library/metadata/<album>/posters like Plex, counting requests."""

    _baseurl = "https://192-168-1-20.0123abcd.plex.direct:32400"

    def __init__(self, photos=(), error=None):
        self.photos = list(photos)
        self.error = error
        self.queries = []

    def query(self, key, timeout=None):
        import xml.etree.ElementTree as ET
        self.queries.append(key)
        if self.error is not None:
            raise self.error
        root = ET.Element("MediaContainer")
        for attrs in self.photos:
            ET.SubElement(root, "Photo", attrs)
        return root


class Item:
    def __init__(self, parent_rating_key="555"):
        self.parentRatingKey = parent_rating_key


AGENT_POSTER = "https://metadata-static.plex.tv/a/people/abcdef0123456789.jpg"


class MinstrelPublicArtworkTests(unittest.TestCase):
    CFG = {"public_artwork_lookup": True, "album_art_timeout_s": 2.0}

    def setUp(self):
        self.bus = use_bus(self, minstrel)

    def published(self):
        raw = self.bus.values.get(minstrel.KEY_PLEX_ART_PUBLIC_URL_UTF8)
        if raw is None:
            return None
        text = bytes(int(b) for b in raw).split(b"\x00", 1)[0].decode()
        return "" if text == "N/A" else text  # Minstrel's convention for "no value"

    def run_lookup(self, server, cfg=None, state=None, detail=None):
        state = {} if state is None else state
        minstrel.publish_public_album_art(server, detail or Item(), None, dict(self.CFG if cfg is None else cfg),
                                          state)
        return state

    def test_selected_agent_poster_with_a_public_url_is_published(self):
        server = FakePosterServer([
            {"key": "/library/metadata/555/file?url=upload%3A%2F%2Fposters%2Fx", "ratingKey": "upload://posters/x"},
            {"key": AGENT_POSTER, "ratingKey": AGENT_POSTER, "selected": "1",
             "provider": "tv.plex.agents.music"},
        ])
        seq = self.bus.get_int("/plex/metadata_seq", 0)
        self.run_lookup(server)
        self.assertEqual(self.published(), AGENT_POSTER)
        self.assertEqual(server.queries, ["/library/metadata/555/posters"])
        self.assertGreater(self.bus.get_int("/plex/metadata_seq", 0), seq, "read inside the metadata window")

    def test_server_stored_or_unsafe_selections_publish_nothing(self):
        cases = [
            [{"key": "/library/metadata/555/thumb/1700000000", "ratingKey": "metadata://posters/abc", "selected": "1"}],
            [{"key": "/library/metadata/555/file?url=upload%3A%2F%2Fx", "ratingKey": "upload://posters/x",
              "selected": "1"}],
            [{"key": "/library/metadata/555/file?url=media%3A%2F%2Fx", "ratingKey": "media://x", "selected": "1"}],
            [{"key": AGENT_POSTER, "ratingKey": AGENT_POSTER}],                     # available but not selected
            [{"key": "https://plex.example.org/photo?X-Plex-Token=tok3n", "ratingKey": "x", "selected": "1"}],
            [{"key": "http://coverartarchive.example.org/a.jpg", "ratingKey": "x", "selected": "1"}],
            [{"key": "https://192.168.1.20:32400/library/metadata/555/thumb", "ratingKey": "x", "selected": "1"}],
            [{"key": "https://192-168-1-20.0123abcd.plex.direct/photo/a.jpg", "ratingKey": "x", "selected": "1"}],
            [],
        ]
        for photos in cases:
            with self.subTest(photos=photos):
                self.bus.values.pop(minstrel.KEY_PLEX_ART_PUBLIC_URL_UTF8, None)
                self.run_lookup(FakePosterServer(photos))
                self.assertEqual(self.published(), "")

    def test_the_servers_own_public_host_is_never_published(self):
        server = FakePosterServer([{"key": "https://plex.example.org/library/metadata/1/thumb/2",
                                    "ratingKey": "https://plex.example.org/library/metadata/1/thumb/2",
                                    "selected": "1"}])
        server._baseurl = "https://plex.example.org"
        self.run_lookup(server)
        self.assertEqual(self.published(), "")

    def test_disabled_by_default_and_never_queries(self):
        server = FakePosterServer([{"key": AGENT_POSTER, "ratingKey": AGENT_POSTER, "selected": "1"}])
        self.run_lookup(server, cfg={})
        self.assertEqual(server.queries, [])
        self.assertEqual(self.published(), "", "a stale URL from an earlier run is cleared")
        self.assertFalse(minstrel.DEFAULTS["public_artwork_lookup"])

    def test_cached_per_album_and_new_album_replaces_the_url(self):
        server = FakePosterServer([{"key": AGENT_POSTER, "ratingKey": AGENT_POSTER, "selected": "1"}])
        state = self.run_lookup(server)
        self.run_lookup(server, state=state)
        self.assertEqual(len(server.queries), 1, "same album: no second request")
        server.photos = [{"key": "/library/metadata/777/thumb/1", "ratingKey": "metadata://posters/x",
                          "selected": "1"}]
        self.run_lookup(server, state=state, detail=Item("777"))
        self.assertEqual(self.published(), "", "the previous album's artwork never lingers")

    def test_lookup_failure_publishes_nothing_and_leaks_nothing(self):
        server = FakePosterServer(error=OSError("GET https://192.168.1.20:32400/x?X-Plex-Token=tok3n failed"))
        out = io.StringIO()
        with redirect_stdout(out):
            state = self.run_lookup(server)
        self.assertEqual(self.published(), "")
        self.assertNotIn("tok3n", out.getvalue())
        self.assertNotIn("192.168", out.getvalue())
        self.assertNotIn("555", state.get("plex_public_art_cache", {}), "failures are retried later")


@unittest.skipUnless(has_module("PIL"), "Pillow is required for the artwork path")
class MinstrelHostedArtworkTests(unittest.TestCase):
    """Minstrel's single cover download feeds both Ember UI and the artwork cache."""

    def setUp(self):
        import artwork_cache
        import shutil
        self.bus = use_bus(self, minstrel)
        minstrel._hosted_art_url = None
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = artwork_cache.ArtworkCache(self.tmp / "artwork", "https://artwork.example.org")

    def server(self, colour=(10, 120, 200), size=512):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (size, size), colour).save(buffer, format="JPEG")
        server = mock.Mock()
        server.url.return_value = "http://192.168.1.20:32400/library/metadata/1/thumb?X-Plex-Token=sekret"
        server.transcodeImage.return_value = "http://192.168.1.20:32400/photo/:/transcode?X-Plex-Token=sekret"
        server._session.get.return_value = mock.Mock(content=buffer.getvalue(), raise_for_status=lambda: None)
        return server

    def hosted(self):
        return self.bus.text("/plex/art_hosted_url_utf8")

    def test_one_larger_download_feeds_the_ui_mosaic_and_the_cache(self):
        server = self.server()
        track = mock.Mock(parentThumb="/library/metadata/1/thumb/1700000000", parentTitle="Come Clarity",
                          grandparentTitle="In Flames")
        seq = self.bus.get_int("/plex/metadata_seq", 0)
        minstrel.publish_album_art(server, track, track, {"album_art_enabled": True}, artwork_cache=self.cache)
        kwargs = server.transcodeImage.call_args.kwargs
        self.assertEqual((kwargs["width"], kwargs["height"], kwargs["imageFormat"]), (512, 512, "jpeg"))
        self.assertEqual(server._session.get.call_count, 1, "no second download")
        self.assertEqual(self.bus.get_int("/plex/art_valid"), 1)
        self.assertEqual((self.bus.get_int("/plex/art_width"), self.bus.get_int("/plex/art_height")), (48, 48))
        url = self.hosted()
        self.assertRegex(url, r"^https://artwork\.example\.org/come-clarity-in-flames\.png\?v=[0-9a-f]{12}$")
        for private in ("sekret", "192.168", "32400", "metadata", "thumb"):
            self.assertNotIn(private, url)
        self.assertGreater(self.bus.get_int("/plex/metadata_seq", 0), seq)
        slug, version = url.rsplit("/", 1)[1].split(".png?v=")
        self.assertTrue(self.cache.read(slug, version).startswith(b"\x89PNG"))

    def test_same_album_keeps_its_url_and_another_album_changes_it(self):
        track = mock.Mock(parentThumb="/library/metadata/1/thumb/1")
        minstrel.publish_album_art(self.server(), track, track, {}, artwork_cache=self.cache)
        first = self.hosted()
        minstrel.publish_album_art(self.server(), track, track, {}, artwork_cache=self.cache)
        self.assertEqual(self.hosted(), first)
        minstrel.publish_album_art(self.server(colour=(250, 10, 10)), track, track, {}, artwork_cache=self.cache)
        self.assertNotEqual(self.hosted(), first)

    def test_disabled_cache_keeps_the_original_tiny_request(self):
        server = self.server(size=48)
        track = mock.Mock(parentThumb="/library/metadata/1/thumb/1")
        minstrel.publish_album_art(server, track, track, {"album_art_enabled": True})
        kwargs = server.transcodeImage.call_args.kwargs
        self.assertEqual((kwargs["width"], kwargs["height"], kwargs["imageFormat"]), (48, 48, "png"))
        self.assertEqual(self.hosted(), "N/A")
        self.assertEqual(list(self.tmp.iterdir()), [], "nothing is cached")

    def test_failures_publish_no_hosted_url_and_leak_nothing(self):
        track = mock.Mock(parentThumb="/library/metadata/1/thumb/1")
        minstrel.publish_album_art(self.server(), track, track, {}, artwork_cache=self.cache)
        self.assertNotEqual(self.hosted(), "N/A")
        broken = self.server()
        broken._session.get.side_effect = ConnectionError("GET /photo?X-Plex-Token=sekret failed")
        out = io.StringIO()
        with redirect_stdout(out):
            minstrel.publish_album_art(broken, track, track, {}, artwork_cache=self.cache)
        self.assertEqual(self.hosted(), "N/A", "a failed download never leaves the previous album's cover")
        self.assertNotIn("sekret", out.getvalue())
        failing = mock.Mock(wraps=self.cache)
        failing.store_cover.side_effect = OSError("disk full at /home/pi/.cache")
        with redirect_stdout(out):
            minstrel.publish_album_art(self.server(), track, track, {}, artwork_cache=failing)
        self.assertEqual(self.bus.get_int("/plex/art_valid"), 1, "Ember UI artwork is unaffected")
        self.assertEqual(self.hosted(), "N/A")
        self.assertNotIn("/home/pi", out.getvalue())

    @unittest.skipUnless(has_module("yaml"), "PyYAML is required to read the config file")
    def test_malformed_service_config_never_stops_minstrel(self):
        (self.tmp / "artwork_service.yaml").write_text("enabled: maybe\n", encoding="utf-8")
        cache = minstrel.open_artwork_cache({"_config_dir": str(self.tmp)}, "minstrel")
        self.assertIsNone(cache)
        self.assertIn("artwork_service_config_invalid", [m.event for m in self.messages])
        self.assertIsNone(minstrel.open_artwork_cache({"_config_dir": str(self.tmp / "missing")}, "minstrel"))


class MinstrelFailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self.bus = use_bus(self, minstrel)

    def test_tokens_are_redacted_from_error_text(self):
        text = "Max retries exceeded with url: /photo/:/transcode?url=%2Fthumb%3FX-Plex-Token%3Dabc123&X-Plex-Token=abc123"
        redacted = minstrel.redact_plex_token(text)
        self.assertNotIn("abc123", redacted)
        self.assertIn("X-Plex-Token=<redacted>", redacted)

    @unittest.skipUnless(has_module("PIL"), "Pillow is required for the artwork path")
    def test_artwork_failure_clears_art_and_never_prints_the_token(self):
        server = mock.Mock()
        server.url.return_value = "http://plex:32400/library/1/thumb?X-Plex-Token=sekret"
        server.transcodeImage.return_value = "http://plex:32400/photo/:/transcode?X-Plex-Token=sekret"
        server._session.get.side_effect = ConnectionError(
            "Max retries exceeded with url: /photo/:/transcode?X-Plex-Token=sekret")
        track = mock.Mock(parentThumb="/library/1/thumb")
        output = io.StringIO()
        with redirect_stdout(output):
            minstrel.publish_album_art(server, track, track, {"album_art_enabled": True})
        self.assertEqual(self.bus.get_int("/plex/art_valid", -1), 0)
        self.assertNotIn("sekret", output.getvalue())

    def test_no_session_publishes_explicit_unavailable_metadata(self):
        state = {}
        minstrel.publish_plex_metadata(None, state)
        seq = self.bus.get_int("/plex/metadata_seq")
        self.assertEqual(self.bus.text("/plex/title_utf8"), "N/A")
        self.assertEqual(self.bus.text("/plex/codec_utf8"), "N/A")
        minstrel.publish_plex_metadata(None, state)
        self.assertEqual(self.bus.get_int("/plex/metadata_seq"), seq)

    def test_missing_ultrablur_marks_palette_invalid(self):
        album = mock.Mock(ultraBlurColors=None)
        track = mock.Mock(album=mock.Mock(return_value=album))
        self.bus.values["/album/ultra/valid"] = 1
        self.assertFalse(minstrel.try_publish_ultrablur(track))
        self.assertEqual(self.bus.get_int("/album/ultra/valid"), 0)

    def test_metadata_text_never_splits_a_utf8_character(self):
        minstrel.publish_utf8("/plex/title_utf8", "é" * 200, max_chars=10)
        self.assertEqual(self.bus.text("/plex/title_utf8"), "é" * 4)


# Token persistence and credential precedence now live in tests/test_plex_auth.py.


# --------------------------------------------------------------------------- MPRIS bridge


class MprisBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bus = use_bus(self, mpris_bridge)
        cfg = dict(mpris_bridge.DEFAULTS, youtube_process_hints=[])
        self.bridge = mpris_bridge.MprisBridge(cfg)

    def test_initial_bus_state_is_unavailable(self):
        self.assertEqual(self.bus.get_int("/mpris/available", -1), 0)
        self.assertEqual(self.bus.get_int("/mpris/art_valid", -1), 0)
        self.assertEqual(self.bus.text("/mpris/title_utf8"), "N/A")

    def test_player_selection_follows_the_hdmi2_context(self):
        players = ["de.haeckerfelix.Shortwave", "chromium.instance42"]
        self.assertEqual(self.bridge._select_player(mpris_bridge.HDMI2_SOURCE_RADIO, players),
                         "de.haeckerfelix.Shortwave")
        self.assertEqual(self.bridge._select_player(mpris_bridge.HDMI2_SOURCE_PLEX, players), "")
        # Sole non-radio player is accepted only in YouTube context.
        self.assertEqual(self.bridge._select_player(mpris_bridge.HDMI2_SOURCE_YOUTUBE, players),
                         "chromium.instance42")
        self.assertEqual(self.bridge._select_player(mpris_bridge.HDMI2_SOURCE_RADIO, ["chromium.instance42"]), "")

    def test_command_without_a_player_is_rejected_and_consumed(self):
        self.bus.values["/mpris/control/next_seq"] = self.bridge.control_seen["next"] + 1
        with mock.patch.object(self.bridge, "_run") as run:
            self.bridge._consume_controls()
            self.bridge._consume_controls()
        run.assert_not_called()
        self.assertEqual(self.bus.get_int("/mpris/control/last_ok"), 0)
        self.assertEqual([m.event for m in self.messages], ["mpris_command_rejected"])

    def test_brief_player_gap_keeps_last_snapshot_then_clears(self):
        self.bridge.snapshot = mpris_bridge.Snapshot(player="p", available=True, title="Song", sampled_at=10.0)
        self.bridge.last_good_seen_at = 10.0
        with mock.patch.object(self.bridge, "_list_players", return_value=[]):
            self.bus.values["/hdmi2/selected_source"] = mpris_bridge.HDMI2_SOURCE_RADIO
            self.bridge._refresh(11.0)
            self.assertEqual(self.bus.text("/mpris/title_utf8"), "Song")
            self.bridge._refresh(13.0)
        self.assertEqual(self.bus.get_int("/mpris/available"), 0)
        self.assertEqual(self.bus.text("/mpris/source_utf8"), "RADIO")

    def test_playerctl_missing_is_an_unavailable_player_not_a_crash(self):
        cfg = dict(mpris_bridge.DEFAULTS, playerctl_path=str(Path(tempfile.gettempdir(), "no-such-playerctl")))
        bridge = mpris_bridge.MprisBridge(cfg)
        self.assertEqual(bridge._list_players(), [])

    def test_text_is_clipped_on_a_character_boundary(self):
        self.assertEqual(mpris_bridge.utf8_prefix("ab€", 4), b"ab")
        self.assertEqual(mpris_bridge.play_state_from_text("Paused\n"), mpris_bridge.PLAY_PAUSED)


# --------------------------------------------------------------------------- HDMI-2 controller


class Hdmi2ControllerTests(unittest.TestCase):
    def setUp(self):
        self.bus = use_bus(self, hdmi2)
        cfg = dict(hdmi2.DEFAULTS, labwc_rules_enabled=False)
        self.controller = hdmi2.Hdmi2SourceController(cfg)
        self.launches = []
        for name, result in (("_launch_shortwave", True), ("_launch_pear", True)):
            patcher = mock.patch.object(self.controller, name, side_effect=lambda n=name, r=result: self.launches.append(n) or r)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.controller, "_close_guest_apps")
        patcher.start()
        self.addCleanup(patcher.stop)

    def request_cycle(self):
        self.bus.values[hdmi2.KEY_CYCLE_SEQ] = self.bus.get_int(hdmi2.KEY_CYCLE_SEQ) + 1
        self.controller._consume_cycles()

    def test_cycle_order_is_plex_radio_youtube_plex(self):
        seen = []
        for _ in range(3):
            self.request_cycle()
            seen.append(self.bus.get_int(hdmi2.KEY_SELECTED_SOURCE))
        self.assertEqual(seen, [hdmi2.HDMI2_SOURCE_RADIO, hdmi2.HDMI2_SOURCE_YOUTUBE, hdmi2.HDMI2_SOURCE_PLEX])
        self.assertEqual(self.launches, ["_launch_shortwave", "_launch_pear"])

    def test_leaving_plex_requests_a_plex_stop_through_minstrels_counter(self):
        before = self.bus.get_int("/plex/control/stop_seq")
        self.request_cycle()
        self.assertEqual(self.bus.get_int("/plex/control/stop_seq"), before + 1)

    def test_failed_guest_launch_reports_error_and_keeps_previous_slot(self):
        self.controller._launch_shortwave.side_effect = None
        self.controller._launch_shortwave.return_value = False
        self.request_cycle()
        self.assertEqual(self.bus.get_int(hdmi2.KEY_STATUS), hdmi2.STATUS_ERROR)
        self.assertEqual(self.controller.current, hdmi2.HDMI2_SOURCE_PLEX)

    def test_startup_state_does_not_replay_old_cycle_requests(self):
        self.bus.values[hdmi2.KEY_CYCLE_SEQ] = 41
        fresh = hdmi2.Hdmi2SourceController(dict(hdmi2.DEFAULTS, labwc_rules_enabled=False))
        with mock.patch.object(fresh, "_select") as select:
            fresh._consume_cycles()
        select.assert_not_called()


if __name__ == "__main__":
    unittest.main()
