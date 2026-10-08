"""Work items for the web UI: the `.md` files in the work-items folder, read, checked and
changed on disk. Files are the source of truth; nothing here commits to git.

Every path comes from the browser, so ``item_path`` is the only way in: a relative path of
plain names ending in ``.md`` that stays inside the folder once symlinks are resolved.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from advpipe.config import load_config
from advpipe.ui.runs import RunRow, load_runs, title_of
from advpipe.workitem import (
    WorkItem,
    WorkItemError,
    format_work_item,
    parse_work_item,
    salvage_work_item,
)
from advpipe.workspace import BRANCH_PREFIX, GitError, branch_exists, git, slugify

SUFFIX = ".md"
MAX_DEPTH = 4  # folders below the work-items folder
MAX_BYTES = 200_000  # a work item is a page of text, not a dataset
# One path part: no leading dot (no "..", no hidden files), no separators or odd characters.
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+ -]*")


class ItemPathError(ValueError):
    """The path isn't an allowed work-item path (shown to the user as is)."""


class ItemConflict(RuntimeError):
    """The file changed on disk since the editor loaded it."""


def item_path(items_dir: Path, rel: str) -> Path:
    """``items_dir / rel`` if ``rel`` names a ``.md`` file that stays in ``items_dir``.

    ``items_dir`` must be resolved. Rejects empty, absolute and backslash paths, ``..`` and
    hidden parts, odd characters, deep nesting, other suffixes, and symlinks that lead out.
    """
    if not rel or "\\" in rel or rel.startswith("/") or len(rel) > 300:
        raise ItemPathError("use a path inside the work-items folder, like server/my-task.md")
    parts = rel.split("/")
    for part in parts:
        if not _PART.fullmatch(part) or part.endswith((" ", ".")):
            raise ItemPathError(
                f"{part!r} isn't allowed in a path: use letters, digits, '-', '_', '.' and "
                "spaces, not starting with '.'"
            )
    if len(parts) > MAX_DEPTH + 1:
        raise ItemPathError(f"folders can be at most {MAX_DEPTH} deep")
    if not parts[-1].endswith(SUFFIX) or parts[-1] == SUFFIX:
        raise ItemPathError("work items are markdown files: the name must end in .md")
    path = items_dir.joinpath(*parts)
    if not path.resolve().is_relative_to(items_dir):
        raise ItemPathError("that path leads outside the work-items folder (a symlink?)")
    return path


def with_suffix(rel: str) -> str:
    """A name typed into a form, with ``.md`` added if it was left off."""
    rel = rel.strip()
    return rel if rel.endswith(SUFFIX) else rel + SUFFIX


def rel_of(items_dir: Path, path: Path) -> str:
    return path.relative_to(items_dir).as_posix()


def digest(data: bytes) -> str:
    """Identifies a version of a file, so a save can tell if it changed underneath."""
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- git status


@dataclass(frozen=True)
class GitInfo:
    label: str
    help: str


GIT_STATES = {
    "untracked": GitInfo("not in git", "This file has never been committed."),
    "modified": GitInfo("uncommitted", "Changed since the last commit."),
    "ignored": GitInfo("ignored", "Ignored by git (.gitignore), so it is never committed."),
}


