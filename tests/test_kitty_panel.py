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

    def test_frame_shape_and_a_visible_trace(self):
        scope = self.sp.PhosphorScope()
        wave = np.sin(np.linspace(0, 6 * np.pi, 160)).astype(np.float32) * 0.5
        frame = scope.render(wave, (200, 60, 90), (300, 120))
        self.assertEqual(frame.shape, (120, 300, 3))
        self.assertEqual(frame.dtype, np.uint8)
        self.assertGreater(int(frame.max()), 200, "the ivory core is drawn")

    def test_silence_draws_a_flat_line_without_amplifying_noise(self):
        scope = self.sp.PhosphorScope()
        frame = scope.render(np.full(160, 1e-4, np.float32), (200, 60, 90), (200, 80))
        bright_rows = np.where(frame[:, :, 0].max(axis=1) > 200)[0]
        self.assertTrue(len(bright_rows) <= 3 and abs(int(bright_rows.mean()) - 40) <= 2)

    def test_afterglow_fades_between_frames(self):
        scope = self.sp.PhosphorScope()
        loud = np.sin(np.linspace(0, 6 * np.pi, 160)).astype(np.float32)
        scope.render(loud, (200, 60, 90), (200, 80))
        quiet = scope.render(np.zeros(160, np.float32), (200, 60, 90), (200, 80))
        later = quiet
        for _ in range(10):
            later = scope.render(np.zeros(160, np.float32), (200, 60, 90), (200, 80))
        self.assertGreater(int(quiet.sum()), int(later.sum()), "glow decays")

    def test_dark_palette_colours_are_lifted(self):
        self.assertGreater(sum(self.sp.visible_colour((10, 5, 5))), 100)
        self.assertEqual(self.sp.visible_colour((0, 0, 0)), (210, 95, 28))


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


class ConfigTests(unittest.TestCase):
    def test_scope_options_validate(self):
        runtime_config.validate_ember_ui({"scope_pixel_fps": 30, "scope_image_update": "edit"})
        for bad in ({"scope_pixel_fps": 0}, {"scope_pixel_fps": 120}, {"scope_image_update": "sideways"}):
            with self.subTest(cfg=bad):
                with self.assertRaises(runtime_config.ConfigError):
                    runtime_config.validate_ember_ui(bad)

    def test_ui_defaults_match_the_validator(self):
        ui = import_engine_module("ember_ui")
        runtime_config.validate_ember_ui({k: v for k, v in ui.DEFAULTS.items()})


if __name__ == "__main__":
    unittest.main()
