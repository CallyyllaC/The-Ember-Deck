"""Hardware-independent tests for Pawprint's physical-control contract."""

import sys
import unittest
from pathlib import Path


ENGINE = Path(__file__).resolve().parents[1] / "EmberEngine"
sys.path.insert(0, str(ENGINE))

from pawprint_logic import (  # noqa: E402
    DebounceState,
    HDMI2_SOURCE_PLEX,
    HDMI2_SOURCE_RADIO,
    MEDIA_TARGET_BLUETOOTH,
    MEDIA_TARGET_MPRIS,
    MEDIA_TARGET_NONE,
    MEDIA_TARGET_PLEX,
    MediaRoutingState,
    bluetooth_transport_action,
    choose_media_target,
    debounce_level,
    hold_reached,
    remap_normalised_voltage,
    selector_position,
)


class MediaRoutingTests(unittest.TestCase):
    def test_active_plex_beats_bluetooth_and_mpris(self):
        target, reason = choose_media_target(MediaRoutingState(
            plex_available=True,
            plex_playing=True,
            bluetooth_claimed=True,
            mpris_available=True,
            mpris_playing=True,
            hdmi2_source=HDMI2_SOURCE_RADIO,
            last_target=MEDIA_TARGET_BLUETOOTH,
        ))
        self.assertEqual(target, MEDIA_TARGET_PLEX)
        self.assertEqual(reason, "Plex playing")

    def test_bluetooth_wins_only_after_plex_is_not_playing(self):
        target, _ = choose_media_target(MediaRoutingState(
            plex_available=True,
            plex_playing=False,
            bluetooth_claimed=True,
            mpris_playing=True,
        ))
        self.assertEqual(target, MEDIA_TARGET_BLUETOOTH)

    def test_playing_mpris_beats_idle_context(self):
        target, _ = choose_media_target(MediaRoutingState(
            mpris_available=True,
            mpris_playing=True,
            hdmi2_source=HDMI2_SOURCE_PLEX,
        ))
        self.assertEqual(target, MEDIA_TARGET_MPRIS)

    def test_local_selector_routes_idle_play_to_mpris(self):
        target, _ = choose_media_target(MediaRoutingState(hdmi2_source=HDMI2_SOURCE_RADIO))
        self.assertEqual(target, MEDIA_TARGET_MPRIS)

    def test_returning_selector_to_plex_reclaims_idle_route(self):
        target, reason = choose_media_target(MediaRoutingState(
            plex_available=False,
            mpris_available=True,
            hdmi2_source=HDMI2_SOURCE_PLEX,
            last_target=MEDIA_TARGET_MPRIS,
        ))
        self.assertEqual(target, MEDIA_TARGET_PLEX)
        self.assertEqual(reason, "HDMI-2 Plex selected")

    def test_cold_boot_defaults_to_plex_without_broadcasting(self):
        target, _ = choose_media_target(MediaRoutingState(last_target=MEDIA_TARGET_NONE))
        self.assertEqual(target, MEDIA_TARGET_PLEX)

    def test_priority_transitions_are_deterministic(self):
        states = [
            MediaRoutingState(),
            MediaRoutingState(bluetooth_claimed=True),
            MediaRoutingState(plex_available=True, plex_playing=True, bluetooth_claimed=True),
            MediaRoutingState(mpris_available=True, mpris_playing=True),
        ]
        self.assertEqual(
            [choose_media_target(state)[0] for state in states],
            [MEDIA_TARGET_PLEX, MEDIA_TARGET_BLUETOOTH, MEDIA_TARGET_PLEX, MEDIA_TARGET_MPRIS],
        )


class PhysicalInputLogicTests(unittest.TestCase):
    def test_bluetooth_command_generation(self):
        self.assertEqual(bluetooth_transport_action("play", False), "play")
        self.assertEqual(bluetooth_transport_action("play", True), "pause")
        self.assertEqual(bluetooth_transport_action("stop", True), "stop")
        self.assertEqual(bluetooth_transport_action("ff", True), "next")
        self.assertEqual(bluetooth_transport_action("rew", True), "previous")
        self.assertEqual(bluetooth_transport_action("eject", False), "")

    def test_initial_held_button_does_not_fire(self):
        state = DebounceState()
        self.assertEqual(debounce_level(state, 1, 10.0, 0.05), (False, False))

    def test_short_bounce_is_rejected(self):
        state = DebounceState(stable=0, candidate=0, changed_at=0.0)
        self.assertEqual(debounce_level(state, 1, 1.00, 0.05), (False, False))
        self.assertEqual(debounce_level(state, 0, 1.02, 0.05), (False, True))
        self.assertEqual(state.stable, 0)

    def test_press_and_release_are_accepted_after_debounce(self):
        state = DebounceState(stable=0, candidate=0, changed_at=0.0)
        debounce_level(state, 1, 2.00, 0.05)
        self.assertEqual(debounce_level(state, 1, 2.051, 0.05), (True, False))
        debounce_level(state, 0, 2.10, 0.05)
        self.assertEqual(debounce_level(state, 0, 2.16, 0.05), (True, False))

    def test_hold_threshold_is_inclusive(self):
        self.assertFalse(hold_reached(5.0, 7.99, 3.0))
        self.assertTrue(hold_reached(5.0, 8.0, 3.0))
        self.assertFalse(hold_reached(None, 99.0, 3.0))

    def test_selector_interpretation_rejects_unwired_pattern(self):
        lookup = {0b11: 3, 0b10: 1, 0b01: 2}
        self.assertEqual(selector_position(0b10, lookup), 1)
        self.assertIsNone(selector_position(0b00, lookup))

    def test_pot_limits_are_normalised_voltage_not_adc_counts(self):
        self.assertAlmostEqual(remap_normalised_voltage(0.36, 0.36, 0.99, 1.0), 0.0)
        self.assertAlmostEqual(remap_normalised_voltage(0.99, 0.36, 0.99, 1.0), 1.0)
        self.assertAlmostEqual(remap_normalised_voltage(0.675, 0.36, 0.99, 1.0), 0.5)


if __name__ == "__main__":
    unittest.main()
