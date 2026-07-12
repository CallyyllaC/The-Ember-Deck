#!/usr/bin/env python3
"""Central structured event scheduler and personality renderer for Ember Deck."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import heapq
import json
import os
from pathlib import Path
import queue
import random
import re
import signal
import sys
import time
import uuid
from multiprocessing.managers import BaseManager
from typing import Any

import synapse as bus
from spirit_ink import OPINIONS, render_message
from spirit_messages import (
    DEFAULT_ROLE_STYLES,
    PRIORITY_AMBIENT,
    PRIORITY_CRITICAL,
    PRIORITY_INTERACTION,
    PRIORITY_LIFECYCLE,
    PRIORITY_REACTION,
    RenderedMessage,
    SpiritMessage,
    render_plain,
)

try:
    from rich.console import Console
    from rich.text import Text
except ImportError:  # Plain output remains available on minimal installs.
    Console = None
    Text = None


# Whisper is normally launched under Foundry rather than attached directly to
# a TTY. Force Rich terminal output so the captured console stream retains its
# ANSI colours. Set WHISPER_COLOUR=0 to return to plain stdout.
_COLOUR_ENABLED = os.environ.get("WHISPER_COLOUR", "0").strip().lower() not in {
    "0", "false", "no", "off",
}
_CONSOLE = (
    Console(force_terminal=True, color_system="truecolor", soft_wrap=True)
    if Console is not None and _COLOUR_ENABLED
    else None
)


def render_console(rendered: RenderedMessage):
    """Build Rich text from semantic fragments without changing plain logs."""
    if Text is None:
        return render_plain(rendered)

    text = Text()
    fragments = rendered.fragments or []
    if not fragments:
        text.append(rendered.text, style=DEFAULT_ROLE_STYLES.get(rendered.style_role))
        return text

    for fragment in fragments:
        text.append(
            fragment.text,
            style=DEFAULT_ROLE_STYLES.get(fragment.role),
        )
    return text


def print_rendered(rendered: RenderedMessage) -> None:
    """Print colour to the live console, with a safe plain-text fallback."""
    if _CONSOLE is None:
        print(render_plain(rendered), flush=True)
        return
    _CONSOLE.print(render_console(rendered))


QUEUE = None
QUEUE_CONNECT_ATTEMPTED = False

DEFAULTS = {
    "proc_name": "whisper_daemon",
    "fps": 10.0,
    "ambient_min_delay": 60.0,
    "ambient_max_delay": 150.0,
    "ambient_after_real_event_delay": 30.0,
    "ambient_after_startup_delay": 45.0,
    "ambient_queue_limit": 3,
    "error_dedupe_cooldown_s": 60.0,
    "reaction_min_delay_s": 2.0,
    "reaction_max_delay_s": 8.0,
    "reaction_max_per_event": 2,
    "reaction_global_cooldown_s": 5.0,
    "reaction_process_cooldown_s": 15.0,
    "structured_log_path": "logs/foundry.structured.jsonl",
    "ui_event_limit": 100,
    "important_log_path": "logs/foundry-errors.jsonl",
    "important_log_max_priority": 0,
    "important_log_max_bytes_mb": 10.0,
    "log_retention_days": 183,
    "log_cleanup_interval_s": 86400.0,
    "legacy_log_paths": ["logs/foundry.log", "logs/browser.log"],
    "critical_visible_s": 12.0,
    "lifecycle_visible_s": 6.0,
    "interaction_visible_s": 4.0,
    "reaction_visible_s": 3.0,
    "ambient_visible_s": 0.0,
}

INFO_EVENTS = {
    "starting": "process_starting",
    "started": "startup_complete",
    "stopping": "process_stopping",
    "stopping_with_force": "process_force_stopping",
    "stopped": "process_stopped",
    "cfg_loaded": "configuration_loaded",
    "enter_main_loop": "main_loop_entered",
    "url_found": "service_url_found",
    "user_found": "service_user_found",
    "led_waiting": "led_configuration_waiting",
    "audio_stream_started": "audio_stream_started",
}

GENERAL_EVENTS = {
    "connected": "process_connected",
    "lyrics_found": "lyrics_found",
    "led_connected": "led_connected",
    "init_audio_busses": "audio_busses_initialized",
    "audio_stream_connected": "audio_stream_connected",
    "init_led_busses": "led_busses_initialized",
}

MEDIA_EVENTS = {
    "track_change": "track_changed",
    "play": "playback_resumed",
    "pause": "playback_paused",
    "stop": "playback_stopped",
}

KNOWN_ERROR_EVENTS = {
    "i2c connection failed": "i2c_connection_failed",
    "i2c peripheral missing": "i2c_peripheral_missing",
    "i2c indicator write failed": "i2c_indicator_write_failed",
    "ads1115 read failed": "ads1115_read_failed",
    "failed to connect to plex": "plex_connection_failed",
    "error querying plex": "plex_query_failed",
    "group_restart": "process_group_restart",
}


def load_cfg() -> dict[str, Any]:
    """Load defaults and merge any component-specific YAML configuration."""
    cfg = dict(DEFAULTS)
    cfg_path = os.environ.get("CONFIG_PATH")
    if cfg_path and Path(cfg_path).exists():
        try:
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as handle:
                cfg.update(yaml.safe_load(handle) or {})
        except Exception:
            pass
    return cfg


def _slug(value: str) -> str:
    """Return the slug result."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "unknown_event"


