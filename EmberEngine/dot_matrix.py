"""Large dot-matrix text for the panel UI, drawn with Braille patterns.

A terminal has one font size, so large text has to be drawn. Each Braille
character (U+2800–U+28FF) is a 2 × 4 grid of dots inside one cell. On the
Deck's roughly 1:2 terminal cells those dots are square, so a 5 × 7 glyph
becomes a capital about two and a half times the height of normal text, in
the style of a vintage LED sign.

Each line of large text is two terminal rows: seven rows of dots for the
glyph and one blank row of dots below it, which separates stacked lines.

The font is capitals, digits and common punctuation, drawn for this project.
Lower case is shown as capitals and accents are dropped (é → E). Text with
any other character (CJK, emoji, symbols) cannot be drawn: render_lines()
returns None and the caller shows it as normal text instead.
"""

from __future__ import annotations

import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

GLYPH_ROWS = 7
LINE_DOTS = 8  # glyph rows plus one blank row; two Braille cells tall
CHAR_GAP = 1   # blank dot columns between glyphs
SPACE_W = 3    # dot columns for a space
# Dot columns per character in ordinary text, gap included, for estimates.
TYPICAL_ADVANCE = 6

_GLYPHS: Dict[str, Tuple[str, ...]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": ("###", ".#.", ".#.", ".#.", ".#.", ".#.", "###"),
    "J": ("..###", "...#.", "...#.", "...#.", "#..#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": (".#.", "##.", ".#.", ".#.", ".#.", ".#.", "###"),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("####.", "....#", "....#", ".###.", "....#", "....#", "####."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "!": ("#", "#", "#", "#", "#", ".", "#"),
    "?": (".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."),
    ".": (".", ".", ".", ".", ".", ".", "#"),
    ",": ("..", "..", "..", "..", ".#", ".#", "#."),
    ":": (".", ".", "#", ".", ".", "#", "."),
    ";": ("..", "..", ".#", "..", ".#", ".#", "#."),
    "'": ("#", "#", ".", ".", ".", ".", "."),
    '"': ("#.#", "#.#", "...", "...", "...", "...", "..."),
    "-": ("....", "....", "....", "####", "....", "....", "...."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    "(": ("..#", ".#.", "#..", "#..", "#..", ".#.", "..#"),
    ")": ("#..", ".#.", "..#", "..#", "..#", ".#.", "#.."),
    "[": ("###", "#..", "#..", "#..", "#..", "#..", "###"),
    "]": ("###", "..#", "..#", "..#", "..#", "..#", "###"),
    "/": ("....#", "...#.", "...#.", "..#..", ".#...", ".#...", "#...."),
    "\\": ("#....", ".#...", ".#...", "..#..", "...#.", "...#.", "....#"),
    "&": (".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    "*": (".....", "#.#.#", ".###.", "#####", ".###.", "#.#.#", "....."),
    "#": (".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."),
    "%": ("##...", "##..#", "...#.", "..#..", ".#...", "#..##", "...##"),
    "@": (".###.", "#...#", "#.###", "#.#.#", "#.###", "#....", ".####"),
    "$": ("..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."),
    "<": ("...#", "..#.", ".#..", "#...", ".#..", "..#.", "...#"),
    ">": ("#...", ".#..", "..#.", "...#", "..#.", ".#..", "#..."),
    "~": (".....", ".....", ".#...", "#.#.#", "...#.", ".....", "....."),
    "|": ("#", "#", "#", "#", "#", "#", "#"),
}

# Typographic punctuation in lyrics, mapped onto the glyphs above.
_SUBSTITUTES = {
    "‘": "'", "’": "'", "‚": ",", "′": "'", "`": "'", "´": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "–": "-", "—": "-", "‐": "-", "‑": "-", "−": "-",
    "…": "...", "·": ".", "•": ".",
    " ": " ", " ": " ", "　": " ",
    "ß": "SS", "Æ": "AE", "æ": "AE", "Œ": "OE", "œ": "OE", "Ø": "O", "ø": "O",
}

# Braille dot bits, indexed [dot row][dot column] inside one cell.
_DOT_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def normalise(text: str) -> Optional[str]:
    """Upper-case text the font can draw, or None if any character is missing."""
    out: List[str] = []
    for char in " ".join(str(text).split()):
        char = _SUBSTITUTES.get(char, char)
        for part in char:
            if part in _GLYPHS or part == " ":
                out.append(part)
                continue
            upper = part.upper()
            if len(upper) == 1 and upper in _GLYPHS:
                out.append(upper)
                continue
            # Drop accents: é → E + combining acute → E.
            base = "".join(c for c in unicodedata.normalize("NFKD", upper)
                           if not unicodedata.combining(c))
            if base and all(c in _GLYPHS for c in base):
                out.append(base)
                continue
            return None
    return "".join(out)


def word_width(word: str) -> int:
    """Width in dot columns of one word with gaps between its glyphs."""
    if not word:
        return 0
    return sum(len(_GLYPHS[c][0]) for c in word) + CHAR_GAP * (len(word) - 1)


def _wrap(text: str, max_dots: int) -> Optional[List[str]]:
    """Word-wrap normalised text to dot-column lines; None if a word cannot fit."""
    lines: List[str] = []
    current = ""
    for word in text.split(" "):
        if not word:
            continue
        if word_width(word) > max_dots:
            return None
        candidate = f"{current} {word}" if current else word
        if current and _line_width(candidate) > max_dots:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _line_width(line: str) -> int:
    """Width in dot columns, matching _bitmap(): a space is SPACE_W plus one gap."""
    words = line.split(" ")
    return sum(word_width(w) for w in words) + (SPACE_W + CHAR_GAP) * (len(words) - 1)


def _bitmap(line: str) -> List[List[bool]]:
    """Dot rows for one line of text, LINE_DOTS tall."""
    columns: List[Sequence[bool]] = []
    blank = [False] * GLYPH_ROWS
    for index, char in enumerate(line):
        if char == " ":
            columns.extend([blank] * (SPACE_W + CHAR_GAP))
            continue
        glyph = _GLYPHS[char]
        for x in range(len(glyph[0])):
            columns.append([glyph[y][x] == "#" for y in range(GLYPH_ROWS)])
        if index != len(line) - 1 and line[index + 1] != " ":
            columns.extend([blank] * CHAR_GAP)
    rows = [[column[y] for column in columns] for y in range(GLYPH_ROWS)]
    rows.extend([[False] * len(columns) for _ in range(LINE_DOTS - GLYPH_ROWS)])
    return rows


def _to_braille(rows: List[List[bool]]) -> Tuple[str, str]:
    """Pack an 8-dot-tall bitmap into two rows of Braille cells."""
    width = len(rows[0]) if rows else 0
    out = []
    for cell_row in range(2):
        chars = []
        for cell_x in range(0, width, 2):
            bits = 0
            for dy in range(4):
                for dx in range(2):
                    x = cell_x + dx
                    if x < width and rows[cell_row * 4 + dy][x]:
                        bits |= _DOT_BITS[dy][dx]
            chars.append(chr(0x2800 + bits) if bits else " ")
        out.append("".join(chars).rstrip())
    return out[0], out[1]


def render_lines(text: str, max_cells: int, max_lines: int) -> Optional[List[Tuple[str, str]]]:
    """Draw ``text`` as large lines, each a pair of terminal rows.

    Returns None when the text has a character the font lacks, a word wider
    than ``max_cells``, or needs more than ``max_lines`` lines. Callers then
    show normal text, so nothing is ever cut off.
    """
    normalised = normalise(text)
    if not normalised or not normalised.strip():
        return None
    lines = _wrap(normalised, max(1, int(max_cells)) * 2)
    if not lines or len(lines) > max_lines:
        return None
    return [_to_braille(_bitmap(line)) for line in lines]
