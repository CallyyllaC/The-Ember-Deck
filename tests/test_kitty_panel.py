"""The kitty panel: image placeholders, graphics commands, the phosphor scope
renderer, and the launcher helper that reads (never writes) existing config."""

import base64
import re
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ENGINE, has_module, import_engine_module  # noqa: E402

import kitty_graphics as kg  # noqa: E402
import panel_terminal as pt  # noqa: E402
import runtime_config  # noqa: E402

import numpy as np  # noqa: E402

COMMAND = re.compile("\x1b_G([^;\x1b]*)(?:;([^\x1b]*))?\x1b\\\\")


class PlaceholderTests(unittest.TestCase):
    def test_every_cell_names_its_row_and_column(self):
        rows = kg.placeholder_rows(5, 3)
        self.assertEqual(len(rows), 3)
        for r, row in enumerate(rows):
            cells = row.split(kg.PLACEHOLDER)[1:]
            self.assertEqual(len(cells), 5)
            for c, marks in enumerate(cells):
                self.assertEqual(marks, kg.DIACRITICS[r] + kg.DIACRITICS[c])

    def test_the_full_diacritic_table_is_present(self):
        self.assertEqual(len(kg.DIACRITICS), 297)
        self.assertEqual(kg.DIACRITICS[0], "̅")
        self.assertEqual(len(set(kg.DIACRITICS)), 297)
        self.assertEqual(len(kg.placeholder_rows(400, 400)), 297, "clamped to what can be addressed")

    @unittest.skipUnless(has_module("rich"), "Rich measures cell widths")
    def test_each_placeholder_is_one_cell_wide_to_rich(self):
        from rich.cells import cell_len
        for cols in (1, 74, 297):
            self.assertEqual(cell_len(kg.placeholder_rows(cols, 1)[0]), cols)

    def test_image_id_round_trips_through_the_colour(self):
        self.assertEqual(kg.id_colour(0xEB5C01), (0xEB, 0x5C, 0x01))

    def test_kitty_is_detected_from_its_environment(self):
        self.assertTrue(kg.is_kitty({"TERM": "xterm-kitty"}))
        self.assertTrue(kg.is_kitty({"KITTY_WINDOW_ID": "1", "TERM": "xterm-256color"}))
        self.assertFalse(kg.is_kitty({"TERM": "xterm-256color"}))


class ImageStreamTests(unittest.TestCase):
    def setUp(self):
        self.out = []
        self.frame = np.zeros((32, 48, 3), np.uint8)
        self.frame[5:9, 10:20] = (255, 120, 40)

    def commands(self):
        return COMMAND.findall("".join(self.out))

    def test_replace_mode_sends_the_image_then_places_it_every_frame(self):
        stream = kg.ImageStream(self.out.append, 77, "replace")
        stream.send(self.frame, 6, 2)
        stream.send(self.frame, 6, 2)
        keys = [k for k, _ in self.commands()]
        self.assertEqual(sum(k.startswith("a=t,") for k in keys), 2)
        self.assertEqual([k for k in keys if k.startswith("a=p")], ["a=p,U=1,i=77,c=6,r=2,q=2"] * 2)
        self.assertIn("f=24,s=48,v=32,o=z,q=2", keys[0])

    def test_edit_mode_edits_in_place_until_the_shape_changes(self):
        stream = kg.ImageStream(self.out.append, 9, "edit")
        stream.send(self.frame, 6, 2)
        stream.send(self.frame, 6, 2)
        stream.send(self.frame, 7, 2)  # resized: must re-place
        keys = [k for k, _ in self.commands()]
        self.assertTrue(keys[0].startswith("a=t,"))
        self.assertTrue(keys[2].startswith("a=f,r=1,X=1,"))
        self.assertEqual(sum(k.startswith("a=p") for k in keys), 2)

    def test_pixels_survive_compression_and_chunking(self):
        big = np.random.default_rng(1).integers(0, 255, (120, 200, 3), dtype=np.uint8)
        kg.ImageStream(self.out.append, 5).send(big, 20, 6)
        chunks = [payload for _, payload in self.commands() if payload]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= kg.CHUNK for c in chunks))
        raw = zlib.decompress(base64.b64decode("".join(chunks)))
        self.assertEqual(raw, big.tobytes())
        keys = [k for k, _ in self.commands()]
        self.assertIn(",m=1", keys[0])
        self.assertEqual(keys[-2], "m=0")  # the last data chunk, before the placement

    def test_delete_removes_the_image(self):
        kg.ImageStream(self.out.append, 5).delete()
        self.assertEqual(self.commands(), [("a=d,d=I,i=5,q=2", "")])


