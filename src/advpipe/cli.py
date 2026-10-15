"""The CLI: `advpipe run | answer | resume | cancel | clean | status | report | levels | ui |
gates`."""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperCommand

from advpipe import answers, decisions, usage
from advpipe.answers import AnswerError
from advpipe.config import Config, load_config
from advpipe.control import CONSOLE_LOG, ControlError, cancel_run, clean_run, start_detached
from advpipe.decisions import DecisionError
from advpipe.gates import run_gates
from advpipe.levels import (
    LEVEL_INFO,
    LEVEL_ORDER,
    SHAPES,
    dropped_safeguards,
    level_number,
    level_tag,
    max_agent_calls,
    parse_level,
    roles_for,
    scan_on,
)
from advpipe.models import Level, RunState, Status
from advpipe.orchestrator import (
    NotResumable,
    Orchestrator,
    check_resumable,
    check_same_shape,
    new_run_id,
    run_many,
)
from advpipe.runlog import RunLog, check_run_id, list_runs, scan_policy_line
from advpipe.runner import SdkAgentRunner
from advpipe.workitem import SCAN_FAILURES, WorkItemError, load_work_item, switch

app = typer.Typer(no_args_is_help=True, help="Adversarial multi-agent coding pipeline.")

RepoOpt = Annotated[Path, typer.Option(help="Target git repository.")]
ConfigOpt = Annotated[
    Path | None, typer.Option(help="pipeline.toml (default: <repo>/pipeline.toml).")
]
QuietOpt = Annotated[bool, typer.Option("--quiet", "-q", help="Don't print live progress.")]
KeepOpt = Annotated[
    bool, typer.Option(help="Keep the worktree after a successful run (it's removed by default).")
]


