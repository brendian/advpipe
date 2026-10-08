"""The local web UI's app factory. Started by `advpipe ui`; see docs/UI_PLAN.md."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.exceptions import HTTPException

from advpipe.ui.routes import actions, help, items, run_detail, runs
from advpipe.ui.security import LOOPBACK_HOSTS, GuardMiddleware
from advpipe.ui.state import UiConfig, ui_config

HERE = Path(__file__).parent

# (label, path) for the top navigation.
NAV: list[tuple[str, str]] = [("Runs", "/"), ("Work items", "/items"), ("Help", "/help")]

# Pages for errors FastAPI raises itself (no route, wrong method): (title, message).
HTTP_ERRORS = {
    404: ("Page not found", "There's no page at this address."),
    405: ("Not allowed", "This page can't be used that way."),
}


def create_ui_app(
    repo: Path,
    items_dir: Path,
    token: str,
    *,
    allowed_hosts: frozenset[str] | None = LOOPBACK_HOSTS,
) -> FastAPI:
    """The UI for one repository. ``token`` is the access token printed at startup;
    ``allowed_hosts`` the Host header names accepted (None: any, for --i-know-this-is-exposed).
    """
    repo = repo.resolve()
    csrf_token = secrets.token_urlsafe(32)
    # Autoescape everything: run data includes agent output, which is untrusted.
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=True,
        undefined=StrictUndefined,
    )

    def context(request: Request) -> dict[str, Any]:
        return {"repo_name": repo.name, "csrf_token": csrf_token, "nav": NAV}

    # No generated API docs: there is no public API.
    app = FastAPI(title="advpipe", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ui = UiConfig(
        repo=repo,
        items_dir=(repo / items_dir).resolve(),
        csrf_token=csrf_token,
        templates=Jinja2Templates(env=env, context_processors=[context]),
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(runs.router)
    app.include_router(actions.router)  # before run_detail: /runs/new isn't a run id
    app.include_router(run_detail.router)
    app.include_router(items.router)
    app.include_router(help.router)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> HTMLResponse:
        title, message = HTTP_ERRORS.get(exc.status_code, ("Something went wrong", ""))
        return ui_config(request).templates.TemplateResponse(
            request,
            "error.html",
            {"title": title, "message": message, "nav_current": ""},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    app.add_middleware(
        GuardMiddleware, token=token, csrf_token=csrf_token, allowed_hosts=allowed_hosts
    )
    return app