@unittest.skipUnless(has_module("PIL"), "Pillow draws the scope")
class PhosphorTests(unittest.TestCase):
    def setUp(self):
        import scope_phosphor
        self.sp = scope_phosphor

    def wave(self, cycles, amplitude=0.5, shift=0.0):
        return (np.sin(np.linspace(0, cycles * 2 * np.pi, 160) + shift) * amplitude).astype(np.float32)

    def test_frame_shape_and_a_visible_trace(self):
        scope = self.sp.PhosphorScope()
        frame = scope.render(self.wave(3), (300, 120))
        self.assertEqual(frame.shape, (120, 300, 3))
        self.assertEqual(frame.dtype, np.uint8)
        self.assertGreater(int(frame.max()), 200, "the ivory trace is drawn")

    def test_silence_draws_a_flat_line_and_there_is_no_grid(self):
        scope = self.sp.PhosphorScope()
        frame = scope.render(np.full(160, 1e-4, np.float32), (200, 80))
        bright_rows = np.where(frame[:, :, 0].max(axis=1) > 200)[0]
        self.assertTrue(len(bright_rows) <= 3 and abs(int(bright_rows.mean()) - 40) <= 2)
        far_from_line = frame[:20].astype(int)
        self.assertLess(int(far_from_line.max()), 30, "nothing is drawn away from the trace: no grid")

    def test_afterimages_stay_faint_behind_the_live_trace(self):
        scope = self.sp.PhosphorScope()
        for step in range(6):
            scope.render(self.wave(2, shift=step * 0.9), (200, 80))
        frame = scope.render(np.full(160, 1e-4, np.float32), (200, 80)).astype(int)
        live = frame[38:43].max()
        ghosts = np.concatenate([frame[:30], frame[52:]]).max()
        self.assertGreater(int(ghosts), 15, "earlier traces are still visible")
        # The newest afterimage, overlapping the afterglow, peaks at about 60% of
        # the live trace (the approved "Foxfire" faintness); older ones fall below.
        self.assertLess(int(ghosts), int(live) * 0.7, "and clearly secondary to the live trace")

    def test_afterimages_and_afterglow_fade_away(self):
        scope = self.sp.PhosphorScope()
        scope.render(self.wave(3, 1.0), (200, 80))
        soon = scope.render(np.zeros(160, np.float32), (200, 80))
        for _ in range(self.sp.AFTERIMAGES + 4):
            later = scope.render(np.zeros(160, np.float32), (200, 80))
        self.assertGreater(int(soon.sum()), int(later.sum()))
        away = np.concatenate([later[:30], later[52:]]).astype(int)
        self.assertLess(int(away.max()), 30, "nothing is left of the old trace once its afterimages are gone")


class AlbumCoverTests(unittest.TestCase):
    def setUp(self):
        from _support import FakeBus
        from unittest import mock
        self.ui = import_engine_module("ember_ui")
        self.bus = FakeBus()
        patcher = mock.patch.object(self.ui, "bus", self.bus)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bus.set_int("/plex/art_valid", 1)
        self.bus.set_int("/plex/art_width", 48)
        self.bus.set_int("/plex/art_height", 48)
        self.bus.set_array("/plex/art_rgb", np.full(48 * 48 * 3, 40, np.uint8), "u8")

    def test_the_full_cover_is_preferred_over_the_mosaic(self):
        panel = np.zeros((320, 320, 3), np.uint8)
        panel[:200, :200] = (200, 100, 50)
        self.bus.set_array("/plex/art_panel_rgb", panel.reshape(-1), "u8")
        self.bus.set_int("/plex/art_panel_size", 200)
        art = self.ui.read_album_art(self.ui.SOURCE_PLEX)
        self.assertEqual(art.shape, (200, 200, 3))
        self.assertEqual(tuple(art[100, 100]), (200, 100, 50))

    def test_without_a_full_cover_the_mosaic_is_used(self):
        self.bus.set_int("/plex/art_panel_size", 0)
        self.assertEqual(self.ui.read_album_art(self.ui.SOURCE_PLEX).shape, (48, 48, 3))

    @unittest.skipUnless(has_module("PIL"), "Pillow fits the cover")
    def test_the_cover_is_fitted_square_and_centred(self):
        widget = self.ui.AlbumArtWidget.__new__(self.ui.AlbumArtWidget)
        widget.cover = np.full((48, 48, 3), 220, np.uint8)
        pixels = widget.cover_pixels(240, 200)
        self.assertEqual(pixels.shape, (200, 240, 3))
        self.assertEqual(tuple(pixels[100, 120]), (220, 220, 220), "cover in the middle")
        self.assertEqual(tuple(pixels[100, 5]), (13, 5, 6), "panel colour beside a square cover")


class PanelTerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_font_comes_from_the_emberdeck_lxterminal_profile(self):
        self.write(".config/lxterminal/lxterminal-emberdeck.conf",
                   "[general]\nfontname=DejaVu Sans Mono 9\nbgcolor=rgb(0,0,0)\n")
        args = pt.kitty_args(self.home)
        self.assertIn("font_family=DejaVu Sans Mono", args)
        self.assertIn("font_size=9", args)

    def test_missing_or_odd_files_fall_back_to_defaults(self):
        self.assertEqual(pt.kitty_args(self.home), ["--class", "emberdeck-panel", "--start-as=fullscreen"])
        self.write(".config/lxterminal/lxterminal-emberdeck.conf", "[general]\nfontname=Monospace\n")
        self.write(".config/labwc/rc.xml", "<not xml")
        self.assertEqual(pt.kitty_args(self.home), ["--class", "emberdeck-panel", "--start-as=fullscreen"])

    def test_an_lxterminal_rule_keeps_matching_and_owns_fullscreen(self):
        self.write(".config/labwc/rc.xml", """<?xml version="1.0"?>
<labwc_config><windowRules>
  <windowRule identifier="lxterminal"><action name="MoveToOutput" output="HDMI-A-1"/>
    <action name="ToggleFullscreen"/></windowRule>
  <windowRule identifier="*youtube*music*"><action name="ToggleFullscreen"/></windowRule>
</windowRules></labwc_config>""")
        args = pt.kitty_args(self.home)
        self.assertEqual(args[args.index("--class") + 1], "lxterminal")
        self.assertNotIn("--start-as=fullscreen", args)

    def test_a_title_rule_without_fullscreen_still_starts_fullscreen(self):
        self.write(".config/labwc/rc.xml", """<labwc_config><windowRules>
  <windowRule title="EMBER DECK"><action name="MoveToOutput" output="HDMI-A-1"/></windowRule>
</windowRules></labwc_config>""")
        args = pt.kitty_args(self.home)
        self.assertEqual(args[args.index("--class") + 1], "emberdeck-panel")
        self.assertIn("--start-as=fullscreen", args)

    def test_the_helper_never_writes(self):
        rc = self.write(".config/labwc/rc.xml", "<labwc_config/>")
        profile = self.write(".config/lxterminal/lxterminal-emberdeck.conf", "[general]\nfontname=Mono 10\n")
        before = (rc.stat().st_mtime_ns, profile.stat().st_mtime_ns)
        pt.kitty_args(self.home)
        self.assertEqual(before, (rc.stat().st_mtime_ns, profile.stat().st_mtime_ns))

    def test_launcher_uses_kitty_with_the_shipped_config(self):
        launcher = (ENGINE / "launch_ember_ui.sh").read_text(encoding="utf-8")
        self.assertIn('--config "$ROOT/kitty-panel.conf"', launcher)
        self.assertIn("TEXTUAL_COLOR_SYSTEM=truecolor", launcher)
        self.assertNotIn("lxterminal \\", launcher)
        self.assertTrue((ENGINE / "kitty-panel.conf").is_file())


def tone_spectrum(hz, amplitude=400.0, bins=1025):
    """An Echo-shaped spectrum with one tone (bin width 23.4 Hz)."""
    spectrum = np.full(bins, 0.01, np.float32)
    spectrum[int(round(hz * 2048 / 48000))] = amplitude
    return spectrum


PALETTE = [(200, 60, 40), (240, 180, 60), (40, 30, 90), (230, 220, 200)]


