"""Structured progress events.

The pipeline reports what it is doing through typed events rather than ``print`` calls, so that the
same stream can drive a CLI today and a GUI/SSE endpoint later (DESIGN.md section 19). Each event
has a plain ``message`` for everyone and a ``technical_message`` for developers; a frontend can show
either without re-deriving anything.

Nothing here imports a web framework: an :class:`EventSink` is just an object with ``emit``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol, TextIO

from pydantic import BaseModel, Field


class Stage(StrEnum):
    """The user-visible stages, in the order a frontend would display them."""

    DISCOVERY = "discovery"
    CLASSIFICATION = "classification"
    README_PARSING = "readme_parsing"
    STRUCTURE_DETECTION = "structure_detection"
    VARIABLE_MATCHING = "variable_matching"
    NORMALIZATION = "normalization"
    VALIDATION = "validation"
    COMPLETE = "complete"


# Weight of each stage in the overall 0-100 progress bar. Chosen so the heavy stages (reading and
# matching) dominate; they are only coarse hints for a progress indicator.
_STAGE_WEIGHTS: dict[Stage, int] = {
    Stage.DISCOVERY: 5,
    Stage.CLASSIFICATION: 5,
    Stage.README_PARSING: 15,
    Stage.STRUCTURE_DETECTION: 30,
    Stage.VARIABLE_MATCHING: 30,
    Stage.NORMALIZATION: 5,
    Stage.VALIDATION: 5,
    Stage.COMPLETE: 5,
}
_STAGE_ORDER: list[Stage] = list(_STAGE_WEIGHTS)


def overall_progress(stage: Stage, within_stage: float = 1.0) -> int:
    """Map (stage, 0..1 within stage) to an overall 0-100 percentage for a progress bar."""
    done = sum(_STAGE_WEIGHTS[s] for s in _STAGE_ORDER[: _STAGE_ORDER.index(stage)])
    total = sum(_STAGE_WEIGHTS.values())
    return round((done + _STAGE_WEIGHTS[stage] * max(0.0, min(1.0, within_stage))) / total * 100)


class PipelineEvent(BaseModel):
    """One thing that happened, ready to serialize straight to a client."""

    sequence: int
    stage: Stage
    status: str  # started | progress | completed | warning | failed
    message: str
    technical_message: str | None = None
    progress: int = 0
    file: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict = Field(default_factory=dict)


class EventSink(Protocol):
    """Anything that can receive events. Implemented by the sinks below and by tests."""

    def emit(self, event: PipelineEvent) -> None: ...


class EventEmitter:
    """Owns the monotonic sequence counter and forwards to a sink.

    Stages receive one of these and call :meth:`emit`; they never touch the sequence number.
    """

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink
        self._sequence = 0

    def emit(
        self,
        stage: Stage,
        status: str,
        message: str,
        *,
        technical_message: str | None = None,
        within_stage: float = 1.0,
        file: str | None = None,
        **data: object,
    ) -> PipelineEvent:
        self._sequence += 1
        event = PipelineEvent(
            sequence=self._sequence,
            stage=stage,
            status=status,
            message=message,
            technical_message=technical_message,
            progress=overall_progress(stage, within_stage),
            file=file,
            data=dict(data),
        )
        self._sink.emit(event)
        return event


class ListSink:
    """Collects events in memory. Used by tests and by any caller that wants the full log."""

    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []

    def emit(self, event: PipelineEvent) -> None:
        self.events.append(event)


class ConsoleSink:
    """Human-readable one-line-per-event output for the CLI."""

    def __init__(self, stream: TextIO | None = None, *, verbose: bool = False) -> None:
        self._stream = stream or sys.stderr
        self._verbose = verbose

    def emit(self, event: PipelineEvent) -> None:
        mark = {"failed": "x", "warning": "!", "completed": "+"}.get(event.status, "·")
        line = f"[{event.progress:3d}%] {mark} {event.message}"
        if self._verbose and event.technical_message:
            line += f"\n        └─ {event.technical_message}"
        print(line, file=self._stream, flush=True)


class JsonLinesSink:
    """Writes one JSON object per line (JSONL). Ready to be tailed or forwarded over SSE."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: PipelineEvent) -> None:
        self._stream.write(event.model_dump_json() + "\n")
        self._stream.flush()


class MultiSink:
    """Fans one event stream out to several sinks (e.g. console + JSONL file)."""

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = sinks

    def emit(self, event: PipelineEvent) -> None:
        for sink in self._sinks:
            sink.emit(event)
