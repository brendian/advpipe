"""Per-app settings shared by the route modules (set by ``create_ui_app``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates


@dataclass(frozen=True)
class UiConfig:
    repo: Path
    items_dir: Path
    csrf_token: str
    templates: Jinja2Templates


def ui_config(request: Request) -> UiConfig:
    config: UiConfig = request.app.state.ui
    return config
