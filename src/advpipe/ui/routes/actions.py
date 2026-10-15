"""Starting runs (the New run dialog), and cancel, resume, answer and clean up on a run.

Every action is a POST from htmx, so it carries the CSRF header (see ui/security.py), and runs
the advpipe CLI through ``ui.actions.call_advpipe``. An action that isn't valid for the run's
status is refused with 409 before anything runs; one the CLI refuses comes back with its
message, status 422. base.html lets htmx swap both into the page. Success answers with an
``HX-Redirect`` to the page to show next.
"""

from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from advpipe import answers, decisions
from advpipe.models import RunState
from advpipe.runlog import RunLog, check_run_id
from advpipe.ui import actions, items, usage_view
from advpipe.ui.actions import RunForm, RunPlan
from advpipe.ui.runs import display_status
from advpipe.ui.state import ui_config

router = APIRouter()

Field = Annotated[str, Form(max_length=items.MAX_BYTES)]
Short = Annotated[str, Form(max_length=300)]

NAV = "/"


def _redirect(to: str, **query: str) -> Response:
    url = to + ("?" + urlencode(query) if query else "")
    return Response(status_code=200, headers={"HX-Redirect": url})


def _result(request: Request, message: str, status_code: int) -> HTMLResponse:
    """A problem, swapped into the action's result area."""
    return ui_config(request).templates.TemplateResponse(
        request, "_action_result.html", {"message": message}, status_code=status_code
    )


def _error(request: Request, status_code: int, title: str, message: str) -> HTMLResponse:
    return ui_config(request).templates.TemplateResponse(
        request,
        "error.html",
        {"title": title, "message": message, "nav_current": NAV},
        status_code=status_code,
    )


def _run(request: Request, run_id: str) -> tuple[RunState, str] | None:
    """The run's state and displayed status, or None if there's no such (readable) run."""
    try:
        log = RunLog.for_run(ui_config(request).repo, check_run_id(run_id))
        state = log.read_state()
    except (OSError, ValueError):
        return None
    return state, display_status(state, log)


NOT_NOW = {
    "cancel": "There's nothing to cancel: no advpipe process is working on this run.",
    "resume": "This run can't be resumed: it's {status}.",
    "clean": "This run can't be cleaned up: it's {status}.",
    "answer": "This run isn't waiting on answers: it's {status}.",
    "decide": "This run isn't waiting for a decision: it's {status}.",
}


def _check(request: Request, run_id: str, action: str) -> tuple[RunState, str] | HTMLResponse:
    found = _run(request, run_id)
    if found is None:
        return _result(request, f"There's no run {run_id}.", 404)
    state, status = found
    if not actions.allowed(status, action):
        label = status.replace("_", " ")
        hint = " Reload the page to see where it is now."
        return _result(request, NOT_NOW[action].format(status=label) + hint, 409)
    return state, status


# --------------------------------------------------------------------------- new run


def _form(
    source: str,
    item: str,
    task: str,
    name: str,
    config: str,
    level: str = "",
    scan: str = "default",
    scan_failures: str = "",
    bypass_seen: str = "",
    bypass: str = "",
) -> RunForm:
    """The dialog's fields. The Bypass checkbox sends nothing when it's unticked, so the
    hidden ``bypass_seen`` says whether the checkbox was in the form at all (if not, the run
    gets the work item's or the config's setting)."""
    return RunForm(
        source=source,
        item=item.strip(),
        task=task,
        name=name.strip(),
        config=config.strip(),
        level=level.strip(),
        scan=scan.strip() or "default",
        scan_failures=scan_failures.strip(),
        bypass=(bypass == "yes") if bypass_seen else None,
    )