@unittest.skipUnless(has_module("PIL"), "Pillow draws the visualisers")
class VisualiserTests(unittest.TestCase):
    def setUp(self):
        self.pv = import_engine_module("panel_visualisers")

    def test_every_visualiser_draws_a_frame_of_the_asked_size(self):
        for name, cls in self.pv.VISUALISERS.items():
            with self.subTest(name=name):
                frame = cls().render(tone_spectrum(440), (160, 96), PALETTE)
                self.assertEqual(frame.shape, (96, 160, 3))
                self.assertEqual(frame.dtype, np.uint8)

    def test_a_tone_lights_its_own_band(self):
        low = self.pv.band_levels(tone_spectrum(60), 40)
        high = self.pv.band_levels(tone_spectrum(8000), 40)
        self.assertGreater(int(np.argmax(high)), int(np.argmax(low)))
        self.assertGreater(float(low.max()), 0.8)
        self.assertLess(float(self.pv.band_levels(np.zeros(1025), 40).max()), 0.01)

    def test_bands_follow_echos_fft_size(self):
        """A bigger fft_size in echo.yaml gives more bins; the same tone stays in its band."""
        big = np.full(2049, 0.01, np.float32)
        big[int(round(1000 * 4096 / 48000))] = 800.0      # same level for a 4096-point window
        small = self.pv.band_levels(tone_spectrum(1000), 40)
        large = self.pv.band_levels(big, 40)
        self.assertEqual(int(np.argmax(large)), int(np.argmax(small)))
        self.assertAlmostEqual(float(large.max()), float(small.max()), places=2)

    def test_peak_caps_hang_then_fall(self):
        bars = self.pv.PeakBars(fps=50)
        bars.render(tone_spectrum(1000), (160, 96), PALETTE)
        held = bars.peak.copy()
        quiet = np.zeros(1025, np.float32)
        for _ in range(5):
            bars.render(quiet, (160, 96), PALETTE)
        np.testing.assert_allclose(bars.peak, held, err_msg="caps hang for a moment")
        for _ in range(150):
            last = bars.render(quiet, (160, 96), PALETTE)
        self.assertLess(float(bars.peak.max()), float(held.max()) * 0.1, "and then fall")
        self.assertLess(int(last.max()), 120, "silence leaves only the unlit segments")

    def test_spectrogram_history_scrolls_left_and_paints_without_adding(self):
        gram = self.pv.Spectrogram()
        gram.add(tone_spectrum(1000), 60, 80)
        for _ in range(9):
            gram.add(np.zeros(1025, np.float32), 60, 80)
        self.assertGreater(float(gram.columns[:, -10].max()), 0.5, "the tone has moved ten columns left")
        self.assertLess(float(gram.columns[:, -1].max()), 0.01)
        before = gram.columns.copy()
        frame = gram.paint(PALETTE)
        self.assertEqual(frame.shape, (60, 80, 3))
        np.testing.assert_array_equal(gram.columns, before)
        with self.assertRaises(ValueError):
            self.pv.Spectrogram().paint(PALETTE)

    def test_spectrogram_scrolls_by_the_clock_not_the_frame_rate(self):
        """One panel width every HISTORY_S seconds, at 50 adds a second or 10."""
        width, seconds = 120, 3.0
        expected = width * seconds / self.pv.Spectrogram.HISTORY_S
        for rate in (50, 10):
            with self.subTest(rate=rate):
                gram = self.pv.Spectrogram()
                gram.add(tone_spectrum(1000), 40, width, now=0.0)       # the marker
                quiet = np.zeros(1025, np.float32)
                for step in range(1, int(seconds * rate) + 1):
                    gram.add(quiet, 40, width, now=step / rate)
                marker = width - 1 - int(np.argmax(gram.columns.max(axis=0)))
                self.assertAlmostEqual(marker, expected, delta=2)

    def test_a_long_pause_does_not_smear_the_history(self):
        gram = self.pv.Spectrogram()
        gram.add(tone_spectrum(1000), 40, 120, now=0.0)
        gram.add(np.zeros(1025, np.float32), 40, 120, now=30.0)
        self.assertGreater(float(gram.columns[:, -2].max()), 0.5, "one column, not a screen of copies")

    def test_radial_stars_drift_outward_and_faster_when_loud(self):
        quiet = self.pv.RadialLogo(fps=30, seed=3)
        loud = self.pv.RadialLogo(fps=30, seed=3)
        start = quiet._star_dist.copy()
        music = np.full(1025, 300.0, np.float32)
        for _ in range(10):
            quiet.render(np.zeros(1025, np.float32), (160, 96))
            loud.render(music, (160, 96))
        kept = (quiet._star_dist > start) & (loud._star_dist > start)   # not yet reborn
        self.assertTrue(kept.any())
        self.assertTrue(np.all(quiet._star_dist[kept] > start[kept]), "stars drift outward")
        self.assertGreater(float((loud._star_dist - start)[kept].mean()),
                           float((quiet._star_dist - start)[kept].mean()) * 2, "faster with the music")
        for _ in range(3000):
            loud._move_stars()
        self.assertTrue(np.all(loud._star_dist <= 1.0), "stars leaving the panel are reborn in the middle")

    def test_radial_art_is_its_own_copy_of_the_boot_art(self):
        """The update keeps boot files a Deck owner changed (flipped for an
        upside-down screen, say); the panel must not pick those changes up."""
        boot = ENGINE / "boot" / "plymouth" / "emberdeck"
        for name in ("crescent.png", "mask.png", "stars.png"):
            with self.subTest(name=name):
                self.assertEqual((self.pv.PANEL_ART / name).read_bytes(), (boot / name).read_bytes())
        self.assertNotIn("boot", self.pv.PANEL_ART.parts)

    def test_radial_starscape_stays_mild(self):
        radial = self.pv.RadialLogo(fps=30, seed=1)
        frame = radial.render(np.full(1025, 300.0, np.float32), (320, 200)).astype(int)
        corner = frame[:30, :50]      # well away from the ring and the logo
        self.assertLess(int(corner.max()), 230, "no star outshines the ring")


