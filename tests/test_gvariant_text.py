"""Reading ``gdbus call`` output (E-4, E-7).

The fixtures follow GLib's printing rules (``g_variant_print_string`` in
glib/gvariant.c, called with type annotations by ``gdbus call``). They are
written from those rules, not captured from a phone or from the Deck.
"""

import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "EmberEngine"
sys.path.insert(0, str(ENGINE))

import gvariant_text  # noqa: E402


class StringLiteralTests(unittest.TestCase):
    def test_single_and_double_quoted_strings(self):
        self.assertEqual(gvariant_text.first_string("(<'playing'>,)"), "playing")
        # GLib switches to double quotes when the string contains '.
        self.assertEqual(gvariant_text.first_string('(<"Don\'t Stop Me Now">,)'), "Don't Stop Me Now")

    def test_escapes_glib_writes(self):
        cases = {
            r"""(<"Say \"Don't\"">,)""": 'Say "Don\'t"',      # both quote characters
            r"(<'AC\\DC'>,)": "AC\\DC",                        # backslash
            r"(<'one\ttwo\nthree'>,)": "one\ttwo\nthree",      # letter escapes
            r"(<'zero​width'>,)": "zero​width",       # \uXXXX
            r"(<'emoji \U0001f525'>,)": "emoji \U0001f525",    # \UXXXXXXXX
            r"(<'it\'s'>,)": "it's",                           # accepted, though GLib would double-quote
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(gvariant_text.first_string(raw), expected)

    def test_invalid_code_points_and_unterminated_text(self):
        self.assertEqual(gvariant_text.first_string(r"(<'a\ud800b'>,)"), "a�b")
        self.assertIsNone(gvariant_text.first_string("(<'never closed"))
        self.assertIsNone(gvariant_text.first_string("(<uint32 5>,)"))
        self.assertIsNone(gvariant_text.first_string(""))
        self.assertIsNone(gvariant_text.first_string(None))


class DictionaryTests(unittest.TestCase):
    TRACK = ("(<{'Title': <\"Don't Stop Me Now\">, 'Artist': <'Queen'>, 'Album': <'Jazz'>, "
             "'Duration': <uint32 209000>, 'TrackNumber': <uint32 12>, 'NumberOfTracks': <uint32 13>}>,)")

    def test_track_dictionary(self):
        self.assertEqual(gvariant_text.dict_strings(self.TRACK),
                         {"Title": "Don't Stop Me Now", "Artist": "Queen", "Album": "Jazz"})
        self.assertEqual(gvariant_text.dict_numbers(self.TRACK),
                         {"Duration": 209000, "TrackNumber": 12, "NumberOfTracks": 13})

    def test_text_that_looks_like_an_entry_is_not_one(self):
        raw = ("(<{'Title': <\"'Duration': <uint32 5>, 'Artist': <'x'>\">, "
               "'Album': <'Artist'>, 'Duration': <uint32 1000>}>,)")
        self.assertEqual(gvariant_text.dict_strings(raw),
                         {"Title": "'Duration': <uint32 5>, 'Artist': <'x'>", "Album": "Artist"})
        self.assertEqual(gvariant_text.dict_numbers(raw), {"Duration": 1000})

    def test_empty_and_malformed(self):
        for raw in ("", None, "(<@a{sv} {}>,)", "(<{'Title': <'cut off", "Error: GDBus.Error:org.bluez.Error.Failed"):
            with self.subTest(raw=raw):
                self.assertEqual(gvariant_text.dict_strings(raw), {})
                self.assertEqual(gvariant_text.dict_numbers(raw), {})


class NumberTests(unittest.TestCase):
    def test_annotated_numbers(self):
        self.assertEqual(gvariant_text.variant_number("(<uint32 215000>,)", -1), 215000)
        self.assertEqual(gvariant_text.variant_number("(<byte 0x55>,)", -1), 85)
        self.assertEqual(gvariant_text.variant_number("(<byte 0x64>,)", -1), 100)
        self.assertEqual(gvariant_text.variant_number("(<byte 0x00>,)", -1), 0)
        self.assertEqual(gvariant_text.variant_number("(<-3>,)", 0), -3)
        self.assertEqual(gvariant_text.variant_number("(<uint64 4294967296>,)", 0), 4294967296)

    def test_missing_number_uses_the_default(self):
        for raw in ("", None, "(<'text'>,)", "Error: GDBus.Error:org.freedesktop.DBus.Error.InvalidArgs"):
            with self.subTest(raw=raw):
                self.assertEqual(gvariant_text.variant_number(raw, -1), -1)


if __name__ == "__main__":
    unittest.main()