def _error_event(summary: str) -> str:
    """Return the error event result."""
    return KNOWN_ERROR_EVENTS.get(summary.strip().lower(), _slug(summary))


def _queue_put(item: Any) -> bool:
    """Return the queue put result."""
    global QUEUE, QUEUE_CONNECT_ATTEMPTED
    if QUEUE is None:
        return False
    try:
        QUEUE.put(item)
        return True
    except (BrokenPipeError, ConnectionError, EOFError, OSError):
        QUEUE = None
        QUEUE_CONNECT_ATTEMPTED = False
        return False


def _connect_queue_from_env() -> None:
    """Handle the connect queue from env lifecycle step."""
    global QUEUE, QUEUE_CONNECT_ATTEMPTED
    if QUEUE is not None or QUEUE_CONNECT_ATTEMPTED:
        return
    QUEUE_CONNECT_ATTEMPTED = True
    host = os.environ.get("WHISPER_MANAGER_HOST")
    port = os.environ.get("WHISPER_MANAGER_PORT")
    auth = os.environ.get("WHISPER_MANAGER_AUTH")
    if not (host and port and auth):
        return

    class QueueClient(BaseManager):
        """Manage QueueClient state and behaviour."""
        pass

    QueueClient.register("get_queue")
    try:
        client = QueueClient(address=(host, int(port)), authkey=bytes.fromhex(auth))
        client.connect()
        QUEUE = client.get_queue()
    except (OSError, ValueError):
        QUEUE = None


def set_queue(value) -> None:
    """Set queue."""
    global QUEUE
    QUEUE = value


def publish(message: SpiritMessage) -> None:
    """Publish a typed fact, with a plain diagnostic fallback when standalone."""
    _connect_queue_from_env()
    if _queue_put(message.to_wire()):
        return
    print_rendered(render_message(message))


def log_info(process: str, event_key: str) -> None:
    """Handle the log info lifecycle step."""
    event = INFO_EVENTS.get(event_key, _slug(event_key))
    publish(SpiritMessage(
        priority=PRIORITY_LIFECYCLE,
        kind="lifecycle",
        process=process,
        event=event,
        metadata={"template_event": event_key},
        persistent=event in {"startup_complete", "process_stopped"},
    ))


