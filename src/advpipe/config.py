"""Load and validate pipeline.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt


class _Strict(BaseModel):
    """Reject unknown keys so config typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class Models(_Strict):
    author: str = "claude-opus-5-5"
    critic: str = "claude-sonnet-5-5"
    arbiter: str = "claude-opus-5-5"


class Limits(_Strict):
    max_rounds_tests: PositiveInt = 2
    max_rounds_code: PositiveInt = 3
    max_turns_per_agent: PositiveInt = 40
    budget_usd_per_task: PositiveFloat = 15.0
    gate_timeout_s: PositiveInt = 600


class Gates(_Strict):
    """Commands run from the worktree root. An empty list means the gate is skipped."""

    test: list[str] = Field(default_factory=lambda: ["pytest", "-q"])
    types: list[str] = Field(default_factory=lambda: ["mypy", "."])
    lint: list[str] = Field(default_factory=lambda: ["ruff", "check", "."])
    security: list[str] = Field(default_factory=lambda: ["semgrep", "--error", "--config", "auto"])


class Paths(_Strict):
    tests: list[str] = Field(default_factory=lambda: ["tests/"])
    standards_doc: str = "CLAUDE.md"


class Config(_Strict):
    models: Models = Field(default_factory=Models)
    limits: Limits = Field(default_factory=Limits)
    gates: Gates = Field(default_factory=Gates)
    paths: Paths = Field(default_factory=Paths)


def load_config(path: Path | None = None, repo: Path | None = None) -> Config:
    """Load config from ``path``, else ``<repo>/pipeline.toml``, else defaults."""
    if path is None and repo is not None and (repo / "pipeline.toml").is_file():
        path = repo / "pipeline.toml"
    if path is None:
        return Config()
    with path.open("rb") as f:
        return Config.model_validate(tomllib.load(f))
