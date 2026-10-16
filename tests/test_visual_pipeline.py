"""Audio/visual pipeline contracts: Echo -> Foxfire -> Will-o'-Wisp, with Aurora colour.

No audio device, BlinkStick or display is touched. The tests check the pure
signal and colour logic plus the Synapse shapes that couple these workers.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import FakeBus, capture_whisper, has_module, import_engine_module  # noqa: E402

aurora = import_engine_module("aurora")
foxfire = import_engine_module("foxfire")
echo = import_engine_module("echo")
willo_wisp = import_engine_module("willo_wisp")
artwork_palette = import_engine_module("artwork_palette")
runtime_config = import_engine_module("runtime_config")


def use_bus(test, *modules) -> FakeBus:
    bus = FakeBus()
    for module in modules:
        patcher = mock.patch.object(module, "bus", bus)
        patcher.start()
        test.addCleanup(patcher.stop)
    return bus


def solid_art(rgb, size=48):
    return list(rgb) * (size * size)


# --------------------------------------------------------------------------- Echo


class EchoTests(unittest.TestCase):
    def test_waveform_is_decimated_to_the_configured_bus_length(self):
        out = echo.decimate_waveform(np.sin(np.linspace(0, 6.28, 2048)), 160)
        self.assertEqual(out.shape, (160,))
        self.assertEqual(out.dtype, np.float32)
        self.assertEqual(echo.decimate_waveform(np.array([]), 160).tolist(), [0.0] * 160)

    def test_stereo_is_mixed_to_mono(self):
        stereo = np.array([[1.0, -1.0], [0.5, 0.5]], dtype=np.float32)
        self.assertEqual(echo.mix_to_mono(stereo).tolist(), [0.0, 0.5])

    @unittest.skipUnless(has_module("yaml"), "PyYAML is required to load worker YAML")
    def test_invalid_yaml_stops_echo_with_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "echo.yaml")
            path.write_text("fft_size: lots\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CONFIG_PATH": str(path)}):
                with self.assertRaisesRegex(runtime_config.ConfigError, "echo: fft_size"):
                    echo.load_cfg()


# --------------------------------------------------------------------------- Foxfire


class FoxfireTests(unittest.TestCase):
    def test_animation_changes_only_while_tape_source_is_engaged(self):
        self.assertEqual(foxfire.resolve_selector_mode(3, 2, 0, 3), (2, True))
        self.assertEqual(foxfire.resolve_selector_mode(1, 3, 2, 3), (2, False))
        self.assertEqual(foxfire.resolve_selector_mode(3, -1, 1, 3), (1, False))
        self.assertEqual(foxfire.valid_mode("junk", 9), 0)

    def test_semantic_gain_midpoint_preserves_normal_level(self):
        self.assertAlmostEqual(foxfire.map_semantic_gain(0.0, 0.08, 1.0, 2.0), 0.08)
        self.assertAlmostEqual(foxfire.map_semantic_gain(0.5, 0.08, 1.0, 2.0), 1.0)
        self.assertAlmostEqual(foxfire.map_semantic_gain(1.0, 0.08, 1.0, 2.0), 2.0)
        self.assertAlmostEqual(foxfire.map_semantic_gain(7.0, 0.08, 1.0, 2.0), 2.0)

    def test_spectrum_is_reduced_to_one_band_per_led(self):
        spectrum = np.ones(1025, dtype=np.float32)
        bands, centres = foxfire.downbin_log(spectrum, 48000.0, 2048, 24, fmin=10.0)
        self.assertEqual(bands.shape, (24,))
        self.assertTrue(np.all(np.diff(centres) > 0))

    def test_frame_uses_palette_tiers_for_colour_and_alpha_for_level(self):
        bus = use_bus(self, foxfire)
        dark, mid1, mid2, light = ([0.1, 0.0, 0.0, 1.0], [0.0, 0.4, 0.0, 1.0],
                                   [0.0, 0.0, 0.9, 1.0], [1.0, 1.0, 1.0, 1.0])
        levels = np.array([0.0, 0.3, 0.6, 1.0], dtype=np.float32)
        rgba = foxfire.apply_quartile_palette(levels, light, mid2, dark, mid1, out_rgba_key="/leds/main/frame_rgba")
        self.assertEqual(len(bus.values["/leds/main/frame_rgba"]), 4 * 4)
        np.testing.assert_allclose(rgba[:, 3], levels)
        np.testing.assert_allclose(rgba[0, :3], dark[:3])
        np.testing.assert_allclose(rgba[3, :3], light[:3])

    def test_every_mode_returns_one_level_per_led(self):
        levels = np.linspace(0.0, 1.0, 24, dtype=np.float32)
        for index, mode in foxfire.MODES.items():
            with self.subTest(mode=index):
                state = {}
                out = mode(levels, 1.0, state)
                out = mode(levels, 1.02, state)
                self.assertEqual(np.asarray(out).shape, (24,))

    @unittest.skipUnless(has_module("yaml"), "PyYAML is required to load worker YAML")
    def test_invalid_strip_length_stops_foxfire_with_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "foxfire.yaml")
            path.write_text("led_count: 0\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CONFIG_PATH": str(path)}):
                with self.assertRaisesRegex(runtime_config.ConfigError, "foxfire: led_count"):
                    foxfire.load_cfg()


# --------------------------------------------------------------------------- Will-o'-Wisp


class WilloWispTests(unittest.TestCase):
    def test_rgba_alpha_scales_and_neutral_light_moves_to_white_channel(self):
        rgbw = willo_wisp.rgba_to_rgbw(np.array([[1.0, 1.0, 1.0, 0.5], [1.0, 0.0, 0.0, 1.0]], dtype=np.float32))
        self.assertEqual(rgbw.tolist(), [[0, 0, 0, 128], [255, 0, 0, 0]])

    def test_waiting_for_foxfire_keeps_heartbeating_and_honours_stop(self):
        bus = use_bus(self, willo_wisp)
        stop_after = iter([False, False, True])
        with capture_whisper(), mock.patch.object(willo_wisp.time, "sleep"):
            result = willo_wisp.wait_for_led_config("willo_wisp", lambda: next(stop_after))
        self.assertIsNone(result)
        self.assertEqual(bus.get_int("/proc/willo_wisp/hb_seq"), 1)

    def test_led_shape_is_taken_from_foxfire_advertisement(self):
        bus = use_bus(self, willo_wisp)
        bus.values.update({"/leds/main/led_count": 24, "/leds/main/fps": 50.0})
        with capture_whisper():
            self.assertEqual(willo_wisp.wait_for_led_config("willo_wisp", lambda: False), (24, 50.0))


# --------------------------------------------------------------------------- Aurora and palettes


class AuroraTests(unittest.TestCase):
    def setUp(self):
        self.bus = use_bus(self, aurora)
        aurora._generated_palette_cache.clear()
        self.addCleanup(aurora._generated_palette_cache.clear)

    def publish_art(self, prefix, rgb, seq=1):
        self.bus.values.update({
            f"{prefix}/art_valid": 1, f"{prefix}/art_width": 48, f"{prefix}/art_height": 48,
            f"{prefix}/art_seq": seq, f"{prefix}/art_rgb": solid_art(rgb),
        })

    def test_bluetooth_and_unknown_sources_use_the_static_palette(self):
        self.publish_art("/plex", (200, 30, 30))
        self.assertIsNone(aurora.read_dynamic_palette_for_source(aurora.MEDIA_SOURCE_PLEX + 1))
        self.assertIsNone(aurora.read_dynamic_palette_for_source(0))

    def test_plex_prefers_ultrablur_then_falls_back_to_artwork(self):
        self.publish_art("/plex", (200, 30, 30))
        for key in ("tl", "tr", "bl", "br"):
            self.bus.values[f"/album/ultra/{key}_rgba"] = [0.0, 0.0, 1.0, 1.0]
        self.bus.values["/album/ultra/valid"] = 1
        self.assertEqual(aurora.read_dynamic_palette_for_source(aurora.MEDIA_SOURCE_PLEX)[0], (0.0, 0.0, 1.0))
        self.bus.values["/album/ultra/valid"] = 0
        red = aurora.read_dynamic_palette_for_source(aurora.MEDIA_SOURCE_PLEX)[0]
        self.assertGreater(red[0], red[2])

    def test_local_media_uses_mpris_artwork(self):
        self.publish_art("/mpris", (20, 180, 40))
        green = aurora.read_dynamic_palette_for_source(aurora.MEDIA_SOURCE_LOCAL)[0]
        self.assertGreater(green[1], green[0])

    def test_missing_or_invalid_artwork_is_not_a_crash(self):
        self.assertIsNone(aurora.read_generated_art_palette("/mpris"))
        self.bus.values.update({"/mpris/art_valid": 1, "/mpris/art_width": 0, "/mpris/art_height": 48})
        self.assertIsNone(aurora.read_generated_art_palette("/mpris"))

    def test_led_lane_dims_with_brightness_but_ui_lane_does_not(self):
        source = [(1.0, 0.0, 0.0)] * 4
        aurora.publish_theme(aurora.transform_dynamic(source, 0.5, 0.0),
                             aurora.transform_dynamic(source, 0.5, 1.0, 0.45), True)
        self.assertEqual(self.bus.values["/theme/ultra/tl_rgba"][:3], [0.0, 0.0, 0.0])
        self.assertGreater(self.bus.values["/theme/ui/tl_rgba"][0], 0.9)
        self.assertEqual(self.bus.get_int("/theme/dynamic_active"), 1)
        self.assertEqual(self.bus.get_int("/theme/palette_seq"), 1)

    def test_static_palette_follows_hue_and_brightness_controls(self):
        palette = aurora.static_palette(0.0, 0.5, 0.5, dict(aurora.DEFAULTS))
        self.assertEqual(len(palette), 4)
        self.assertTrue(all(max(rgb) <= 0.5 + 1e-9 for rgb in palette))

    def test_theme_requires_exactly_four_colours(self):
        with self.assertRaises(ValueError):
            aurora.publish_theme([(0, 0, 0)] * 3, [(0, 0, 0)] * 4, False)


class ArtworkPaletteContractTests(unittest.TestCase):
    def test_solid_cover_yields_its_colour_as_dominant(self):
        palette = artwork_palette.extract_palette(solid_art((10, 120, 200), 8), 8, 8)
        self.assertEqual(palette.dominant, (10, 120, 200))
        self.assertEqual(len(palette.output_colours()), 4)

    def test_short_pixel_buffer_is_rejected(self):
        with self.assertRaises(ValueError):
            artwork_palette.extract_palette([0] * 10, 8, 8)

    def test_output_policy_needs_four_roles(self):
        with self.assertRaises(ValueError):
            artwork_palette.normalise_output_colours([(0.0, 0.0, 0.0)] * 3)


if __name__ == "__main__":
    unittest.main()
