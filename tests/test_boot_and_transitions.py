"""Boot set (ignition, boot running light, boot theme files) and the
supporter-only CRT page transition."""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ENGINE, FakeBus, has_module, import_engine_module  # noqa: E402

import boot_lights  # noqa: E402
import crt_transition  # noqa: E402
import ignition  # noqa: E402
import runtime_config  # noqa: E402

import numpy as np  # noqa: E402

ROOT = ENGINE.parent


class IgnitionTests(unittest.TestCase):
    def test_phases(self):
        leds = 24
        sweep = ignition.frame(0.4, leds)
        self.assertGreater(sweep[0, 3], 0.0, "the start of the strip is lit during the sweep")
        self.assertEqual(sweep[-1, 3], 0.0, "the far end is still dark")
        self.assertLessEqual(float(sweep[:, 3].max()), ignition.SWEEP_INTENSITY + 1e-6)
        rise = [float(ignition.frame(t, leds)[:, 3].mean()) for t in (0.9, 1.2, 1.5)]
        self.assertEqual(rise, sorted(rise), "intensity rises")
        flash = ignition.frame(1.65, leds)
        np.testing.assert_allclose(flash, 1.0)
        fade = [float(ignition.frame(t, leds)[:, 3].mean()) for t in (1.8, 2.2, 2.55)]
        self.assertEqual(fade, sorted(fade, reverse=True), "then fades out")
        self.assertIsNone(ignition.frame(ignition.DURATION, leds))

    def test_colours_are_the_awoo_palette_and_follow_brightness(self):
        full = ignition.frame(1.5, 24, brightness=1.0)
        dim = ignition.frame(1.5, 24, brightness=0.25)
        np.testing.assert_allclose(dim[:, :3], full[:, :3] * 0.25, rtol=1e-5)
        np.testing.assert_allclose(full[:, 3], dim[:, 3])
        first = tuple(int(round(c * 255)) for c in ignition.frame(0.5, 24)[0, :3])  # before the drift
        self.assertEqual(first, ignition.VIOLET)

    def test_frames_cover_the_whole_animation(self):
        frames = list(ignition.frames(24, 50))
        self.assertEqual(len(frames), int(round(ignition.DURATION * 50)))
        self.assertTrue(all(f.shape == (24, 4) for f in frames))


class FakeStrip:
    def __init__(self, fail_after=None):
        self.shown = []
        self.pixels = {}
        self.fail_after = fail_after

    def set_pixel(self, idx, rgbw):
        self.pixels[idx] = rgbw

    def show(self):
        if self.fail_after is not None and len(self.shown) >= self.fail_after:
            raise OSError("unplugged")
        self.shown.append(dict(self.pixels))


class FakeHeartbeat:
    def __init__(self):
        self.ticks = 0

    def tick(self, now=None):
        self.ticks += 1


