"""Read the few values Ember Deck needs from ``gdbus call`` output.

``gdbus call`` prints its result with ``g_variant_print(result, TRUE)``
(GLib ``gio/gdbus-tool.c``). The rules below are GLib's, from
``g_variant_print_string`` in ``glib/gvariant.c``:

* A string is quoted with ``'`` unless it contains ``'``, in which case it is
  quoted with ``"``. Inside, the chosen quote and ``\\`` are escaped with a
  backslash. Non-printable characters become ``\\a \\b \\f \\n \\r \\t \\v``,
  ``\\uXXXX`` or ``\\UXXXXXXXX``.
* With type annotations, a byte prints as ``byte 0x55`` (hexadecimal) and a
  uint32 as ``uint32 215000``. Plain strings and int32 are not annotated.

So ``'Title': <"Don't Stop Me Now">`` and ``(<byte 0x55>,)`` are both normal
output. This is a reader for that text, not a full GVariant parser: it finds
string literals, ``'key': <value>`` dictionary entries and single numbers.
No D-Bus library is needed.
"""

from __future__ import annotations

import re
from string import hexdigits
from typing import Dict, Iterator, List, Optional, Tuple

_LETTER_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_QUOTES = ("'", '"')

# An optional type annotation, then a hexadecimal or decimal integer.
_NUMBER = r"(?:(?:byte|int16|uint16|int32|uint32|int64|uint64|handle)\s+)?(0[xX][0-9a-fA-F]+|-?\d+)"
_VARIANT_NUMBER = re.compile(r"<\s*" + _NUMBER + r"\s*>")
_ENTRY_STRING = re.compile(r"\s*:\s*<\s*$")
_ENTRY_NUMBER = re.compile(r"\s*:\s*<\s*" + _NUMBER + r"\s*>")
_ENTRY_CLOSE = re.compile(r"\s*>")


def _to_int(token: str) -> int:
    return int(token, 16) if token[:2].lower() == "0x" else int(token, 10)


def _read_string(text: str, start: int) -> Optional[Tuple[str, int]]:
    """Decode the literal opening at ``text[start]``; return (value, end) or None."""
    quote = text[start]
    out: List[str] = []
    i = start + 1
    while i < len(text):
        c = text[i]
        if c == quote:
            return "".join(out), i + 1
        if c == "\\" and i + 1 < len(text):
            escape = text[i + 1]
            if escape in _LETTER_ESCAPES:
                out.append(_LETTER_ESCAPES[escape])
                i += 2
                continue
            if escape in "uU":
                width = 4 if escape == "u" else 8
                digits = text[i + 2:i + 2 + width]
                if len(digits) == width and all(d in hexdigits for d in digits):
                    code = int(digits, 16)
                    valid = code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF
                    out.append(chr(code) if valid else "\ufffd")
                    i += 2 + width
                    continue
            # \\, \' and \" (and any other escaped character) stand for themselves.
            out.append(escape)
            i += 2
            continue
        out.append(c)
        i += 1
    return None  # unterminated: not trustworthy


def string_literals(text: str) -> Iterator[Tuple[int, int, str]]:
    """Yield ``(start, end, value)`` for each quoted string, left to right."""
    text = text or ""
    i = 0
    while i < len(text):
        if text[i] in _QUOTES:
            literal = _read_string(text, i)
            if literal is None:
                return
            value, end = literal
            yield i, end, value
            i = end
        else:
            i += 1


def first_string(text: str) -> Optional[str]:
    """The first string literal, e.g. ``(<'playing'>,)`` -> ``playing``."""
    for _, _, value in string_literals(text):
        return value
    return None


def dict_strings(text: str) -> Dict[str, str]:
    """String values of ``'key': <'value'>`` entries (the first one wins)."""
    text = text or ""
    literals = list(string_literals(text))
    found: Dict[str, str] = {}
    for (_, key_end, key), (value_start, value_end, value) in zip(literals, literals[1:]):
        if (_ENTRY_STRING.match(text[key_end:value_start])
                and _ENTRY_CLOSE.match(text, value_end)
                and key not in found):
            found[key] = value
    return found


def dict_numbers(text: str) -> Dict[str, int]:
    """Integer values of ``'key': <uint32 N>`` (or ``byte 0x..``) entries."""
    text = text or ""
    found: Dict[str, int] = {}
    for _, key_end, key in string_literals(text):
        match = _ENTRY_NUMBER.match(text, key_end)
        if match and key not in found:
            found[key] = _to_int(match.group(1))
    return found


def variant_number(text: str, default: int) -> int:
    """The number inside the first ``<...>``, e.g. ``(<byte 0x55>,)`` -> 85."""
    match = _VARIANT_NUMBER.search(text or "")
    return _to_int(match.group(1)) if match else int(default)
