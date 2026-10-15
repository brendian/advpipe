"""The login and usage line the pages show next to the budget (docs/LEVELS_PLAN.md §3.15).

Claude reports usage only when it changes, so everything here is "last known", with its age,
never a live gauge. A run's page uses the snapshots saved in its run.json; every other page uses
the repo-wide cache (``.advpipe/usage.json``), read once per render.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from advpipe import usage
from advpipe.models import LoginKind, RateLimitSnapshot, RunState


@dataclass(frozen=True)
class Meter:
    window: str
    label: str  # "5-hour"
    percent: int | None  # None: no figure, or the window has reset since
    text: str  # "62% used, resets 14:00"
    status: str  # allowed | allowed_warning | rejected
    stale: bool
    seen: str  # "5 min ago"


@dataclass(frozen=True)
class UsageView:
    login: LoginKind
    meters: list[Meter] = field(default_factory=list)
    console: bool = False  # known or likely Console credits: pages warn about real money
    warn: str = ""  # a window is near or at its limit
    note: str = ""  # said when there are no meters

    @property
    def login_label(self) -> str:
        return usage.LOGIN_LABELS[self.login]

    @property
    def cost_label(self) -> str:
        """How the page labels dollar figures: real money, or an API-equivalent estimate."""
        if self.console or self.login == "console":
            return usage.cost_label("console")
        return usage.cost_label(self.login)

    @property
    def known(self) -> bool:
        return self.login != "unknown" or bool(self.meters)

    def console_text(self, budget_usd: float | str | None) -> str:
        """The warning for a form that starts or restarts agent work."""
        spend = f"up to ${budget_usd} of real money" if budget_usd is not None else "real money"
        return f"This run uses Anthropic Console credits: it can spend {spend}."


def _meters(windows: Mapping[str, RateLimitSnapshot], now: datetime) -> list[Meter]:
    order = list(usage.WINDOW_LABELS)
    out = []
    for window in sorted(windows, key=lambda w: order.index(w) if w in order else len(order)):
        snap = windows[window]
        stale = usage.is_stale(snap, now)
        text = usage.describe(snap, now).split(": ", 1)[1]
        out.append(
            Meter(
                window=window,
                label=usage.window_label(window),
                percent=None if stale else usage.percent(snap),
                text=text,
                status="allowed" if stale else snap.status,
                stale=stale,
                seen=usage.age(snap, now),
            )
        )
    return out


def _view(
    login: LoginKind, windows: Mapping[str, RateLimitSnapshot], console: bool, now: datetime
) -> UsageView:
    worst = usage.worst_status(windows, now)
    warn = {
        "rejected": "A usage limit is reached: a run started now will pause until it resets.",
        "allowed_warning": "A usage window is nearly used up: a long run may pause at its limit.",
    }.get(worst, "")
    if login == "console" or console:
        note = "Console credits have no usage windows: every call is billed."
    else:
        note = "Not known yet: advpipe sees usage only when Claude reports a change."
    return UsageView(login, _meters(windows, now), console, warn, note)


def for_repo(repo: Path, now: datetime | None = None) -> UsageView:
    """From the repo-wide cache: the last known login and usage, whatever run saw them."""
    now = now or datetime.now(UTC)
    cache = usage.load_cache(repo)
    console = cache.login == "console" or (cache.login == "unknown" and usage.likely_console(repo))
    login: LoginKind = "console" if console and cache.login == "unknown" else cache.login
    return _view(login, cache.windows, console, now)


def for_run(repo: Path, state: RunState, now: datetime | None = None) -> UsageView:
    """From the run's own saved snapshots. A run that hasn't learned its login yet falls back to
    what the repo's cache says about whether it will cost real money."""
    now = now or datetime.now(UTC)
    console = state.auth == "console" or (state.auth == "unknown" and usage.likely_console(repo))
    return _view(state.auth, state.rate_limits, console, now)