def _dialog(
    request: Request,
    plan: RunPlan,
    *,
    template: str = "run_new.html",
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    ui = ui_config(request)
    listing = items.load_items(ui.repo, ui.items_dir)
    choices = [row.rel for _, rows in listing.groups for row in rows]
    return ui.templates.TemplateResponse(
        request,
        template,
        {
            "plan": plan,
            "form": plan.form,
            "choices": choices,
            "errors": errors or [],
            "config_files": items.config_files(ui.repo),
            "usage": usage_view.for_repo(ui.repo),  # read once per render
            "nav_current": NAV,
        },
        status_code=status_code,
    )


@router.get("/runs/new", response_class=HTMLResponse)
def new_run_page(request: Request, item: str = "") -> HTMLResponse:
    ui = ui_config(request)
    source = "item" if item or items.load_items(ui.repo, ui.items_dir).count else "task"
    form = RunForm(source=source, item=item)
    return _dialog(request, actions.plan_run(ui.repo, ui.items_dir, form))


@router.post("/runs/new/check", response_class=HTMLResponse)
def check_new_run(
    request: Request,
    source: Short = "item",
    item: Short = "",
    task: Field = "",
    name: Short = "",
    config: Short = "",
    level: Short = "",
    scan: Short = "default",
    scan_failures: Short = "",
    bypass_seen: Short = "",
    bypass: Short = "",
) -> HTMLResponse:
    """The dialog's summary (level, budget, branch, problems), re-fetched as the form changes."""
    ui = ui_config(request)
    form = _form(source, item, task, name, config, level, scan, scan_failures, bypass_seen, bypass)
    plan = actions.plan_run(ui.repo, ui.items_dir, form)
    return _dialog(request, plan, template="_run_new_summary.html")


@router.post("/runs/start")
def start_run(
    request: Request,
    source: Short = "item",
    item: Short = "",
    task: Field = "",
    name: Short = "",
    config: Short = "",
    budget: Short = "",
    level: Short = "",
    scan: Short = "default",
    scan_failures: Short = "",
    bypass_seen: Short = "",
    bypass: Short = "",
    shown: Short = "",
) -> Response:
    """Start the run in the background (`advpipe run --detach`) and go to its page."""
    ui = ui_config(request)
    form = _form(source, item, task, name, config, level, scan, scan_failures, bypass_seen, bypass)
    plan = actions.plan_run(ui.repo, ui.items_dir, form)

    def again(problem: str) -> HTMLResponse:
        return _dialog(
            request, plan, template="_run_new_form.html", errors=[problem], status_code=422
        )

    if not plan.ok:
        return again("Not started: fix the problems marked below first.")
    if budget != plan.budget_text:
        # The form shows the budget it was rendered with; never start on one nobody saw.
        return again(
            f"Not started: the budget is ${plan.budget_text}, not the ${budget or '?'} shown "
            "before (the config changed). Check it and start again."
        )
    if shown != plan.shown_text:
        # Likewise for the level, scan, scan policy and Bypass: never start on settings that
        # weren't on the screen (the summary re-renders a moment after the form changes).
        return again(
            f"Not started: the checking is {plan.shown_text.replace(';', ', ')}, not what the "
            "summary showed a moment ago. Check it and start again."
        )
    result = actions.call_advpipe(ui.repo, plan.args)
    if not result.ok:
        return again(f"advpipe refused to start it: {result.message}")
    try:
        run_id = actions.started_run_id(result)
    except ValueError:
        return again(f"Started, but advpipe printed no run id: {result.stdout.strip()!r}")
    return _redirect(f"/runs/{run_id}", notice="started")


# --------------------------------------------------------------------------- cancel, resume


@router.post("/runs/{run_id}/cancel")
def cancel_run(request: Request, run_id: str) -> Response:
    """Send the run's process an interrupt (`advpipe cancel`). It stays resumable."""
    checked = _check(request, run_id, "cancel")
    if isinstance(checked, HTMLResponse):
        return checked
    ui = ui_config(request)
    result = actions.call_advpipe(ui.repo, actions.cancel_args(ui.repo, run_id))
    if not result.ok:
        return _result(request, f"advpipe couldn't cancel it: {result.message}", 422)
    return _redirect(f"/runs/{run_id}", notice="cancelled")


@router.post("/runs/{run_id}/resume")
def resume_run(request: Request, run_id: str, budget: Short = "") -> Response:
    """Continue where the run stopped, in the background (`advpipe resume --detach`)."""
    checked = _check(request, run_id, "resume")
    if isinstance(checked, HTMLResponse):
        return checked
    state, status = checked
    try:
        new_budget = actions.parse_budget(budget, state, required=status == "budget_exceeded")
    except ValueError as e:
        return _result(request, f"Not resumed: {e}", 422)
    ui = ui_config(request)
    result = actions.call_advpipe(ui.repo, actions.resume_args(ui.repo, run_id, new_budget))
    if not result.ok:
        return _result(request, f"advpipe couldn't resume it: {result.message}", 422)
    return _redirect(f"/runs/{run_id}", notice="resumed")


# --------------------------------------------------------------------------- answer

ANSWER_FIELD = re.compile(r"answer_(Q[1-9][0-9]{0,3})")
BYPASS_FIELD = re.compile(r"bypass_(Q[1-9][0-9]{0,3})")


@router.post("/runs/{run_id}/answer")
async def answer_run(request: Request, run_id: str) -> Response:
    """Answer the spec's blocking questions, or let the agent decide (Bypass), and resume the
    run in the background (`advpipe answer --detach`). The answers are checked here first, so a
    refused form never starts a process; the CLI checks again.

    A ticked "let the agent decide" box wins over any text in its answer box, and the *Bypass
    all* button (``bypass_all=yes``) bypasses every question the form asks."""
    checked = _check(request, run_id, "answer")
    if isinstance(checked, HTMLResponse):
        return checked
    state, _ = checked
    # Field names are answer_Q1, bypass_Q1, ...; anything else in the form is ignored.
    async with request.form(
        max_fields=100, max_part_size=answers.MAX_TOTAL_ANSWER_CHARS * 4
    ) as form:
        fields = [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
    current = answers.migrate(state)
    bypass_all = ("bypass_all", "yes") in fields
    bypass = {m.group(1) for k, v in fields if (m := BYPASS_FIELD.fullmatch(k)) and v == "yes"}
    if bypass_all:
        bypass = {q.id for q in answers.to_answer(current)}
    given = {
        m.group(1): value
        for key, value in fields
        if (m := ANSWER_FIELD.fullmatch(key)) and m.group(1) not in bypass
    }
    try:
        answers.check_answers(current, given, bypass)
    except answers.AnswerError as e:
        return _result(request, f"Not saved: {e}.", 422)
    ui = ui_config(request)
    ids = sorted(bypass, key=lambda i: int(i[1:]))
    result = actions.call_advpipe(ui.repo, actions.answer_args(ui.repo, run_id, given, ids))
    if not result.ok:
        return _result(request, f"advpipe didn't take the answers: {result.message}", 422)
    return _redirect(f"/runs/{run_id}", notice="bypassed" if bypass and not given else "answered")


# --------------------------------------------------------------------------- decide

NoteField = Annotated[str, Form(max_length=decisions.MAX_NOTE_CHARS * 2)]


def _choice_problem(state: RunState, choice: str, note: str, max_fix: int) -> str:
    """Why ``choice`` (with ``note``) can't be sent for this run's open decision, or ''."""
    d = state.decision
    if d is None or d.choice or state.ended_by:
        return "This run isn't waiting for a decision any more. Reload the page."
    if choice not in decisions.CHOICES[d.kind]:
        return f"{choice!r} isn't a choice here: choose {' or '.join(decisions.CHOICES[d.kind])}."
    if note and choice != "fix":
        return "A note goes with the fix choice only."
    if len(note) > decisions.MAX_NOTE_CHARS:
        return f"The note is {len(note):,} characters; keep it under {decisions.MAX_NOTE_CHARS:,}."
    if choice == "fix" and state.fix_passes >= max_fix:
        return (
            f"This run has used all {state.fix_passes} fix passes it's allowed. Take over, or "
            "start a new run."
        )
    return ""


def _max_fix(request: Request, run_id: str) -> int:
    log = RunLog.for_run(ui_config(request).repo, check_run_id(run_id))
    return decisions.max_fix_passes(log)


@router.get("/runs/{run_id}/decide/{choice}", response_class=HTMLResponse)
def decide_confirm_page(request: Request, run_id: str, choice: str) -> HTMLResponse:
    """Taking over, or stopping, ends the run: this page says exactly what happens first."""
    found = _run(request, run_id)
    if found is None:
        return _error(request, 404, "No such run", f"There's no run {run_id} in this repo.")
    state, status = found
    d = state.decision
    if not actions.allowed(status, "decide") or d is None or choice not in decisions.ENDING:
        return _error(
            request,
            409,
            "Nothing to confirm",
            "This run isn't waiting for that choice. Open the run's page to see where it is.",
        )
    if choice not in decisions.CHOICES[d.kind]:
        return _error(request, 409, "Nothing to confirm", f"{choice!r} isn't a choice here.")
    ui = ui_config(request)
    return ui.templates.TemplateResponse(
        request,
        "run_decide.html",
        {
            "state": state,
            "run_id": run_id,
            "choice": choice,
            "decision": d,
            "info": actions.clean_info(ui.repo, state),
            "nav_current": NAV,
        },
    )


@router.post("/runs/{run_id}/decide")
def decide_run(
    request: Request,
    run_id: str,
    choice: Short = "",
    note: NoteField = "",
    confirm: Short = "",
) -> Response:
    """Record the person's choice at the run's decision point (`advpipe decide`). A choice that
    carries on resumes the run in the background; takeover and stop end it, and only the
    confirmation page sends ``confirm=yes`` for those."""
    checked = _check(request, run_id, "decide")
    if isinstance(checked, HTMLResponse):
        return checked
    state, _ = checked
    choice, note = choice.strip().lower(), note.strip()
    problem = _choice_problem(state, choice, note, _max_fix(request, run_id))
    if problem:
        return _result(request, f"Not sent: {problem}", 422)
    if choice in decisions.ENDING and confirm != "yes":
        return _result(request, "Not done: that needs confirming first.", 400)
    ui = ui_config(request)
    result = actions.call_advpipe(ui.repo, actions.decide_args(ui.repo, run_id, choice, note))
    if not result.ok:
        return _result(request, f"advpipe refused: {result.message}", 422)
    notice = {"takeover": "handed_over", "stop": "ended", "finish": "finished"}.get(
        choice, "decided"
    )
    return _redirect(f"/runs/{run_id}", notice=notice)


# --------------------------------------------------------------------------- clean up


@router.get("/runs/{run_id}/clean", response_class=HTMLResponse)
def clean_page(request: Request, run_id: str) -> HTMLResponse:
    """Cleaning up asks first: this page is the question."""
    found = _run(request, run_id)
    if found is None:
        return _error(request, 404, "No such run", f"There's no run {run_id} in this repo.")
    state, status = found
    if not actions.allowed(status, "clean"):
        return _error(
            request,
            409,
            "Can't clean up this run",
            "Only runs that stopped without finishing can be cleaned up. "
            + (
                "This one is still running: cancel it first."
                if status == "running"
                else "This one is complete: its branch is the result. Delete it with git once "
                "you've merged it."
            ),
        )
    ui = ui_config(request)
    return ui.templates.TemplateResponse(
        request,
        "run_clean.html",
        {
            "state": state,
            "run_id": run_id,
            "info": actions.clean_info(ui.repo, state),
            "nav_current": NAV,
        },
    )


@router.post("/runs/{run_id}/clean")
def clean_run(
    request: Request, run_id: str, confirm: Short = "", force: Short = "", logs: Short = ""
) -> Response:
    """Remove the run's worktree and branch, and its log if asked (`advpipe clean`)."""
    checked = _check(request, run_id, "clean")
    if isinstance(checked, HTMLResponse):
        return checked
    if confirm != "yes":  # only the confirmation page sends this
        return _result(request, "Not cleaned up: that needs confirming first.", 400)
    ui = ui_config(request)
    delete_logs = logs == "yes"
    args = actions.clean_args(ui.repo, run_id, force=force == "yes", logs=delete_logs)
    result = actions.call_advpipe(ui.repo, args)
    if not result.ok:
        return _result(request, f"advpipe refused: {result.message}", 422)
    if delete_logs:
        return _redirect("/", removed=run_id)
    return _redirect(f"/runs/{run_id}", notice="cleaned")