def _run_id_arg(value: str | None) -> str | None:
    """Run ids are directory names: reject anything that could leave .advpipe/runs/."""
    if value is None:
        return None
    try:
        return check_run_id(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


RunIdArg = Annotated[str, typer.Argument(callback=_run_id_arg, help="Run id (see advpipe status).")]


def _level_opt(value: str | None) -> Level | None:
    if value is None:
        return None
    try:
        return parse_level(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


def _scan_failures_opt(value: str | None) -> str | None:
    if value is not None and value not in SCAN_FAILURES:
        raise typer.BadParameter(f"use one of {', '.join(SCAN_FAILURES)}")
    return value


@app.callback()
def main() -> None:
    """Adversarial multi-agent coding pipeline."""


def make_progress(quiet: bool, label: str = "") -> Callable[[str], None]:
    """Progress printer: timestamped lines on stderr (stdout stays for the final summary)."""
    if quiet:
        return lambda message: None
    prefix = f"[{label}] " if label else ""

    def emit(message: str) -> None:
        print(f"{datetime.now():%H:%M:%S} {prefix}{message}", file=sys.stderr, flush=True)

    return emit


def _summary(state: RunState, runlog: RunLog) -> None:
    tag = level_tag(state)  # nothing for full with the scan on: that line reads as it always has
    # A subscription login isn't billed per call, so its dollar figure is an estimate.
    estimate = " (API-equivalent)" if state.auth == "subscription" else ""
    typer.echo(
        f"{state.run_id}  {state.status.value}  ${state.cost_usd:.2f}{estimate}  {state.branch}"
        + (f"  [{tag}]" if tag else "")
    )
    if state.auth == "subscription" and state.rate_limits:
        typer.echo(f"  usage, last known: {'; '.join(usage.describe_all(state.rate_limits))}")
    if answers.awaiting_answers(state, answers.max_spec_passes(runlog)):
        n = len(answers.pending(state))
        typer.echo(
            f"  {n} blocking question{'s' if n != 1 else ''}. Answer them, and the run "
            f"continues: {answers.answer_command(state.run_id)}"
        )
    if decisions.awaiting_decision(state):
        for line in _decision_lines(state):
            typer.echo(f"  {line}")
    elif state.ended_by:
        typer.echo(f"  {_ended_line(state)}")
    typer.echo(f"  report: {runlog.root / 'report.md'}")


def _decision_lines(state: RunState) -> list[str]:
    """What a run waiting at a decision point asks, and the command for each choice."""
    d = state.decision
    assert d is not None
    lines = [d.explanation]
    for choice in decisions.CHOICES[d.kind]:
        label = decisions.CHOICE_LABELS[choice]
        lines.append(f"{label + ':':<44}advpipe decide {state.run_id} {choice}")
    return lines


def _ended_line(state: RunState) -> str:
    if state.ended_by == "takeover":
        return f"handed over to you: git switch {state.branch}"
    if state.ended_by == "finish":
        return f"finished as is: the uncommitted work is in {state.worktree}"
    return "stopped at your choice; nothing more will run"


def _warn_console(repo: Path, budget_usd: float, runs: int = 1) -> bool:
    """Warn, before agent work starts, when the login is known or likely to be Anthropic Console
    credits (real money). A warning, not a prompt: scripted and detached runs aren't held up.
    True if it was printed (so the run needn't say it again)."""
    if not usage.likely_console(repo.resolve()):
        return False
    typer.echo(usage.console_warning(budget_usd, runs), err=True)
    return True


def _short(text: str, width: int) -> str:
    """First line of ``text``, cut to ``width`` characters."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first if len(first) <= width else first[: width - 3] + "..."


def _read_items(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _interruptible() -> None:
    """Make SIGINT raise KeyboardInterrupt even if this process was started with it ignored
    (e.g. by a shell's `&`), so `advpipe cancel` always works."""
    if signal.getsignal(signal.SIGINT) is signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)


def _detach(
    repo: Path,
    run_id: str,
    *,
    work_item: str | None,
    item: Path | None,
    config: Path | None,
    name: str | None,
    keep_worktree: bool,
    level: Level | None,
    security_scan: bool | None,
    scan_failures: str | None,
    bypass_questions: bool | None,
) -> None:
    """Start the same run in a background process; print its run id and return at once."""
    args = ["run", "--repo", str(repo.resolve()), "--run-id", run_id]
    if config is not None:
        args += ["--config", str(config.resolve())]
    if name is not None:
        args += ["--name", name]
    if level is not None:
        args += ["--level", level.value]
    if security_scan is not None:
        args.append("--security-scan" if security_scan else "--no-security-scan")
    if scan_failures is not None:
        args += ["--scan-failures", scan_failures]
    if bypass_questions is not None:
        args.append("--bypass-questions" if bypass_questions else "--no-bypass-questions")
    if keep_worktree:
        args.append("--keep-worktree")
    if item is not None:
        args += ["--item", str(item.resolve())]
    else:
        assert work_item is not None
        args += ["--", work_item]  # "--": a work item starting with "-" isn't an option
    _background(repo, run_id, args, "started")


def _background(repo: Path, run_id: str, args: list[str], verb: str) -> None:
    """Run ``advpipe <args>`` detached; print the run id on stdout and how to follow it."""
    runlog = RunLog.for_run(repo.resolve(), run_id)
    pid = start_detached(runlog, args)
    typer.echo(run_id)
    typer.echo(
        f"{verb} in the background (pid {pid}); output: {runlog.root / CONSOLE_LOG}\n"
        f"follow it: advpipe status {run_id}   stop it: advpipe cancel {run_id}",
        err=True,
    )


@app.command()
def run(
    work_item: Annotated[
        str | None, typer.Argument(help="The feature or bug fix to build.")
    ] = None,
    repo: RepoOpt = Path("."),
    config: ConfigOpt = None,
    item: Annotated[
        Path | None,
        typer.Option(
            help="Read the work item from this markdown file (front matter may set name and "
            "config).",
            dir_okay=False,
        ),
    ] = None,
    from_file: Annotated[
        Path | None, typer.Option(help="Run every non-empty line of this file as a work item.")
    ] = None,
    parallel: Annotated[int, typer.Option(min=1, help="Work items to run at once.")] = 1,
    keep_worktree: KeepOpt = False,
    quiet: QuietOpt = False,
    name: Annotated[
        str | None,
        typer.Option(
            help="Branch name: advpipe/<name>. Default: derived from the work item's first line."
        ),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option(
            help="Run in the background: print the run id and return. Output goes to the run's "
            "console.log."
        ),
    ] = False,
    run_id: Annotated[
        str | None, typer.Option(hidden=True, callback=_run_id_arg, help="Use this run id.")
    ] = None,
    level: Annotated[
        str | None,
        typer.Option(
            callback=_level_opt,
            help="How much checking: basic, reviewer, tests, adversarial or full (or 1-5). "
            "Default: the work item's front matter, else [pipeline] level, else full. See "
            "advpipe levels.",
        ),
    ] = None,
    security_scan: Annotated[
        bool | None,
        typer.Option(
            "--security-scan/--no-security-scan",
            help="Run the security scan. Default: the work item's front matter, else "
            "[pipeline] security_scan, else on at level full only.",
            show_default=False,
        ),
    ] = None,
    scan_failures: Annotated[
        str | None,
        typer.Option(
            callback=_scan_failures_opt,
            help="Below level full, what a failing security scan means: ask (the run stops and "
            "asks you; the default), ignore (record the hits as minor findings) or block (treat "
            "them like a failing test).",
        ),
    ] = None,
    bypass_questions: Annotated[
        bool | None,
        typer.Option(
            "--bypass-questions/--no-bypass-questions",
            help="Never stop for blocking questions: the agent answers them itself, with its "
            "best judgement, and says so in task.md and the report. Default: the work item's "
            "front matter, else [pipeline] bypass_questions, else off. Rarely the right choice.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Drive work items through the pipeline, leaving a branch per item for human review."""
    item_file = ""
    run_level = parse_level(level) if level is not None else None  # checked by _level_opt
    item_level, item_scan, item_failures, item_bypass = None, None, None, None
    if sum(x is not None for x in (work_item, item, from_file)) != 1:
        raise typer.BadParameter("give exactly one of WORK_ITEM, --item or --from-file")
    if from_file is not None:
        items = _read_items(from_file)
        if not items:
            raise typer.BadParameter(f"no work items in {from_file}")
    elif item is not None:
        try:
            parsed = load_work_item(item)
        except WorkItemError as e:
            raise typer.BadParameter(str(e)) from e
        items, item_file = [parsed.body], str(item.resolve())
        # Command-line options win over front matter. Its config path is relative to the repo.
        name = name if name is not None else parsed.name
        item_level = parse_level(parsed.level) if parsed.level else None
        item_scan = switch(parsed.security_scan) if parsed.security_scan else None
        item_failures = parsed.scan_failures
        item_bypass = switch(parsed.bypass_questions) if parsed.bypass_questions else None
        if config is None and parsed.config is not None:
            config = repo / parsed.config
            if not config.is_file():
                raise typer.BadParameter(f"config file not found: {config} (set in {item})")
    else:
        assert work_item is not None
        if not work_item.strip():
            raise typer.BadParameter(
                'the work item is empty. If you used "$(cat FILE)", check that FILE exists, '
                "or use --item FILE."
            )
        items = [work_item]
    if name is not None and len(items) > 1:
        raise typer.BadParameter("--name works with a single work item, not --from-file")
    if (detach or run_id is not None) and len(items) > 1:
        raise typer.BadParameter("--detach works with a single work item, not --from-file")
    if run_id is not None and (RunLog.for_run(repo.resolve(), run_id).root / "run.json").exists():
        raise typer.BadParameter(f"run {run_id} already exists")
    cfg = _with_run_settings(
        load_config(config, repo),
        level=run_level or item_level,
        security_scan=security_scan if security_scan is not None else item_scan,
        scan_failures=scan_failures or item_failures,
        bypass_questions=bypass_questions if bypass_questions is not None else item_bypass,
    )
    warned = _warn_console(repo, cfg.limits.budget_usd_per_task * len(items), len(items))
    if detach:
        _detach(
            repo,
            run_id or new_run_id(),
            work_item=work_item,
            item=item,
            config=config,
            name=name,
            keep_worktree=keep_worktree,
            level=run_level,
            security_scan=security_scan,
            scan_failures=scan_failures,
            bypass_questions=bypass_questions,
        )
        return
    _interruptible()
    orchestrators: list[Orchestrator] = []

    def make(item: str) -> Orchestrator:
        # Several runs share the terminal: prefix their lines with "item N" to tell them apart.
        label = f"item {len(orchestrators) + 1}" if len(items) > 1 else ""
        orch = Orchestrator(
            cfg,
            SdkAgentRunner(),
            repo,
            item,
            keep_worktree=keep_worktree,
            progress=make_progress(quiet, label),
            name=name,
            run_id=run_id,
            work_item_file=item_file,
            console_warned=warned,
        )
        orchestrators.append(orch)
        return orch

    try:
        states = asyncio.run(run_many(items, make, parallel))
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None  # the run already said how to resume
    for orch, state in zip(orchestrators, states, strict=True):
        _summary(state, orch.runlog)
    if len(states) == 1 and _decide_now(states[0], repo, keep_worktree=keep_worktree, quiet=quiet):
        return
    if any(s.status is not Status.COMPLETE for s in states):
        raise typer.Exit(code=1)


def _with_run_settings(
    cfg: Config,
    *,
    level: Level | None,
    security_scan: bool | None,
    scan_failures: str | None,
    bypass_questions: bool | None,
) -> Config:
    """``cfg`` with the level and scan settings from the command line or front matter (the
    caller has picked whichever wins) in its [pipeline] table, so the run's config.json
    records what it runs under."""
    update: dict[str, object] = {}
    if level is not None:
        update["level"] = level
    if security_scan is not None:
        update["security_scan"] = security_scan
    if scan_failures is not None:
        update["scan_failures"] = scan_failures
    if bypass_questions is not None:
        update["bypass_questions"] = bypass_questions
    if not update:
        return cfg
    pipeline = cfg.pipeline.model_validate({**cfg.pipeline.model_dump(), **update})
    return cfg.model_copy(update={"pipeline": pipeline})


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _answers_given(answer: list[str], file: Path | None) -> tuple[dict[str, str], set[str]]:
    """Answers from --file, then --answer ID=TEXT (which wins for the same question), and the
    ids a file bypasses (``Q2: BYPASS``)."""
    given: dict[str, str] = {}
    skipped: set[str] = set()
    if file is not None:
        try:
            from_file, skipped = answers.split_bypass(answers.parse_answers_file(file.read_text()))
        except OSError as e:
            raise AnswerError(f"can't read {file}: {e}") from e
        given.update(from_file)
    for item in answer:
        qid, sep, text = item.partition("=")
        if not sep or not answers.QUESTION_ID_RE.fullmatch(qid.strip().upper()):
            raise AnswerError(
                f"--answer takes ID=TEXT, like --answer 'Q1=raise ValueError'; got {item!r}"
            )
        given[qid.strip().upper()] = text
        skipped.discard(qid.strip().upper())
    return given, skipped


def _bypass_ids(values: list[str], state: RunState, given: dict[str, str]) -> set[str]:
    """The question ids ``--bypass`` names: a bare ``--bypass`` (or ``all``) means every
    question still waiting that isn't answered with --answer or --file."""
    ids: set[str] = set()
    for value in values:
        for part in value.replace(" ", "").split(","):
            if not part:
                continue
            if part.lower() == "all":
                ids |= {q.id for q in answers.pending(state)} - set(given)
            elif answers.QUESTION_ID_RE.fullmatch(part.upper()):
                ids.add(part.upper())
            else:
                raise AnswerError(f"--bypass takes question ids like Q2, or nothing; got {part!r}")
    return ids


def _ask(state: RunState) -> tuple[dict[str, str], set[str]]:
    """Ask each question in the terminal. An empty answer offers Bypass for that question."""
    given: dict[str, str] = {}
    skipped: set[str] = set()
    questions = answers.to_answer(state)
    typer.echo(
        f"The spec for run {state.run_id} has {len(questions)} blocking question(s). "
        "Answer each on one line (for longer answers, use --file). Leave an answer empty to "
        "let the agent decide.",
        err=True,
    )
    for q in questions:
        typer.echo(f"\n{q.id}: {q.text}", err=True)
        while True:
            text = typer.prompt(">", default=q.answer, show_default=bool(q.answer))
            if text.strip():
                given[q.id] = text
                break
            if typer.confirm(f"Bypass {q.id}? The agent decides it with its best judgement"):
                skipped.add(q.id)
                break
    return given, skipped


class _AnswerCommand(TyperCommand):
    """`--bypass` takes question ids, or nothing (every question still waiting). Click can't
    make a value optional, so a bare `--bypass` is rewritten to `--bypass=all` first."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        out: list[str] = []
        for i, arg in enumerate(args):
            following = args[i + 1] if i + 1 < len(args) else ""
            has_value = following.lower() == "all" or answers.QUESTION_ID_RE.fullmatch(
                following.upper().split(",")[0]
            )
            out.append("--bypass=all" if arg == "--bypass" and not has_value else arg)
        return super().parse_args(ctx, out)


@app.command(cls=_AnswerCommand)
def answer(
    run_id: RunIdArg,
    repo: RepoOpt = Path("."),
    answer: Annotated[
        list[str] | None,
        typer.Option(
            "--answer",
            help="An answer as ID=TEXT, e.g. --answer 'Q1=raise ValueError'. Repeat it for "
            "each question.",
        ),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option(
            dir_okay=False,
            help="Read the answers from this file: each starts with a line 'Q1: ...' (see "
            "--template).",
        ),
    ] = None,
    bypass: Annotated[
        list[str] | None,
        typer.Option(
            "--bypass",
            metavar="[IDS]",
            help="Let the agent decide instead of answering: --bypass for every question not "
            "answered with --answer, or --bypass Q2 (repeatable) for just those.",
        ),
    ] = None,
    template: Annotated[
        bool, typer.Option(help="Print an answers file to fill in, then exit.")
    ] = False,
    resume_run: Annotated[
        bool,
        typer.Option("--resume/--no-resume", help="Resume the run once the answers are saved."),
    ] = True,
    detach: Annotated[
        bool, typer.Option(help="Resume in the background (output goes to console.log).")
    ] = False,
    budget: Annotated[
        float | None, typer.Option(help="New per-task budget in USD for the resumed run.")
    ] = None,
    config: ConfigOpt = None,
    keep_worktree: KeepOpt = False,
    quiet: QuietOpt = False,
) -> None:
    """Answer the blocking questions a run's spec stopped on, then resume the run."""
    runlog = RunLog.for_run(repo.resolve(), run_id)
    if not (runlog.root / "run.json").is_file():
        typer.echo(f"no run {run_id} in {repo}", err=True)
        raise typer.Exit(code=2)
    state = answers.migrate(runlog.read_state())
    cap = answers.max_spec_passes(runlog)
    waiting = answers.awaiting_answers(state, cap)
    ready = answers.answers_ready(state, cap)
    try:
        if not (waiting or ready):
            raise AnswerError(answers.not_waiting_message(state, cap))
        if template:
            typer.echo(answers.answers_template(state), nl=False)
            return
        skipped: set[str] = set()
        if answer or file is not None or bypass:
            given, skipped = _answers_given(answer or [], file)
            skipped |= _bypass_ids(bypass or [], state, given)
        elif ready:
            given = {}  # answered already (e.g. with --no-resume): just resume
        elif _stdin_is_tty():
            given, skipped = _ask(state)
        else:
            lines = [f"run {run_id} is waiting on answers to:"]
            lines += [f"  {q.id}: {q.text}" for q in answers.to_answer(state)]
            lines.append(
                f"answer with: advpipe answer {run_id} --answer 'Q1=...' (one per question), "
                f"or --file FILE (advpipe answer {run_id} --template prints one to fill in). "
                f"To let the agent decide instead: advpipe answer {run_id} --bypass"
            )
            typer.echo("\n".join(lines), err=True)
            raise typer.Exit(code=2)
        if given or skipped:
            saved = answers.record_answers(repo, run_id, given, skipped)
            ids = ", ".join(q.id for q in answers.unapplied(saved))
            if ids:
                typer.echo(f"answers saved for {ids}", err=True)
            if skipped:
                names = ", ".join(sorted(skipped, key=lambda i: (len(i), i)))
                typer.echo(f"Bypass: the agent decides {names}", err=True)
    except AnswerError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    if not resume_run:
        typer.echo(f"continue with: advpipe resume {run_id}", err=True)
        return
    resume(
        run_id,
        repo=repo,
        config=config,
        budget=budget,
        keep_worktree=keep_worktree,
        quiet=quiet,
        detach=detach,
        detached_child=False,
    )


def _decide_now(state: RunState, repo: Path, *, keep_worktree: bool, quiet: bool) -> bool:
    """A foreground run stopped at a decision point and a person is at the terminal: ask at
    once. True if they chose (and what followed has decided the exit code), False to leave the
    run waiting ("later"), as a detached run does."""
    if not (decisions.awaiting_decision(state) and _stdin_is_tty()):
        return False
    d = state.decision
    assert d is not None
    options = [*decisions.CHOICES[d.kind], "later"]
    typer.echo("")
    typer.echo(d.explanation)
    for choice in decisions.CHOICES[d.kind]:
        typer.echo(f"  {choice}: {decisions.CHOICE_LABELS[choice]}")
    typer.echo("  later: decide later (the run waits)")
    while True:
        pick = typer.prompt(f"Your choice ({' / '.join(options)})", default="later").strip().lower()
        if pick in options:
            break
        typer.echo(f"choose one of: {', '.join(options)}", err=True)
    if pick == "later":
        typer.echo(f"decide later with: {decisions.decide_command(state.run_id, d.kind)}")
        return False
    note = ""
    if pick == "fix":
        note = typer.prompt("A note for the author (optional)", default="", show_default=False)
    _choose(
        state.run_id,
        repo,
        pick,
        note,
        resume_run=True,
        detach=False,
        budget=None,
        config=None,
        keep_worktree=keep_worktree,
        quiet=quiet,
    )
    return True


def _choose(
    run_id: str,
    repo: Path,
    choice: str,
    note: str,
    *,
    resume_run: bool,
    detach: bool,
    budget: float | None,
    config: Path | None,
    keep_worktree: bool,
    quiet: bool,
) -> None:
    """Record the choice, then resume the run exactly as `advpipe resume` does (unless the
    choice ended the run)."""
    try:
        state = decisions.decide(repo, run_id, choice, note)
    except DecisionError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    if state.ended_by:
        _print_ended(state, repo)
        return
    typer.echo(f"you chose {choice}", err=True)
    if not resume_run:
        typer.echo(f"continue with: advpipe resume {run_id}", err=True)
        return
    resume(
        run_id,
        repo=repo,
        config=config,
        budget=budget,
        keep_worktree=keep_worktree,
        quiet=quiet,
        detach=detach,
        detached_child=False,
    )


def _print_ended(state: RunState, repo: Path) -> None:
    """What's where after a choice that ends the run."""
    if state.ended_by == "takeover":
        cfg = RunLog.for_run(repo.resolve(), state.run_id).read_config() or Config()
        typer.echo(f"handed over: branch {state.branch} has the work as it was")
        typer.echo(f"  git switch {state.branch}")
        checks = [" ".join(cmd) for cmd in (cfg.gates.test, cfg.gates.types, cfg.gates.lint) if cmd]
        if checks:
            typer.echo("  the checks: " + "; ".join(checks))
        typer.echo(f"  clean up later with: advpipe clean {state.run_id} --force")
    elif state.ended_by == "finish":
        typer.echo(f"finished as is: run {state.run_id} ends here; nothing was committed")
        typer.echo(f"  the work so far, uncommitted, is in {state.worktree}")
        typer.echo(f"  look at it: git -C {state.worktree} status")
        typer.echo(f"  clean up later with: advpipe clean {state.run_id} (--force deletes it)")
    else:
        typer.echo(f"stopped: run {state.run_id} ends here; nothing was written to the branch")


@app.command()
def decide(
    run_id: RunIdArg,
    choice: Annotated[
        str | None,
        typer.Argument(
            help="fix | takeover (checks are failing), ignore | block (security scan hits), "
            "retry | finish (a usage limit or credit balance ran out), "
            "begin | stop (blocking questions after the last spec pass)."
        ),
    ] = None,
    repo: RepoOpt = Path("."),
    note: Annotated[
        str | None,
        typer.Option(
            "--note",
            help="With fix: a note for the author, passed along as you wrote it.",
        ),
    ] = None,
    resume_run: Annotated[
        bool,
        typer.Option("--resume/--no-resume", help="Resume the run once the choice is saved."),
    ] = True,
    detach: Annotated[
        bool, typer.Option(help="Resume in the background (output goes to console.log).")
    ] = False,
    budget: Annotated[
        float | None, typer.Option(help="New per-task budget in USD for the resumed run.")
    ] = None,
    config: ConfigOpt = None,
    keep_worktree: KeepOpt = False,
    quiet: QuietOpt = False,
) -> None:
    """Choose what a run does at the point where it stopped to ask you, then carry on."""
    runlog = RunLog.for_run(repo.resolve(), run_id)
    if not (runlog.root / "run.json").is_file():
        typer.echo(f"no run {run_id} in {repo}", err=True)
        raise typer.Exit(code=2)
    state = runlog.read_state()
    if choice is None:
        if not decisions.awaiting_decision(state):
            typer.echo(decisions.not_waiting_message(state), err=True)
            raise typer.Exit(code=2)
        if _stdin_is_tty():
            _decide_now(state, repo, keep_worktree=keep_worktree, quiet=quiet)
            return
        typer.echo(f"run {run_id} is waiting for a decision:", err=True)
        for line in _decision_lines(state):
            typer.echo(f"  {line}", err=True)
        raise typer.Exit(code=2)
    _choose(
        run_id,
        repo,
        choice,
        note or "",
        resume_run=resume_run,
        detach=detach,
        budget=budget,
        config=config,
        keep_worktree=keep_worktree,
        quiet=quiet,
    )


@app.command()
def resume(
    run_id: RunIdArg,
    repo: RepoOpt = Path("."),
    config: ConfigOpt = None,
    budget: Annotated[
        float | None, typer.Option(help="New per-task budget in USD (e.g. after budget_exceeded).")
    ] = None,
    keep_worktree: KeepOpt = False,
    quiet: QuietOpt = False,
    detach: Annotated[
        bool,
        typer.Option(
            help="Resume in the background and return at once. Output goes to the run's "
            "console.log."
        ),
    ] = False,
    detached_child: Annotated[
        bool, typer.Option(hidden=True, help="Set by --detach: run.lock already names us.")
    ] = False,
) -> None:
    """Resume an interrupted run at the first stage that didn't finish."""
    cfg = load_config(config) if config else None
    if detach:
        try:
            state = check_resumable(repo, run_id)
            if cfg is not None:
                check_same_shape(state, cfg)
        except NotResumable as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=2) from e
        budget_now = (
            budget
            or (
                cfg or RunLog.for_run(repo.resolve(), run_id).read_config() or Config()
            ).limits.budget_usd_per_task
        )
        _warn_console(repo, budget_now)
        args = ["resume", run_id, "--repo", str(repo.resolve()), "--detached-child"]
        if config is not None:
            args += ["--config", str(config.resolve())]
        if budget is not None:
            args += ["--budget", repr(budget)]
        if keep_worktree:
            args.append("--keep-worktree")
        _background(repo, run_id, args, "resumed")
        return
    try:
        orch = Orchestrator.resume(
            SdkAgentRunner(),
            repo,
            run_id,
            config=cfg,
            budget_usd=budget,
            keep_worktree=keep_worktree,
            progress=make_progress(quiet),
            locked_for_me=detached_child,
            console_warned=detached_child
            or _warn_console(repo, budget or _budget_of(repo, run_id, cfg)),
        )
    except NotResumable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    _interruptible()
    try:
        state = asyncio.run(orch.run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    _summary(state, orch.runlog)
    if _decide_now(state, repo, keep_worktree=keep_worktree, quiet=quiet):
        return
    if state.status is not Status.COMPLETE:
        raise typer.Exit(code=1)


def _budget_of(repo: Path, run_id: str, cfg: Config | None) -> float:
    """The per-task budget a resumed run will run under."""
    saved = RunLog.for_run(repo.resolve(), run_id).read_config()
    return (cfg or saved or Config()).limits.budget_usd_per_task


@app.command()
def cancel(run_id: RunIdArg, repo: RepoOpt = Path(".")) -> None:
    """Stop an active run at its next safe point. It stays resumable (advpipe resume)."""
    try:
        pid = cancel_run(repo.resolve(), run_id)
    except ControlError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    typer.echo(f"sent interrupt to pid {pid}; run {run_id} stops at its next safe point")
    typer.echo(f"check with: advpipe status {run_id}   continue with: advpipe resume {run_id}")


@app.command()
def clean(
    run_id: RunIdArg,
    repo: RepoOpt = Path("."),
    force: Annotated[
        bool,
        typer.Option(help="Delete the branch even if it has commits no other branch has."),
    ] = False,
    logs: Annotated[
        bool, typer.Option(help="Also delete the run's log directory (.advpipe/runs/<id>).")
    ] = False,
) -> None:
    """Remove the worktree and branch of a run that isn't complete (e.g. a failed run)."""
    try:
        done = clean_run(repo.resolve(), run_id, force=force, logs=logs)
    except ControlError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2) from e
    for line in done:
        typer.echo(line)


@app.command()
def status(
    run_id: Annotated[
        str | None, typer.Argument(callback=_run_id_arg, help="Run id (default: list all runs).")
    ] = None,
    repo: RepoOpt = Path("."),
) -> None:
    """List runs, or show one run's state."""
    repo = repo.resolve()
    if run_id is None:
        runs = list_runs(repo)
        if not runs:
            typer.echo("no runs")
        for s in runs:
            active = " (active)" if RunLog.for_run(repo, s.run_id).lock_holder() else ""
            item = _short(s.work_item, 50)
            typer.echo(
                f"{s.run_id}  {s.status.value + active:<16} {s.stage.value:<12} "
                f"${s.cost_usd:>6.2f}  {item}"
            )
        return
    runlog = RunLog.for_run(repo, run_id)
    if not (runlog.root / "run.json").is_file():
        typer.echo(f"no run {run_id}", err=True)
        raise typer.Exit(code=2)
    s = runlog.read_state()
    holder = runlog.lock_holder()
    lines = [
        f"run:      {s.run_id}",
        f"status:   {s.status.value}" + (f" (active, pid {holder})" if holder else ""),
        f"stage:    {s.stage.value}" + (f" round {s.round}" if s.round else ""),
        f"item:     {_short(s.work_item, 100)}",
        f"level:    {s.level.value} ({level_number(s.level)} of {len(LEVEL_ORDER)}), "
        f"security scan {'on' if scan_on(s) else 'off'}",
        f"branch:   {s.branch}",
        f"cost:     ${s.cost_usd:.2f}" + (" (API-equivalent)" if s.auth == "subscription" else ""),
        f"done:     {', '.join(s.commits) or '-'}",
        f"open:     {len(s.open_findings)} blocking, {len(s.minor_findings)} minor",
        f"updated:  {s.updated_at:%Y-%m-%d %H:%M:%S} UTC",
    ]
    s = answers.migrate(s)
    if s.auth != "unknown":
        lines.append(f"login:    {usage.LOGIN_LABELS[s.auth]}")
    if s.rate_limits:
        lines.append("usage:    " + "; ".join(usage.describe_all(s.rate_limits)) + " (last known)")
    if decisions.awaiting_decision(s):
        lines.append("waiting for a decision:")
        lines += [f"  {line}" for line in _decision_lines(s)]
    elif s.ended_by:
        lines.append(f"ended:    {_ended_line(s)}")
    if s.fix_passes:
        lines.append(f"fix:      {s.fix_passes} fix pass(es) you asked for")
    if scan_line := scan_policy_line(s):
        lines.append(f"scan:     {scan_line}")
    if s.bypass_questions:
        lines.append("bypass:   blocking questions bypassed up front; the agent answers them")
    done = [d for d in s.decisions if d.choice]
    if done:
        lines.append("decisions:")
        for n, d in enumerate(s.decisions, 1):
            what = f"you chose {d.choice}" if d.choice else "waiting"
            note = f" (note: {_short(d.note, 60)})" if d.note.strip() else ""
            lines.append(f"  {n}. {d.kind} at {d.stage.value}: {what}{note}")
    ignored = [f for f in s.minor_findings if f.claim.endswith("ignored by you")]
    if ignored:
        lines.append(f"ignored:  {len(ignored)} security scan finding(s), ignored by you")
    if s.questions:
        lines.append("questions:")
        for q in s.questions:
            lines.append(f"  {q.id}  {_short(q.text, 90)}")
            if q.answer:
                who = " (agent's best judgement)" if q.source == "agent" else ""
                lines.append(f"      answer{who}: {_short(q.answer, 84 - len(who))}")
            elif q.bypassed:
                lines.append("      (Bypass: the agent decides when the run resumes)")
            else:
                lines.append("      (waiting)")
        if answers.awaiting_answers(s, answers.max_spec_passes(runlog)):
            lines.append(f"answer them: {answers.answer_command(s.run_id)}")
    typer.echo("\n".join(lines))


@app.command(name="usage")
def usage_cmd(repo: RepoOpt = Path(".")) -> None:
    """Show the login and the last known subscription usage (read-only; no API calls).

    Claude reports usage only when it changes, so this is what advpipe last saw, with its age,
    never a live gauge.
    """
    cache = usage.load_cache(repo.resolve())
    typer.echo(f"login: {usage.LOGIN_LABELS[cache.login]}")
    if cache.login == "unknown":
        typer.echo("  (advpipe learns the login from the first agent call of a run;")
        typer.echo("   cost figures are API-equivalent if you're on a subscription)")
    if not cache.windows:
        typer.echo(
            "usage: not known yet: advpipe sees usage only when Claude reports a change"
            if cache.login != "console"
            else "usage: Console credits have no usage windows"
        )
        return
    typer.echo("usage, last known:")
    for window in sorted(
        cache.windows,
        key=lambda w: list(usage.WINDOW_LABELS).index(w) if w in usage.WINDOW_LABELS else 99,
    ):
        snap = cache.windows[window]
        typer.echo(f"  {usage.describe(snap)}  (seen {usage.age(snap)})")


@app.command()
def report(run_id: RunIdArg, repo: RepoOpt = Path(".")) -> None:
    """Print a run's report.md."""
    path = RunLog.for_run(repo.resolve(), run_id).root / "report.md"
    if not path.is_file():
        typer.echo(f"no report for run {run_id}", err=True)
        raise typer.Exit(code=2)
    typer.echo(path.read_text(), nl=False)


@app.command()
def levels(repo: RepoOpt = Path("."), config: ConfigOpt = None) -> None:
    """Show the levels a run can have: what each runs, its agent calls, and what it skips."""
    cfg = load_config(config, repo)
    for level in LEVEL_ORDER:
        info, shape = LEVEL_INFO[level], SHAPES[level]
        happy, worst = max_agent_calls(level, cfg.limits)
        agents = len(roles_for(level))
        mark = "  (default)" if level is cfg.pipeline.level else ""
        typer.echo(f"{level_number(level)}  {info.label} ({level.value}){mark}")
        typer.echo(f"   {info.description}")
        typer.echo(f"   good for: {info.good_for}")
        typer.echo(
            f"   {agents} agent{'s' if agents != 1 else ''}: {', '.join(roles_for(level))}; "
            f"{happy} agent call{'s' if happy != 1 else ''} if all goes well, at most {worst}"
        )
        scan = "on" if shape.security_scan else "off"
        typer.echo(f"   security scan: {scan} by default")
        dropped = dropped_safeguards(level, shape.security_scan)
        if dropped:
            typer.echo("   not done: " + "; ".join(dropped))
        typer.echo("")
    typer.echo(
        "Pick one with: advpipe run --level NAME, 'level:' in a work item's front matter, or "
        "[pipeline] level in pipeline.toml."
    )


LOOPBACK = ("127.0.0.1", "localhost", "::1")


@app.command()
def ui(
    repo: RepoOpt = Path("."),
    port: Annotated[int, typer.Option(min=1, max=65535, help="Port to serve on.")] = 8765,
    items_dir: Annotated[
        Path, typer.Option(help="Work-items folder, relative to the repo.")
    ] = Path("work-items"),
    host: Annotated[
        str, typer.Option(help="Address to serve on. Only this machine (127.0.0.1) by default.")
    ] = "127.0.0.1",
    i_know_this_is_exposed: Annotated[
        bool,
        typer.Option(
            "--i-know-this-is-exposed",
            help="Allow a --host other than this machine. Anyone with the link can start runs.",
        ),
    ] = False,
) -> None:
    """Serve the local web UI: watch runs in a browser. Needs: pip install -e '.[ui]'."""
    if not repo.is_dir():
        raise typer.BadParameter(f"not a directory: {repo}", param_hint="--repo")
    exposed = host not in LOOPBACK
    if exposed and not i_know_this_is_exposed:
        raise typer.BadParameter(
            f"refusing to serve on {host}: the UI can start runs, which spend money and run "
            "commands. It's meant for this machine only (127.0.0.1). Pass "
            "--i-know-this-is-exposed to do it anyway.",
            param_hint="--host",
        )
    try:
        import uvicorn

        from advpipe.ui.app import create_ui_app
        from advpipe.ui.security import LOOPBACK_HOSTS, new_token
    except ImportError as e:
        typer.echo(
            f"the web UI needs extra packages ({e.name} is missing): pip install -e '.[ui]'",
            err=True,
        )
        raise typer.Exit(code=2) from e
    token = new_token()
    # Exposed: browsers will send whatever name reaches this machine, so the Host check is off.
    allowed = None if exposed else LOOPBACK_HOSTS
    web_app = create_ui_app(repo, items_dir, token, allowed_hosts=allowed)
    if exposed:
        typer.echo(
            f"WARNING: serving on {host}, not just this machine. Anyone who can reach it and "
            "gets the link can start runs on your account.",
            err=True,
        )
    url_host = f"[{host}]" if ":" in host else host
    typer.echo(f"advpipe ui for {repo.resolve()}")
    typer.echo(f"open: http://{url_host}:{port}/?token={token}")
    typer.echo("stop: Ctrl-C (runs started elsewhere keep going)")
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


GATE_NAMES = ("test", "types", "lint", "security")


@app.command()
def gates(
    repo: RepoOpt = Path("."),
    config: ConfigOpt = None,
    only: Annotated[
        str, typer.Option(help="Comma-separated gates to run: test, types, lint, security.")
    ] = "test,types,lint",
) -> None:
    """Run the configured gates once. Exits 2 on failure (for use as a Claude Code hook)."""
    names = [n.strip() for n in only.split(",") if n.strip()]
    unknown = [n for n in names if n not in GATE_NAMES]
    if unknown:
        raise typer.BadParameter(f"unknown gate(s): {', '.join(unknown)}")
    cfg = load_config(config, repo)
    results = asyncio.run(run_gates(cfg, repo.resolve(), names))
    failed = [r for r in results if not r.passed]
    for r in results:
        if r.passed:
            typer.echo(f"{r.name}: {'skipped (' + r.skip_reason + ')' if r.skipped else 'pass'}")
    # Failures go to stderr: a PostToolUse hook exiting 2 feeds stderr back to Claude.
    for r in failed:
        typer.echo(r.summary(), err=True)
    if failed:
        raise typer.Exit(code=2)
