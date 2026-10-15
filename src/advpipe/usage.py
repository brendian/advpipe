"""Which login the agents run under, how much of a subscription's usage windows is used, and
what to say when a limit stops a run (docs/LEVELS_PLAN.md §3.15).

advpipe talks to Claude through the Agent SDK, which runs the Claude Code CLI with whatever
login that CLI has. It never asks for, reads or stores a key or token. It only keeps what the
SDK's stream reports: the init message's ``apiKeySource``, rate-limit events, and the error of
an assistant message. Those events are sparse (the CLI sends one when a status *changes*), so
everything shown here is "last known", with its age, never a live gauge.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from advpipe.models import LoginKind, RateLimitSnapshot

# Fixed links for the "add usage or credits" step. They are constants, never built from run data.
ADD_USAGE_URL = "https://claude.ai/settings/usage"
ADD_CREDITS_URL = "https://platform.claude.com/settings/billing"

WINDOW_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "weekly",
    "seven_day_opus": "weekly (Opus)",
    "seven_day_sonnet": "weekly (Sonnet)",
    "overage": "extra usage",
}
# Where the window's limit is explained to a person who hit it.
WINDOW_NOUNS = {
    "five_hour": "5-hour usage limit",
    "seven_day": "weekly usage limit",
    "seven_day_opus": "weekly Opus usage limit",
    "seven_day_sonnet": "weekly Sonnet usage limit",
    "overage": "extra-usage limit",
}

LOGIN_LABELS: dict[LoginKind, str] = {
    "console": "Anthropic Console credits (real money)",
    "subscription": "Claude subscription",
    "unknown": "not known yet",
}

# Claude Code's `apiKeySource` values (read from the bundled CLI, checked 2026-10-15): where the
# credential used for requests came from. `oauth` is a `/login` with a Claude account (a
# subscription); the others are API keys, i.e. Console credits. `none` and `temporary` say
# nothing about which login it is, so they fall through to the next fallback.
CONSOLE_KEY_SOURCES = frozenset(
    {"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key", "user", "project", "org"}
)
SUBSCRIPTION_KEY_SOURCES = frozenset({"oauth"})


class _RateLimitInfo(Protocol):
    """The fields of the SDK's ``RateLimitInfo`` that are used here."""

    @property
    def status(self) -> str: ...

    @property
    def resets_at(self) -> int | None: ...

    @property
    def rate_limit_type(self) -> str | None: ...

    @property
    def utilization(self) -> float | None: ...


# --------------------------------------------------------------------------- login


def detect_login(
    api_key_source: object, saw_rate_limit_event: bool, env: Mapping[str, str] | None = None
) -> LoginKind:
    """Which login a call ran under, from the best evidence there is, in order: the init
    message's ``apiKeySource``; a rate-limit event arriving (those apply to subscription logins
    only); ``ANTHROPIC_API_KEY`` set in the environment (Console). With none of those it's
    ``unknown``, never a guess: silence doesn't mean Console."""
    if isinstance(api_key_source, str):
        if api_key_source in CONSOLE_KEY_SOURCES:
            return "console"
        if api_key_source in SUBSCRIPTION_KEY_SOURCES:
            return "subscription"
    if saw_rate_limit_event:
        return "subscription"
    if (os.environ if env is None else env).get("ANTHROPIC_API_KEY"):
        return "console"
    return "unknown"


def snapshot_of(info: _RateLimitInfo, now: datetime | None = None) -> RateLimitSnapshot:
    """The part of an SDK rate-limit event that is kept (never its raw dict)."""
    status = info.status if info.status in ("allowed", "allowed_warning", "rejected") else "allowed"
    resets = (
        datetime.fromtimestamp(info.resets_at, UTC) if isinstance(info.resets_at, int) else None
    )
    return RateLimitSnapshot(
        window=info.rate_limit_type or "five_hour",
        status=status,
        utilization=info.utilization,
        resets_at=resets,
        seen_at=now or datetime.now(UTC),
    )


def merge_snapshots(
    into: dict[str, RateLimitSnapshot], snapshots: Iterable[RateLimitSnapshot]
) -> None:
    """Keep the latest snapshot per window."""
    for snap in snapshots:
        into[snap.window] = snap


# --------------------------------------------------------------------------- the repo cache


class UsageCache(BaseModel):
    """``.advpipe/usage.json``: the last known login and usage per window, repo-wide, so the UI
    can show them even when no run is going."""

    login: LoginKind = "unknown"
    updated_at: datetime | None = None
    windows: dict[str, RateLimitSnapshot] = Field(default_factory=dict)


def cache_path(repo: Path) -> Path:
    return repo / ".advpipe" / "usage.json"


def load_cache(repo: Path) -> UsageCache:
    """The cache, or an empty one when it's missing or unreadable."""
    try:
        return UsageCache.model_validate_json(cache_path(repo).read_text())
    except (OSError, ValueError):
        return UsageCache()


def record(repo: Path, login: LoginKind, snapshots: Iterable[RateLimitSnapshot]) -> None:
    """Update the cache with what a call reported. Written via a temporary file and a rename,
    like run.json. A cache that can't be written is not worth stopping a run for."""
    snaps = list(snapshots)
    cache = load_cache(repo)
    if login == "unknown" and not snaps:
        return
    if login != "unknown":
        cache.login = login
    merge_snapshots(cache.windows, snaps)
    cache.updated_at = datetime.now(UTC)
    path = cache_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(cache.model_dump_json(indent=2))
        os.replace(tmp, path)
    except OSError:
        return