def git_changes(repo: Path, items_dir: Path) -> dict[Path, str] | None:
    """``GIT_STATES`` key per changed path under ``items_dir`` (a folder's path covers the
    files in it). Clean files aren't listed. None if git can't tell (not a git repo, or the
    folder is outside it)."""
    try:
        top = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
        if not items_dir.is_relative_to(top):
            return None
        out = git(
            top,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--",
            str(items_dir),
        )
    except (GitError, OSError):
        return None
    changes: dict[Path, str] = {}
    entries = iter(out.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        code, name = entry[:2], entry[3:]
        if code[0] in "RC":
            next(entries, None)  # the rename's source path follows
        kind = {"??": "untracked", "!!": "ignored"}.get(code, "modified")
        changes[(top / name.rstrip("/")).resolve()] = kind
    return changes


def git_state(changes: dict[Path, str], path: Path) -> str:
    """The ``GIT_STATES`` key for ``path`` ('' if it's committed and unchanged)."""
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate in changes:
            return changes[candidate]
    return ""


# --------------------------------------------------------------------------- the list


@dataclass(frozen=True)
class ItemRow:
    rel: str  # path inside the work-items folder, e.g. "server/s03-stock.md"
    title: str  # the body's first line, or the problem if the file can't be read
    name: str | None  # front matter
    config: str | None
    problem: str | None  # why `advpipe run --item` would reject it
    last_run: RunRow | None
    git: str  # GIT_STATES key, or ''

    @property
    def filename(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def git_info(self) -> GitInfo | None:
        return GIT_STATES.get(self.git)


@dataclass(frozen=True)
class ItemList:
    folder_exists: bool
    groups: list[tuple[str, list[ItemRow]]]  # (folder, items); "" is the top level
    skipped: list[str]  # .md files that can't be managed here (odd names, symlinks out)
    git_known: bool

    @property
    def count(self) -> int:
        return sum(len(rows) for _, rows in self.groups)


def last_runs(repo: Path) -> dict[Path, RunRow]:
    """The newest run started from each work-item file (``advpipe run --item``)."""
    latest: dict[Path, RunRow] = {}
    for row in load_runs(repo).rows:  # newest first
        if row.work_item_file:
            latest.setdefault(Path(row.work_item_file), row)
    return latest


def _row(path: Path, rel: str, runs: dict[Path, RunRow], changes: dict[Path, str]) -> ItemRow:
    name = config = problem = None
    text = ""
    try:
        text = path.read_bytes().decode("utf-8")
        item = parse_work_item(text, rel)
        title, name, config = title_of(item.body), item.name, item.config
    except UnicodeDecodeError:
        title, problem = "(not UTF-8 text)", "the file isn't UTF-8 text"
    except OSError as e:
        title, problem = "(can't read)", str(e)
    except WorkItemError as e:
        fields, body = salvage_work_item(text)
        title, problem = title_of(body), str(e)
        name, config = fields.get("name"), fields.get("config")
    return ItemRow(
        rel=rel,
        title=title,
        name=name,
        config=config,
        problem=problem,
        last_run=runs.get(path.resolve()),
        git=git_state(changes, path),
    )


def load_items(repo: Path, items_dir: Path) -> ItemList:
    """Every ``.md`` file under ``items_dir``, grouped by folder. Doesn't follow symlinked
    folders, and skips hidden ones."""
    if not items_dir.is_dir():
        return ItemList(False, [], [], True)
    runs = last_runs(repo)
    changes = git_changes(repo, items_dir)
    groups: dict[str, list[ItemRow]] = {}
    skipped: list[str] = []
    for root, dirs, files in os.walk(items_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for filename in sorted(files):
            if not filename.endswith(SUFFIX) or filename.startswith("."):
                continue
            path = Path(root) / filename
            rel = rel_of(items_dir, path)
            try:
                item_path(items_dir, rel)
            except ItemPathError:
                skipped.append(rel)
                continue
            folder = rel.rpartition("/")[0]
            groups.setdefault(folder, []).append(_row(path, rel, runs, changes or {}))
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] != "", kv[0]))
    return ItemList(True, ordered, skipped, changes is not None)


