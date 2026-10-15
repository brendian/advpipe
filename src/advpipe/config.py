"""Load and validate pipeline.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, field_validator

from advpipe.models import Level, ScanPolicy


class _Strict(BaseModel):
    """Reject unknown keys so config typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class Models(_Strict):
    author: str = "claude-opus-5-5"
    critic: str = "claude-sonnet-5-5"
    arbiter: str = "claude-opus-5-5"


class Limits(_Strict):
    # Spec-writer passes per run: the first spec, plus one per round of answers to its
    # blocking questions. 1 = a blocked spec can't be answered (start a new run instead).
    max_rounds_spec: PositiveInt = 3
    max_rounds_tests: PositiveInt = 2
    max_rounds_code: PositiveInt = 3
    max_turns_per_agent: PositiveInt = 40
    budget_usd_per_task: PositiveFloat = 15.0
    gate_timeout_s: PositiveInt = 600
    # Extra fix passes a person can ask for in one run, when checks stay red.
    max_fix_passes: PositiveInt = 3


class Gates(_Strict):
    """Commands run from the worktree root. An empty list means the gate is skipped."""

    test: list[str] = Field(default_factory=lambda: ["pytest", "-q"])
    types: list[str] = Field(default_factory=lambda: ["mypy", "."])
    lint: list[str] = Field(default_factory=lambda: ["ruff", "check", "."])
    security: list[str] = Field(default_factory=lambda: ["semgrep", "--error", "--config", "auto"])


class Paths(_Strict):
    tests: list[str] = Field(default_factory=lambda: ["tests/"])
    standards_doc: str = "CLAUDE.md"


class Pipeline(_Strict):
    """Which level runs start at, and the security-scan switch (docs/LEVELS_PLAN.md).
    Work-item front matter and command-line options override these."""

    level: Level = Level.FULL
    security_scan: bool | None = None  # None: the level's default (on at full only)
    scan_failures: ScanPolicy = "ask"  # what a failing scan means below full
    bypass_questions: bool = False  # the agent answers blocking questions; nobody is asked

    @field_validator("level", mode="before")
    @classmethod
    def _level_name_or_number(cls, value: object) -> object:
        from advpipe.levels import parse_level  # levels imports runner, which imports us

        if isinstance(value, str | int) and not isinstance(value, bool):
            return parse_level(value)
        return value


class Config(_Strict):
    models: Models = Field(default_factory=Models)
    limits: Limits = Field(default_factory=Limits)
    gates: Gates = Field(default_factory=Gates)
    paths: Paths = Field(default_factory=Paths)
    pipeline: Pipeline = Field(default_factory=Pipeline)


def load_config(path: Path | None = None, repo: Path | None = None) -> Config:
    """Load config from ``path``, else ``<repo>/pipeline.toml``, else defaults."""
    if path is None and repo is not None and (repo / "pipeline.toml").is_file():
        path = repo / "pipeline.toml"
    if path is None:
        return Config()
    with path.open("rb") as f:
        return Config.model_validate(tomllib.load(f))