class WispIgnitionTests(unittest.TestCase):
    def setUp(self):
        self.wisp = import_engine_module("willo_wisp")
        self.bus = FakeBus({"/io/in/control/brightness": 0.5})
        patcher = mock.patch.object(self.wisp, "bus", self.bus)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch.object(self.wisp.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_plays_every_frame_and_ticks_the_heartbeat(self):
        strip, beat = FakeStrip(), FakeHeartbeat()
        self.wisp.play_ignition(strip, 24, 50, beat, lambda: False)
        self.assertEqual(len(strip.shown), int(round(ignition.DURATION * 50)))
        self.assertEqual(beat.ticks, len(strip.shown))
        flash = strip.shown[int(1.65 * 50)]
        self.assertTrue(all(w > 0 for (_r, _g, _b, w) in flash.values()), "the flash uses the white channel")

    def test_stops_on_request_or_on_a_write_failure(self):
        strip = FakeStrip()
        self.wisp.play_ignition(strip, 24, 50, FakeHeartbeat(), lambda: len(strip.shown) >= 5)
        self.assertEqual(len(strip.shown), 5)
        failing = FakeStrip(fail_after=3)
        self.wisp.play_ignition(failing, 24, 50, FakeHeartbeat(), lambda: False)
        self.assertEqual(len(failing.shown), 3)

    def test_ignition_option_validates(self):
        runtime_config.validate_willo_wisp({"ignition": False})
        with self.assertRaises(runtime_config.ConfigError):
            runtime_config.validate_willo_wisp({"ignition": "no"})


class BootLightsTests(unittest.TestCase):
    def test_coordinates_match_pawprint(self):
        source = (ENGINE / "pawprint.py").read_text(encoding="utf-8")
        graph = re.search(r"GRAPH_COORDS = (\[.*?\])", source, re.S).group(1)
        self.assertEqual(eval(graph), boot_lights.GRAPH_COORDS)  # noqa: S307 - literal list from our own file
        for name, coord in (("front", boot_lights.FRONT_COORD),):
            self.assertIn(f'"{name}": {coord}', source)
        for coord in boot_lights.TOP_COORDS:
            self.assertIn(str(coord), source)

    def test_running_light_bounces_end_to_end(self):
        masks = [boot_lights.pattern(step)[0] for step in range(32)]
        self.assertTrue(all(bin(m).count("1") == boot_lights.RUN_WIDTH for m in masks))
        self.assertIn(0b11, masks)
        self.assertIn(0b11 << (len(boot_lights.GRAPH_COORDS) - 2), masks)
        self.assertEqual(masks[:16], masks[16:32], "it repeats")
        tops = [boot_lights.pattern(step * boot_lights.TOP_EVERY)[1] for step in range(8)]
        self.assertEqual(tops[:5], [0b10001, 0b01010, 0b00100, 0b01010, 0b10001],
                         "status lamps sweep in from both ends, meet in the middle, then back out")
        for mask in tops:
            self.assertEqual(mask, int(f"{mask:05b}"[::-1], 2), "always symmetrical")
        self.assertTrue(boot_lights.pattern(7)[2], "front lamp stays on")

    def test_hands_over_when_pawprint_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "1").mkdir()
            (proc / "1" / "cmdline").write_bytes(b"/sbin/init\0")
            (proc / "self").mkdir()
            self.assertFalse(boot_lights.pawprint_running(proc))
            (proc / "4242").mkdir()
            (proc / "4242" / "cmdline").write_bytes(b"/home/me/EmberDeck/.venv/bin/python\0pawprint.py\0")
            self.assertTrue(boot_lights.pawprint_running(proc))

    def test_service_template_is_complete(self):
        unit = (ENGINE / "system" / "emberdeck-boot-lights.service").read_text(encoding="utf-8")
        for needle in ("User=[USERNAME]", "[HOME]/EmberDeck/boot_lights.py", "DefaultDependencies=no",
                       "WantedBy=multi-user.target"):
            self.assertIn(needle, unit)


