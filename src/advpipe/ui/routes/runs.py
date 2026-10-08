"""The runs list: the home page, and the fragment it re-fetches every few seconds."""

from __future__ import annotations

import shlex

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from advpipe.ui.runs import FILTERS, STATUSES, load_runs
from advpipe.ui.state import ui_config

router = APIRouter()

REFRESH_SECONDS = 3


def _render(request: Request, template: str, show: str) -> HTMLResponse:
    show = show if show in FILTERS else "all"
    ui = ui_config(request)
    runs = load_runs(ui.repo)
    return ui.templates.TemplateResponse(
        request,
        template,
        {
            "show": show,
            "filters": FILTERS,
            "counts": runs.counts(),
            "rows": runs.only(show),
            "total": len(runs.rows),
            "unreadable": runs.unreadable,
            "statuses": STATUSES,
            "refresh_seconds": REFRESH_SECONDS,
            "repo": str(ui.repo),
            "repo_arg": shlex.quote(str(ui.repo)),
            "nav_current": "/",
        },
    )


@router.get("/", response_class=HTMLResponse)
def runs_page(request: Request, show: str = "all") -> HTMLResponse:
    return _render(request, "runs.html", show)


@router.get("/runs/list", response_class=HTMLResponse)
def runs_fragment(request: Request, show: str = "all") -> HTMLResponse:
    return _render(request, "_runs_list.html", show)
