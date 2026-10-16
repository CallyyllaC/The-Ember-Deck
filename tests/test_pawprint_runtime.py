"""Pawprint runtime behaviour with GPIO, I2C and BlueZ replaced by fakes.

tests/test_pawprint_logic.py covers the pure decision functions. These tests
drive the real ``Pawprint`` object, so they also cover the Synapse side
effects: which counters a key press increments, what is persisted, and how
routing reacts when Minstrel or Bluetooth are unavailable.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import FakeBus, capture_whisper, has_module, import_engine_module  # noqa: E402

pawprint = import_engine_module("pawprint")
import contracts  # noqa: E402


@unittest.skipUnless(has_module("yaml"), "PyYAML is required for Pawprint's state store")
class PawprintRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        patcher = mock.patch.object(pawprint, "bus", self.bus)
        patcher.start()
        self.addCleanup(patcher.stop)
        whisper = capture_whisper()
        self.messages = whisper.__enter__()
        self.addCleanup(whisper.__exit__, None, None, None)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = dict(pawprint.DEFAULTS, state_path=str(Path(self.tmp.name, "pawprint_state.yaml")))
        self.app = pawprint.Pawprint(self.cfg)
        self.addCleanup(self.app.media_executor.shutdown, wait=False, cancel_futures=True)
        # BlueZ is never queried; tests set the cached Bluetooth state directly.
        self.app.bluetooth_media.refresh = lambda: self.app.bluetooth_media.state

    # ------------------------------------------------------------ helpers
    def minstrel_alive(self, now: float, play_state: int):
        self.bus.values["/proc/minstrel/heartbeat_ms"] = int(now * 1000) & pawprint.bus.MS_CLOCK_MASK
        self.bus.values["/plex/play_state"] = play_state

    def press(self, name: str, now: float):
        self.app._route_transport_command(name, now)

    # ------------------------------------------------------------ startup contract
    def test_startup_publishes_restored_controls_and_cold_boot_route(self):
        for name in pawprint.CONTROL_NAMES:
            self.assertEqual(self.bus.get_float(f"/io/in/control/{name}"), 0.5)
        self.assertEqual(self.bus.get_int("/media/active_source"), pawprint.MEDIA_TARGET_PLEX)
        self.assertEqual(self.bus.get_int("/io/out/led/graph_override", -1), 0)
        self.assertEqual(self.bus.text("/bt/title_utf8"), "N/A")

    def test_restart_does_not_reset_pending_hdmi2_cycle_counter(self):
        self.bus.values["/hdmi2/control/cycle_seq"] = 7
        again = pawprint.Pawprint(self.cfg)
        self.addCleanup(again.media_executor.shutdown, wait=False, cancel_futures=True)
        self.assertEqual(self.bus.get_int("/hdmi2/control/cycle_seq"), 7)

    # ------------------------------------------------------------ routing
    def test_play_key_reaches_plex_while_minstrel_reports_playing(self):
        self.minstrel_alive(100.0, pawprint.PLEX_PLAYING)
        self.press("play", 100.0)
        self.assertEqual(self.bus.get_int("/plex/control/playpause_seq"), 1)
        self.assertEqual(self.bus.get_int("/io/health/media_command_last_target"), pawprint.MEDIA_TARGET_PLEX)
        self.assertEqual(self.bus.get_int("/mpris/control/playpause_seq"), 0)

    def test_stale_minstrel_cannot_hold_the_route_with_old_playing_state(self):
        self.minstrel_alive(100.0, pawprint.PLEX_PLAYING)
        self.bus.values["/hdmi2/selected_source"] = contracts.HDMI2_SOURCE_RADIO
        self.press("ff", 200.0)  # Minstrel heartbeat is 100 s old
        self.assertEqual(self.bus.get_int("/mpris/control/next_seq"), 1)
        self.assertEqual(self.bus.get_int("/plex/control/next_seq"), 0)
        self.assertEqual(self.bus.get_int("/io/health/plex_media_available"), 0)

    def test_live_minstrel_without_a_plex_connection_is_not_plex_available(self):
        self.minstrel_alive(100.0, pawprint.PLEX_PLAYING)
        for state in (contracts.PLEX_UNCONFIGURED, contracts.PLEX_UNAVAILABLE, contracts.PLEX_AUTH_FAILED):
            self.bus.values["/plex/connection_state"] = state
            self.assertFalse(self.app._plex_is_available(100.0), state)
        self.bus.values["/plex/connection_state"] = contracts.PLEX_CONNECTED
        self.assertTrue(self.app._plex_is_available(100.0))
        del self.bus.values["/plex/connection_state"]  # an older Minstrel without the key
        self.assertTrue(self.app._plex_is_available(100.0))

    def test_minstrel_heartbeat_age_survives_the_31_bit_clock_wrap(self):
        self.bus.values["/proc/minstrel/heartbeat_ms"] = 0x7FFFFFFF - 1000
        wrapped_now = (2 ** 31 + 1000) / 1000.0
        self.assertTrue(self.app._plex_is_available(wrapped_now))
        self.app._set_last_media_target(pawprint.MEDIA_TARGET_PLEX, 30 * 86400.0, "test")  # must not overflow

    def test_bluetooth_route_without_a_player_fails_visibly(self):
        self.app.bluetooth_media.state = pawprint.BluetoothMediaState(connected=True, playing=True, status="playing",
                                                                     title="Song", artist="Band")
        self.press("play", 100.0)
        self.assertEqual(self.bus.get_int("/io/health/media_command_last_status"), -1)
        self.assertIn("bluetooth_media_command_failed", [m.event for m in self.messages])

    def test_bluetooth_command_is_dispatched_off_the_gpio_loop(self):
        self.app.bluetooth_media.state = pawprint.BluetoothMediaState(
            connected=True, playing=True, status="playing", title="Song", artist="Band",
            player_path="/org/bluez/hci0/dev_X/player0")
        with mock.patch.object(self.app.bluetooth_media, "send", return_value=(True, "")) as send:
            self.press("play", 100.0)
            self.app.media_executor.shutdown(wait=True)
        send.assert_called_once_with("pause", "/org/bluez/hci0/dev_X/player0")
        self.assertEqual(self.bus.get_int("/media/active_source"), pawprint.MEDIA_TARGET_BLUETOOTH)

    # ------------------------------------------------------------ Bluetooth claim
    def test_notification_blip_does_not_claim_but_continuous_audio_does(self):
        blip = pawprint.BluetoothMediaState(connected=True, playing=True, status="playing")
        self.assertFalse(self.app._bluetooth_should_claim(blip, 10.0))
        self.assertFalse(self.app._bluetooth_should_claim(blip, 11.0))
        self.assertTrue(self.app._bluetooth_should_claim(blip, 12.1))
        paused = pawprint.BluetoothMediaState(connected=True, playing=False, status="paused")
        self.assertTrue(self.app._bluetooth_should_claim(paused, 30.0))  # linger
        self.assertFalse(self.app._bluetooth_should_claim(paused, 12.1 + 46.0))

    def test_real_media_metadata_claims_immediately(self):
        song = pawprint.BluetoothMediaState(connected=True, playing=True, status="playing", title="Song", artist="Band")
        self.assertTrue(self.app._bluetooth_should_claim(song, 10.0))

    def test_gdbus_track_variant_parsing(self):
        raw = ("({'Title': <'Don\\'t Stop'>, 'Artist': <'Band'>, 'Duration': <uint32 215000>, "
               "'TrackNumber': <uint32 3>},)")
        track = pawprint.BluetoothMediaController._parse_track(raw)
        self.assertEqual((track["Title"], track["Artist"], track["Album"]), ("Don't Stop", "Band", "N/A"))
        self.assertEqual((track["Duration"], track["TrackNumber"]), (215000, 3))

    # ------------------------------------------------------------ physical holds
    def drive_record(self, pressed: int, now: float):
        self.app._debounce_record_toggle(pressed, now)
        self.app._update_record_source_hold(now)

    def test_rec_tap_toggles_pot_bank_and_long_hold_cycles_hdmi2_once(self):
        self.drive_record(0, 0.0)
        self.drive_record(1, 1.0)
        self.drive_record(1, 1.1)
        self.drive_record(0, 1.2)
        self.drive_record(0, 1.3)
        self.assertTrue(self.app.rec_bank)
        self.assertEqual(self.bus.get_int("/hdmi2/control/cycle_seq"), 0)

        for t in (2.0, 2.1, 4.0, 5.2, 6.0, 7.0):
            self.drive_record(1, t)
        self.assertEqual(self.bus.get_int("/hdmi2/control/cycle_seq"), 1)
        self.drive_record(0, 7.1)
        self.drive_record(0, 7.2)
        self.assertTrue(self.app.rec_bank, "a long hold must not also toggle the pot bank")

    def test_eject_hold_requests_shutdown_once(self):
        with mock.patch.object(self.app, "_request_shutdown") as shutdown:
            self.app._on_transport_edge("eject", 1, 10.0)
            self.app._update_eject_hold(14.9)
            shutdown.assert_not_called()
            self.app._update_eject_hold(15.0)
            self.app._update_eject_hold(16.0)
        shutdown.assert_called_once()

    def test_restored_value_needs_pickup_before_pot_changes_it(self):
        self.app.controls["colour"] = 0.5
        self.app._apply_pot("colour", 0.95)
        self.assertEqual(self.app.controls["colour"], 0.5)
        self.app._apply_pot("colour", 0.52)  # within the default 0.045 pickup tolerance
        self.app._apply_pot("colour", 0.62)
        self.assertAlmostEqual(self.app.controls["colour"], 0.62)
        self.assertAlmostEqual(self.bus.get_float("/io/in/control/colour"), 0.62)

    # ------------------------------------------------------------ outputs and persistence
    def test_track_progress_graph(self):
        self.bus.values.update({"/media/active_source": pawprint.MEDIA_TARGET_PLEX, "/plex/play_state": 1,
                                "/plex/position_sec": 90.0, "/plex/duration_sec": 180.0})
        self.assertEqual(self.app._track_progress_mask(), 0b11111)
        self.bus.values["/plex/position_sec"] = 0.5
        self.assertEqual(self.app._track_progress_mask(), 0b1)
        self.bus.values["/plex/play_state"] = 0
        self.assertEqual(self.app._track_progress_mask(), 0)

    def test_control_state_round_trip_is_atomic_and_clamped(self):
        store = pawprint.ControlStateStore(Path(self.cfg["state_path"]), 0.5, {"gain": 0.8})
        store.save({"gain": 0.95, "brightness": 0.2, "colour": float("nan"), "saturation": 2.0})
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), ["pawprint_state.yaml"])
        loaded = store.load()
        self.assertEqual(loaded, {"gain": 0.8, "brightness": 0.2, "colour": 0.5, "saturation": 1.0})

    def test_corrupt_state_file_falls_back_to_defaults_and_reports(self):
        path = Path(self.cfg["state_path"])
        path.write_text("controls: [broken", encoding="utf-8")
        values = pawprint.ControlStateStore(path, 0.5, {}).load()
        self.assertEqual(set(values.values()), {0.5})
        self.assertIn("control_state_load_failed", [m.event for m in self.messages])

    def test_selector_events_land_in_the_ring_with_matching_sequence_stamps(self):
        self.app.events.push(pawprint.EVENT_SELECTOR, pawprint.EVENT_CODES["source_selector"], 3)
        seq = self.bus.get_int("/io/events/seq")
        self.assertEqual(seq, self.bus.get_int("/io/events/seq2"))
        ring = self.bus.values["/io/events/ring"]
        offset = (seq % self.app.events.capacity) * pawprint.EVENT_RECORD_WIDTH
        self.assertEqual(ring[offset], seq)
        self.assertEqual(ring[offset + 2:offset + 5], [pawprint.EVENT_SELECTOR, 20, 3])


if __name__ == "__main__":
    unittest.main()
