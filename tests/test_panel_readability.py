"""Readability and colour behaviour on the Deck's small terminal grids.

Started by Codex (Now Playing in large type, readable labels, the scope in
album colours); extended with the label colour, the scope's colour fade and
the radial's sizes."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _support import has_module, import_engine_module

ui = import_engine_module('ember_ui')
scope = import_engine_module('scope_phosphor')


@unittest.skipUnless(has_module("rich"), "Rich measures and styles the panel text")
class ReadabilityTests(unittest.TestCase):
    def test_metadata_fits_all_supported_grids(self):
        for width in range(40, 101, 4):
            for height in range(18, 61, 3):
                w, h = ui.compute_layout(width, height, 1, 'now').inner('now-metadata')
                for title in ('The Trial', 'A very long title ' * 12, '日本語の曲 🎵'):
                    meta = dict(title=title, artist='The Pretty Wild', album='zero.point.genesis')
                    rows = ui.now_playing_details(meta, w, h, (220, 255, 255))
                    with self.subTest(grid=(width, height), title=title):
                        self.assertLessEqual(len(rows) + 2, h)
                        self.assertTrue(all(row.cell_len <= w for row in rows))

    def test_roomy_panel_uses_large_letters_and_keeps_unicode(self):
        meta = dict(title='The Trial', artist='The Pretty Wild', album='living dead')
        rows = ui.now_playing_details(meta, 46, 18, (220, 255, 255))
        self.assertTrue(any('\u2800' <= c <= '\u28ff' for c in rows[0].plain))
        self.assertGreater(sum(bool(row.plain) for row in rows), 3)
        meta['title'] = '日本語の曲 🎵'
        rows = ui.now_playing_details(meta, 46, 18, (220, 255, 255))
        self.assertIn(meta['title'], '\n'.join(row.plain for row in rows))

    def test_metadata_values_are_not_dimmed_with_their_labels(self):
        text = ui.key_value_block([], [('CODEC', 'flac')], 30, 5)
        self.assertNotIn('dim', str(text.style))
        self.assertTrue(all('dim' not in str(span.style) for span in text.spans))

    def test_labels_are_a_dimmer_cream_than_their_values(self):
        """Readable on the panel, but still a step below the values."""
        from rich.color import Color
        text = ui.key_value_block([], [('CODEC', 'flac')], 30, 5)
        label = [span for span in text.spans if 'CODEC' in text.plain[span.start:span.end]]
        self.assertTrue(label, 'the label has its own style')
        rgb = Color.parse(str(label[0].style)).get_truecolor()
        luma = 0.2126 * rgb.red + 0.7152 * rgb.green + 0.0722 * rgb.blue
        self.assertGreater(luma, 170, 'clearly readable, not brown')
        self.assertLess(luma, 235, 'and dimmer than the warm-white values')
        self.assertGreater(rgb.red, rgb.blue, 'a warm cream, not grey')


@unittest.skipUnless(has_module("PIL"), "Pillow draws the scope")
class ScopeColourTests(unittest.TestCase):
    def test_scope_follows_album_accent_and_keeps_dark_albums_visible(self):
        wave = np.sin(np.linspace(0, 4 * np.pi, 160))
        red = scope.PhosphorScope().render(wave, (160, 96), [(0, 0, 0), (200, 10, 5)])
        blue = scope.PhosphorScope().render(wave, (160, 96), [(0, 0, 0), (5, 10, 200)])
        self.assertGreater(red[:, :, 0].mean(), red[:, :, 2].mean() * 1.5)
        self.assertGreater(blue[:, :, 2].mean(), blue[:, :, 0].mean() * 1.5)
        for palette in ([(0, 0, 0)], [(1, 0, 0)], []):
            frame = scope.PhosphorScope().render(wave, (160, 96), palette)
            self.assertGreater(frame.max(), 200)

    def test_scope_fades_to_a_new_album_colour_in_about_a_second(self):
        wave = np.sin(np.linspace(0, 4 * np.pi, 160))
        red, blue = [(0, 0, 0), (210, 20, 10)], [(0, 0, 0), (10, 20, 210)]
        s = scope.PhosphorScope()
        s.render(wave, (160, 96), red, now=0.0)
        np.testing.assert_allclose(s._colour, scope.glow_colour(red), err_msg='the first frame starts on the album')
        s.render(wave, (160, 96), blue, now=0.1)
        self.assertGreater(s._colour[0], s._colour[2], 'a tenth of a second in, still mostly red')
        s.render(wave, (160, 96), blue, now=0.4)
        self.assertTrue(10 < s._colour[2] < 200 and s._colour[0] > 20, 'part way there')
        s.render(wave, (160, 96), blue, now=1.5)
        np.testing.assert_allclose(s._colour, scope.glow_colour(blue), atol=8, err_msg='there after a second or so')

    def test_scope_fade_speed_does_not_depend_on_frame_rate(self):
        wave = np.sin(np.linspace(0, 4 * np.pi, 160))
        red, blue = [(0, 0, 0), (210, 20, 10)], [(0, 0, 0), (10, 20, 210)]
        results = []
        for fps in (10, 30):
            s = scope.PhosphorScope()
            s.render(wave, (64, 32), red, now=0.0)
            for step in range(1, int(0.5 * fps) + 1):
                s.render(wave, (64, 32), blue, now=step / fps)
            results.append(s._colour.copy())
        np.testing.assert_allclose(results[0], results[1], atol=1.0)


class RadialSizeTests(unittest.TestCase):
    def test_loud_spokes_stay_inside_the_panel_and_clear_the_logo(self):
        pv = import_engine_module('panel_visualisers')
        r = pv.RadialLogo
        self.assertLessEqual(r.RING_IN + r.RING_REACH, 0.5, 'a full spoke ends inside the panel')
        moon_radius = 253 / r.LOGO_ART * r.LOGO_SIZE * 1.024   # the art's moon, at its largest "breath"
        self.assertGreater(r.RING_IN, moon_radius + 0.03, 'the ring clears the logo')
        self.assertGreaterEqual(r.RING_REACH, 0.14, 'and the spokes keep room to move')


if __name__ == '__main__':
    unittest.main()
