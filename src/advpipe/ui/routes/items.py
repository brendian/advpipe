"""Work items: the list, the editor (new and edit, with a live preview and checks), rename and
delete. Changes go straight to the files; nothing is committed to git.

Every POST comes from htmx, so it carries the CSRF header (see ui/security.py). A form with
problems comes back as the form again, status 422, which base.html tells htmx to swap in.
Success answers with an ``HX-Redirect`` to the page to show next.
"""

from __future__ import annotations

import shlex
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from advpipe.ui import items
from advpipe.ui.items import Draft, ItemConflict, ItemPathError
from advpipe.ui.render import markdown
from advpipe.ui.runs import STATUSES
from advpipe.ui.state import UiConfig, ui_config
from advpipe.workitem import KEYS, WorkItemError

router = APIRouter()

NAV = "/items"
FIX_FIRST = "Not saved: fix the problems marked below first."
# Form field limits, so a request can't make the server hold huge strings.
Field = Annotated[str, Form(max_length=items.MAX_BYTES)]
Short = Annotated[str, Form(max_length=300)]


def _page(
    request: Request, template: str, values: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    ui = ui_config(request)
    return ui.templates.TemplateResponse(
        request,
        template,
        {"nav_current": NAV, "items_dir": _items_dir_label(ui), **values},
        status_code=status_code,
    )


def _items_dir_label(ui: UiConfig) -> str:
    try:
        return ui.items_dir.relative_to(ui.repo).as_posix()
    except ValueError:
        return str(ui.items_dir)


def _error(request: Request, status_code: int, title: str, message: str) -> HTMLResponse:
    return _page(
        request,
        "error.html",
        {"title": title, "message": message, "back": ("/items", "All work items")},
        status_code,
    )


def _redirect(to: str, **query: str) -> Response:
    """Tell htmx to load another page (a full page load, so the nav and title are right)."""
    url = to + ("?" + urlencode(query) if query else "")
    return Response(status_code=200, headers={"HX-Redirect": url})


def _run_command(ui: UiConfig, rel: str) -> str:
    path = items.item_path(ui.items_dir, rel)
    return f"advpipe run --repo {shlex.quote(str(ui.repo))} --item {shlex.quote(str(path))}"


def _missing(request: Request, rel: str) -> HTMLResponse:
    return _error(request, 404, "No such work item", f"There's no work item {rel} any more.")


def _bad_path(request: Request, e: ItemPathError) -> HTMLResponse:
    return _error(request, 400, "Not a work-item path", str(e))


# --------------------------------------------------------------------------- list


@router.get("/items", response_class=HTMLResponse)
def items_page(request: Request, deleted: str = "") -> HTMLResponse:
    ui = ui_config(request)
    listing = items.load_items(ui.repo, ui.items_dir)
    return _page(
        request,
        "items.html",
        {
            "listing": listing,
            "statuses": STATUSES,
            "deleted": deleted,
        },
    )


# --------------------------------------------------------------------------- editor


def _editor(
    request: Request,
    draft: Draft,
    *,
    new: bool,
    errors: list[str] | None = None,
    notice: str = "",
    fragment: bool = False,
) -> HTMLResponse:
    """The editor page, or (``fragment``) just its form, re-rendered after a failed save."""
    ui = ui_config(request)
    checked = items.check_draft(ui.repo, draft)
    last_run = command = None
    if not new:
        try:
            path = items.item_path(ui.items_dir, draft.rel)
            last_run = items.last_runs(ui.repo).get(path.resolve())
            command = _run_command(ui, draft.rel)
        except ItemPathError:
            pass
    return _page(
        request,
        "_item_form.html" if fragment else "item_edit.html",
        {
            "draft": draft,
            "new": new,
            "errors": errors or [],
            "notice": notice,
            "checked": checked,
            "preview": markdown(draft.body) if draft.body else None,
            "folders": items.folders(ui.items_dir),
            "config_files": items.config_files(ui.repo),
            "keys": KEYS,
            "last_run": last_run,
            "statuses": STATUSES,
            "command": command,
            "q": urlencode({"path": draft.rel}),
        },
        status_code=422 if errors else 200,
    )


@router.get("/items/new", response_class=HTMLResponse)
def new_item_page(request: Request, folder: str = "") -> HTMLResponse:
    rel = f"{folder.strip('/')}/" if folder.strip("/") else ""
    return _editor(request, Draft(rel=rel), new=True)


@router.post("/items/new")
def create_item(
    request: Request,
    path: Short,
    body: Field,
    name: Short = "",
    config: Short = "",
    then: Short = "",
) -> Response:
    ui = ui_config(request)
    draft = items.from_form(items.with_suffix(path) if path.strip() else "", name, config, body)
    checked = items.check_draft(ui.repo, draft)
    if not checked.ok:
        return _editor(request, draft, new=True, errors=[FIX_FIRST], fragment=True)
    try:
        items.create_item(ui.items_dir, draft)
    except ItemPathError as e:
        return _editor(request, draft, new=True, errors=[f"File name: {e}"], fragment=True)
    except FileExistsError:
        message = f"{draft.rel} already exists. Pick another name, or edit that item instead."
        return _editor(request, draft, new=True, errors=[message], fragment=True)
    except (WorkItemError, OSError) as e:
        return _editor(request, draft, new=True, errors=[str(e)], fragment=True)
    if then == "run":
        return _redirect("/runs/new", item=draft.rel)
    return _redirect("/items/edit", path=draft.rel, notice="created")


NOTICES = {
    "created": "Created.",
    "saved": "Saved.",
    "unchanged": "No changes to save.",
    "renamed": "Renamed.",
}


@router.get("/items/edit", response_class=HTMLResponse)
def edit_item_page(request: Request, path: str, notice: str = "") -> HTMLResponse:
    ui = ui_config(request)
    try:
        draft = items.read_draft(ui.items_dir, path)
    except ItemPathError as e:
        return _bad_path(request, e)
    except FileNotFoundError:
        return _missing(request, path)
    except (OSError, ValueError) as e:
        return _error(request, 400, "Can't edit this file here", f"{path}: {e}")
    return _editor(request, draft, new=False, notice=NOTICES.get(notice, ""))


@router.post("/items/save")
def save_item(
    request: Request,
    path: Short,
    body: Field,
    base: Short = "",
    name: Short = "",
    config: Short = "",
    then: Short = "",
) -> Response:
    ui = ui_config(request)
    draft = items.from_form(path, name, config, body, base)
    try:
        items.item_path(ui.items_dir, path)
    except ItemPathError as e:
        return _bad_path(request, e)
    if not items.check_draft(ui.repo, draft).ok:
        return _editor(request, draft, new=False, errors=[FIX_FIRST], fragment=True)
    try:
        changed = items.save_item(ui.items_dir, draft)
    except FileNotFoundError:
        return _editor(
            request,
            draft,
            new=False,
            errors=[f"{path} was deleted or moved since you opened it. Copy your text first."],
            fragment=True,
        )
    except ItemConflict:
        return _editor(
            request,
            draft,
            new=False,
            errors=[
                f"{path} was changed on disk since you opened it, so saving would overwrite "
                "those changes. Copy your text, reload the page, and merge by hand."
            ],
            fragment=True,
        )
    except (WorkItemError, OSError) as e:
        return _editor(request, draft, new=False, errors=[str(e)], fragment=True)
    if then == "run":  # "Save & run": the New run dialog shows the budget before starting
        return _redirect("/runs/new", item=path)
    return _redirect("/items/edit", path=path, notice="saved" if changed else "unchanged")


@router.post("/items/preview", response_class=HTMLResponse)
def preview(
    request: Request, body: Field = "", name: Short = "", config: Short = ""
) -> HTMLResponse:
    """The preview pane and the inline checks, re-fetched as the user types."""
    ui = ui_config(request)
    draft = items.from_form("", name, config, body)
    return _page(
        request,
        "_item_preview.html",
        {
            "checked": items.check_draft(ui.repo, draft),
            "preview": markdown(draft.body) if draft.body else None,
        },
    )


# --------------------------------------------------------------------------- rename, delete


def _existing(request: Request, rel: str) -> HTMLResponse | None:
    """An error page unless ``rel`` is an existing work item."""
    try:
        if not items.item_path(ui_config(request).items_dir, rel).is_file():
            return _missing(request, rel)
    except ItemPathError as e:
        return _bad_path(request, e)
    return None


@router.get("/items/rename", response_class=HTMLResponse)
def rename_page(request: Request, path: str) -> HTMLResponse:
    problem = _existing(request, path)
    if problem is not None:
        return problem
    return _rename_form(request, path, path)


def _rename_form(
    request: Request, rel: str, new_rel: str, error: str = "", fragment: bool = False
) -> HTMLResponse:
    return _page(
        request,
        "_item_rename_form.html" if fragment else "item_rename.html",
        {"rel": rel, "new_rel": new_rel, "error": error, "q": urlencode({"path": rel})},
        status_code=422 if error else 200,
    )


@router.post("/items/rename")
def rename_item(request: Request, path: Short, new_path: Short) -> Response:
    ui = ui_config(request)
    problem = _existing(request, path)
    if problem is not None:
        return problem
    new_rel = items.with_suffix(new_path)
    if new_rel == path:
        return _rename_form(request, path, new_path, "That's its name already.", fragment=True)
    try:
        items.rename_item(ui.items_dir, path, new_rel)
    except ItemPathError as e:
        return _rename_form(request, path, new_path, f"New name: {e}", fragment=True)
    except FileExistsError:
        return _rename_form(request, path, new_path, f"{new_rel} already exists.", fragment=True)
    except OSError as e:
        return _rename_form(request, path, new_path, str(e), fragment=True)
    return _redirect("/items/edit", path=new_rel, notice="renamed")


@router.get("/items/delete", response_class=HTMLResponse)
def delete_page(request: Request, path: str) -> HTMLResponse:
    """Deleting asks first: this page is the question."""
    problem = _existing(request, path)
    if problem is not None:
        return problem
    ui = ui_config(request)
    file = items.item_path(ui.items_dir, path)
    changes = items.git_changes(ui.repo, ui.items_dir)
    state = items.git_state(changes, file) if changes is not None else "unknown"
    return _page(
        request,
        "item_delete.html",
        {"rel": path, "git": state, "q": urlencode({"path": path})},
    )


@router.post("/items/delete")
def delete_item(request: Request, path: Short, confirm: Short = "") -> Response:
    problem = _existing(request, path)
    if problem is not None:
        return problem
    if confirm != "yes":  # only the confirmation page sends this
        return _error(request, 400, "Not deleted", "Deleting needs confirming first.")
    try:
        items.delete_item(ui_config(request).items_dir, path)
    except OSError as e:
        return _error(request, 500, "Couldn't delete", f"{path}: {e}")
    return _redirect("/items", deleted=path)
