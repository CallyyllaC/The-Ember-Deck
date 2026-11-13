"""Large dot-matrix lyric text (dot_matrix.py) and when the UI uses it."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import has_module, import_engine_module  # noqa: E402

import dot_matrix as dm  # noqa: E402

LYRIC = "With nothing but my hate in a carousel of agony"


def dots(cell: str) -> int:
    return 0 if cell == " " else bin(ord(cell) - 0x2800).count("1")


class FontTests(unittest.TestCase):
    def test_every_glyph_is_seven_rows_of_one_width(self):
        for char, glyph in dm._GLYPHS.items():
            with self.subTest(char=char):
                self.assertEqual(len(glyph), dm.GLYPH_ROWS)
                self.assertEqual(len({len(row) for row in glyph}), 1)
                self.assertTrue(set("".join(glyph)) <= {"#", "."})

    def test_letters_digits_and_lyric_punctuation_are_covered(self):
        for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!?.,:;'\"-()&/":
            with self.subTest(char=char):
                self.assertIn(char, dm._GLYPHS)

    def test_text_is_upper_cased_and_accents_dropped(self):
        self.assertEqual(dm.normalise("Café déjà vu — it’s 2 a.m."), "CAFE DEJA VU - IT'S 2 A.M.")
        self.assertEqual(dm.normalise("  spaced   out  "), "SPACED OUT")
        self.assertEqual(dm.normalise("Straße"), "STRASSE")

    def test_characters_the_font_lacks_refuse_the_whole_line(self):
        for text in ("君の名は", "love ❤", "Ω mega"):
            with self.subTest(text=text):
                self.assertIsNone(dm.normalise(text))
                self.assertIsNone(dm.render_lines(text, 80, 3))


class RenderTests(unittest.TestCase):
    def test_lines_are_pairs_of_braille_rows_within_the_width(self):
        lines = dm.render_lines(LYRIC, 74, 3)
        self.assertIsNotNone(lines)
        self.assertEqual(len(lines), 2)
        for top, bottom in lines:
            for row in (top, bottom):
                self.assertLessEqual(len(row), 74)
                self.assertTrue(all(c == " " or 0x2800 < ord(c) <= 0x28FF for c in row))

    def test_bitmap_width_matches_the_wrap_measurement(self):
        for text in ("HELLO WORLD", "I AM 1", "A, B. C!", "(WHY?) - 'CAUSE"):
            with self.subTest(text=text):
                self.assertEqual(len(dm._bitmap(text)[0]), dm._line_width(text))

    def test_the_eighth_dot_row_is_blank_so_stacked_lines_do_not_touch(self):
        top, bottom = dm.render_lines("HELLO", 40, 1)[0]
        for cell in bottom:
            if cell != " ":
                self.assertFalse((ord(cell) - 0x2800) & (0x40 | 0x80), "dots 7 and 8 stay off")

    def test_a_letter_draws_the_expected_number_of_dots(self):
        top, bottom = dm.render_lines("L", 4, 1)[0]
        self.assertEqual(sum(dots(c) for c in top + bottom), 11)  # 7 down, 4 along

    def test_text_that_needs_too_many_lines_or_a_wider_word_is_refused(self):
        self.assertIsNone(dm.render_lines(LYRIC, 74, 1))
        self.assertIsNone(dm.render_lines("Supercalifragilistic", 10, 5))
        self.assertIsNone(dm.render_lines("", 74, 3))


@unittest.skipUnless(has_module("rich"), "Rich is needed for styled text")
class LyricBandTests(unittest.TestCase):
    def setUp(self):
        self.ui = import_engine_module("ember_ui")
        self.context = ("Ever on and on, I continue circling", LYRIC, "Till slowly I forget")

    def braille_rows(self, text):
        return [row for row in text.plain.split("\n") if any(0x2800 < ord(c) <= 0x28FF for c in row)]

    def test_large_lyrics_keep_their_neighbours_when_the_band_is_tall_enough(self):
        rendered = self.ui.lyric_context_widget(self.context, 74, 7, large=True)
        rows = rendered.plain.split("\n")
        self.assertLessEqual(len(rows), 7)
        self.assertEqual(len(self.braille_rows(rendered)), 4)
        self.assertIn("Ever on and on", rendered.plain)
        self.assertIn("Till slowly", rendered.plain)

    def test_small_bands_and_the_option_off_use_normal_text(self):
        for width, height, large in ((54, 4, True), (74, 3, True), (74, 7, False)):
            rendered = self.ui.lyric_context_widget(self.context, width, height, large=large)
            with self.subTest(width=width, height=height, large=large):
                self.assertEqual(self.braille_rows(rendered), [])
                self.assertIn("With nothing", rendered.plain)

    def test_text_the_font_cannot_draw_falls_back_to_normal_text(self):
        rendered = self.ui.lyric_context_widget(("", "君の名は。", ""), 74, 7, large=True)
        self.assertEqual(self.braille_rows(rendered), [])
        self.assertIn("君の名は", rendered.plain)

    def test_large_lyrics_are_on_for_an_80_by_30_grid_and_off_for_60_by_22(self):
        for grid, expected in (((80, 30), True), ((70, 26), True), ((60, 22), False), ((56, 20), False)):
            plan = self.ui.compute_layout(*grid, 1, "now")
            with self.subTest(grid=grid):
                self.assertEqual(self.ui.large_lyrics_fit(*plan.inner("now-lyrics")), expected)


class ConfigTests(unittest.TestCase):
    def test_large_lyrics_is_a_validated_boolean(self):
        import runtime_config
        runtime_config.validate_ember_ui({"large_lyrics": False})
        with self.assertRaisesRegex(runtime_config.ConfigError, "large_lyrics"):
            runtime_config.validate_ember_ui({"large_lyrics": "yes"})


if __name__ == "__main__":
    unittest.main()
