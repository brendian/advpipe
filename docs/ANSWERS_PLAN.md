# Answering blocking questions: plan

Today, when the spec writer marks an open question `BLOCKING:`, the run stops at SPEC with
status `needs_human`, and the only way forward is to reword the work item and start a **new**
run, paying for the spec stage again and losing the run's history. This plan lets a person
**answer the questions and resume the same run**:

```sh
$ advpipe run --item work-items/s04-refunds.md
20261009-101500-ab12cd  needs_human  $0.31  advpipe/s04-refunds
  2 blocking questions. Answer them with: advpipe answer 20261009-101500-ab12cd

$ advpipe answer 20261009-101500-ab12cd
Q1: Should partial refunds round half-up or to even?
> half-up, like the invoices
Q2: Are refunds allowed after 90 days?
> No. Reject with RefundWindowClosed.
answers saved; resuming at SPEC
... (spec revised with the answers, then TESTS, CODE, ... as usual)
20261009-101500-ab12cd  complete  $1.84  advpipe/s04-refunds
```

The same works from the web UI (an answer form on the run page) and inside Claude Code (the
skill asks you in the session).

This plan is for **future Claude Code sessions**. Each milestone below (A0 to A2) is sized for
one session and ends with tests. See [How to use this plan](#how-to-use-this-plan-in-a-session).
Read `CLAUDE.md` and `docs/ARCHITECTURE.md` first. Paths and names below are from the code as
of 2026-10-09; check them before relying on them.

---

## 1. Principles

These follow from the design rules in `CLAUDE.md`. Don't trade them away for convenience.

- **The orchestrator decides, not an agent.** Whether a run is waiting on answers, whether it
  can resume, and when the spec stage is finished are all decided in code from `run.json`.
- **Answers become part of `task.md`, verbatim.** Later agents (test author, critics,
  reviewers) see only artifacts, never reasoning (rule 4). The human's answers are a new
  artifact, so they go into `task.md`, and the orchestrator writes them in itself (§3.5) so an
  agent can't drop or paraphrase them.
- **Bounded.** Each answer round costs one more spec-writer call. The number of spec passes is
  capped (`limits.max_rounds_spec`, §3.7). After the cap, the run stops for good, as today.
- **Same run, same branch, same budget.** Answering resumes the existing run: same run id,
  branch, worktree, cost so far and budget. Nothing already paid for is redone, except the spec
  writer's revision pass.
- **One path for everyone.** The CLI is the source of truth. The UI calls the CLI (as U5 does
  for cancel and resume), and the Claude Code skill follows the same procedure as instructions.
- **Old runs keep working.** A run stopped by an older advpipe (questions only in
  `open_findings`) can be answered too (§3.9).

## 2. What happens today (the starting point)

Read these before changing anything:

- `orchestrator.Orchestrator._spec`: calls the spec writer, saves `spec/author.txt` and a copy
  of `task.md` in the run directory, then `stages.blocking_open_questions(task_md)`. If any are
  found, it stops with `Status.NEEDS_HUMAN` and one `Finding` per question (`id="Q1"...`,
  `criterion="open-question"`, `category="scope"`). **`task.md` is left uncommitted** in the
  worktree, and `state.commits` has no `"spec"` key.
- `models.RESUMABLE = {RUNNING, ERROR, BUDGET_EXCEEDED}`: `needs_human` is not resumable, so
  `orchestrator.check_resumable` refuses it ("nothing to resume").
- `workspace.Workspace.reopen` (used on every resume) runs `git reset --hard HEAD` and
  `git clean -fd` in the worktree. **This deletes the uncommitted `task.md`.** The previous spec
  must therefore be read from the run directory's copy (`.advpipe/runs/<id>/task.md`), not from
  the worktree.
- `orchestrator._open_workspace` (resume branch) clears `state.open_findings`, sets status
  `running`, and appends the note "Resumed at stage SPEC".
- `orchestrator._run`: `if "spec" in done: ... elif not await self._spec(ctx): return`. A
  resumed run without a spec commit runs `_spec` again from scratch.
- `ui/actions.py VALID`: `needs_human` allows only `clean`. `ui/runs.py display_status` maps
  `running` without a live lock to `stopped` (a display-only status). Follow that pattern.
- `.claude/skills/adversarial-build/SKILL.md`, Stage 1: "If Open questions contains any
  `BLOCKING:` item, **stop**".

## 3. Design

### 3.1 Data model (`models.py`)

```python
class OpenQuestion(BaseModel):
    id: str                       # "Q1", "Q2", ... unique within the run, never reused
    text: str                     # as the spec writer wrote it, without the BLOCKING: prefix
    asked_in: int                 # spec pass that asked it (1 = the first spec)
    answer: str = ""              # empty until answered
    answered_at: datetime | None = None
    applied: bool = False         # a spec revision has used this answer

class RunState(BaseModel):
    ...
    questions: list[OpenQuestion] = Field(default_factory=list)  # every blocking question, all passes
    spec_passes: int = 0          # spec-writer passes completed (1 after the first spec)
```

Both fields have defaults, so old `run.json` files still load.

Derived properties on `RunState`, so the CLI, the UI and `check_resumable` all agree:

- `pending_questions`: questions with no answer yet.
- `unapplied_answers`: answered but `applied` is False.
- `awaiting_answers`: `status is NEEDS_HUMAN and stage is SPEC and "spec" not in commits and
  pending_questions` (non-empty) **and** the spec-pass cap isn't reached (needs the config, so
  make it a function `awaiting_answers(state, limits)` in `answers.py` if a property can't see
  the cap).