class ScopeSelectorTests(unittest.TestCase):
    """Centre on the source selector picks the Scope page's visualiser."""

    def setUp(self):
        from _support import FakeBus
        from unittest import mock
        self.ui = import_engine_module("ember_ui")
        self.bus = FakeBus()
        patcher = mock.patch.object(self.ui, "bus", self.bus)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scope = self.ui.ScopeWidget.__new__(self.ui.ScopeWidget)
        self.scope.visualiser = "scope"
        self.scope._last_frame_at = 5.0
        app = self.ui.EmberUI.__new__(self.ui.EmberUI)
        app.page = "now"
        app._crt = None
        app._transition_running = False
        app._pending_page = None
        app.query_one = lambda *a, **k: self.scope
        app._set_page = lambda page: setattr(app, "page", page)
        self.app = app

    def select(self, source, mode):
        self.bus.set_int("/io/in/selector/source_selector", source)
        self.bus.set_int("/io/in/selector/visualiser_mode", mode)
        self.app._update_page_from_selectors()

    def test_centre_chooses_each_visualiser_and_shows_the_scope_page(self):
        for mode, name in ((1, "scope"), (2, "bars"), (3, "spectrogram"), (4, "radial")):
            with self.subTest(mode=mode):
                self.select(2, mode)
                self.assertEqual(self.scope.visualiser, name)
                self.assertEqual(self.app.page, "scope")
        self.assertEqual(self.scope._last_frame_at, 0.0, "a new visualiser draws straight away")

    def test_ui_position_changes_pages_and_keeps_the_visualiser(self):
        self.select(2, 4)
        self.select(1, 3)
        self.assertEqual(self.app.page, "info")
        self.assertEqual(self.scope.visualiser, "radial")

    def test_led_position_changes_nothing(self):
        self.select(3, 2)
        self.assertEqual((self.app.page, self.scope.visualiser), ("now", "scope"))

    def test_hidden_spectrogram_keeps_filling_at_its_last_size(self):
        pv = import_engine_module("panel_visualisers")
        self.scope.renderers = {"spectrogram": pv.Spectrogram()}
        self.scope.visualiser = "spectrogram"
        self.scope._history_px = None
        self.scope.feed_history(tone_spectrum(500))
        self.assertIsNone(self.scope.renderers["spectrogram"].columns, "nothing before it has been shown")
        self.scope._history_px = (40, 30)
        from unittest import mock
        clock = iter([100.0, 100.4, 100.8, 101.2])          # 1.2 s: 4 columns of a 40-wide, 12 s history
        with mock.patch.object(self.ui.time, "monotonic", lambda: next(clock)):
            self.scope.feed_history(tone_spectrum(500))
            for _ in range(3):
                self.scope.feed_history(np.zeros(1025, np.float32))
        columns = self.scope.renderers["spectrogram"].columns
        self.assertEqual(columns.shape, (30, 40))
        self.assertEqual(39 - int(np.argmax(columns.max(axis=0))), 4, "the tone has scrolled 4 columns")


class ConfigTests(unittest.TestCase):
    def test_scope_options_validate(self):
        runtime_config.validate_ember_ui({"scope_pixel_fps": 30, "scope_image_update": "edit"})
        for name in ("scope", "bars", "spectrogram", "radial"):
            runtime_config.validate_ember_ui({"scope_visualiser": name})
        for bad in ({"scope_pixel_fps": 0}, {"scope_pixel_fps": 120}, {"scope_image_update": "sideways"},
                    {"scope_visualiser": "lava-lamp"}):
            with self.subTest(cfg=bad):
                with self.assertRaises(runtime_config.ConfigError):
                    runtime_config.validate_ember_ui(bad)

    def test_ui_defaults_match_the_validator(self):
        ui = import_engine_module("ember_ui")
        runtime_config.validate_ember_ui({k: v for k, v in ui.DEFAULTS.items()})


if __name__ == "__main__":
    unittest.main()
