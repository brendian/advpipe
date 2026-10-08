"""The live log: a run's events.jsonl, read from a byte offset and streamed as Server-Sent Events.

The stream sends three SSE event types:

- ``log``: one progress event, as an escaped ``<li>`` the page appends to its log. Its SSE
  ``id`` is the byte offset just past that line, so a browser that reconnects (and sends
  ``Last-Event-ID``) carries on from where it was, without repeats.
- ``refresh``: after each batch of new events; the page re-fetches its header and timeline.
- ``end``: nothing is driving the run any more (no live process holds ``run.lock``). The page
  closes the stream (``sse-close="end"``); a resumed run is a page reload away.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from markupsafe import Markup

from advpipe.runlog import EVENTS_FILE, RunLog

POLL_SECONDS = 0.5
KEEPALIVE_SECONDS = 15.0
_KIND_RE = re.compile(r"[a-z_]{1,32}")


@dataclass(frozen=True)
class LogEvent:
    end: int  # byte offset just past this event's line in events.jsonl
    kind: str
    ts: str  # ISO timestamp as written, e.g. "2026-10-08T04:41:02.512Z"
    stage: str
    round: int
    message: str

    @property
    def time(self) -> str:
        """'04:41:02' (UTC), or the timestamp as written if it isn't ISO-shaped."""
        return self.ts[11:19] if len(self.ts) >= 19 and self.ts[10] == "T" else self.ts

    @property
    def where(self) -> str:
        """'CODE r2', 'SPEC', or '' for an event outside any stage."""
        if not self.stage or self.stage == "INIT":
            return ""
        return f"{self.stage} r{self.round}" if self.round else self.stage

    def html(self) -> Markup:
        """One line of the log. Everything from the file is escaped: messages quote agent
        output and findings. Kept on one line, as SSE data can't contain raw newlines."""
        kind = self.kind if _KIND_RE.fullmatch(self.kind) else "other"
        message = " ".join(self.message.splitlines())
        return Markup(
            '<li class="ev ev-{kind}"><time datetime="{ts}" title="{ts}">{time}</time> '
            '<span class="ev-where">{where}</span> <span class="ev-msg">{message}</span></li>'
        ).format(kind=kind, ts=self.ts, time=self.time, where=self.where, message=message)


def _parse(line: str, end: int) -> LogEvent:
    try:
        data = json.loads(line)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return LogEvent(end, "other", "", "", 0, line)
    return LogEvent(
        end=end,
        kind=str(data.get("kind", "other")),
        ts=str(data.get("ts", "")),
        stage=str(data.get("stage", "")),
        round=data["round"] if isinstance(data.get("round"), int) else 0,
        message=str(data.get("message", "")),
    )


def line_start(path: Path, offset: int) -> int:
    """``offset`` clamped to the file, and moved forward to the start of a line if it points
    into the middle of one (a hand-edited ?offset=, or a file that was replaced)."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return 0
    offset = min(max(offset, 0), size)
    if offset == 0:
        return 0
    with path.open("rb") as f:
        f.seek(offset - 1)
        if f.read(1) == b"\n":
            return offset
        rest = f.read()
    newline = rest.find(b"\n")
    return size if newline == -1 else offset + newline + 1


def read_events(path: Path, offset: int = 0) -> tuple[list[LogEvent], int]:
    """Complete lines of events.jsonl from byte ``offset``, and the offset after the last one.

    A trailing line without its newline is still being written: it's left for the next read.
    """
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], offset
    complete = data[: data.rfind(b"\n") + 1]
    events = []
    pos = offset
    for raw in complete.splitlines(keepends=True):
        pos += len(raw)
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            events.append(_parse(line, pos))
    return events, pos


def sse(event: str, data: str = "", event_id: int | None = None) -> str:
    """One Server-Sent Event."""
    head = f"event: {event}\n" + (f"id: {event_id}\n" if event_id is not None else "")
    body = "".join(f"data: {line}\n" for line in (data.split("\n") if data else [""]))
    return head + body + "\n"


async def stream_events(
    log: RunLog,
    offset: int,
    *,
    poll: float = POLL_SECONDS,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    """Follow ``log``'s events.jsonl from ``offset`` until no process is driving the run."""
    path = log.root / EVENTS_FILE
    offset = line_start(path, offset)
    yield "retry: 3000\n\n"  # reconnect after 3s if the connection drops
    idle = 0.0
    while True:
        events, offset = read_events(path, offset)
        driven = log.lock_holder() is not None
        if not driven:
            # Its last lines may have landed between the read above and the lock check.
            more, offset = read_events(path, offset)
            events += more
        for ev in events:
            yield sse("log", ev.html(), ev.end)
        # Always one refresh before `end`: the header must show the run's final status.
        if events or not driven:
            yield sse("refresh")
            idle = 0.0
        if not driven:
            yield sse("end", "", offset)
            return
        if not events:
            idle += poll
            if idle >= keepalive:
                yield ": keepalive\n\n"
                idle = 0.0
        await asyncio.sleep(poll)
