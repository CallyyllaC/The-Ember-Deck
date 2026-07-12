"""
spirit_ink

Personality and semantic styling for Ember Deck's structured log messages.
Workers publish facts; this module decides how those facts are spoken.
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field
from typing import Any

from spirit_messages import RenderedMessage, SpiritMessage, StyledFragment


PROCESS_NAMES = {
    "whisper": "Whisper",
    "whisper_daemon": "Whisper",
    "pawprint": "Pawprint",
    "minstrel": "Minstrel",
    "foxfire": "Foxfire",
    "aurora": "Aurora",
    "echo": "Echo",
    "willo_wisp": "Will-o-Wisp",
    "mpris_bridge": "MPRIS Bridge",
    "hdmi2_source_controller": "HDMI-2",
    "foundrycore": "Foundry",
}

COMPOSITION_MODES = {
    "direct",
    "compact",
    "combined",
    "narrative",
    "delayed_chain",
    "ensemble",
    "clinical",
}


@dataclass(slots=True)
class TemplateSpec:
    """One selectable rendering variant for a structured event."""

    lines: list[str] = field(default_factory=list)
    fragments: list[tuple[str, str]] = field(default_factory=list)
    composition_modes: set[str] = field(default_factory=lambda: {"direct"})
    processes: set[str] | None = None
    moods: set[str] | None = None
    required_metadata: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Legacy pools
# ---------------------------------------------------------------------------
# These remain available to the old dress_* helpers while the final
# Pawprint.status_note compatibility publisher still exists. Structured
# logging uses TEMPLATES below.

INFO_POOLS = {
    "starting": [
        "{process} stirs behind the panels.",
        "{process} gathers its limbs and prepares for duty.",
        "{process} has been summoned back to work.",
    ],
    "started": [
        "{process} joins the household.",
        "{process} is awake and pretending this was voluntary.",
        "{process} reports for duty with suspicious composure.",
    ],
    "stopping": [
        "{process} begins putting its tools away.",
        "{process} takes the hint and starts shutting down.",
        "{process} withdraws from the machinery.",
    ],
    "stopping_with_force": [
        "{process} missed the hint. The shutdown has become persuasive.",
        "{process} refuses the graceful exit; force will do.",
        "{process} is being removed from the stage by management.",
    ],
    "stopped": [
        "{process} has gone quiet.",
        "{process} leaves only warm circuits behind.",
        "{process} is absent from the household.",
    ],
    "cfg_loaded": [
        "{process} accepts the new rules without reading the terms.",
        "{process} studies the revised instructions and finds them suspicious.",
        "{process} absorbs its latest set of demands.",
    ],
    "enter_main_loop": [
        "{process} settles into its watch.",
        "{process} takes its post.",
        "{process} begins the endless little ritual.",
    ],
    "url_found": [
        "{process} finds the service entrance.",
        "{process} locates the address it was promised.",
        "{process} finds a door that answers.",
    ],
    "user_found": [
        "{process} recognises the household.",
        "{process} recovers the identity it was given.",
        "{process} finds the right name to use.",
    ],
    "led_waiting": [
        "{process} waits for someone to count the lights.",
        "{process} has the enthusiasm, but not yet the dimensions.",
        "{process} waits for the LED map before setting anything ablaze.",
    ],
    "audio_stream_started": [
        "{process} opens the audio path.",
        "{process} lets the signal through.",
        "{process} has sound in its veins again.",
    ],
}

ERROR_TEMPLATES = [
    "{process} bares its teeth: {summary}. {details}",
    "{process} has found a fresh way to suffer: {summary}. {details}",
    "{process} objects loudly: {summary}. {details}",
]

MEDIA_TEMPLATES = {
    "track_change": [
        "{process} turns the room toward {track} by {artist}.",
        "{process} places {track} by {artist} at the centre of the room.",
        "{process} lets {track} take the lead.",
    ],
    "play": [
        "{process} lets {track} loose again.",
        "The pause breaks. {track} returns.",
        "{process} puts the room back in motion.",
    ],
    "pause": [
        "{process} catches {track} mid-step.",
        "The music is held exactly where it was.",
        "{process} arrests the rhythm.",
    ],
    "stop": [
        "{process} dismisses the music.",
        "Playback ends. The room keeps the echo.",
        "{process} closes the session and leaves the silence to explain itself.",
    ],
}

EVENT_POOLS = {
    "connected": [
        "{process} has a line into {server}.",
        "{process} finds {server} willing to answer.",
        "{process} opens a path into {server}'s archives.",
    ],
    "lyrics_found": [
        "{process} finds the words to {track}.",
        "{process} pulls the lyrics for {track} into the light.",
        "Apparently {track} did have something to say.",
    ],
    "led_connected": [
        "{process} counts {led_count} lights and claims all of them.",
        "{process} finds {led_count} LEDs waiting at {fps} FPS.",
        "{process}'s little army is present: {led_count} lights at {fps} FPS.",
    ],
    "init_audio_busses": [
        "{process} lays out {bins} spectral bins and tries not to look smug.",
        "{process} divides the sound into {bins} pieces at {sr} Hz.",
        "{process} maps {bins} places for the spectrum to misbehave.",
    ],
    "audio_stream_connected": [
        "{process} catches the stream from {device}: {channels} channels at {sr} Hz.",
        "{process} pins down the audio source on {device}.",
        "{process} has the signal from {device} firmly in hand.",
    ],
    "init_led_busses": [
        "{process} maps the bed around {led_count} lights.",
        "{process} marks out {led_count} places for fire at {fps} FPS.",
        "{process} gives every one of the {led_count} lights somewhere to stand.",
    ],
}

OPINIONS = {
    "track_change": [
        "{process} tests the pulse of {track}.",
        "{process} finds something worth watching in {track}.",
        "{process} considers {track} and withholds judgement for dramatic effect.",
    ],
    "play": [
        "{process} wakes with the beat.",
        "{process} settles back into the rhythm.",
        "{process} approves, though not in writing.",
    ],
    "pause": [
        "{process} keeps hold of the last moment.",
        "{process} waits beside the halted music.",
        "{process} regards the sudden silence with suspicion.",
    ],
    "stop": [
        "{process} lets the ending settle.",
        "{process} returns to the quiet.",
        "{process} leaves the silence undisturbed.",
    ],
}

HEARTBEAT_TEMPLATES = [
    "{process} remains at its post.",
    "{process} keeps a quiet pulse.",
    "{process} is still here.",
]


# ---------------------------------------------------------------------------
# Structured template registry
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, list[TemplateSpec]] = {}


def _line_specs(pool: dict[str, list[str]], *, modes: set[str] | None = None) -> None:
    """Handle the line specs lifecycle step."""
    selected_modes = modes or {"direct", "compact", "narrative", "clinical"}
    for event, lines in pool.items():
        TEMPLATES[event] = [
            TemplateSpec(lines=lines, composition_modes=set(selected_modes))
        ]


_line_specs(INFO_POOLS)
_line_specs(EVENT_POOLS)


# Media gets several genuinely different shapes rather than merely carrying a
# composition label that changes nothing.
TEMPLATES["track_change"] = [
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" turns the room toward ", "secondary"),
            ("“{track}”", "title"),
            (" by ", "secondary"),
            ("{artist}", "artist"),
            (".", "secondary"),
        ],
        composition_modes={"direct"},
        required_metadata={"track", "artist"},
    ),
    TemplateSpec(
        fragments=[
            ("“{track}”", "title"),
            (" by ", "secondary"),
            ("{artist}", "artist"),
            (". ", "secondary"),
            ("{process}", "speaker"),
            (" has spoken.", "secondary"),
        ],
        composition_modes={"compact"},
        required_metadata={"track", "artist"},
    ),
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" places ", "secondary"),
            ("“{track}”", "title"),
            (" by ", "secondary"),
            ("{artist}", "artist"),
            (" at the centre of the room.", "secondary"),
        ],
        composition_modes={"narrative"},
        required_metadata={"track", "artist"},
    ),
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" begins ", "secondary"),
            ("“{track}”", "title"),
            (". The household gathers around it.", "secondary"),
        ],
        composition_modes={"combined", "ensemble", "delayed_chain"},
        required_metadata={"track"},
    ),
    TemplateSpec(
        fragments=[
            ("Playback changed: ", "secondary"),
            ("“{track}”", "title"),
            (" by ", "secondary"),
            ("{artist}", "artist"),
            (".", "secondary"),
        ],
        composition_modes={"clinical"},
        required_metadata={"track", "artist"},
    ),
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" turns the room toward ", "secondary"),
            ("“{track}”", "title"),
            (".", "secondary"),
        ],
        composition_modes=set(COMPOSITION_MODES),
        required_metadata={"track"},
    ),
    TemplateSpec(
        lines=["{process} chooses the next track."],
        composition_modes=set(COMPOSITION_MODES),
    ),
]

TEMPLATES["play"] = [
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" lets ", "secondary"),
            ("“{track}”", "title"),
            (" loose again.", "secondary"),
        ],
        composition_modes={"direct", "compact"},
        required_metadata={"track"},
    ),
    TemplateSpec(
        fragments=[
            ("The pause breaks. ", "secondary"),
            ("“{track}”", "title"),
            (" returns, and the household moves with it.", "secondary"),
        ],
        composition_modes={"narrative", "combined", "ensemble"},
        required_metadata={"track"},
    ),
    TemplateSpec(
        lines=[
            "{process} puts the room back in motion.",
            "Playback resumes. The silence loses its argument.",
        ],
        composition_modes=set(COMPOSITION_MODES),
    ),
]

TEMPLATES["pause"] = [
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" catches ", "secondary"),
            ("“{track}”", "title"),
            (" mid-step.", "secondary"),
        ],
        composition_modes={"direct", "compact"},
        required_metadata={"track"},
    ),
    TemplateSpec(
        lines=[
            "The music is held exactly where it was.",
            "{process} arrests the rhythm; the room keeps the tension.",
        ],
        composition_modes={"narrative", "combined", "ensemble"},
    ),
    TemplateSpec(
        lines=["Playback paused."],
        composition_modes={"clinical"},
    ),
    TemplateSpec(
        lines=["{process} holds the moment."],
        composition_modes=set(COMPOSITION_MODES),
    ),
]

TEMPLATES["stop"] = [
    TemplateSpec(
        lines=[
            "{process} dismisses the music.",
            "{process} closes the session and leaves the silence to explain itself.",
        ],
        composition_modes={"direct", "compact"},
    ),
    TemplateSpec(
        lines=[
            "Playback ends. The room keeps the echo.",
            "The last note leaves; the household settles behind it.",
        ],
        composition_modes={"narrative", "combined", "ensemble"},
    ),
    TemplateSpec(lines=["Playback stopped."], composition_modes={"clinical"}),
]


# Process-aware secondary reactions. Generic variants remain as a fallback for
# any future process names, because even spirits occasionally arrive uninvited.
TEMPLATES["opinion:track_change"] = [
    TemplateSpec(
        lines=[
            "Foxfire tests the pulse of {track}.",
            "Foxfire finds a colour for {track}.",
            "Foxfire catches the rhythm and keeps it.",
        ],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"foxfire"},
        required_metadata={"track"},
    ),
    TemplateSpec(
        lines=[
            "Aurora studies what {track} brought with it.",
            "Aurora gives {track} the room it thinks it deserves.",
            "Aurora watches the scene settle around {track}.",
        ],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"aurora"},
        required_metadata={"track"},
    ),
    TemplateSpec(
        lines=[
            "Pawprint feels the change through the controls.",
            "Pawprint keeps one paw on the new rhythm.",
        ],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"pawprint"},
    ),
    TemplateSpec(
        lines=[
            "Whisper notes the change and says nothing incriminating.",
            "Whisper records the choice for later judgement.",
        ],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"whisper"},
    ),
    TemplateSpec(
        lines=OPINIONS["track_change"],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        required_metadata={"track"},
    ),
]

TEMPLATES["opinion:play"] = [
    TemplateSpec(
        lines=["Foxfire wakes with the beat.", "Foxfire lets the colours move again."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"foxfire"},
    ),
    TemplateSpec(
        lines=["Aurora lets the room breathe again.", "Aurora restores motion to the scene."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"aurora"},
    ),
    TemplateSpec(
        lines=["Pawprint releases the held mechanism.", "Pawprint approves the renewed motion."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"pawprint"},
    ),
    TemplateSpec(
        lines=OPINIONS["play"],
        composition_modes={"direct", "delayed_chain", "ensemble"},
    ),
]

TEMPLATES["opinion:pause"] = [
    TemplateSpec(
        lines=["Foxfire holds the glow.", "Foxfire keeps the last pulse under glass."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"foxfire"},
    ),
    TemplateSpec(
        lines=["Aurora keeps the last frame.", "Aurora lets the stillness remain visible."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"aurora"},
    ),
    TemplateSpec(
        lines=["Pawprint keeps one paw on the moment.", "Pawprint waits beside the halted controls."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"pawprint"},
    ),
    TemplateSpec(
        lines=OPINIONS["pause"],
        composition_modes={"direct", "delayed_chain", "ensemble"},
    ),
]

TEMPLATES["opinion:stop"] = [
    TemplateSpec(
        lines=["Foxfire lets the last light die slowly.", "Foxfire banks the glow."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"foxfire"},
    ),
    TemplateSpec(
        lines=["Aurora leaves the display to its silence.", "Aurora lets the final image fade."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"aurora"},
    ),
    TemplateSpec(
        lines=["Pawprint returns the controls to rest.", "Pawprint relaxes its grip on the mechanism."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"pawprint"},
    ),
    TemplateSpec(
        lines=["Whisper records the quiet.", "Whisper closes the little chapter."],
        composition_modes={"direct", "delayed_chain", "ensemble"},
        processes={"whisper"},
    ),
    TemplateSpec(
        lines=OPINIONS["stop"],
        composition_modes={"direct", "delayed_chain", "ensemble"},
    ),
]


TEMPLATES["error"] = [
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" bares its teeth: ", "secondary"),
            ("{summary}", "error"),
            (". ", "secondary"),
            ("{details}", "secondary"),
        ],
        composition_modes={"direct", "narrative"},
        required_metadata={"summary", "details"},
    ),
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" objects loudly: ", "secondary"),
            ("{summary}", "error"),
            (".", "secondary"),
        ],
        composition_modes={"direct", "compact", "combined", "ensemble"},
        required_metadata={"summary"},
    ),
    TemplateSpec(
        fragments=[
            ("Error in ", "secondary"),
            ("{process}", "speaker"),
            (": ", "secondary"),
            ("{summary}", "error"),
            (". ", "secondary"),
            ("{details}", "secondary"),
        ],
        composition_modes={"clinical"},
        required_metadata={"summary", "details"},
    ),
    TemplateSpec(
        lines=["{process} reports a fault: {summary}."],
        composition_modes=set(COMPOSITION_MODES),
        required_metadata={"summary"},
    ),
]

TEMPLATES["duplicate_summary"] = [
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" reports the same wound ", "secondary"),
            ("×{count}", "count"),
            (".", "secondary"),
        ],
        composition_modes={"direct", "compact", "narrative", "ensemble"},
        required_metadata={"count"},
    ),
    TemplateSpec(
        fragments=[
            ("Repeated fault in ", "secondary"),
            ("{process}", "speaker"),
            (": ", "secondary"),
            ("{fault_event}", "error"),
            (" ", "secondary"),
            ("×{count}", "count"),
            (".", "secondary"),
        ],
        composition_modes={"clinical"},
        required_metadata={"fault_event", "count"},
    ),
]

TEMPLATES["recovery"] = [
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" finds its footing again.", "recovery"),
        ],
        composition_modes={"direct", "compact"},
    ),
    TemplateSpec(
        fragments=[
            ("The wound closes. ", "recovery"),
            ("{process}", "speaker"),
            (" is steady again.", "recovery"),
        ],
        composition_modes={"narrative", "combined", "ensemble"},
    ),
    TemplateSpec(
        fragments=[
            ("{process}", "speaker"),
            (" recovered after ", "recovery"),
            ("{duration_s}s", "duration"),
            (".", "recovery"),
        ],
        composition_modes={"clinical"},
        required_metadata={"duration_s"},
    ),
]


TEMPLATES["heartbeat"] = [
    TemplateSpec(
        lines=[
            "Whisper keeps watch between the lines.",
            "The household is quiet. Whisper does not entirely trust it.",
            "Whisper listens to the machinery breathe.",
            "Whisper keeps the silence properly supervised.",
        ],
        composition_modes=set(COMPOSITION_MODES),
        processes={"whisper"},
    ),
    TemplateSpec(
        lines=[
            "Pawprint keeps one ear on the controls.",
            "Pawprint remains curled around the switches.",
            "Pawprint tests nothing. A rare act of restraint.",
            "Pawprint keeps the buttons out of trouble.",
        ],
        composition_modes=set(COMPOSITION_MODES),
        processes={"pawprint"},
    ),
    TemplateSpec(
        lines=[
            "Minstrel keeps a tune tucked beneath the silence.",
            "Minstrel waits with the next song behind its teeth.",
            "Minstrel counts the quiet in bars.",
            "Minstrel keeps the silence in time.",
        ],
        composition_modes=set(COMPOSITION_MODES),
        processes={"minstrel"},
    ),
    TemplateSpec(
        lines=[
            "Foxfire keeps a low ember.",
            "Foxfire idles among the colours.",
            "Foxfire behaves itself. For now.",
            "Foxfire keeps one spark awake.",
        ],
        composition_modes=set(COMPOSITION_MODES),
        processes={"foxfire"},
    ),
    TemplateSpec(
        lines=[
            "Aurora holds the room in a soft glow.",
            "Aurora watches the empty frames.",
            "Aurora keeps the edges lit.",
            "Aurora tends the quiet side of the display.",
        ],
        composition_modes=set(COMPOSITION_MODES),
        processes={"aurora"},
    ),
    TemplateSpec(
        lines=HEARTBEAT_TEMPLATES,
        composition_modes=set(COMPOSITION_MODES),
    ),
]


STYLE_BY_KIND = {
    "error": "error",
    "recovery": "recovery",
    "lifecycle": "lifecycle",
    "info": "lifecycle",
    "event": "interaction",
    "interaction": "interaction",
    "media": "interaction",
    "reaction": "secondary",
    "opinion": "secondary",
    "ambient": "ambient",
    "heartbeat": "ambient",
}

SEVERITY_BY_PRIORITY = {
    0: "critical",
    1: "lifecycle",
    2: "interaction",
    3: "reaction",
    4: "ambient",
}


def _display_process(process: str) -> str:
    """Return the display process result."""
    return PROCESS_NAMES.get(process.lower(), process)


def _has_metadata(message: SpiritMessage, key: str) -> bool:
    """Return whether metadata."""
    value = message.metadata.get(key)
    return value is not None and value != ""


def _format_template(template: str, message: SpiritMessage) -> str:
    """Format template."""
    values = _template_values(message)
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return f"[{_display_process(message.process)}] {message.event}"


def _template_values(message: SpiritMessage) -> dict[str, Any]:
    """Return the template values result."""
    return {
        "process": _display_process(message.process),
        "summary": message.metadata.get("summary", message.event),
        "details": message.metadata.get("details", ""),
        "track": message.metadata.get("track", "the current track"),
        "album": message.metadata.get("album", ""),
        "artist": message.metadata.get("artist", ""),
        **message.metadata,
    }


FIELD_ROLES = {
    "process": "process",
    "track": "title",
    "title": "title",
    "artist": "artist",
    "album": "album",
    "device": "device",
    "server": "endpoint",
    "url": "endpoint",
    "summary": "error",
    "details": "secondary",
    "count": "count",
    "repeat_count": "count",
    "duration": "duration",
    "fault_duration_s": "duration",
    "command": "control",
    "control": "control",
    "button": "control",
    "action": "control",
}

NUMBER_FIELDS = {
    "libraries", "led_count", "fps", "usb_ma", "bins", "sr", "fftn",
    "channels", "blocksize", "missing_count",
}


def _process_role(process: str) -> str:
    """Return the process role result."""
    return f"process:{process.strip().lower()}"


def _split_service_names(text: str, message: SpiritMessage) -> list[StyledFragment]:
    """Colour service names embedded literally in existing prose templates."""
    aliases: dict[str, str] = {}
    for process, display in PROCESS_NAMES.items():
        aliases.setdefault(display.lower(), process)
    aliases.setdefault(_display_process(message.process).lower(), message.process.lower())
    if not text or not aliases:
        return [StyledFragment(text, "body", message.process)] if text else []
    pattern = re.compile(
        "(" + "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)) + ")",
        re.IGNORECASE,
    )
    output: list[StyledFragment] = []
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            output.append(StyledFragment(text[cursor:match.start()], "body", message.process))
        process = aliases[match.group(0).lower()]
        output.append(StyledFragment(match.group(0), _process_role(process), process))
        cursor = match.end()
    if cursor < len(text):
        output.append(StyledFragment(text[cursor:], "body", message.process))
    return output


def _field_role(field_name: str, message: SpiritMessage) -> str:
    """Return the field role result."""
    root = field_name.split(".", 1)[0].split("[", 1)[0]
    role = FIELD_ROLES.get(root)
    if role == "process":
        return _process_role(message.process)
    if role:
        return role
    if root in NUMBER_FIELDS:
        return "number"
    return "metadata"


def _render_template_fragments(template: str, message: SpiritMessage) -> list[StyledFragment]:
    """Render placeholders independently so only semantic values receive colour."""
    values = _template_values(message)
    fragments: list[StyledFragment] = []
    formatter = string.Formatter()
    try:
        parsed = list(formatter.parse(template))
        for literal, field_name, format_spec, conversion in parsed:
            fragments.extend(_split_service_names(literal, message))
            if field_name is None:
                continue
            value, _ = formatter.get_field(field_name, (), values)
            if conversion:
                value = formatter.convert_field(value, conversion)
            rendered = formatter.format_field(value, format_spec)
            fragments.append(StyledFragment(rendered, _field_role(field_name, message), message.process))
        return fragments
    except (KeyError, ValueError, AttributeError):
        return _split_service_names(_format_template(template, message), message)


def _spec_matches(
    spec: TemplateSpec,
    message: SpiritMessage,
    composition_mode: str,
    mood: str,
) -> bool:
    """Return the spec matches result."""
    return (
        composition_mode in spec.composition_modes
        and (spec.processes is None or message.process in spec.processes)
        and (spec.moods is None or mood in spec.moods)
        and all(_has_metadata(message, key) for key in spec.required_metadata)
    )


def _specificity(spec: TemplateSpec) -> tuple[int, int, int]:
    """Prefer process and mood variants, then richer metadata variants."""
    return (
        1 if spec.processes is not None else 0,
        1 if spec.moods is not None else 0,
        len(spec.required_metadata),
    )


def _select_spec(
    template_key: str,
    message: SpiritMessage,
    composition_mode: str,
    mood: str,
) -> TemplateSpec | None:
    """Select spec."""
    variants = TEMPLATES.get(template_key, [])

    eligible = [
        spec
        for spec in variants
        if _spec_matches(spec, message, composition_mode, mood)
    ]

    # A requested fancy mode should never produce a raw fallback merely because
    # this event only has a direct form.
    if not eligible and composition_mode != "direct":
        eligible = [
            spec
            for spec in variants
            if _spec_matches(spec, message, "direct", mood)
        ]

    if not eligible:
        return None

    best_score = max(_specificity(spec) for spec in eligible)
    best = [spec for spec in eligible if _specificity(spec) == best_score]
    return random.choice(best)


def _render_from_spec(
    spec: TemplateSpec,
    message: SpiritMessage,
    default_role: str,
) -> tuple[str, list[StyledFragment]]:
    """Render from spec."""
    if spec.fragments:
        fragments = []
        for fragment, role in spec.fragments:
            rendered = _format_template(fragment, message)
            if role in {"body", "secondary"}:
                fragments.extend(_split_service_names(rendered, message))
            elif role == "speaker":
                fragments.append(StyledFragment(rendered, _process_role(message.process), message.process))
            else:
                fragments.append(StyledFragment(rendered, role, message.process))
        return "".join(fragment.text for fragment in fragments), fragments

    template = random.choice(spec.lines)
    fragments = _render_template_fragments(template, message)
    return "".join(fragment.text for fragment in fragments), fragments


def render_message(
    message: SpiritMessage,
    *,
    composition_mode: str = "direct",
    mood: str = "calm",
    minimum_visible_seconds: float | None = None,
) -> RenderedMessage:
    """Render one structured fact into plain text plus semantic fragments."""

    if composition_mode not in COMPOSITION_MODES:
        composition_mode = "direct"

    template_key = str(message.metadata.get("template_event", message.event))

    if message.event == "duplicate_summary":
        template_key = "duplicate_summary"
    elif message.kind in {"reaction", "opinion"}:
        template_key = f"opinion:{template_key}"
    elif message.kind in {"ambient", "heartbeat"}:
        template_key = "heartbeat"
    elif message.kind == "error":
        event_error_key = f"error:{template_key}"
        template_key = event_error_key if event_error_key in TEMPLATES else "error"
    elif message.kind == "recovery":
        template_key = template_key if template_key in TEMPLATES else "recovery"

    default_role = STYLE_BY_KIND.get(message.kind, "secondary")

    if message.event == "legacy_text":
        text = str(message.metadata.get("text", ""))
        fragments = _split_service_names(text, message)
    else:
        spec = _select_spec(template_key, message, composition_mode, mood)
        if spec is None and message.event == "duplicate_summary":
            spec = _select_spec("duplicate_summary", message, composition_mode, mood)
        if spec is None and message.kind == "recovery":
            spec = _select_spec("recovery", message, composition_mode, mood)

        if spec is not None:
            text, fragments = _render_from_spec(spec, message, default_role)
        else:
            text = f"[{_display_process(message.process)}] {message.event}"
            fragments = _split_service_names(text, message)

    return RenderedMessage(
        text=text,
        severity=SEVERITY_BY_PRIORITY.get(message.priority, "interaction"),
        speaker=_display_process(message.process),
        process=message.process,
        style_role=default_role,
        persistent=message.persistent,
        minimum_visible_seconds=minimum_visible_seconds,
        fragments=fragments,
        event=message.event,
        correlation_id=message.correlation_id,
        metadata={
            **message.metadata,
            "composition_mode": composition_mode,
            "mood": mood,
        },
    )


# ---------------------------------------------------------------------------
# Legacy dress functions
# ---------------------------------------------------------------------------


def dress_info(process, event_key):
    """Handle the dress info lifecycle step."""
    if event_key not in INFO_POOLS:
        raise ValueError(f"INFO event_key '{event_key}' has no template pool defined.")
    return [random.choice(INFO_POOLS[event_key]).format(process=_display_process(process))]


def dress_event(process, event_key, metadata=None):
    """Handle the dress event lifecycle step."""
    if event_key not in EVENT_POOLS:
        raise ValueError(f"EVENT event_key '{event_key}' has no template pool defined.")

    md = metadata or {}
    try:
        line = random.choice(EVENT_POOLS[event_key]).format(
            process=_display_process(process),
            **md,
        )
    except KeyError as missing:
        raise ValueError(
            f"Missing metadata key '{missing.args[0]}' for event '{event_key}'."
        ) from None
    return [line]


def dress_error(process, summary, details):
    """Handle the dress error lifecycle step."""
    line = random.choice(ERROR_TEMPLATES).format(
        process=_display_process(process),
        summary=summary,
        details=details,
    )
    return [line]


def dress_media(process, media_event, metadata):
    """Handle the dress media lifecycle step."""
    if media_event not in MEDIA_TEMPLATES:
        raise ValueError(f"MEDIA event '{media_event}' has no template pool defined.")

    md = metadata or {}
    values = {
        "process": _display_process(process),
        "track": md.get("track", "the current track"),
        "album": md.get("album", ""),
        "artist": md.get("artist", ""),
    }
    return [random.choice(MEDIA_TEMPLATES[media_event]).format(**values)]


def dress_heartbeat(process):
    """Handle the dress heartbeat lifecycle step."""
    process_key = str(process).lower()
    message = type(
        "LegacyHeartbeat",
        (),
        {
            "process": process_key,
            "metadata": {},
            "event": "heartbeat",
        },
    )()
    spec = _select_spec("heartbeat", message, "direct", "calm")
    if spec is None:
        return [random.choice(HEARTBEAT_TEMPLATES).format(process=_display_process(process_key))]
    return [_format_template(random.choice(spec.lines), message)]


def dress_opinion(process, media_event, metadata=None):
    """Handle the dress opinion lifecycle step."""
    if media_event not in OPINIONS:
        raise ValueError(f"Opinion missing for media event '{media_event}'.")

    md = metadata or {}
    message = type(
        "LegacyOpinion",
        (),
        {
            "process": str(process).lower(),
            "metadata": md,
            "event": media_event,
        },
    )()
    spec = _select_spec(f"opinion:{media_event}", message, "direct", "calm")
    if spec is None:
        return [random.choice(OPINIONS[media_event]).format(
            process=_display_process(process),
            track=md.get("track", "the current track"),
        )]
    return [_format_template(random.choice(spec.lines), message)]
