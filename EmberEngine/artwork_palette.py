"""Semantic colour-palette extraction for decoded album artwork.

The extractor deliberately describes the whole cover rather than averaging
spatial quadrants. Its four results are dominant, accent, dark and light;
hue-aware output-safe variants are available for the Deck's LEDs and UI.
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

Rgb8 = Tuple[int, int, int]
RgbFloat = Tuple[float, float, float]

MAXIMUM_ACCENT_LIGHTNESS = 0.72
MINIMUM_DISTINCT_ACCENT_DISTANCE = 0.035
BORDER_WEIGHT = 0.15

ROLE_MINIMUM_SATURATION = (0.55, 0.65, 0.70, 0.30)
ROLE_MINIMUM_VALUE = (0.28, 0.48, 0.10, 0.75)


@dataclass(frozen=True)
class OutputPalettePolicy:
    """Visibility floors for dominant, accent, dark and light output roles."""

    near_black_maximum_value: float = 0.05
    near_black_saturation: float = 0.78
    grayscale_maximum_saturation: float = 0.12
    pale_grayscale_minimum_value: float = 0.60
    pure_black_gray_value: float = 0.035
    minimum_saturation: Tuple[
        float, float, float, float
    ] = ROLE_MINIMUM_SATURATION
    minimum_value: Tuple[float, float, float, float] = ROLE_MINIMUM_VALUE


@dataclass(frozen=True)
class ArtworkPalette:
    """Raw semantic colours selected from one piece of artwork."""

    dominant: Rgb8
    accent: Rgb8
    dark: Rgb8
    light: Rgb8

    def raw_colours(self) -> List[RgbFloat]:
        """Return unmodified semantic colours in Deck role order."""
        return [
            _to_float_rgb(self.dominant),
            _to_float_rgb(self.accent),
            _to_float_rgb(self.dark),
            _to_float_rgb(self.light),
        ]

    def output_colours(
        self,
        policy: OutputPalettePolicy = OutputPalettePolicy(),
    ) -> List[RgbFloat]:
        """Return hue-aware, output-safe colours in Deck role order."""
        return normalise_output_colours(self.raw_colours(), policy)


def normalise_output_colours(
    colours: Sequence[RgbFloat],
    policy: OutputPalettePolicy = OutputPalettePolicy(),
) -> List[RgbFloat]:
    """Make palette roles visible without destroying their colour identity.

    Exact black becomes neutral dark gray. Near-black and dark grayscale roles
    borrow the best chromatic artwork hue. Pale neutrals remain untouched, and
    existing chromatic colours keep their hue while only gaining saturation or
    value where their semantic role needs it.
    """
    if len(colours) != 4:
        raise ValueError("output palette must contain four colours")

    source = [tuple(_unit(channel) for channel in colour) for colour in colours]
    reference_hue = _reference_hue(source, policy)
    output: List[RgbFloat] = []

    for index, colour in enumerate(source):
        if max(colour) == 0.0:
            gray = _unit(policy.pure_black_gray_value)
            output.append((gray, gray, gray))
            continue

        hue, saturation, value = colorsys.rgb_to_hsv(*colour)
        minimum_saturation = _unit(policy.minimum_saturation[index])
        minimum_value = _unit(policy.minimum_value[index])

        if (
            saturation <= _unit(policy.grayscale_maximum_saturation)
            and value >= _unit(policy.pale_grayscale_minimum_value)
        ):
            output.append(colour)
            continue

        if value < _unit(policy.near_black_maximum_value):
            if reference_hue is None:
                saturation = 0.0
            else:
                hue = reference_hue
                saturation = _unit(policy.near_black_saturation)
            value = max(value, minimum_value)
        elif saturation <= _unit(policy.grayscale_maximum_saturation):
            if reference_hue is None:
                saturation = 0.0
            else:
                hue = reference_hue
                saturation = minimum_saturation
            value = max(value, minimum_value)
        else:
            saturation = max(saturation, minimum_saturation)
            value = max(value, minimum_value)

        output.append(colorsys.hsv_to_rgb(hue, saturation, value))

    return output


def _reference_hue(
    colours: Sequence[RgbFloat],
    policy: OutputPalettePolicy,
) -> Optional[float]:
    # Accent is normally the strongest chromatic reference, followed by the
    # dominant, light and dark roles.
    for index in (1, 0, 3, 2):
        hue, saturation, value = colorsys.rgb_to_hsv(*colours[index])
        if (
            value >= _unit(policy.near_black_maximum_value)
            and saturation > _unit(policy.grayscale_maximum_saturation)
        ):
            return hue
    return None


def _unit(value: object) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True)
class _AnalysedColour:
    colour: Rgb8
    weight: float
    saturation: float
    hsl_lightness: float
    relative_luminance: float


def extract_palette(
    pixels: Sequence[int],
    width: int,
    height: int,
    *,
    stride_width: Optional[int] = None,
) -> Optional[ArtworkPalette]:
    """Extract dominant, accent, dark and light colours from packed RGB bytes.

    ``stride_width`` permits a smaller valid image to live in the top-left of
    the Deck's fixed 48-pixel-wide shared-memory canvas.
    """
    width = int(width)
    height = int(height)
    stride_width = width if stride_width is None else int(stride_width)
    if width <= 0 or height <= 0 or stride_width < width:
        raise ValueError("RGB pixel dimensions must be positive and fit the stride")
    if len(pixels) < stride_width * height * 3:
        raise ValueError("RGB pixel data does not match the supplied dimensions")

    border_width = max(1, round(width * 0.05))
    border_height = max(1, round(height * 0.05))
    buckets: Dict[int, List[float]] = {}

    for y in range(height):
        row = y * stride_width * 3
        for x in range(width):
            offset = row + x * 3
            red = _byte(pixels[offset])
            green = _byte(pixels[offset + 1])
            blue = _byte(pixels[offset + 2])
            key = ((red >> 4) << 8) | ((green >> 4) << 4) | (blue >> 4)
            is_border = (
                x < border_width
                or x >= width - border_width
                or y < border_height
                or y >= height - border_height
            )
            weight = BORDER_WEIGHT if is_border else 1.0
            bucket = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0])
            bucket[0] += red * weight
            bucket[1] += green * weight
            bucket[2] += blue * weight
            bucket[3] += weight

    if not buckets:
        return None

    colours = [_finish_bucket(bucket) for bucket in buckets.values()]
    dominant = max(colours, key=lambda colour: colour.weight)
    return ArtworkPalette(
        dominant=dominant.colour,
        accent=_select_accent(colours, dominant),
        dark=_select_dark(colours, dominant),
        light=_select_light(colours, dominant),
    )


def _byte(value: object) -> int:
    result = int(value)
    if not 0 <= result <= 255:
        raise ValueError("RGB channels must be bytes")
    return result


def _finish_bucket(bucket: Sequence[float]) -> _AnalysedColour:
    weight = bucket[3]
    colour = (
        round(bucket[0] / weight),
        round(bucket[1] / weight),
        round(bucket[2] / weight),
    )
    _, lightness, saturation = colorsys.rgb_to_hls(*_to_float_rgb(colour))
    return _AnalysedColour(
        colour=colour,
        weight=weight,
        saturation=saturation,
        hsl_lightness=lightness,
        relative_luminance=_relative_luminance(colour),
    )


def _select_accent(
    colours: Sequence[_AnalysedColour],
    dominant: _AnalysedColour,
) -> Rgb8:
    distinct = [
        colour
        for colour in colours
        if colour.colour != dominant.colour
        and _perceptual_distance(colour.colour, dominant.colour)
        >= MINIMUM_DISTINCT_ACCENT_DISTANCE
    ]
    if not distinct:
        return _rotate_hue(dominant.colour, 180.0)

    eligible = [
        colour
        for colour in distinct
        if colour.hsl_lightness <= MAXIMUM_ACCENT_LIGHTNESS
    ]
    candidates = eligible or distinct
    maximum_weight = max(colour.weight for colour in candidates)

    def score(colour: _AnalysedColour) -> float:
        population = math.sqrt(colour.weight / maximum_weight)
        chroma = math.pow(max(colour.saturation, 0.01), 1.35)
        distance = _perceptual_distance(colour.colour, dominant.colour)
        pale_penalty = (
            0.05 if colour.hsl_lightness > MAXIMUM_ACCENT_LIGHTNESS else 1.0
        )
        return population * chroma * distance * pale_penalty

    return max(candidates, key=score).colour


def _select_dark(
    colours: Sequence[_AnalysedColour],
    dominant: _AnalysedColour,
) -> Rgb8:
    candidates = [
        colour for colour in colours if colour.relative_luminance <= 0.32
    ]
    if not candidates:
        return tuple(round(channel * 0.28) for channel in dominant.colour)

    return max(
        candidates,
        key=lambda colour: (
            math.pow(colour.weight, 0.4)
            * math.pow(1.05 - colour.relative_luminance, 2)
            * (0.35 + _perceptual_distance(colour.colour, dominant.colour))
        ),
    ).colour


def _select_light(
    colours: Sequence[_AnalysedColour],
    dominant: _AnalysedColour,
) -> Rgb8:
    candidates = [
        colour for colour in colours if colour.relative_luminance >= 0.68
    ]
    if not candidates:
        return tuple(
            round(channel + (255 - channel) * 0.65)
            for channel in dominant.colour
        )

    return max(
        candidates,
        key=lambda colour: (
            math.pow(colour.weight, 0.4)
            * math.pow(colour.relative_luminance + 0.05, 2)
            * (0.35 + _perceptual_distance(colour.colour, dominant.colour))
        ),
    ).colour


def _perceptual_distance(first: Rgb8, second: Rgb8) -> float:
    first_lab = _to_oklab(first)
    second_lab = _to_oklab(second)
    return math.sqrt(
        sum((left - right) ** 2 for left, right in zip(first_lab, second_lab))
    )


def _to_oklab(colour: Rgb8) -> Tuple[float, float, float]:
    red, green, blue = (_to_linear(value / 255.0) for value in colour)
    l_value = math.cbrt(
        0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    )
    m_value = math.cbrt(
        0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    )
    s_value = math.cbrt(
        0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    )
    return (
        0.2104542553 * l_value + 0.7936177850 * m_value - 0.0040720468 * s_value,
        1.9779984951 * l_value - 2.4285922050 * m_value + 0.4505937099 * s_value,
        0.0259040371 * l_value + 0.7827717662 * m_value - 0.8086757660 * s_value,
    )


def _relative_luminance(colour: Rgb8) -> float:
    red, green, blue = (_to_linear(value / 255.0) for value in colour)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else math.pow((value + 0.055) / 1.055, 2.4)


def _rotate_hue(colour: Rgb8, degrees: float) -> Rgb8:
    hue, lightness, saturation = colorsys.rgb_to_hls(*_to_float_rgb(colour))
    return _from_hls((hue + degrees / 360.0) % 1.0, lightness, saturation)


def _from_hls(hue: float, lightness: float, saturation: float) -> Rgb8:
    return tuple(
        round(channel * 255.0)
        for channel in colorsys.hls_to_rgb(hue, lightness, saturation)
    )


def _to_float_rgb(colour: Rgb8) -> RgbFloat:
    return tuple(channel / 255.0 for channel in colour)
