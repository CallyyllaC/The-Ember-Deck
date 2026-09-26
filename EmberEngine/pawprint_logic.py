"""Pure physical-control decisions used by Pawprint.

This module deliberately has no GPIO, D-Bus, shared-memory, or Raspberry Pi
imports.  Keeping the decision rules here makes the appliance behaviour
testable without pretending that a development machine has Ember Deck
hardware attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple


MEDIA_TARGET_NONE = 0
MEDIA_TARGET_PLEX = 1
MEDIA_TARGET_BLUETOOTH = 2
MEDIA_TARGET_MPRIS = 3

HDMI2_SOURCE_PLEX = 1
HDMI2_SOURCE_RADIO = 2
HDMI2_SOURCE_YOUTUBE = 3


@dataclass(frozen=True)
class MediaRoutingState:
    """Current facts needed to select exactly one transport destination."""

    plex_available: bool = False
    plex_playing: bool = False
    plex_paused: bool = False
    bluetooth_claimed: bool = False
    bluetooth_playing: bool = False
    mpris_available: bool = False
    mpris_playing: bool = False
    hdmi2_source: int = HDMI2_SOURCE_PLEX
    last_target: int = MEDIA_TARGET_NONE


def choose_media_target(state: MediaRoutingState) -> Tuple[int, str]:
    """Return one deterministic target and an operator-facing reason.

    Active Plex playback is intentionally checked first.  A connected phone or
    desktop MPRIS player can therefore never steal a physical transport command
    while the Deck's Plexamp is playing.
    """

    if state.plex_playing:
        return MEDIA_TARGET_PLEX, "Plex playing"
    if state.bluetooth_playing:
        return MEDIA_TARGET_BLUETOOTH, "Bluetooth playing"
    if state.mpris_playing:
        return MEDIA_TARGET_MPRIS, "MPRIS playing"
    if state.hdmi2_source in (HDMI2_SOURCE_RADIO, HDMI2_SOURCE_YOUTUBE):
        return MEDIA_TARGET_MPRIS, "selected HDMI-2 local"
    if state.plex_paused and state.last_target == MEDIA_TARGET_PLEX:
        return MEDIA_TARGET_PLEX, "Plex paused"
    if state.bluetooth_claimed and state.last_target == MEDIA_TARGET_BLUETOOTH:
        return MEDIA_TARGET_BLUETOOTH, "remembered Bluetooth"
    if state.hdmi2_source == HDMI2_SOURCE_PLEX:
        return MEDIA_TARGET_PLEX, "HDMI-2 Plex selected"
    if state.last_target == MEDIA_TARGET_PLEX and state.plex_available:
        return MEDIA_TARGET_PLEX, "remembered Plex"
    if state.last_target == MEDIA_TARGET_MPRIS and state.mpris_available:
        return MEDIA_TARGET_MPRIS, "remembered MPRIS"
    if state.last_target == MEDIA_TARGET_NONE:
        return MEDIA_TARGET_PLEX, "cold boot default"
    if state.plex_available:
        return MEDIA_TARGET_PLEX, "Plex fallback"
    return MEDIA_TARGET_MPRIS, "MPRIS fallback"


@dataclass
class DebounceState:
    """State for one level-triggered physical input."""

    stable: Optional[int] = None
    candidate: Optional[int] = None
    changed_at: float = 0.0


def debounce_level(
    state: DebounceState,
    level: int,
    now: float,
    debounce_s: float,
) -> Tuple[bool, bool]:
    """Update a debouncer and return ``(accepted, rejected_candidate)``.

    Initial state is sampled without generating an action.  A candidate which
    returns to the stable level before the interval expires is counted as a
    rejected/bounced edge, which gives diagnostics a useful signal without
    logging every polling iteration.
    """

    level = int(level)
    rejected = False
    if state.candidate != level:
        rejected = (
            state.stable is not None
            and state.candidate is not None
            and state.candidate != state.stable
            and level == state.stable
        )
        state.candidate = level
        state.changed_at = float(now)

    if state.stable is None:
        state.stable = level
        return False, rejected

    if state.stable != state.candidate and now - state.changed_at >= max(0.0, float(debounce_s)):
        state.stable = int(state.candidate)
        return True, rejected
    return False, rejected


def selector_position(bits: int, lookup: Mapping[int, int]) -> Optional[int]:
    """Translate an electrical selector bit pattern into a physical position."""

    return lookup.get(int(bits))


def hold_reached(started_at: Optional[float], now: float, threshold_s: float) -> bool:
    """Return whether a continuously held input reached its action threshold."""

    return started_at is not None and now - started_at >= max(0.0, float(threshold_s))


def bluetooth_transport_action(button: str, playing: bool) -> str:
    """Map one physical tape key to exactly one BlueZ MediaPlayer1 method name."""

    if button == "play":
        return "pause" if playing else "play"
    return {"stop": "stop", "ff": "next", "rew": "previous"}.get(button, "")


def remap_normalised_voltage(value: float, minimum: float, maximum: float, curve: float) -> float:
    """Map a normalised ADS1115 voltage span to the semantic 0..1 range."""

    minimum = float(minimum)
    maximum = float(maximum)
    if maximum <= minimum:
        raise ValueError("input maximum must be greater than input minimum")
    normalised = max(0.0, min(1.0, (float(value) - minimum) / (maximum - minimum)))
    return normalised ** max(0.05, float(curve))