class BootThemeTests(unittest.TestCase):
    THEME = ENGINE / "boot" / "plymouth" / "emberdeck"

    def test_every_image_the_script_loads_ships(self):
        script = (self.THEME / "emberdeck.script").read_text(encoding="utf-8")
        for name in set(re.findall(r'"([a-z_]+\.png)"', script)):
            with self.subTest(image=name):
                self.assertTrue((self.THEME / name).is_file())
                self.assertEqual((self.THEME / name).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_theme_file_points_at_its_install_location(self):
        theme = (self.THEME / "emberdeck.plymouth").read_text(encoding="utf-8")
        self.assertIn("ModuleName=script", theme)
        self.assertIn("ScriptFile=/usr/share/plymouth/themes/emberdeck/emberdeck.script", theme)

    def test_boot_set_installer_can_undo_itself(self):
        script = (ROOT / "tools" / "boot_set" / "install-boot-set.sh").read_text(encoding="utf-8")
        for needle in ("--uninstall", "previous-theme", "plymouth-set-default-theme -R", "read -r -p"):
            self.assertIn(needle, script)

    def test_boot_set_installer_keeps_theme_files_changed_on_the_deck(self):
        script = (ROOT / "tools" / "boot_set" / "install-boot-set.sh").read_text(encoding="utf-8")
        self.assertIn('! cmp -s "$src" "$dest"', script)
        self.assertIn("--force) FORCE=1", script)
        self.assertNotIn('rm -rf "$THEME_DIR"\ninstall', script, "a reinstall never wipes the theme first")

    def test_logo_centres_on_display_zero(self):
        script = (self.THEME / "emberdeck.script").read_text(encoding="utf-8")
        self.assertIn("Window.GetX(0) + Math.Int((Window.GetWidth(0) - logo_size) / 2)", script)
        self.assertIn("Window.GetY(0) + Math.Int((Window.GetHeight(0) - logo_size) / 2)", script)


class CrtTransitionTests(unittest.TestCase):
    def test_frames_switch_off_hold_black_and_switch_on(self):
        frames, hold = crt_transition.frames(64, 48)
        self.assertEqual(len(frames), crt_transition.OFF_STEPS + 1 + crt_transition.ON_STEPS)
        self.assertTrue((frames[hold][:, :, 3] == 255).all() and (frames[hold][:, :, :3] == 0).all())
        first_open = (frames[0][:, :, 3] < 200).mean()
        later_open = (frames[5][:, :, 3] < 200).mean()
        self.assertGreater(first_open, later_open, "the picture collapses")
        self.assertLess((frames[-1][:, :, 3] > 128).mean(), 0.2, "the last frame is nearly clear")

    def test_frames_upload_once_and_play_with_tiny_commands(self):
        out = []
        crt = crt_transition.CrtTransition(out.append)
        crt.prepare(80, 30, (8, 16))
        uploads = re.findall("\x1b_Ga=t,i=(\\d+)", "".join(out))
        self.assertEqual(len(uploads), crt.count)
        out.clear()
        crt.prepare(80, 30, (8, 16))
        self.assertEqual(out, [], "same size: nothing re-sent")
        place = crt.place(3)
        self.assertTrue(place.startswith("\x1b7\x1b[1;1H") and place.endswith("\x1b8"))
        self.assertIn(f"i={crt.first_id + 3},p=1,c=80,r=30,z={crt_transition.OVERLAY_Z},C=1", place)
        self.assertLess(len(place), 120)
        crt.release()
        self.assertEqual(len(re.findall("a=d,d=I", "".join(out))), len(uploads))


@unittest.skipUnless(has_module("cryptography"), "the cryptography package verifies licences")
class SupporterTests(unittest.TestCase):
    def setUp(self):
        from test_awoo_supporter import Signer
        import awoo_supporter
        self.ui = import_engine_module("ember_ui")
        self.signer = Signer()
        real = awoo_supporter.verify_licence
        patcher = mock.patch.object(awoo_supporter, "verify_licence",
                                    lambda text, trusted_keys=None: real(text, trusted_keys=self.signer.trusted))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "supporter.licence"

    def test_valid_licence(self):
        self.path.write_text(self.signer.licence(), encoding="ascii")
        self.assertEqual(self.ui.supporter_status(self.path), (True, "licensed"))

    def test_missing_or_bad_licence_is_simply_not_licensed(self):
        self.assertEqual(self.ui.supporter_status(self.path), (False, "no supporter licence installed"))
        self.path.write_text("AWOO1.nope", encoding="ascii")
        licensed, reason = self.ui.supporter_status(self.path)
        self.assertFalse(licensed)
        self.assertNotIn("AWOO1.nope", reason, "the licence text never appears")


@unittest.skipUnless(has_module("cryptography"), "the cryptography package verifies licences")
class SupporterCheckTests(unittest.TestCase):
    def setUp(self):
        import contextlib
        import io
        from test_awoo_supporter import Signer
        import awoo_supporter
        import supporter_check
        self.io, self.redirect = io, contextlib.redirect_stdout
        self.tool = supporter_check
        self.signer = Signer()
        real = awoo_supporter.verify_licence
        patcher = mock.patch.object(awoo_supporter, "verify_licence",
                                    lambda text, trusted_keys=None: real(text, trusted_keys=self.signer.trusted))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "awoo" / "supporter.licence"

    def run_tool(self, *args, code=None):
        out = self.io.StringIO()
        with self.redirect(out):
            if code is not None:
                status = self.tool.install(self.path, code)
            else:
                status = self.tool.main(["--path", str(self.path), *args])
        return status, out.getvalue()

    def test_install_checks_first_then_saves_privately(self):
        code = self.signer.licence()
        status, out = self.run_tool(code=code + "\n")
        self.assertEqual(status, 0)
        self.assertEqual(self.path.read_text(encoding="ascii"), code + "\n")
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(code, out)
        status, out = self.run_tool()
        self.assertEqual(status, 0)
        self.assertIn("Valid supporter licence", out)

    def test_a_bad_code_saves_nothing(self):
        status, out = self.run_tool(code="AWOO1.not.a.code")
        self.assertEqual(status, 1)
        self.assertIn("Nothing was saved", out)
        self.assertFalse(self.path.exists())

    def test_check_reports_missing_and_rejected_licences(self):
        status, out = self.run_tool()
        self.assertEqual((status, "No supporter licence" in out), (1, True))
        self.path.parent.mkdir(parents=True)
        self.path.write_text("AWOO1.bogus.code.here\n", encoding="ascii")
        status, out = self.run_tool()
        self.assertEqual(status, 1)
        self.assertIn("not accepted", out)
        self.assertNotIn("AWOO1.bogus", out)


class ConfigTests(unittest.TestCase):
    def test_transition_options_validate(self):
        runtime_config.validate_ember_ui({"page_transitions": False, "supporter_licence_path": "~/x.licence"})
        with self.assertRaises(runtime_config.ConfigError):
            runtime_config.validate_ember_ui({"page_transitions": "yes"})


if __name__ == "__main__":
    unittest.main()