# --------------------------------------------------------------------------- wording


def window_label(window: str) -> str:
    return WINDOW_LABELS.get(window, window.replace("_", " "))


def local(when: datetime) -> datetime:
    return when.astimezone()


def format_reset(when: datetime, now: datetime | None = None) -> str:
    """A reset time for a person: ``14:00`` today, ``Mon 14:00`` within the week, else
    ``Oct 21 14:00``; in the machine's local time."""
    at, today = local(when), local(now or datetime.now(UTC))
    days = (at.date() - today.date()).days
    if days == 0:
        return at.strftime("%H:%M")
    if 0 < days < 7:
        return at.strftime("%a %H:%M")
    return at.strftime("%b %d %H:%M")


def is_stale(snap: RateLimitSnapshot, now: datetime | None = None) -> bool:
    """The window has reset since the snapshot was taken, so its usage is unknown now."""
    return snap.resets_at is not None and snap.resets_at <= (now or datetime.now(UTC))


def age(snap: RateLimitSnapshot, now: datetime | None = None) -> str:
    seconds = int(((now or datetime.now(UTC)) - snap.seen_at).total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def percent(snap: RateLimitSnapshot) -> int | None:
    return None if snap.utilization is None else round(snap.utilization * 100)


def describe(snap: RateLimitSnapshot, now: datetime | None = None) -> str:
    """``5-hour: 62% used, resets 14:00`` (or ``5-hour: reset since; usage unknown``)."""
    label = window_label(snap.window)
    if is_stale(snap, now):
        return f"{label}: reset since; usage unknown"
    pct = percent(snap)
    used = (
        f"{pct}% used"
        if pct is not None
        else {"rejected": "limit reached"}.get(snap.status, "no usage figure")
    )
    resets = f", resets {format_reset(snap.resets_at, now)}" if snap.resets_at else ""
    return f"{label}: {used}{resets}"


def describe_all(
    windows: Mapping[str, RateLimitSnapshot], now: datetime | None = None
) -> list[str]:
    order = list(WINDOW_LABELS)
    keys = sorted(windows, key=lambda w: order.index(w) if w in order else len(order))
    return [describe(windows[w], now) for w in keys]


def worst_status(windows: Mapping[str, RateLimitSnapshot], now: datetime | None = None) -> str:
    """``rejected`` or ``allowed_warning`` if a window that hasn't reset since says so, else
    ``allowed``."""
    worst = "allowed"
    for snap in windows.values():
        if is_stale(snap, now):
            continue
        if snap.status == "rejected":
            return "rejected"
        if snap.status == "allowed_warning":
            worst = "allowed_warning"
    return worst


def limit_explanation(stage: str, snap: RateLimitSnapshot | None, billing: bool) -> str:
    """The plain-words explanation shown at a ``usage_limit`` decision point."""
    tail = (
        "Or finish the run as it is: the work so far stays in the worktree, uncommitted, for "
        "you to do with as you please."
    )
    if billing:
        return (
            f"Your Anthropic Console credit balance ran out during the {stage} step. Add "
            f"credits ({ADD_CREDITS_URL}), then press Try again. {tail}"
        )
    if snap is not None:
        noun = WINDOW_NOUNS.get(snap.window, f"{window_label(snap.window)} limit")
        reset = f" It resets at {format_reset(snap.resets_at)}." if snap.resets_at else ""
        return (
            f"Your Claude subscription's {noun} was reached during the {stage} step.{reset} "
            f"Add extra usage to your plan ({ADD_USAGE_URL}), or wait for the reset, then press "
            f"Try again. {tail}"
        )
    return (
        f"Claude reported a usage limit during the {stage} step (no details were given). If "
        f"you're on a subscription, wait for the window to reset or add extra usage "
        f"({ADD_USAGE_URL}); with Console credits, check the balance ({ADD_CREDITS_URL}). Then "
        f"press Try again. {tail}"
    )


def retry_warning(snap: RateLimitSnapshot | None) -> str:
    """What *Try again* says about a limit that may still be in force."""
    base = "If the limit is still in force, the run will pause again."
    if snap is not None and snap.resets_at is not None and not is_stale(snap):
        return f"{base} The limit resets at {format_reset(snap.resets_at)}."
    return base


def console_warning(budget_usd: float, runs: int = 1) -> str:
    """The warning for a run that spends Console credits (real money)."""
    if runs > 1:
        return (
            f"Warning: these {runs} runs use Anthropic Console credits, which is real money. "
            f"They can spend up to ${budget_usd:.2f} in total (the budgets; change each with "
            "--budget)."
        )
    return (
        "Warning: this run uses Anthropic Console credits, which is real money. It can spend up "
        f"to ${budget_usd:.2f} (the budget; change it with --budget)."
    )


def likely_console(repo: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether the next agent calls will probably spend Console credits: ``ANTHROPIC_API_KEY`` is
    set, or the last known login was Console. Silence is not a reason to say yes."""
    if (os.environ if env is None else env).get("ANTHROPIC_API_KEY"):
        return True
    return load_cache(repo).login == "console"


def cost_label(auth: LoginKind) -> str:
    """How the dollar figures are labelled for this login."""
    return {
        "console": "real money",
        "subscription": "API-equivalent, not billed",
        "unknown": "API-equivalent if you're on a subscription",
    }[auth]
