# advpipe UI: plan

A small local web interface for advpipe, so people can use the tool without living in the
terminal. It covers three jobs:

1. **Watch runs**, live or finished: which stage, what each agent said, check results, findings,
   cost, and how to merge the result.
2. **See everything the agents work from**: the work item, the generated `task.md`, the config
   and checks used, the repo's standards doc, and the role prompts.
3. **Manage work items** (the `.md` task files you feed the pipeline): add, edit, rename and
   delete them, and start a run from one with a click.

This plan is for **future Claude Code sessions**. Each milestone below is sized for one session
and ends with tests. See [How to use this plan](#how-to-use-this-plan-in-a-session).

> **This changes a decision in `BUILD_SPEC.md` §9 ("No web UI. CLI and files only.").** The
> change was requested by Brendan on 2026-10-08. The UI stays optional and local: the CLI and
> files remain the source of truth, and the UI only reads them or calls the same code.

---

## 1. Principles

- **Local and private.** `advpipe ui` serves on `127.0.0.1` only, behind a random access token
  printed at startup (like Jupyter). It can start runs, which spend money and let agents run
  shell commands, so it must never be reachable from the network.
- **Files are the source of truth.** The UI reads run directories (`.advpipe/runs/<id>/`) and
  work-item files. Runs started from the UI are ordinary `advpipe run` processes, so they keep
  going if the UI is closed, and runs started from the terminal show up live in the UI too.
- **No build step.** Server-rendered HTML (Jinja2 templates) plus [htmx](https://htmx.org) for
  small interactions and Server-Sent Events for live updates. htmx is vendored as one static
  file, so there's no CDN, no Node, and nothing to compile. Anyone can change a page by editing
  a template.
- **Optional install.** The UI's dependencies live in an extra, `pip install -e '.[ui]'`. The
  core pipeline stays lean.
- **It never merges.** As in the CLI (`BUILD_SPEC.md` rule: no pushing or merging), the UI
  *shows* merge commands with copy buttons; a human runs them.
- **Plain language.** Say "checks" rather than "gates" in the UI, explain statuses in a tooltip,
  and every empty state says what to do next.

## 2. Tech choices

| Item | Choice | Why |
|---|---|---|
| Server | FastAPI + uvicorn | Same stack as other projects here; easy to test with `TestClient` |
| Pages | Jinja2 templates | Server-rendered; no client framework |
| Interactivity | htmx 2 (vendored `static/htmx.min.js`) + its SSE extension | Live updates and forms without writing JavaScript |
| Live updates | SSE endpoint that tails `events.jsonl` | Works for runs started anywhere |
| Markdown | `markdown-it-py`, HTML escaped, no raw HTML | Renders `task.md` and `report.md` safely |
| Styling | One hand-written `static/app.css` (system font, a light and a dark theme) | Simple, readable, no framework |
| Editor | `<textarea>` with a live preview pane | Good enough; no editor dependency |

New package: `src/advpipe/ui/` (`app.py`, `routes/`, `templates/`, `static/`). New extra in
`pyproject.toml`: `ui = ["fastapi", "uvicorn", "jinja2", "markdown-it-py", "python-multipart"]`.

## 3. Screens

```
┌ advpipe ─ home-inventory ───────────────────────────────── Runs │ Work items │ Help ┐
│                                                                                    │
│  Runs                                                                [ New run ]   │
│  ● running   S03 stock changes      CODE r2/3    $1.10 / $8.00   started 3 min ago  │
│  ✔ complete  S02 items              DONE         $1.50           merge ▸            │
│  ⚠ needs you  S01 database          ARBITER      $0.98           why? ▸             │
└────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Runs list** (home page). One row per run, newest first: status chip, name or branch,
   stage and round, cost against budget, age. Running rows update live. Filters for all,
   running, needs you, complete. Buttons: *New run*, plus *Clean up* for dead runs (see §4.6).
2. **Run detail.**
   - **Header:** status, branch (with copy button), cost meter, *Cancel* / *Resume* buttons.
   - **Timeline:** SPEC → TESTS → CODE → REVIEW → ARBITER → FINAL CHECKS, each showing its
     rounds. Click a round to see:
     - what the author replied
     - the diff the critic judged
     - the check results (pass or fail, with the output tail)
     - the critic's verdict and its findings as cards (file:line, claim, evidence)
   - **Live log:** the progress lines, streaming while the run is going.
   - **Report:** the rendered `report.md`.
   - **Next steps** for complete or needs-human runs: the exact `git log` / `git merge` /
     cleanup commands, with copy buttons.
3. **Spec & context** (a tab on run detail). Everything the agents worked from:
   - the work item text
   - the spec writer's `task.md`, with acceptance criteria and open questions highlighted
   - the config used (`config.json`) and the check commands
   - the standards doc (`CLAUDE.md`)
   - the eight role prompts (read-only)

   Each block says which agents see it. For example, "Critics see: task.md, the diff, check
   output. Never the author's reasoning."
4. **Work items.** A file list from the work-items folder, grouped by subfolder (e.g.
   `server/`, `mobile/`). Each item shows:
   - its name
   - its last run's status, linked to that run
   - whether it has uncommitted changes in git

   Buttons: *New*, *Edit*, *Rename*, *Delete* (with a confirmation), *Run*.
5. **Work item editor.**
   - The title field and the markdown body, with a preview beside them.
   - Fields for the **branch name** and the **pipeline config** to use. These are stored as
     front matter in the file, see §4.2.
   - Inline checks: it warns on an empty body, a missing config file, or a name that would
     clash with an existing branch.
   - *Save*, and *Save & run*.
6. **New run dialog.** Pick a work item (or type a one-off task), confirm the config, branch
   name and budget, then *Start*. It shows "This can spend up to $X" before starting.

## 4. Changes needed in advpipe core

These come first (milestone U0) and are useful without the UI too.

### 4.1 Structured event log
Today progress goes only to a callback (printed by the CLI). Instead, write every progress event
to `.advpipe/runs/<id>/events.jsonl`, one JSON object per line, appended and flushed
immediately:

```json
{"ts": "2026-10-08T04:41:02Z", "kind": "agent_done", "stage": "CODE", "round": 2,
 "role": "coder", "message": "coder done in 63s, $0.31 (run total $1.10)",
 "cost_usd": 0.31, "total_usd": 1.10}
```

`kind` is one of `run_start`, `stage`, `round`, `agent_start`, `agent_done`, `gates`,
`verdict`, `guard`, `test_fix`, `ruling`, `status`, `error`. The CLI printer and the event file
are both fed from one `emit()`. Keep the current human `message` text, so the terminal output
doesn't change.

### 4.2 Work-item files as first-class input
Add `advpipe run --item FILE`, which reads the file itself. That removes the `"$(cat FILE)"`
pattern, which caused the empty-work-item runs on 2026-10-08. An optional front-matter block
sets defaults:

```markdown
---
name: s03-stock
config: pipeline.server.toml
---
Add stock level changes with a change history...
```

Only simple `key: value` lines are parsed (no YAML dependency), and unknown keys are an error.
Command-line options override front matter. Record the file in `RunState.work_item_file`, so
the UI can show each item's last run.

### 4.3 Save what each critic judged
Write `stage-*/round-N/diff.patch` (the exact diff in the critic's prompt) and
`review/diff.patch`, so the run detail page can show them.

### 4.4 Cancel
`advpipe cancel <run-id>` sends SIGINT to the process in `run.lock`. The run stops at a safe
point with status `running`, and it can then be resumed, as with Ctrl-C today.

### 4.5 Run as a background process
`advpipe run --detach` starts the run as a detached child process, writes its output to
`.advpipe/runs/<id>/console.log`, prints the run id, and exits. The UI uses this to start runs.

### 4.6 Clean up dead runs
`advpipe clean <run-id>` removes the worktree and branch, and optionally the log, of a run that
isn't `complete`. It refuses runs that are active or whose branch has commits that aren't on the
base branch, unless `--force` is given. (This is what was done by hand for the three empty runs.)

## 5. Security checklist (every milestone)

- Bind to `127.0.0.1` only. Refuse `--host 0.0.0.0` unless `--i-know-this-is-exposed` is
  passed, and even then warn.
- Require the access token on every request: a cookie set from `?token=...` on first visit.
  Reject everything else with 401.
- Protect state-changing requests (save, delete, run, cancel, clean) with a CSRF token, and
  make them POST only.
- Work-item paths: resolve, and require them to stay inside the work-items folder. Allow only
  `.md` files, and no symlinks out. Test traversal attempts (`../`, absolute paths, `%2e%2e`).
- Render all agent output and markdown escaped. Agents' text is untrusted input.
- The UI never takes a command or git ref from the browser and passes it to a shell. Runs are
  started with an argument list, and branch names come from advpipe's own `slugify`.

## 6. Milestones

Each one is a single Claude Code session. Each ends with `pytest`, `ruff check .`,
`ruff format --check .` and `mypy --strict src/` green, the README updated, and a commit.

**U0: core support (no UI).** Implement §4.1–4.6, with tests:
- events written and flushed in order, and the CLI output unchanged
- front-matter parsing, including errors for unknown keys and empty bodies
- `--item` respects command-line overrides
- `diff.patch` files exist per round
- cancel leaves a resumable run
- `--detach` returns immediately while the run continues (test with the fake runner)
- `clean` refuses unsafe cases

**U1: UI skeleton and runs list.**
- The `advpipe ui [--repo PATH] [--port 8765] [--items-dir work-items]` command.
- The app factory `create_ui_app(repo, items_dir, token)`, the token and CSRF middleware, the
  base layout and nav, and vendored htmx plus the SSE extension.
- The runs list page, with a list that refreshes itself every few seconds.
- Tests with `TestClient`, building run directories with `FakeAgentRunner` and the orchestrator,
  as `tests/test_orchestrator.py` does.

**U2: run detail and live log.**
- The timeline built from `run.json` and the round directories, and the round panels (author
  reply, diff, checks, verdict cards).
- The rendered report, and the next-steps commands with copy buttons.
- The SSE endpoint `/runs/{id}/events` streaming `events.jsonl` from a given offset, closing
  once the run reaches a final status.
- Tests: the SSE stream yields appended events in order; pages render for every status
  (`running`, `complete`, `needs_human`, `budget_exceeded`, `error`); agent text containing HTML
  is escaped.

**U3: spec & context tab.**
- Work item, `task.md` (with acceptance criteria and open-question highlighting), `config.json`
  and checks, standards doc, and role prompts, each with "who sees this".
- Tests: each block is present, and a missing file shows a clear empty state.

**U4: work items: list and editor.**
- List grouped by folder with last-run status (via `RunState.work_item_file`) and git status.
- Create, edit (front-matter fields plus body, with preview), rename and delete with
  confirmation, and the inline checks.
- Tests: a full create/edit/rename/delete round trip on disk; path traversal is rejected;
  front matter round-trips unchanged; deleting asks first.

**U5: actions.**
- *Run* from an item or the New run dialog: shows the budget, then `advpipe run --item ...
  --detach`.
- *Cancel* (`advpipe cancel`), *Resume* (`advpipe resume`), *Clean up* (`advpipe clean`).
- Buttons appear only when the action is valid for that status.
- Tests: subprocess calls are monkeypatched, and the exact argument lists are checked (no
  shell); CSRF is required; invalid actions are rejected.

**U6: polish and docs.**
- Empty states and help text.
- A *Help* page explaining the pipeline in plain words.
- Dark mode, keyboard focus styles, and a responsive layout down to tablet width.
- A README section "Using the web UI" with screenshots, and a `docs/ARCHITECTURE.md` section on
  the UI.

Optional later: browser tests with Playwright (via the `run` skill), a diff view with syntax
highlighting, and editing a run's `task.md` before tests start (a "pause after spec" option).

## 7. Testing approach

- **No API calls, as everywhere in advpipe.** UI tests create real run directories by running
  the orchestrator with `FakeAgentRunner` against a temporary copy of
  `tests/fixtures/sample_repo`.
- Pages are checked with `TestClient`: status codes, key text, escaped output, and form
  round trips.
- SSE is tested by appending to `events.jsonl` while reading the stream.
- Security tests are mandatory in every milestone that adds a route: no token → 401; missing
  CSRF → 403; path traversal → 400 or 404.
- A manual smoke check at the end of each UI milestone: `advpipe ui --repo
  ~/claude/home-inventory`, then click through. Report what was checked by hand and what wasn't.

## 8. Open questions (for Brendan, before U4/U5)

- **Should saving a work item commit it to git?** Proposed: no. **BRENDAN AGREES WITH PROPOSED, NO** Show "uncommitted" in the list
  and leave committing to you, as the pipeline never commits on your branches.
- **One repo per UI, or several?** Proposed: one repo per `advpipe ui` process (`--repo`).
  Several can run on different ports. -Brendan says one repo per
- **Should other people on the network use it?** Proposed: no. If that's wanted later, put it
  behind Tailscale with real accounts. That's a separate project. -Brendan says no

## How to use this plan in a session

Start each session with something like:

> Read `docs/UI_PLAN.md`, `CLAUDE.md` and `docs/ARCHITECTURE.md`. Implement milestone **U1**
> only. Follow the principles and security checklist. Stop at the end of the milestone and
> summarize what you built, how you verified it, and anything you couldn't check.

Milestones are ordered by dependency. U0 first, then U1 → U2 → U3. U4 can come before U3. U5
needs U0 and U4.