def log_error(
    process: str,
    summary: str,
    details: str,
    *,
    event: str | None = None,
    dedupe_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Handle the log error lifecycle step."""
    stable_event = event or _error_event(summary)
    md = {"summary": summary, "details": details, **(metadata or {})}
    publish(SpiritMessage(
        priority=PRIORITY_CRITICAL,
        kind="error",
        process=process,
        event=stable_event,
        metadata=md,
        dedupe_key=dedupe_key or f"{process}:{stable_event}:{md.get('device', '')}",
        persistent=True,
    ))


def log_recovery(
    process: str,
    event: str,
    *,
    recovers_key: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Handle the log recovery lifecycle step."""
    publish(SpiritMessage(
        priority=PRIORITY_LIFECYCLE,
        kind="recovery",
        process=process,
        event=event,
        metadata={"recovers_key": recovers_key, **(metadata or {})},
        persistent=True,
    ))


def log_event(process: str, event: str, metadata: dict | None = None) -> None:
    """Handle the log event lifecycle step."""
    publish(SpiritMessage(
        priority=PRIORITY_LIFECYCLE,
        kind="lifecycle",
        process=process,
        event=GENERAL_EVENTS.get(event, _slug(event)),
        metadata={"template_event": event, **(metadata or {})},
    ))


def log_interaction(
    process: str,
    event: str,
    metadata: dict[str, Any] | None = None,
    *,
    correlation_id: str | None = None,
) -> None:
    """Publish a user or physical-control fact at interaction priority."""
    publish(SpiritMessage(
        priority=PRIORITY_INTERACTION,
        kind="interaction",
        process=process,
        event=_slug(event),
        metadata=metadata or {},
        correlation_id=correlation_id or uuid.uuid4().hex,
    ))


def log_legacy(process: str, text: str, *, priority: int = PRIORITY_INTERACTION) -> None:
    """Temporary adapter for operational notes not yet expressed as facts."""
    publish(SpiritMessage(
        priority=priority,
        kind="legacy",
        process=process,
        event="legacy_text",
        metadata={"text": text},
    ))


def log_media(process: str, media_event: str, metadata: dict | None = None) -> None:
    """Handle the log media lifecycle step."""
    publish(SpiritMessage(
        priority=PRIORITY_INTERACTION,
        kind="media",
        process=process,
        event=MEDIA_EVENTS.get(media_event, _slug(media_event)),
        metadata={"template_event": media_event, **(metadata or {})},
        correlation_id=uuid.uuid4().hex,
    ))


def log_heartbeat(process: str) -> None:
    """Handle the log heartbeat lifecycle step."""
    publish(SpiritMessage(
        priority=PRIORITY_AMBIENT,
        kind="ambient",
        process=process,
        event="heartbeat",
        dedupe_key=f"heartbeat:{process}",
    ))


def schedule_reaction(
    process: str,
    event: str,
    metadata: dict[str, Any] | None = None,
    *,
    correlation_id: str | None = None,
    delay_s: float = 3.0,
) -> None:
    """Handle the schedule reaction lifecycle step."""
    publish(SpiritMessage(
        priority=PRIORITY_REACTION,
        kind="reaction",
        process=process,
        event="reaction",
        metadata={
            "template_event": event,
            "delay_s": delay_s,
            "composition_mode": "delayed_chain",
            **(metadata or {}),
        },
        correlation_id=correlation_id,
    ))


def foundry_heartbeat(proc_name: str) -> None:
    """Handle the foundry heartbeat lifecycle step."""
    now_ms = int(time.monotonic() * 1000)
    bus.set_int(f"/proc/{proc_name}/heartbeat_ms", now_ms)
    seq_key = f"/proc/{proc_name}/hb_seq"
    bus.set_int(seq_key, bus.get_int(seq_key, 0) + 1)


class WhisperScheduler:
    """Manage WhisperScheduler state and behaviour."""
    def __init__(self, cfg: dict[str, Any], *, now: float | None = None):
        """Initialize configuration, dependencies, and runtime state."""
        self.cfg = cfg
        self.ready = [deque() for _ in range(5)]
        self.ambient: deque[SpiritMessage] = deque()
        self.delayed: list[tuple[float, int, SpiritMessage]] = []
        self._sequence = 0
        self.last_process: str | None = None
        self.last_real_event_at = now if now is not None else time.monotonic()
        self.next_ambient_at = self.last_real_event_at + float(cfg["ambient_after_startup_delay"])
        self.faults: dict[str, dict[str, Any]] = {}
        self.recent_events: deque[float] = deque()
        self.recovering_until = 0.0
        self.celebratory_until = 0.0
        self.last_reaction_at = 0.0
        self.last_process_reaction: dict[str, float] = {}

    def _schedule_ambient_deadline(self, now: float) -> None:
        """Handle the schedule ambient deadline lifecycle step."""
        self.next_ambient_at = now + random.uniform(
            float(self.cfg["ambient_min_delay"]),
            float(self.cfg["ambient_max_delay"]),
        )

    def _cancel_reactions(self, scope: str | None = None) -> None:
        """Handle the cancel reactions lifecycle step."""
        if not self.delayed:
            return
        self.delayed = [
            entry for entry in self.delayed
            if entry[2].kind != "reaction"
            or (scope is not None and entry[2].metadata.get("reaction_scope") != scope)
        ]
        heapq.heapify(self.delayed)

    def _schedule_media_reactions(self, message: SpiritMessage, now: float) -> None:
        """Handle the schedule media reactions lifecycle step."""
        template_event = str(message.metadata.get("template_event", ""))
        if template_event not in OPINIONS:
            return
        if message.event == "track_changed":
            self._cancel_reactions("track")
            scope = "track"
            self.celebratory_until = now + 10.0
        else:
            scope = f"media:{message.process}"
            self._cancel_reactions(scope)

        maximum = max(0, int(self.cfg["reaction_max_per_event"]))
        count = random.randint(0, maximum)
        candidates = [name for name in self.last_process_reaction if name != message.process]
        random.shuffle(candidates)
        for process in candidates[:count]:
            process_last = self.last_process_reaction.get(process, 0.0)
            if now - process_last < float(self.cfg["reaction_process_cooldown_s"]):
                continue
            delay = random.uniform(
                float(self.cfg["reaction_min_delay_s"]),
                float(self.cfg["reaction_max_delay_s"]),
            )
            reaction = SpiritMessage(
                priority=PRIORITY_REACTION,
                kind="reaction",
                process=process,
                event="reaction",
                metadata={
                    **message.metadata,
                    "template_event": template_event,
                    "reaction_scope": scope,
                    "composition_mode": "delayed_chain",
                },
                correlation_id=message.correlation_id,
            )
            self._sequence += 1
            heapq.heappush(self.delayed, (now + delay, self._sequence, reaction))

    def enqueue(self, message: SpiritMessage, *, now: float | None = None) -> bool:
        """Return the enqueue result."""
        now = time.monotonic() if now is None else now
        message.priority = min(PRIORITY_AMBIENT, max(PRIORITY_CRITICAL, int(message.priority)))

        delay_s = max(0.0, float(message.metadata.pop("delay_s", 0.0)))
        if delay_s:
            self._sequence += 1
            heapq.heappush(self.delayed, (now + delay_s, self._sequence, message))
            return True

        if message.priority == PRIORITY_AMBIENT:
            if any(existing.process == message.process for existing in self.ambient):
                return False
            limit = max(1, int(self.cfg["ambient_queue_limit"]))
            if len(self.ambient) >= limit:
                return False
            self.ambient.append(message)
            self.last_process_reaction.setdefault(message.process, 0.0)
            return True

        if message.kind == "error" and message.dedupe_key:
            fault = self.faults.get(message.dedupe_key)
            if fault and now - float(fault["last_printed_at"]) < float(self.cfg["error_dedupe_cooldown_s"]):
                fault["suppressed"] += 1
                fault["last_seen_at"] = now
                return False
            if fault and fault["suppressed"]:
                self.ready[PRIORITY_LIFECYCLE].append(self._duplicate_summary(message.dedupe_key, fault, now))
                fault["suppressed"] = 0
            self.faults[message.dedupe_key] = {
                "message": message,
                "first_at": fault["first_at"] if fault else now,
                "last_seen_at": now,
                "last_printed_at": now,
                "suppressed": fault["suppressed"] if fault else 0,
            }

        recovers_key = message.metadata.get("recovers_key")
        if message.kind == "recovery" and recovers_key in self.faults:
            fault = self.faults.pop(str(recovers_key))
            message.metadata["repeat_count"] = int(fault["suppressed"])
            message.metadata["fault_duration_s"] = max(0.0, now - float(fault["first_at"]))
            self.recovering_until = now + 20.0

        if message.priority <= PRIORITY_INTERACTION:
            self.last_real_event_at = now
            self.next_ambient_at = max(
                self.next_ambient_at,
                now + float(self.cfg["ambient_after_real_event_delay"]),
            )
            self.recent_events.append(now)
        if message.kind == "media":
            self._schedule_media_reactions(message, now)
        self.ready[message.priority].append(message)
        return True

    def _duplicate_summary(self, key: str, fault: dict[str, Any], now: float) -> SpiritMessage:
        """Return the duplicate summary result."""
        original: SpiritMessage = fault["message"]
        return SpiritMessage(
            priority=PRIORITY_LIFECYCLE,
            kind="lifecycle",
            process=original.process,
            event="duplicate_summary",
            metadata={
                "fault_event": original.event,
                "count": int(fault["suppressed"]),
                "duration": max(0.0, now - float(fault["first_at"])),
                "dedupe_key": key,
            },
            correlation_id=original.correlation_id,
        )

    def _release_due(self, now: float) -> None:
        """Handle the release due lifecycle step."""
        while self.delayed and self.delayed[0][0] <= now:
            _, _, message = heapq.heappop(self.delayed)
            if message.kind == "reaction":
                eligible_at = max(
                    self.last_reaction_at + float(self.cfg["reaction_global_cooldown_s"]),
                    self.last_process_reaction.get(message.process, 0.0)
                    + float(self.cfg["reaction_process_cooldown_s"]),
                )
                if eligible_at > now:
                    self._sequence += 1
                    heapq.heappush(self.delayed, (eligible_at, self._sequence, message))
                    continue
            self.ready[message.priority].append(message)

    def _flush_duplicate_summaries(self, now: float) -> None:
        """Handle the flush duplicate summaries lifecycle step."""
        cooldown = float(self.cfg["error_dedupe_cooldown_s"])
        for key, fault in list(self.faults.items()):
            if fault["suppressed"] and now - float(fault["last_seen_at"]) >= cooldown:
                self.ready[PRIORITY_LIFECYCLE].append(self._duplicate_summary(key, fault, now))
                fault["suppressed"] = 0

    def _fair_pop(self, priority: int) -> SpiritMessage:
        """Return the fair pop result."""
        items = self.ready[priority]
        if priority < PRIORITY_INTERACTION or len(items) < 2:
            return items.popleft()
        for index, message in enumerate(items):
            if message.process != self.last_process:
                items.rotate(-index)
                chosen = items.popleft()
                items.rotate(index)
                return chosen
        return items.popleft()

    def pop_next(self, *, now: float | None = None) -> SpiritMessage | None:
        """Return the pop next result."""
        now = time.monotonic() if now is None else now
        self._release_due(now)
        self._flush_duplicate_summaries(now)
        for priority in range(PRIORITY_AMBIENT):
            if self.ready[priority]:
                message = self._fair_pop(priority)
                self.last_process = message.process
                if message.kind == "reaction":
                    self.last_reaction_at = now
                    self.last_process_reaction[message.process] = now
                return message
        if self.ambient and now >= self.next_ambient_at:
            message = self.ambient.popleft()
            self.last_process = message.process
            self._schedule_ambient_deadline(now)
            return message
        return None

    def mood(self, *, now: float | None = None) -> str:
        """Return the mood result."""
        now = time.monotonic() if now is None else now
        while self.recent_events and self.recent_events[0] < now - 15.0:
            self.recent_events.popleft()
        if self.faults:
            return "wounded"
        if now < self.recovering_until:
            return "recovering"
        if now < self.celebratory_until:
            return "celebratory"
        if len(self.recent_events) >= 6:
            return "busy"
        return "calm"


def _minimum_visible(cfg: dict[str, Any], message: SpiritMessage) -> float:
    """Return the minimum visible result."""
    key = {
        PRIORITY_CRITICAL: "critical_visible_s",
        PRIORITY_LIFECYCLE: "lifecycle_visible_s",
        PRIORITY_INTERACTION: "interaction_visible_s",
        PRIORITY_REACTION: "reaction_visible_s",
        PRIORITY_AMBIENT: "ambient_visible_s",
    }[message.priority]
    return float(cfg[key])


class EventFileSink:
    """Bounded UI snapshot plus rotated, error-only durable history."""

    def __init__(self, cfg: dict[str, Any], base: Path):
        """Initialize configuration, dependencies, and runtime state."""
        ui_value = str(cfg.get("structured_log_path", "")).strip()
        error_value = str(cfg.get("important_log_path", "")).strip()
        self.ui_path = Path(ui_value) if ui_value else None
        self.error_base = Path(error_value) if error_value else None
        if self.ui_path is not None and not self.ui_path.is_absolute():
            self.ui_path = base / self.ui_path
        if self.error_base is not None and not self.error_base.is_absolute():
            self.error_base = base / self.error_base
        self.ui_records: deque[dict[str, Any]] = deque(maxlen=max(1, int(cfg["ui_event_limit"])))
        self.max_priority = int(cfg["important_log_max_priority"])
        self.max_bytes = max(1, int(float(cfg["important_log_max_bytes_mb"]) * 1024 * 1024))
        self.retention_days = max(1, int(cfg["log_retention_days"]))
        self.cleanup_interval = max(60.0, float(cfg["log_cleanup_interval_s"]))
        self.legacy_paths = []
        for value in cfg.get("legacy_log_paths", []):
            path = Path(str(value))
            self.legacy_paths.append(path if path.is_absolute() else base / path)
        self.next_cleanup_at = 0.0
        self.last_write_error: str | None = None
        self.last_error_report_at = 0.0

    def initialize(self) -> None:
        """Create the UI source immediately so the UI never latches onto fallback logs."""
        try:
            self._write_ui_snapshot()
            self.last_write_error = None
        except OSError as exc:
            self.last_write_error = repr(exc)
            print(f"[whisper] structured UI snapshot unavailable: {exc}", file=sys.stderr, flush=True)

    def _write_ui_snapshot(self) -> None:
        """Write ui snapshot."""
        if self.ui_path is None:
            return
        self.ui_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.ui_path.with_suffix(self.ui_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for record in self.ui_records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        os.replace(temporary, self.ui_path)

    def _monthly_error_path(self, now: datetime) -> Path | None:
        """Return the monthly error path result."""
        if self.error_base is None:
            return None
        suffix = self.error_base.suffix or ".jsonl"
        return self.error_base.with_name(
            f"{self.error_base.stem}-{now:%Y-%m}{suffix}"
        )

    def _append_important(self, record: dict[str, Any], now: datetime) -> None:
        """Handle the append important lifecycle step."""
        path = self._monthly_error_path(now)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= self.max_bytes:
            rollover = path.with_name(f"{path.stem}-{now:%d-%H%M%S}{path.suffix}")
            os.replace(path, rollover)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def cleanup(self, *, now: datetime | None = None) -> None:
        """Handle the cleanup lifecycle step."""
        if self.error_base is None:
            return
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.retention_days)
        pattern = f"{self.error_base.stem}-*{self.error_base.suffix or '.jsonl'}"
        for path in self.error_base.parent.glob(pattern):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified < cutoff:
                    path.unlink()
            except OSError:
                continue
        for path in self.legacy_paths:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified < cutoff:
                    path.unlink()
            except OSError:
                continue

    def write(self, rendered: RenderedMessage, priority: int, *, monotonic_now: float) -> None:
        """Handle the write lifecycle step."""
        record = rendered.to_record()
        try:
            self.ui_records.append(record)
            self._write_ui_snapshot()
            wall_now = datetime.now(timezone.utc)
            if priority <= self.max_priority:
                self._append_important(record, wall_now)
            if monotonic_now >= self.next_cleanup_at:
                self.cleanup(now=wall_now)
                self.next_cleanup_at = monotonic_now + self.cleanup_interval
        except OSError as exc:
            # Logging must never take down a hardware worker or the UI service.
            self.last_write_error = repr(exc)
            if monotonic_now - self.last_error_report_at >= 60.0:
                print(f"[whisper] event file write failed: {exc}", file=sys.stderr, flush=True)
                self.last_error_report_at = monotonic_now


def _coerce_message(item: Any) -> SpiritMessage | None:
    """Return the coerce message result."""
    message = SpiritMessage.from_wire(item)
    if message is not None:
        return message
    if isinstance(item, str):
        return SpiritMessage(
            priority=PRIORITY_INTERACTION,
            kind="legacy",
            process="legacy",
            event="legacy_text",
            metadata={"text": item},
        )
    return None


def main() -> None:
    """Configure and run the component until shutdown."""
    cfg = load_cfg()
    proc_name = str(cfg["proc_name"])
    fps = max(1.0, float(cfg["fps"]))
    event_files = EventFileSink(cfg, Path(__file__).resolve().parent)
    event_files.initialize()

    _connect_queue_from_env()
    while QUEUE is None:
        foundry_heartbeat(proc_name)
        time.sleep(1.0 / fps)
        _connect_queue_from_env()

    scheduler = WhisperScheduler(cfg)
    stopping = False

    def on_stop(signum, frame):
        """Mark the component for an orderly shutdown."""
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)
    log_info(proc_name, "started")
    last_foundry_hb = 0.0

    while not stopping:
        now = time.monotonic()
        if now - last_foundry_hb >= 1.0:
            foundry_heartbeat(proc_name)
            last_foundry_hb = now

        try:
            while True:
                message = _coerce_message(QUEUE.get_nowait())
                if message is not None:
                    scheduler.enqueue(message, now=now)
        except queue.Empty:
            pass
        except (BrokenPipeError, ConnectionError, EOFError, OSError):
            break

        message = scheduler.pop_next(now=now)
        if message is not None:
            rendered = render_message(
                message,
                composition_mode=str(message.metadata.get("composition_mode", "direct")),
                mood=scheduler.mood(now=now),
                minimum_visible_seconds=_minimum_visible(cfg, message),
            )
            print_rendered(rendered)
            event_files.write(rendered, message.priority, monotonic_now=now)

        time.sleep(1.0 / fps)

    # This may be consumed before the manager is stopped, or fall back plainly.
    log_info(proc_name, "stopped")
    bus.close_all()


if __name__ == "__main__":
    main()
