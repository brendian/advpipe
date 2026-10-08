"""The run detail page, its refreshable fragment, the live event stream, and the spec &
context tab."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from advpipe.runlog import EVENTS_FILE, RunLog, check_run_id
from advpipe.ui import live
from advpipe.ui.context import load_context
from advpipe.ui.detail import RunDetail, Starting, load_detail, load_starting
from advpipe.ui.state import ui_config

router = APIRouter()

T = TypeVar("T")


def _error(request: Request, status_code: int, title: str, message: str) -> HTMLResponse:
    return ui_config(request).templates.TemplateResponse(
        request,
        "error.html",
        {"title": title, "message": message, "nav_current": "/"},
        status_code=status_code,
    )


def _load(
    request: Request, run_id: str, loader: Callable[[Path, str], T | None]
) -> T | HTMLResponse:
    """What ``loader`` reads for the run, or an error page saying why there's nothing."""
    try:
        check_run_id(run_id)
    except ValueError:
        return _error(request, 404, "No such run", "That isn't a valid run id.")
    try:
        detail = loader(ui_config(request).repo, run_id)
    except ValueError as e:
        return _error(request, 500, "Can't read this run", f"run.json couldn't be parsed: {e}")
    except OSError as e:
        return _error(request, 500, "Can't read this run", f"run.json couldn't be read: {e}")
    if detail is None:
        return _error(request, 404, "No such run", f"There's no run {run_id} in this repo.")
    return detail


def _context(detail: RunDetail) -> dict[str, object]:
    return {
        "d": detail,
        "state": detail.state,
        "nav_current": "/",
    }


# After an action, the page it redirects to says what happened (fixed texts, keyed by name).
NOTICES = {
    "started": "Started in the background. It keeps going if you close this page or the UI.",
    "resumed": "Resumed in the background, from the first step that didn't finish.",
    "cancelled": "Asked it to stop. It stops at its next safe point (after the agent call or "
    "check it's in), and can be resumed later.",
    "cleaned": "Cleaned up: its working copy and branch are removed. The log stays here.",
}


def _starting(request: Request, run_id: str) -> Starting | None:
    try:
        return load_starting(ui_config(request).repo, check_run_id(run_id))
    except (OSError, ValueError):
        return None


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str, notice: str = "") -> HTMLResponse:
    starting = _starting(request, run_id)
    if starting is not None:
        return ui_config(request).templates.TemplateResponse(
            request,
            "run_starting.html",
            {"s": starting, "notice": NOTICES.get(notice, ""), "nav_current": "/"},
        )
    detail = _load(request, run_id, load_detail)
    if isinstance(detail, HTMLResponse):
        return detail
    log = RunLog.for_run(ui_config(request).repo, run_id)
    # The page shows the log so far; the live stream carries on from exactly this offset.
    events, offset = live.read_events(log.root / EVENTS_FILE)
    return ui_config(request).templates.TemplateResponse(
        request,
        "run.html",
        {
            **_context(detail),
            "events": [e.html() for e in events],
            "offset": offset,
            "tab": "progress",
            "notice": NOTICES.get(notice, ""),
        },
    )


@router.get("/runs/{run_id}/starting", response_class=HTMLResponse)
def run_starting(request: Request, run_id: str) -> Response:
    """Polled by the starting-up page: the same panel again, or, once run.json exists, a
    reload of the page, which then shows the run."""
    starting = _starting(request, run_id)
    if starting is None:
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return ui_config(request).templates.TemplateResponse(
        request, "_run_starting.html", {"s": starting}
    )


@router.get("/runs/{run_id}/context", response_class=HTMLResponse)
def run_context(request: Request, run_id: str) -> HTMLResponse:
    """The spec & context tab: everything the agents worked from, and who sees what."""
    ctx = _load(request, run_id, load_context)
    if isinstance(ctx, HTMLResponse):
        return ctx
    return ui_config(request).templates.TemplateResponse(
        request, "run_context.html", {"c": ctx, "tab": "context", "nav_current": "/"}
    )


@router.get("/runs/{run_id}/body", response_class=HTMLResponse)
def run_fragment(request: Request, run_id: str) -> HTMLResponse:
    """The header (swapped in place) plus the timeline and report (swapped out of band).
    Re-fetched by the page on each ``refresh`` event of the live stream."""
    detail = _load(request, run_id, load_detail)
    if isinstance(detail, HTMLResponse):
        return detail
    return ui_config(request).templates.TemplateResponse(
        request, "_run_fragment.html", _context(detail)
    )


@router.get("/runs/{run_id}/events")
def run_events(
    request: Request,
    run_id: str,
    offset: int = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> Response:
    """Server-Sent Events: the run's events.jsonl from byte ``offset`` (or from the browser's
    ``Last-Event-ID`` when it reconnects), until no process is driving the run."""
    try:
        log = RunLog.for_run(ui_config(request).repo, run_id)
    except ValueError:
        return Response("no such run\n", status_code=404, media_type="text/plain")
    if not (log.root / "run.json").is_file():
        return Response("no such run\n", status_code=404, media_type="text/plain")
    if last_event_id is not None and last_event_id.isdigit():
        offset = int(last_event_id)
    return StreamingResponse(
        live.stream_events(log, offset, poll=live.POLL_SECONDS),
        media_type="text/event-stream",
        headers={"x-accel-buffering": "no"},
    )
