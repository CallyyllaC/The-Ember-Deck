"""Panel UI layout and text fitting.

The terminal grid is set by the LXTerminal profile's font size, so a bigger
font means fewer cells. These tests hold the layout rules for every likely
grid, from the minimum the UI accepts up to the 80 × 60 design canvas, and
check the text helpers that keep content from wrapping into the wrong place:
the log tail, status tokens, lyric band and metadata lists.
"""

import itertools
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import has_module, import_engine_module  # noqa: E402

ui = import_engine_module("ember_ui")
HAS_RICH = has_module("rich")

GRIDS = [(w, h) for w, h in itertools.product(range(40, 101, 4), range(18, 61, 3))] + [
    (80, 30), (80, 60), (60, 22), (56, 20), (70, 26), (40, 18),
]


def overlaps(a, b):
    return not (a.x + a.width <= b.x or b.x + b.width <= a.x
                or a.y + a.height <= b.y or b.y + b.height <= a.y)


class LayoutTests(unittest.TestCase):
    def plans(self):
        for (w, h), page in itertools.product(GRIDS, ui.PAGES):
            for safe in (0, 1):
                yield (w, h, safe, page), ui.compute_layout(w, h, safe, page)

    def test_every_region_fits_inside_its_container(self):
        for key, plan in self.plans():
            for widget_id, region in plan.regions.items():
                if region is None:
                    continue
                with self.subTest(grid=key, widget=widget_id):
                    self.assertGreaterEqual(region.x, 0)
                    self.assertGreaterEqual(region.y, 0)
                    self.assertGreater(region.width, 0)
                    self.assertGreater(region.height, 0)
                    self.assertLessEqual(region.x + region.width, plan.width)
                    if widget_id in ui.HEADER_WIDGETS:
                        self.assertLessEqual(region.y + region.height, ui.HEADER_H - 1,
                                             "header text stays above the header rule")
                    else:
                        self.assertGreaterEqual(region.y, ui.HEADER_H, "pages start below the header")
                        self.assertLessEqual(region.y + region.height, plan.height)

    def test_nothing_overlaps_within_the_header_or_a_page(self):
        for key, plan in self.plans():
            groups = [ui.HEADER_WIDGETS, *ui.PAGE_WIDGETS.values()]
            for group in groups:
                placed = [(name, plan.regions[name]) for name in group if plan.regions.get(name)]
                for (name_a, a), (name_b, b) in itertools.combinations(placed, 2):
                    with self.subTest(grid=key, a=name_a, b=name_b):
                        self.assertFalse(overlaps(a, b))

    def test_every_page_widget_has_a_rectangle_or_is_deliberately_hidden(self):
        for key, plan in self.plans():
            for page, widgets in ui.PAGE_WIDGETS.items():
                for widget_id in widgets:
                    with self.subTest(grid=key, widget=widget_id):
                        self.assertIn(widget_id, plan.regions)
                        if widget_id != "now-art":
                            self.assertIsNotNone(plan.regions[widget_id])

    def test_lyric_bands_always_hold_three_lines(self):
        for key, plan in self.plans():
            for widget_id in ("now-lyrics", "scope-lyrics"):
                with self.subTest(grid=key, widget=widget_id):
                    self.assertGreaterEqual(plan.inner(widget_id)[1], 3)

    def test_cover_is_square_on_a_two_to_one_cell_grid(self):
        for key, plan in self.plans():
            if plan.regions["now-art"] is None:
                continue
            columns, rows = plan.inner("now-art")
            with self.subTest(grid=key):
                self.assertEqual(columns, rows * 2)
                self.assertGreaterEqual(plan.inner("now-metadata")[0], min(ui.NOW_METADATA_MIN_W, plan.width // 2) - ui.PANEL_CHROME_W)

    def test_header_shows_track_context_on_now_playing_and_the_track_elsewhere(self):
        now = ui.compute_layout(80, 30, 1, "now")
        self.assertIsNone(now.regions["global-title"])
        self.assertIsNone(now.regions["global-progress"])
        for page in ("scope", "info", "console"):
            other = ui.compute_layout(80, 30, 1, page)
            with self.subTest(page=page):
                self.assertIsNotNone(other.regions["global-title"])
                self.assertIsNotNone(other.regions["global-progress"])

    def test_console_status_band_grows_and_the_log_keeps_at_least_four_rows(self):
        for lines in range(1, 12):
            plan = ui.compute_layout(56, 20, 1, "console", lines)
            with self.subTest(lines=lines):
                self.assertGreaterEqual(plan.inner("console-logging")[1], 4)
        self.assertEqual(ui.compute_layout(80, 30, 1, "console", 4).inner("console-status")[1], 4)

    def test_safe_margin_shrinks_on_tiny_grids(self):
        plan = ui.compute_layout(40, 18, 3, "now")
        self.assertEqual(plan.safe, 0)
        self.assertEqual((plan.width, plan.height), (40, 18))
        plan = ui.compute_layout(80, 30, 1, "now")
        self.assertEqual((plan.safe, plan.width, plan.height), (1, 78, 28))


class PlainTextTests(unittest.TestCase):
    def test_wrap_breaks_between_words(self):
        self.assertEqual(ui.wrap_words_cells("the quick brown fox", 10, 3), ["the quick", "brown fox"])

    def test_a_word_wider_than_the_line_is_split(self):
        lines = ui.wrap_words_cells("supercalifragilistic", 8, 5)
        self.assertEqual("".join(lines), "supercalifragilistic")
        self.assertTrue(all(len(line) <= 8 for line in lines))

    def test_cut_text_ends_with_an_ellipsis(self):
        lines = ui.wrap_words_cells("one two three four five six", 9, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertLessEqual(len(lines[-1]), 9)

    def test_missing_text_wraps_to_nothing(self):
        self.assertEqual(ui.wrap_words_cells("", 10, 2), [])
        self.assertEqual(ui.wrap_words_cells("N/A", 10, 2), [])

    def test_metadata_helpers_skip_unknown_values(self):
        self.assertEqual(ui.known_join(["FLAC", "N/A", "", 0, "44.1 kHz"]), "FLAC · 44.1 kHz")
        self.assertEqual(ui.track_label(3, 12), "TRACK 3/12")
        self.assertEqual(ui.track_label(3, 0), "TRACK 3")
        self.assertEqual(ui.track_label(0, 12), "")
        self.assertEqual(ui.bit_depth_label("24"), "24-bit")
        self.assertEqual(ui.bit_depth_label("N/A"), "")


@unittest.skipUnless(HAS_RICH, "Rich is needed for styled text")
class StyledTextTests(unittest.TestCase):
    def setUp(self):
        from rich.text import Text
        self.Text = Text

    def test_log_tail_keeps_the_newest_entries(self):
        entries = [self.Text(f"entry {i} " + "word " * (i % 4)) for i in range(40)]
        shown = ui.tail_wrapped_lines(entries, 20, 10)
        self.assertLessEqual(len(shown), 10)
        self.assertTrue(all(line.cell_len <= 20 for line in shown))
        numbers = [int(n) for n in re.findall(r"entry (\d+)", " ".join(line.plain for line in shown))]
        self.assertEqual(numbers, list(range(numbers[0], 40)), "a contiguous run ending at the newest entry")
        self.assertGreater(len(numbers), 3)

    def test_log_tail_shows_the_end_of_one_oversized_entry(self):
        shown = ui.tail_wrapped_lines([self.Text("word " * 50)], 12, 4)
        self.assertEqual(len(shown), 4)

    def test_wrapping_keeps_styles(self):
        text = self.Text("speaker ", style="bold")
        text.append("says something long enough to wrap", style="italic")
        lines = ui.wrap_text(text, 16)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(line.plain + " " for line in lines).split(), text.plain.split())
        self.assertTrue(lines[0].spans)

    def test_status_tokens_pack_within_the_width(self):
        tokens = [self.Text(label) for label in ("I/O ●", "AUDIO ●", "PLAYER ●", "HDMI-2 ●", "LED ●", "COLOUR ●", "BUS ●")]
        for width in (20, 40, 54, 74):
            rows = ui.pack_tokens(tokens, width)
            with self.subTest(width=width):
                self.assertTrue(all(row.cell_len <= width for row in rows))
                self.assertEqual(" ".join(" ".join(row.plain.split()) for row in rows).count("●"), 7)
        # Seven heartbeats fit one 54-cell row once the spacing tightens.
        self.assertEqual(len(ui.pack_tokens(tokens, 54)), 1)

    def test_lyric_band_never_exceeds_its_height(self):
        context = ("previous line", "the current line is long enough to need two rows here", "next line")
        for height in range(1, 8):
            plain = ui.lyric_context_widget(context, 24, height).plain
            rows = plain.split("\n")
            with self.subTest(height=height):
                self.assertLessEqual(len(rows), height)
                self.assertIn("the current", plain)
                self.assertTrue(all(len(row) <= 24 for row in rows))
        self.assertIn("previous line", ui.lyric_context_widget(context, 24, 5).plain)

    def test_metadata_lists_leave_out_what_the_source_did_not_report(self):
        block = ui.key_value_block(["Title", "N/A"], [("YEAR", "2009"), ("MOOD", "N/A"), ("BPM", "")], 30, 10).plain
        self.assertEqual(block.split("\n"), ["Title", "YEAR 2009"])
        empty = ui.key_value_block([], [("MOOD", "N/A")], 30, 10).plain
        self.assertEqual(empty, "Not reported by this source")
        cut = ui.key_value_block([], [("FILE", "x" * 50)], 12, 10).plain
        self.assertLessEqual(len(cut), 12)


if __name__ == "__main__":
    unittest.main()
