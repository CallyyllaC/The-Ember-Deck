"""Typed facts and rendered output for the Ember Deck logging pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any, Iterable


PRIORITY_CRITICAL = 0
PRIORITY_LIFECYCLE = 1
PRIORITY_INTERACTION = 2
PRIORITY_REACTION = 3
PRIORITY_AMBIENT = 4


@dataclass(slots=True)
class SpiritMessage:
    """Manage SpiritMessage state and behaviour."""
    priority: int
    kind: str
    process: str
    event: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)
    correlation_id: str | None = None
    dedupe_key: str | None = None
    persistent: bool = False

    def to_wire(self) -> dict[str, Any]:
        """Convert the value to wire."""
        return {"_spirit_message": 1, **asdict(self)}

    @classmethod
    def from_wire(cls, value: Any) -> SpiritMessage | None:
        """Create an instance from wire."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or value.get("_spirit_message") != 1:
            return None
        fields = dict(value)
        fields.pop("_spirit_message", None)
        return cls(**fields)


@dataclass(slots=True)
class StyledFragment:
    """Manage StyledFragment state and behaviour."""
    text: str
    role: str = "secondary"
    process: str | None = None


@dataclass(slots=True)
class RenderedMessage:
    """Manage RenderedMessage state and behaviour."""
    text: str
    severity: str
    speaker: str | None = None
    process: str | None = None
    style_role: str | None = None
    persistent: bool = False
    minimum_visible_seconds: float | None = None
    fragments: list[StyledFragment] = field(default_factory=list)
    event: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """Convert the value to record."""
        record = asdict(self)
        record["rendered_at"] = time.time()
        return record


DEFAULT_ROLE_STYLES = {
    "body": "white",
    "speaker": "bold cyan",
    "title": "bold #ffd166",
    "artist": "italic #8ecae6",
    "album": "#b8c0ff",
    "device": "#f4a261",
    "endpoint": "#2a9d8f",
    "control": "bold #e9c46a",
    "metadata": "#bde0fe",
    "number": "#cdb4db",
    "secondary": "dim white",
    "error": "bold red",
    "recovery": "bold green",
    "count": "yellow",
    "duration": "yellow",
    "process:whisper": "bold #c77dff",
    "process:whisper_daemon": "bold #c77dff",
    "process:pawprint": "bold #ff9f1c",
    "process:minstrel": "bold #70e000",
    "process:foxfire": "bold #ff5400",
    "process:aurora": "bold #00d4ff",
    "process:echo": "bold #4895ef",
    "process:willo_wisp": "bold #fee440",
    "process:mpris_bridge": "bold #52b788",
    "process:hdmi2_source_controller": "bold #48cae4",
    "process:foundrycore": "bold #90a4ae",
}


def render_plain(message: RenderedMessage) -> str:
    """Return markup-free text for stdout and ordinary log files."""
    return message.text


def render_rich(message: RenderedMessage, role_styles: dict[str, str] | None = None):
    """Build a Rich Text object lazily; event data never contains markup."""
    try:
        from rich.text import Text
    except ImportError:
        # Diagnostics environments need no Rich dependency; Textual installs it
        # on the Deck, while callers elsewhere still receive plain text.
        return message.text

    styles = dict(DEFAULT_ROLE_STYLES)
    if role_styles:
        styles.update(role_styles)
    if not message.fragments:
        return Text(message.text, style=styles.get(message.style_role or "secondary", ""))
    output = Text()
    for fragment in message.fragments:
        output.append(fragment.text, style=styles.get(fragment.role, ""))
    return output


def fragments_text(fragments: Iterable[StyledFragment]) -> str:
    """Return the fragments text result."""
    return "".join(fragment.text for fragment in fragments)