def folders(items_dir: Path) -> list[str]:
    """Existing sub-folders of ``items_dir`` (for the "new item" form's suggestions)."""
    if not items_dir.is_dir():
        return []
    found: list[str] = []
    for root, dirs, _ in os.walk(items_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        rel = Path(root).relative_to(items_dir)
        if rel.parts and len(rel.parts) <= MAX_DEPTH:
            found.append(rel.as_posix())
    return found


def config_files(repo: Path) -> list[str]:
    """``*.toml`` files at the repo root, as suggestions for the config field."""
    return sorted(p.name for p in repo.glob("*.toml") if p.name != "pyproject.toml")


# --------------------------------------------------------------------------- the editor


@dataclass(frozen=True)
class Draft:
    """What the editor shows: the fields as text ('' when unset)."""

    rel: str
    name: str = ""
    config: str = ""
    body: str = ""
    base: str = ""  # digest of the file as loaded; '' for a new item
    problem: str | None = None  # why the file as loaded doesn't parse

    def item(self) -> WorkItem:
        return WorkItem(body=self.body, name=self.name or None, config=self.config or None)


def from_form(rel: str, name: str, config: str, body: str, base: str = "") -> Draft:
    """A draft from form fields. Browsers send textarea lines ending in CRLF."""
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    return Draft(rel=rel, name=name.strip(), config=config.strip(), body=body, base=base)


def read_draft(items_dir: Path, rel: str) -> Draft:
    """Load a file for the editor. A file that doesn't parse still opens, with what could be
    read and the problem. Raises ItemPathError, FileNotFoundError, or ValueError (not text)."""
    path = item_path(items_dir, rel)
    if not path.is_file():
        raise FileNotFoundError(rel)
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError(f"the file is over {MAX_BYTES // 1000} kB; edit it in a text editor")
    text = data.decode("utf-8")  # UnicodeDecodeError is a ValueError
    try:
        item = parse_work_item(text, rel)
    except WorkItemError as e:
        fields, body = salvage_work_item(text)
        name, config = fields.get("name", ""), fields.get("config", "")
        return Draft(rel, name, config, body, digest(data), problem=str(e))
    return Draft(rel, item.name or "", item.config or "", item.body, digest(data))


@dataclass(frozen=True)
class Check:
    level: str  # "error" (can't save), "warning" or "info"
    text: str


@dataclass(frozen=True)
class Checked:
    checks: list[Check] = field(default_factory=list)
    branch: str = ""

    @property
    def ok(self) -> bool:
        return not any(c.level == "error" for c in self.checks)


def check_draft(repo: Path, draft: Draft) -> Checked:
    """The editor's inline checks. Errors stop a save (``advpipe run --item`` would reject the
    file); warnings are things a run would trip over."""
    checks: list[Check] = []
    if not draft.body:
        checks.append(Check("error", "The body is empty: a run would have nothing to build."))
    for key, value in (("branch name", draft.name), ("config", draft.config)):
        if "\n" in value or "\r" in value:
            checks.append(Check("error", f"The {key} must be a single line."))
    branch = BRANCH_PREFIX + slugify(draft.name or draft.body) if draft.name or draft.body else ""
    if branch and branch_exists(repo, branch):
        checks.append(
            Check(
                "warning",
                f"Branch {branch} already exists, so a run would make {branch}-2 instead. "
                "Pick another branch name to tell them apart.",
            )
        )
    checks.extend(_config_checks(repo, draft.config))
    return Checked(checks, branch)


def _config_checks(repo: Path, config: str) -> list[Check]:
    if not config:
        default = "pipeline.toml" if (repo / "pipeline.toml").is_file() else "advpipe's defaults"
        return [Check("info", f"No config set: runs use {default}.")]
    path = (repo / config).resolve()
    if not path.is_relative_to(repo):
        return [Check("warning", f"Config {config} is outside the repo; it wasn't checked.")]
    if not path.is_file():
        return [Check("warning", f"Config file {config} doesn't exist: a run would refuse it.")]
    try:
        load_config(path)
    except (OSError, ValueError) as e:  # TOML and validation errors are ValueErrors
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return [Check("warning", f"Config file {config} has errors: {first}")]
    return []


# --------------------------------------------------------------------------- changes on disk


def _encode(draft: Draft) -> bytes:
    data = format_work_item(draft.item()).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise WorkItemError(f"the work item is over {MAX_BYTES // 1000} kB")
    return data


def create_item(items_dir: Path, draft: Draft) -> Path:
    """Write a new file. Raises ItemPathError, WorkItemError, or FileExistsError."""
    path = item_path(items_dir, draft.rel)
    data = _encode(draft)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:  # never overwrite
        f.write(data)
    return path


def save_item(items_dir: Path, draft: Draft) -> bool:
    """Write an edited file; False if nothing changed (the file is left byte for byte as it
    was, front matter layout included). Raises ItemConflict if the file changed since
    ``draft.base`` was read, and ItemPathError, FileNotFoundError or WorkItemError."""
    path = item_path(items_dir, draft.rel)
    current = path.read_bytes()
    if draft.base and digest(current) != draft.base:
        raise ItemConflict(draft.rel)
    data = _encode(draft)
    try:
        if parse_work_item(current.decode("utf-8")) == draft.item():
            return False
    except (UnicodeDecodeError, WorkItemError):
        pass
    path.write_bytes(data)
    return True


def rename_item(items_dir: Path, rel: str, new_rel: str) -> Path:
    """Move a file within the folder. Raises ItemPathError, FileNotFoundError, FileExistsError."""
    source = item_path(items_dir, rel)
    target = item_path(items_dir, new_rel)
    if not source.is_file():
        raise FileNotFoundError(rel)
    if target.exists() or target.is_symlink():
        raise FileExistsError(new_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    return target


def delete_item(items_dir: Path, rel: str) -> None:
    path = item_path(items_dir, rel)
    if not path.is_file():
        raise FileNotFoundError(rel)
    path.unlink()