- `answers_ready`: as above, but every question asked in the latest pass is answered (so
  `pending_questions` is empty and `unapplied_answers` isn't).

New question ids continue the run's numbering: if Q1 and Q2 were asked in pass 1 and the
revision asks one more, it is Q3. A person never sees two different questions with the same id.

### 3.2 Recording the questions when the spec stops

In `_spec`, when `blocking_open_questions` finds questions:

- append them to `state.questions` (new ids, `asked_in = state.spec_passes`), skipping any whose
  normalised text (lower-case, collapsed whitespace) matches an already *answered* question:
  that one is a re-ask (§3.6);
- keep setting `open_findings` to one `Finding` per **pending** question, as today (the report
  and the UI's "What's left open" already show them);
- emit a new event kind `questions` with `ids` and `message` like
  `"spec has 2 blocking question(s); answer them with: advpipe answer <run-id>"`.

### 3.3 Recording answers (`src/advpipe/answers.py`, new)

```python
class AnswerError(ValueError): ...

def record_answers(repo: Path, run_id: str, answers: dict[str, str], *, limits: Limits) -> RunState
```

- Refuses (raises `AnswerError`, with a message fit to show a person) if:
  - the run doesn't exist, or a live process holds `run.lock`;
  - it isn't awaiting answers (wrong status or stage, already has a spec commit, cap reached);
  - an id isn't one of the run's pending questions;
  - a pending question from the latest pass is left unanswered (all-or-nothing, see §8);
  - an answer is empty after `strip()`, or longer than `MAX_ANSWER_CHARS` (proposed 8,000;
    the UI passes each answer as one command-line argument);
  - the worktree can't be brought back: `state.branch` no longer exists
    (`workspace.branch_exists`), e.g. after `advpipe clean`.
- Takes the lock for the write (`acquire_lock()` then `release_lock()` in a `finally`), sets
  `answer` and `answered_at`, writes `run.json` with `RunLog.write_state`, and appends an
  `answers` event (`ids`, and a message such as `"answers recorded for Q1, Q2"`). It never
  changes `status`: the run stays `needs_human` until something resumes it.
- Answering again before resuming replaces the earlier answers (same validation).

### 3.4 Resuming with answers (`orchestrator.py`)

- `check_resumable` also accepts `NEEDS_HUMAN` when `answers_ready(state)` holds. Otherwise, for
  a run awaiting answers, it raises `NotResumable` naming the pending ids and the command:
  `"run X is waiting on answers to Q1, Q2: advpipe answer X"`. Leave `models.RESUMABLE`
  unchanged; this is an extra rule, not a new resumable status.
- `_run`: unchanged shape. `_spec` decides which kind of pass to run:
  - no unapplied answers → the first spec, exactly as today (`spec_prompt`);
  - unapplied answers → a **revision** (`spec_revision_prompt`, §3.5).
- After any spec pass: `state.spec_passes += 1`; mark all answers it was given `applied = True`;
  then run the usual checks (stray files reverted, `task.md` exists, blocking questions). If
  blocking questions remain and `spec_passes == limits.max_rounds_spec`, stop with
  `needs_human` and **no** way to answer (`awaiting_answers` is false because of the cap), with
  the finding/report text: "the spec still has blocking questions after N passes; reword the
  work item and start a new run".
- A revision interrupted (Ctrl-C, budget, error) leaves its answers unapplied, so a plain
  `advpipe resume` (or `resume --budget`) runs the revision again. Nothing extra is needed for
  that: it follows from "unapplied answers → revision".
- Everything after SPEC is unchanged: the spec commit, then TESTS, CODE and so on.

### 3.5 The revision pass and the Answers section

`stages.spec_revision_prompt(work_item, previous_task_md, questions)` gives the spec writer:

- the work item (as `spec_prompt` does);
- the previous `task.md`, read from the **run directory** (§2: the worktree copy is gone);
- every answered question so far, as `Q1: <question>` / `Answer: <answer>` pairs;
- the instruction to write the complete `task.md` again: turn each answer into acceptance
  criteria, constraints or scope as appropriate, remove answered questions from **Open
  questions**, keep the AC numbering stable where criteria are unchanged, and add a new
  `BLOCKING:` question only if an answer raises a genuinely new blocker. It must not re-ask
  an answered question and must not write an `## Answers` section itself.

Add a short "Revising after answers" section to `src/advpipe/prompts/spec-writer.md` and copy it
into `.claude/agents/spec-writer.md` (`test_subagents_in_sync_with_prompts` enforces the sync).

After the spec writer finishes, **the orchestrator** (code, not the agent) rewrites the
`## Answers` section of `task.md`: it removes any existing `## Answers` section and appends one
listing every answered question verbatim:

```markdown
## Answers
Answers from the person who started this run. They override anything above that disagrees.
- **Q1:** Should partial refunds round half-up or to even?
  **Answer:** half-up, like the invoices
```

Put it after **Open questions**. `stages.blocking_open_questions` stops at the next `## `
heading, so answers can never be mistaken for open questions. Test that explicitly. Then the
usual `runlog.write_text("task.md", ...)` copy and, if no blocking questions remain, the spec
commit. Keep the helper pure and testable: `stages.with_answers_section(task_md, questions)`.

### 3.6 A re-asked question

If the revision lists as `BLOCKING:` a question whose normalised text matches an answered one,
don't record it as new (§3.2), add a note ("spec writer re-asked Q1; its answer stands"), and
don't stop for it. The answer is already in the Answers section. Only genuinely new blocking
questions stop the run.

### 3.7 Config

`Limits.max_rounds_spec: PositiveInt = 3`: spec-writer passes per run, so the first spec plus
up to two answer rounds. Add it to `pipeline.example.toml`, the README's configuration block
and the `[limits]` comments. `1` turns answering off (today's behaviour).

### 3.8 Run directory files

Keep pass 1 where it is today, so U2/U3 code and old runs need no special cases:

| Path | What |
|---|---|
| `spec/author.txt` | pass 1: the spec writer's reply (unchanged) |
| `spec/pass-N/author.txt` | pass N ≥ 2: the spec writer's reply |
| `spec/pass-N/answers.json` | pass N ≥ 2: the questions and answers it was given |
| `spec/pass-N/task.md` | `task.md` as pass N left it (written for every pass, including 1) |
| `task.md` | the latest `task.md`, with the Answers section (unchanged meaning) |

### 3.9 Old runs

A run stopped by an older advpipe has `questions == []` but its `open_findings` hold `Q1...`
with `criterion == "open-question"`. When loading such a run for answering or display, derive
the questions from those findings (`asked_in=1`, `spec_passes=1`). Put this in one function,
`answers.questions_of(state)`, used by `record_answers`, `check_resumable`, the CLI and the UI.

### 3.10 Report, status and summary

- `runlog.render_report`: a `## Questions and answers` section (every question, the pass that
  asked it, the answer or "(not answered yet)"). For a run awaiting answers, add the command to
  answer under **Notes**.
- `cli._summary`: for a run awaiting answers, a second line
  `  N blocking question(s). Answer them with: advpipe answer <run-id>`.
- `advpipe status <run-id>`: list the questions with their answers (or `(waiting)`).
- New event kinds `questions` and `answers` in `events.EventKind` and in the events table in
  `docs/ARCHITECTURE.md`.

### 3.11 The `advpipe answer` command (`cli.py`)

```sh
advpipe answer <run-id> [--repo PATH]
               [--answer ID=TEXT]...      # repeatable; one per question
               [--file FILE]              # answers file, see below
               [--template]               # print an answers file to fill in, then exit
               [--no-resume] [--detach] [--budget USD] [--config FILE] [--quiet]
```

- With neither `--answer` nor `--file`: if stdin is a terminal, ask each pending question in
  turn (`typer.prompt`); otherwise print the questions and the usage, and exit with code 2.
- `--template` prints a ready-to-edit answers file:

  ```text
  # Answers for run 20261009-101500-ab12cd. Lines starting with # are ignored.
  # Q1: Should partial refunds round half-up or to even?
  Q1:
  # Q2: Are refunds allowed after 90 days?
  Q2:
  ```

  Answers file format: a line starting with `Q<n>:` begins that answer, and continuation lines
  belong to it until the next `Q<n>:` line. Lines starting with `#` are ignored. Parse it in
  `answers.parse_answers_file(text) -> dict[str, str]`, which raises `AnswerError` on an unknown
  or duplicate id.
- Then `record_answers`, then, unless `--no-resume`, resume exactly as `advpipe resume` does
  (foreground, or `--detach` via the same `_background` helper, with `--budget`/`--config`
  passed through). Exit codes match `resume`: 0 complete, 1 another final status, 2 refused,
  130 interrupted, and with `--detach`, 0 once started.
- `advpipe resume` on a run awaiting answers fails with the `NotResumable` message from §3.4.
  On a run whose answers were saved with `--no-resume`, it simply resumes.

### 3.12 Web UI

- `ui/runs.py`: a display status `needs_answers` (label "needs answers", icon `?`, help "The
  spec has questions only you can answer. Answer them on the run page and the run continues."),
  derived in `display_status` when `awaiting_answers` holds. Include it in the "Needs you"
  filter.
- `ui/actions.py VALID["needs_answers"] = {"answer", "clean"}`; `answer_args(repo, run_id,
  answers, budget)` builds `["answer", run_id, "--repo", repo, "--detach",
  "--answer=Q1=<text>", ...]`. Use the `--answer=` form so text starting with `-` can't look
  like an option. Never a shell, and no other browser value becomes an argument.
- Run page: in the header, for `needs_answers`, replace "What's left open" with an **Answer and
  continue** form: each question with a `<textarea name="answer_Q1">` (required, `maxlength`),
  the cost so far and the budget, and one *Save answers and continue* button.
  `POST /runs/{id}/answer` (CSRF as for every POST): validate that ids match `Q\d+` and the run's
  pending set, then call `advpipe answer ... --detach` and redirect to the run page, which goes
  live. On a refusal, show the form again with the CLI's message and the typed answers kept.
- Timeline: SPEC shows one round panel per pass ("Spec", "Revision 1 (answers to Q1, Q2)", ...)
  from §3.8's files, each with its reply, its `task.md` and the questions it asked. The Spec &
  context tab shows the latest `task.md` (the Answers section highlights like open questions).
- Next steps for `needs_answers`: `advpipe answer <id> --repo <repo>` with a copy button.
- Help page: one paragraph on answering, in the SPEC step's explanation.
- Optional (only if §8 says yes): after answering, offer to copy the Q&A into the work-item
  file (U4's editor) so a fresh run of that item won't ask again.

### 3.13 Claude Code skill (`.claude/skills/adversarial-build/SKILL.md`)

Stage 1 becomes: if `task.md` has `BLOCKING:` questions, **ask the user** in the session (all
at once, numbered). With answers, launch `spec-writer` again with the previous `task.md` and the
Q&A (the revision instructions from §3.5), then append the Answers section yourself, exactly as
§3.5 shows. At most `max_rounds_spec - 1` answer rounds (default 2). If the user declines to
answer, stop with `needs_human` and the questions, as today. Update the README section "Using
it inside Claude Code".

## 4. Invariants to test (all milestones)

- The test author's first prompt contains the revised `task.md`, including the Answers section,
  and nothing else from the answer round (no spec-writer reply, no prompt text).
- Critics' and reviewers' prompts contain the Answers section (it's part of `task.md`) and no
  spec-writer reasoning (extend `test_critics_get_artifacts_not_reasoning`).
- `blocking_open_questions` never reports anything from the Answers section.
- No answer round runs past `max_rounds_spec`; at the cap the run stops `needs_human` and is
  not answerable.
- Answering never changes files in the user's working tree or any branch other than the run's.
- Every answer text that reaches a page is escaped; every answer that reaches a subprocess is
  one argument of a list.

## 5. Milestones

Each is a single Claude Code session. Each ends with `pytest`, `ruff check .`,
`ruff format --check .` and `mypy --strict src/` green, `README.md` and `docs/ARCHITECTURE.md`
updated for what changed, and a commit. Tests use `FakeAgentRunner` (`tests/fakes.py`): no API
calls. `fakes.TASK_MD_BLOCKING` is the ready-made spec with one blocking question.

**A0: core (no CLI command, no UI).** §3.1–3.10 except the `answer` command, plus §3.5's prompt
change. Tests, in a new `tests/test_answers.py`:
- a blocking spec records `state.questions` (ids, text, `asked_in`), `open_findings`, and a
  `questions` event;
- `record_answers`: happy path; every refusal in §3.3 (unknown id, missing answer, empty, too
  long, wrong status, live lock, branch deleted, cap reached); answering twice replaces;
  it writes no files except `run.json` and `events.jsonl`;
- resume after answers: the spec writer gets a revision prompt with the previous `task.md`
  (from the run directory, after `reopen` wiped the worktree) and the Q&A; the committed
  `task.md` has the Answers section; the run completes through TESTS/CODE/REVIEW with one extra
  spec call and no other repeated calls; `spec/pass-2/*` files exist;
- the revision asks a new blocking question → stops again with `Q2` (numbering continues) →
  answer → completes;
- the cap: `max_rounds_spec=2` with a revision that still blocks → `needs_human`, not
  answerable, and the message says to start a new run;
- a re-asked question doesn't stop the run (§3.6);
- interrupted or `budget_exceeded` during the revision → `resume` reruns the revision;
- an old-format run (questions only in `open_findings`) can be answered and resumed;
- `with_answers_section` is idempotent, and `blocking_open_questions` ignores its content;
- `check_resumable` messages for waiting and not-answerable runs;
- the report's Questions and answers section.

**A1: CLI and the Claude Code skill.** §3.11 and §3.13. Tests in `tests/test_cli.py` (or
`tests/test_answers_cli.py`) with `CliRunner` and the fake runner, as the existing CLI tests do:
- `--answer` (repeated), `--file`, `--template` round trip (fill the template, pass it back);
- interactive prompting when stdin is a TTY, and exit 2 with the questions printed when it
  isn't;
- `--no-resume` then `advpipe resume`; `resume` on a waiting run fails and names `advpipe
  answer`;
- `--detach` starts the background process (use `tests/advpipe_fake_child.py`, as
  `test_control.py` does) and the run completes;
- exit codes (0, 1, 2, 130), the summary line, and `status <id>` listing the questions;
- answers files: continuation lines, `#` comments, unknown/duplicate ids rejected.
Update the README (a new `advpipe answer` section; the status table's `needs_human` row and
"If the status is `needs_human`"; Troubleshooting "The run stopped at the spec"; Configuration
`max_rounds_spec`) and the skill.

**A2: Web UI.** §3.12. Tests in `tests/test_ui_answers.py` with `TestClient` over real run
directories from the orchestrator with fake agents:
- the run list and run page show `needs answers` and the form with one field per pending
  question;
- POST without CSRF → 403; without the token → 401; ids not pending, empty or too-long answers
  → the form again with a message, and no subprocess call;
- a valid POST calls `advpipe answer` with the exact argument list (monkeypatch
  `actions.subprocess.run` and `control.child_command`, as `test_ui_actions.py` does), including an answer that starts with `-` and
  one containing HTML, then redirects to the run page;
- answers containing HTML are escaped on every page that shows them (timeline, report,
  context tab);
- the SPEC timeline shows each pass;
- the `answer` button and form appear only for `needs_answers`.
Plus a manual browser check, as in U2: answer a blocked run from the page and watch it go live
and complete. Report what was checked by hand and what wasn't. Update the README's UI section,
the Help page, and `docs/UI_PLAN.md` (a line noting this feature).

Order: A0 → A1 → A2. A2 needs A1 (it calls `advpipe answer`).

## 6. Things that will bite you

- **`Workspace.reopen` wipes the uncommitted `task.md`** (§2). Read the previous spec from
  `.advpipe/runs/<id>/task.md`. A test must cover this, or the revision will silently see no
  previous spec.
- **`_open_workspace` clears `open_findings`** on resume. Keep the questions in
  `state.questions`, not only in findings.
- **Two BLOCKING regexes**: `stages.blocking_open_questions` and `ui/render.py _BLOCKING` must
  keep accepting the same prefix forms.
- **The lock**: `record_answers` takes and releases `run.lock`. With `--detach`, the background
  child takes it again via `start_detached`. Between the two, `check_resumable` already refuses
  a live holder, so don't add another mechanism.
- **Windows**: process liveness and pid checks were just reworked (commits `bfe57f7`,
  `8f556c3`). Use `RunLog.lock_holder()` and `is_this_process`, never `os.kill` directly.
- **Argument size**: each answer is one command-line argument from the UI. Keep
  `MAX_ANSWER_CHARS` well under the 128 KiB Linux per-argument limit (and Windows' 32 KiB
  whole command line: 8,000 characters for a few answers is safe; the UI should also cap the
  total, e.g. 24,000).
- **Budget**: answering is free; the revision pass is charged to the `spec` stage like the
  first pass. A run that ran out of budget during the revision is `budget_exceeded`, and
  `resume --budget` handles it as today.
- **Prompts are the source of truth**: edit `src/advpipe/prompts/spec-writer.md`, then copy the
  body to `.claude/agents/spec-writer.md`.

## 7. Docs to update (by the milestone that changes the behaviour)

- `README.md`: the run flow diagram ("if a question needs a human answer, stops here, before
  any code" becomes "stops for your answers"), `advpipe answer`, status table, Troubleshooting,
  Configuration, the UI section, the Claude Code section.
- `docs/ARCHITECTURE.md`: the spec stage, resume rules, persistence table (§3.8), events table,
  the web UI section.
- `BUILD_SPEC.md` §"Pipeline" line `[1] Spec writer`: note that blocking questions can be
  answered and the run resumed (as `UI_PLAN.md` did when it changed a BUILD_SPEC decision).
- `CLAUDE.md` Layout: add `docs/ANSWERS_PLAN.md` (done with this plan).

## 8. Open questions (for Brendan, before A0)

Sessions: use the proposed answer unless Brendan has replied here.

- **All or nothing?** Must every blocking question be answered before resuming? Proposed:
  **yes**. A half-answered spec just stops again and costs another pass. A person who doesn't
  care can write "your call" as the answer.
- **How many answer rounds?** Proposed: `max_rounds_spec = 3` (the first spec plus two answer
  rounds).
- **Write answers back into the work-item file?** Proposed: **not automatically**. The pipeline
  never edits your files. A2 may offer a button that copies the Q&A into the item through the
  U4 editor (you still save it). Say if you want that button.
- **A new `Status` value instead of `needs_human` plus a derived display status?** Proposed:
  **no new status**. `needs_answers` is display-only, like `stopped`. That keeps `run.json`
  compatible and avoids touching every status table. Revisit if scripts need to tell the cases
  apart from `run.json` alone (they can: `questions` and `spec_passes` are there).
- **Also allow plain `resume` for the other spec stop ("spec writer did not produce
  task.md")?** Proposed: out of scope for this plan.

## How to use this plan in a session

Start each session with something like:

> Read `docs/ANSWERS_PLAN.md`, `CLAUDE.md` and `docs/ARCHITECTURE.md`. Implement milestone
> **A0** only. Follow the principles and the invariants in §4. Stop at the end of the
> milestone and summarize what you built, how you verified it, and anything you couldn't
> check.

If something in the code no longer matches §2, trust the code, adjust the design minimally,
and say so in the summary and in this file.
