"""Structured progress events: one emit() feeds both events.jsonl and the live CLI printer."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from advpipe.gates import GateResult
from advpipe.models import RunState
from advpipe.runlog import RunLog

EventKind = Literal[
    "run_start",
    "stage",
    "round",
    "agent_start",
    "agent_done",
    "gates",
    "verdict",
    "guard",
    "test_fix",
    "ruling",
    "status",
    "error",
]


def emit(
    runlog: RunLog,
    state: RunState,
    progress: Callable[[str], None],
    kind: EventKind,
    message: str,
    *,
    echo: bool = True,
    **data: object,
) -> None:
    """Append an event to the run's events.jsonl, then pass ``message`` to the printer.

    ``echo=False`` records the event without printing it (used for events that would add a
    line the terminal never showed).
    """
    event: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "kind": kind,
        "stage": state.stage.value,
        "round": state.round,
        "message": message,
        **data,
    }
    runlog.append_event(event)
    if echo:
        progress(message)


def gate_statuses(results: list[GateResult]) -> dict[str, str]:
    """``{"test": "fail", "lint": "pass", "types": "skipped"}`` for a gates event."""
    return {r.name: "skipped" if r.skipped else ("pass" if r.passed else "fail") for r in results}
