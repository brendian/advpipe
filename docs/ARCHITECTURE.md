# How advpipe works

This document explains the internals: what happens during a run, which file and function does
each part, and why it's built that way. For installing and using the tool, see the
[README](../README.md).

- [The big picture](#the-big-picture)
- [Levels](#levels)
- [Module map](#module-map)
- [A run, step by step](#a-run-step-by-step)
- [The adversarial loop in detail](#the-adversarial-loop-in-detail)
- [Gates: ground truth](#gates-ground-truth)
- [Guards: rules enforced in code](#guards-rules-enforced-in-code)
- [Data contracts](#data-contracts)
- [Talking to Claude](#talking-to-claude)
- [Isolation: worktrees and branches](#isolation-worktrees-and-branches)
- [Persistence, resume and locking](#persistence-resume-and-locking)
- [Progress events](#progress-events)
- [Background runs, cancel and clean](#background-runs-cancel-and-clean)
- [Work-item files](#work-item-files)
- [Parallel runs](#parallel-runs)
- [Budget](#budget)
- [The web UI](#the-web-ui)
- [The Claude Code version](#the-claude-code-version-no-python)
- [Testing strategy](#testing-strategy)
- [Extending it](#extending-it)

---

## The big picture

advpipe takes one work item ("add a `clamp` function", "fix the refund rounding bug") and drives
it through a fixed pipeline of AI agents. Each agent has one job. The core idea is
**adversarial pairs**: one agent produces work, and a second agent, with a different prompt
and usually a different model, tries to find concrete reasons it's wrong.

```
work item
   │
   ▼
[1] spec-writer ──► task.md          (acceptance criteria: the contract)
   │
   ▼
[2] test-author ⇄ test-critic        ≤ 2 rounds   gate: new tests exist and FAIL
   │
   ▼
[3] coder ⇄ code-critic              ≤ 3 rounds   gate: tests + types + lint green
   │
   ▼
[4] standards-reviewer ∥ security-reviewer       (parallel, one pass)
   │
   ▼
[5] arbiter                          (only if blocking findings are left or disputed)
   │
   ▼
[6] final gates → complete (branch ready for review) | needs_human
```

Seven rules shape everything below:

1. **The orchestrator is code, not an agent.** `Orchestrator._run` is a plain `if`/`await`
   sequence. No model ever decides whether a stage runs or is skipped.
2. **Tests before code.** The test author only sees `task.md`.
3. **One writer at a time.** Critics, reviewers and the arbiter can't edit files at all. Their
   tool list doesn't include editing tools.
4. **Critics get artifacts, not reasoning.** A critic sees `task.md`, a diff and gate output,
   never what the author said about its own work.
5. **Every loop is bounded:** round caps, a turn cap per agent, and a dollar budget per task.
6. **Critics may say PASS**, and their findings must point at a file and line, or at an
   acceptance criterion.
7. **Ground truth beats opinion.** A failing test or type check is a blocking finding, and no
   agent can argue it away.

## Levels

Every run has a **level** (`RunState.level`), picked by a person and fixed for the whole run
(plan: [LEVELS_PLAN.md](LEVELS_PLAN.md)). A level is a name for a **shape**, a few switches the
orchestrator reads (`levels.SHAPES`):

| # | Level | `solo` | `critics` | `review` | `arbiter` | scan by default | Steps |
|---|---|---|---|---|---|---|---|
| 1 | `basic` | yes | no | none | no | off | SOLO → FINAL CHECKS |
| 2 | `reviewer` | yes | no | single | no | off | SOLO → REVIEW → FINAL CHECKS |
| 3 | `tests` | no | no | single | no | off | SPEC → TESTS → CODE → REVIEW → FINAL CHECKS |
| 4 | `adversarial` | no | yes | none | yes | off | SPEC → TESTS ⇄ → CODE ⇄ → ARBITER → FINAL CHECKS |
| 5 | `full` | no | yes | full | yes | on | the pipeline above |

**All five levels run,** and the web UI shows them: the New run dialog's slider, the level on
every run, the solo stage and single review in the timeline, and decision panels (see
[The web UI](#the-web-ui)). Everything said about a level (its roles, steps, the safeguards it
drops, its agent-call bounds, its label) is derived from `SHAPES` in `levels.py`, so the CLI,
the report and the UI can't drift apart:

- `parse_level` (a name or 1-5), `level_number`, `LEVEL_ORDER`, `LEVEL_INFO`.
- `roles_for(level)`: the roles a run can call, as `Role` members (`Role.SOLO` for levels 1
  and 2, the one agent that does everything).
- `steps_for(level)`: the timeline steps (`Stage.SOLO` already exists for this).
- `dropped_safeguards(level, scan)`: plain-language lines for the report and `advpipe levels`;
  empty for `full` with the scan on.
- `max_agent_calls(level, limits)`: (happy path, worst case) agent calls between two person
  decisions. Full: 7 and 30 with the default caps; Adversarial: 5 and 23; Tests: 4 and 11;
  Reviewer: 2 and 4; Basic: 1 and 1. The worst case counts
  one JSON retry per critic, reviewer and arbiter call, the test-fix pass, and the arbiter and
  final author pass after each stage's cap. `tests/test_levels.py` (Full, Adversarial) and
  `tests/test_tests_level.py` (Tests) and `tests/test_solo.py` (Basic, Reviewer) run a script
  that fails as late as it can at every stage and check the count matches, so the formula can't
  drift; `tests/test_level_invariants.py` runs the happy path and a red path at all five.
- `level_tag(state)`: `"adversarial"`, `"full, no scan"`, ... for the summary line and the
  `run_start` message; empty for `full` with the scan on, so those read as they always have.

**Where it comes from** (highest first): `advpipe run --level` → the work item's `level:` →
`[pipeline] level` → `full`. The CLI merges the first two into the config's `[pipeline]` table
(`cli._with_run_settings`), so the run's `config.json` records them, and
`Orchestrator._new_state` copies them into `RunState` once. No agent output is ever read for
them. Runs recorded before levels existed have no `level` in `run.json` and load as `full`.

**The security scan** is a switch of its own: `RunState.security_scan`, from
`--security-scan/--no-security-scan` → front matter `security_scan:` → `[pipeline]
security_scan` → the level's default (`Shape.security_scan`: on at `full` only). `scan_on(state)`
reads it (None, in an old run, means the level's default). At `full` the review stage runs the
scanner for the security reviewer, as before; with the scan off it's skipped there ("turned off
for this run") and the security reviewer works from the diff alone. Below `full`, with the scan
on, `stages.run_gate_names(state)` adds `security` to the checks run after every code round (or
solo pass), after final passes and at the final gates, and to the gate commands the author is
given. There's no security reviewer to say whether a hit is real, so what a failure means is the
run's **scan policy** (`RunState.scan_policy`, from `--scan-failures` / front matter /
`[pipeline] scan_failures`), applied in one place, `stages.checked_findings`:

- `ask` (the default): the first failing scan stops the run at a `scan_hits` [decision
  point](#decision-points): *ignore* or *block*. The choice sticks for the rest of the run, so
  it's asked at most once. It's raised as `decisions.DecisionNeeded` from wherever the gates ran
  and caught in `Orchestrator.run`, like `BudgetExceeded`.
- `block`: the hits are blocking gate findings, like a failing test. The author gets them, the
  scan reruns, and a clean scan lets the run go on. Hits the author can't clear end at a
  `red_checks` decision point.
- `ignore`: the hits are recorded as a minor finding "ignored by you" (with the scan output),
  and the run carries on.

At `full` the scan is the security reviewer's input and is never asked about. An uninstalled
scanner is skipped at every level, and the report says so once (`note_scanner_skipped`).

**Design rules by level.** Rules 2, 4 and 6 apply at the levels that have the agents they
govern; the default level, `full`, applies all of them:

| Rule | Basic | Reviewer | Tests | Adversarial | Full |
|---|---|---|---|---|---|
| 1. Orchestrator is code | ✔ | ✔ | ✔ | ✔ | ✔ |
| 2. Tests before code (test author sees `task.md` only) | ✘ one agent | ✘ one agent | ✔ | ✔ | ✔ |
| 3. One writer at a time; reviewers read-only | ✔ (one writer) | ✔ | ✔ | ✔ | ✔ |
| 4. Critics get artifacts, not reasoning | n/a | ✔ | ✔ | ✔ | ✔ |
| 5. Every loop bounded | ✔ | ✔ | ✔ | ✔ | ✔ |
| 6. Critics may PASS; grounded findings | n/a | ✔ | ✔ | ✔ | ✔ |
| 7. Failed gate = blocking finding | ✔ | ✔ | ✔ | ✔ | ✔ |

## Module map

All code is in `src/advpipe/`.

| File | Responsibility | Key names |
|---|---|---|
| `cli.py` | Typer CLI: `run`, `answer`, `decide`, `resume`, `cancel`, `clean`, `status`, `report`, `levels`, `ui`, `gates` | `run`, `answer`, `decide`, `resume`, `cancel`, `clean`, `status`, `report`, `levels`, `ui`, `gates` |
| `levels.py` | Pipeline levels as data: what each level runs, and everything derived from that | `SHAPES`, `Shape`, `parse_level`, `roles_for`, `steps_for`, `dropped_safeguards`, `max_agent_calls`, `scan_on`, `level_tag` |
| `decisions.py` | Decision points: where a run stops and asks a person to choose; `advpipe decide` | `decide`, `CHOICES`, `awaiting_decision`, `decision_ready`, `pending_choice`, `take`, `DecisionNeeded` |
| `answers.py` | Blocking spec questions and their answers (or Bypass: the agent decides): waiting/ready checks, recording answers, answers files, the Answers section of `task.md` | `record_answers`, `awaiting_bypass`, `awaiting_answers`, `answers_ready`, `with_answers_section`, `parse_answers_file`, `migrate` |
| `orchestrator.py` | The state machine; resume; parallel runs | `Orchestrator`, `Orchestrator._run`, `Orchestrator._settle`, `Orchestrator.resume`, `check_same_shape`, `run_many` |
| `stages.py` | What each stage does; prompt building; JSON parsing and retry | `adversarial_stage`, `review_stage`, `single_review_stage`, `checked_findings`, `arbiter_stage`, `final_author_pass`, `call_critic`, `bypass_questions`, `test_file_guard`, `run_gate_names`, `Context` |
| `models.py` | Pydantic data contracts and validation rules | `Finding`, `Verdict`, `AuthorResponse`, `ArbiterRuling`, `RunState`, `Decision`, `Stage`, `Status`, `Level` |
| `runner.py` | The agent abstraction and the Claude Agent SDK implementation; role → tools/model table | `AgentRunner`, `SdkAgentRunner`, `Role`, `ROLE_TOOLS`, `ROLE_MODEL`, `build_request` |
| `gates.py` | Runs check commands; turns failures into findings | `run_gates`, `gate_findings`, `red_gate_findings`, `GateResult` |
| `workspace.py` | Git worktree per run; diffs; reverting files | `Workspace.create`, `.reopen`, `.remove`, `.diff`, `.changed_files`, `.restore` |
| `budget.py` | Cost tracking with a hard stop | `Budget`, `BudgetExceeded` |
| `runlog.py` | The on-disk run directory, lock file, event log and `report.md`; run-id validation | `RunLog`, `check_run_id`, `render_report`, `list_runs` |
| `events.py` | One `emit()` for progress: appends to `events.jsonl` and prints | `emit`, `EventKind`, `gate_statuses` |
| `control.py` | Acting on runs from outside: detached start, cancel, clean | `start_detached`, `cancel_run`, `clean_run`, `ControlError` |
| `workitem.py` | Work-item files with optional front matter | `parse_work_item`, `load_work_item`, `format_work_item`, `WorkItem` |
| `config.py` | `pipeline.toml` loading and validation | `Config`, `load_config` |
| `ui/` | Optional local web UI (the `ui` extra); see [The web UI](#the-web-ui) | `create_ui_app`, `GuardMiddleware`, `load_runs`, `load_detail`, `stream_events`, `item_path`, `plan_run`, `call_advpipe` |
| `prompts/*.md` | One system prompt per role. **Source of truth** for agent behaviour. | |

## A run, step by step

Everything starts in `Orchestrator.run`, which wraps `Orchestrator._run` in error handling.

```python
# orchestrator.py, simplified
async def run(self):
    self.runlog.acquire_lock()
    try:
        await self._run()
    except BudgetExceeded:      status = BUDGET_EXCEEDED
    except CancelledError:      note "Interrupted", re-raise (status stays "running" → resumable)
    except Exception:           status = ERROR
    finally:
        write run.json and report.md; release the lock
```

The `finally` block means **every run leaves a `report.md`**, including runs that crashed or
ran out of money. Besides the level line (`runlog._level`: the level, its agent count, the scan
setting, and "Not done at this level" from `dropped_safeguards`; just the name for `full` with
the scan on), the report has a `## Questions and answers` section (agent answers marked, with
their reasons) and a `## Decisions` section (`runlog._decisions`: each decision point in order,
the explanation, the person's choice and note, a hand-over or stop line, and the number of fix
passes). Ignored scan hits are minor findings "ignored by you", and `runlog.scan_policy_line`
says whether the policy was chosen up front or at a decision point. `RunLog.write_state` also
writes each decision to `decisions/N.json`, as asked and as decided. `advpipe status <id>` shows
the same facts (`cli.status`).

`_run` then does the following:

### 0. Set up the workspace (`Orchestrator._open_workspace`)
A new run calls `Workspace.create`. That makes a git worktree at
`<repo>/.advpipe/worktrees/<run-id>` on a new, human-readable branch (see
[isolation](#isolation-worktrees-and-branches)), starting from the
repo's current `HEAD`. It also saves the effective config to `config.json` in the run directory.
The run's level and security-scan setting were fixed when its `RunState` was made
(`_new_state`); the `run_start` event carries both (`level`, `security_scan`), and its message
names the level unless it's `full` with the scan on.
A resumed run calls `Workspace.reopen` instead (see [resume](#persistence-resume-and-locking)).

### 1. Spec (`Orchestrator._spec`)
- Calls the **spec-writer** with the work item (`spec_prompt`).
- **Guard:** the spec writer may only create `task.md`. Any other file it touched is reverted
  with `Workspace.restore` and noted in the report.
- If `task.md` is missing, the run stops with `needs_human`.
- `blocking_open_questions` scans the `## Open questions` section for items starting with
  `BLOCKING:`. If there are any, the run stops with `needs_human` **before any code is
  written**. The idea is that a human answers a question once instead of agents guessing and
  arguing. Each question is recorded in `state.questions` (`OpenQuestion`: id `Q1`, `Q2`, ...,
  numbered across the whole run; its text; the spec pass that asked it) and as an open
  finding, and a `questions` event says how to answer.
- Commits `task.md`. That commit becomes `state.commits["spec"]`.

**Answering and resuming** (`answers.py`; plan: [ANSWERS_PLAN.md](ANSWERS_PLAN.md)). A person
answers with `advpipe answer` (or the web UI). `answers.record_answers` checks that the run
is waiting on answers (`awaiting_answers`: `needs_human` at SPEC, no spec commit, questions
pending, spec passes left under `limits.max_rounds_spec`), that every question no pass has
used an answer for yet is answered (all or nothing; empty and over-long answers refused), that
no live process holds the lock and that the branch still exists. It saves the answers in
`run.json` (under the lock) and appends an `answers` event; the status stays `needs_human`.
Answering again before resuming replaces the answers. Then:

- `check_resumable` accepts a `needs_human` run whose answers are all in
  (`answers_ready`), and for one still waiting raises `NotResumable` naming the questions and
  `advpipe answer <run-id>`. `RESUMABLE` itself is unchanged.
- `_spec` runs a **revision** whenever some answers haven't been used yet
  (`answers.unapplied`): `spec_revision_prompt` gives the spec writer the work item, its
  previous `task.md` and every question with its answer. The previous `task.md` comes from the
  run directory, because `Workspace.reopen` has wiped the worktree's uncommitted copy. After
  the pass (`state.spec_passes` += 1, its answers marked `applied`) the orchestrator rewrites
  the `## Answers` section of `task.md` itself (`answers.with_answers_section`): every answered
  question with the answer verbatim, indented so no line of an answer can start a heading or a
  list item, so `blocking_open_questions` never reads an answer as a question. Later agents see
  the answers as part of `task.md`, never the spec writer's reply.
- A revision may ask new blocking questions: they get the next ids and the run stops again.
  One that repeats an answered question (same text, ignoring case and spacing) isn't asked
  again; a note says its answer stands. When the pass that still has blocking questions is
  pass number `max_rounds_spec` (default 3), the run doesn't ask them one more time: it stops
  at a `spec_cap` [decision point](#decision-points), *Begin work?*, where the person chooses
  `begin` or `stop`. The questions can no longer be answered (`advpipe answer` is refused, and
  says so, naming `advpipe decide <id> begin`).
- **Bypass** (docs/LEVELS_PLAN.md §3.6). `advpipe answer --bypass` (or `Q2: BYPASS` in an
  answers file, or an empty answer confirmed in the terminal) records `bypassed = True` on a
  question instead of an answer: `record_answers(..., bypass=ids)` treats it as dealt with, so
  the run is resumable (`answers_ready` also holds when a bypassed question still has no
  answer; `awaiting_bypass` lists them), and an id can't be both answered and bypassed. When
  `_spec` starts a pass it first calls `_bypass`, which calls `stages.bypass_questions`: one
  JSON call to the role that asked (`spec-writer` here; the solo agent at levels 1 and 2), parsed with
  `call_json_agent` (so one retry on bad JSON) into `BypassAnswers`, charged to stage `spec`,
  with the reply saved to `spec/pass-N/bypass.json`. Files the call changed are reverted
  (`task.md` is put back exactly as it was). Each question gets `answer`, `reasoning` and
  `source = "agent"`; a missing or unusable answer becomes a stand-in (`FALLBACK_ANSWER`) with a
  note, so the run can't stall. The usual revision pass then runs with those answers, and
  `with_answers_section` marks them ("agent's best judgement", with the reason) and says so in
  its intro; with person answers only, its wording is unchanged. Answers the agent already chose
  are not asked for again if the revision is interrupted. Bypass covers one stop: the next
  pass's new questions are pending again. `RunState.bypass_questions` (up front: `--bypass-questions`,
  front matter, `[pipeline]`) makes `_spec` mark each new question bypassed instead
  of stopping; below the spec-pass cap it loops into the next revision, at the cap it asks the
  agent, writes the answers into `task.md` without another revision and commits. A resumed run
  keeps the setting (`check_same_shape` refuses a config that changes it).
- **Begin work?** (`spec_cap`, `Orchestrator._begin_spec`). `begin` sets
  `state.bypass_questions = True` (so no later question stops the run) and marks every question
  still pending as bypassed; the spec writer then answers them in one bypass call
  (`spec/pass-N/bypass-begin.json`), the answers go into `task.md` without another revision
  pass, and the spec is committed. `stop` ends the run (`ended_by = "stop"`): nothing is
  committed, the last `task.md` stays in the run directory, and the run can't be resumed.
- An interrupted, failed or over-budget revision leaves its answers unapplied, so a plain
  `advpipe resume` runs the revision again.
- Runs stopped by an older advpipe keep their questions only as open findings
  (`criterion: open-question`); `answers.migrate` turns those into `questions`.

`advpipe answer` (`cli.answer`) collects the answers (from `--answer ID=TEXT`, `--file` parsed
by `answers.parse_answers_file`, or prompts when stdin is a terminal), calls `record_answers`,
then calls the `resume` command function, so foreground, `--detach`, `--budget` and the exit
codes are exactly `advpipe resume`'s. A run whose answers are already saved (`--no-resume`)
is just resumed. `cli._summary` and `advpipe status <run-id>` print the questions and the
`advpipe answer` command while a run is waiting. The Claude Code skill follows the same
procedure as instructions: it asks the user in the session, relaunches the spec writer with the
answers, and writes the Answers section itself.

### 2. Tests (`adversarial_stage(ctx, "tests", critic=shape.critics)`)
The test-author / test-critic loop, capped at `max_rounds_tests` (default 2). Its gate is
*inverted*: the new tests must exist and must **fail**. See
[the loop](#the-adversarial-loop-in-detail). Then `_settle` handles anything still open, and
the result is committed (`state.commits["tests"]`).

### 3. Code (`adversarial_stage(ctx, "code", critic=shape.critics)`)
The coder / code-critic loop, capped at `max_rounds_code` (default 3). Its gate requires tests,
types and lint to be green. The test-file guard runs after every coder pass. Then `_settle`, and
a commit (`state.commits["code"]`).

**Without critics** (`critic=False`, the `tests` level), each round still runs the author, the
test-file guard (code stage) and the stage's gates, and writes `author.txt`, `diff.patch` and
`gates.json`, but makes no critic call and writes no `critic.json`. The only blocking findings
are then gate and guard findings, which go back to the author up to the same round cap. The red
gate still applies to the tests stage. The test-defect path needs a code critic's finding, so it
never fires at this level.

### 4. Review (`review_stage`, or `single_review_stage`)
At levels whose shape has `review == "full"` (`full`), the two reviewers below. At `review ==
"single"` (`tests`), one code-critic pass instead (below). At `adversarial` (`review ==
"none"`) the run goes from the code stage straight to the final gates, and the report lists
"No standards review" and "No security review" under *Not done at this level*.

**Single review** (`single_review_stage`): one `code-critic` call, no back-and-forth. Its
prompt is `critic_prompt(..., single=True)`: `task.md`, the full diff since the base commit
(tests included, `task.md` excluded), the current gate output, the list of test files changed
in the run, and the line "This is a single review pass" (the code critic's system prompt says
what that means; the flag is in the prompt text, never a different system prompt). Findings
are prefixed `REV-`. A `test-defect` finding is an ordinary blocking finding here: there's no
test-fix pass, and the coder's test-file guard still protects the tests. Blocking findings go
to `_settle` (below), so the coder gets one pass, then the gates. Files: `review/diff.patch`,
`review/single.json`; charged to stage `review`.

At `full`:

- Runs the security scanner gate (semgrep by default), unless the run's security scan is off.
  It's optional: if the tool isn't installed, the gate is skipped and the report says so.
- Calls **standards-reviewer** and **security-reviewer** at the same time with
  `asyncio.gather`. Both get `task.md`, the full diff since the base commit, and gate output.
  The standards reviewer also gets the repo's standards doc (`CLAUDE.md` by default). The
  security reviewer also gets the scanner output, which it must check for reachability rather
  than repeat.
- Blocking findings from either reviewer go to `_settle` (the arbiter). There's no back-and-forth
  with reviewers.

### 5. Arbiter (`Orchestrator._settle` → `arbiter_stage` → `final_author_pass`)
`_settle` runs after every stage. If the stage passed, it records the minor findings and
returns. Otherwise:

1. `arbiter_stage` rules on each open finding:
   - **Gate and guard findings are ruled `fix` automatically**, with no model call, because
     the test runner isn't up for debate.
   - Everything else goes to the **arbiter** agent, which returns `fix` or `dismiss` with a
     reason. A finding the arbiter skips defaults to `fix`. Unparseable arbiter output makes
     everything `fix`.
2. If anything was ruled `fix`, `final_author_pass` gives the relevant author (test-author for
   the tests stage, coder otherwise) **one** more pass, with **no critic round** afterwards.
   Gates (and the test-file guard, for the coder) run again.
3. If gates still fail, the run stops at a `red_checks` [decision point](#decision-points)
   (status `needs_human`, those failures as open findings): a person chooses a further fix pass
   or taking over.

At levels without an arbiter (`_settle(..., arbiter=False)`, the `tests` level), step 1 is
done by code: every open finding is ruled `fix` with the reason "no arbiter at level tests",
recorded in `state.rulings` (so the report shows why) and as `ruling` events, with no model call
and no ARBITER step. The final author pass (step 2) is then told "these findings are still
open" rather than "the arbiter ruled", and is charged to the stage being settled
(`stage-tests`, `stage-code`, `solo` or `review`) instead of `arbiter`. Step 3 is the same.
At the solo levels the author is the solo agent, and the final pass is `final-pass-solo/`.

### 6. Final gates
All gates (`run_gate_names`: plus the security scan below `full` when it's on) run one last
time on the finished branch. Green means `complete`, and the worktree
is removed (the branch stays). Red stops at a `red_checks` decision point (`needs_human`).

### The solo stage (levels basic and reviewer)
`Orchestrator._solo` runs instead of SPEC, TESTS and CODE. One agent, `solo` (the author model,
writer tools, prompt `prompts/solo.md`), writes `task.md` first, then the tests and the code,
and runs the checks. One call per entry, no automatic retries:

- **Blocking questions.** `task.md` goes through `blocking_open_questions` and the same
  recording as the spec stage (`Orchestrator._question_stop`, shared with `_spec`): the
  questions get ids, `state.spec_passes` counts the pass, and **everything except `task.md` is
  reverted**, because the agent wasn't allowed to write code yet. The run stops for answers, or
  at *Begin work?* after `max_rounds_spec` passes, exactly as at the spec stage; `advpipe
  answer` works unchanged (`answers.spec_stage(state)` says the stage is SOLO and the commit
  key `solo`, wherever `answers.py` and the UI used to assume SPEC and `spec`). Answers (or,
  under Bypass, the solo agent's own, from `bypass_questions` called with `Role.SOLO`) come
  back in a revision pass (`solo_revision_prompt`: previous `task.md` from the run directory
  and the Q&A), which does the work. With Bypass on and the cap reached, the solo agent gets one
  more pass past the cap, because the pass that asked did none of the work; if that pass still
  has questions they are answered by a bypass call and the checks decide.
- **Checks.** `task.md` must exist (else a blocking `guard:task-md` finding), then the gates
  (`run_gate_names`) run. There's no test-file guard (one agent owns both) and no red gate
  (tests and code arrive together). Red goes straight to a `red_checks` decision point.
- **Commit** when the checks pass: `state.commits["solo"]`. Files:
  `stage-solo/pass-N/{author.txt, diff.patch, gates.json, task.md}` (plus `answers.json`,
  `bypass.json`, and `request.json` for a pass a person asked for).

At **Reviewer** the REVIEW step follows: `single_review_stage` (one `code-critic` pass over
the whole change, as at Tests), and blocking findings go to one automatic pass by `solo`
(`_settle(..., Role.SOLO, "solo", arbiter=False)`), then the gates; still red asks.

## Decision points

A **decision point** is a stop where the run waits for a person to choose one of two things
(`decisions.py`, plan: [LEVELS_PLAN.md](LEVELS_PLAN.md) §3.5). It has status `needs_human`, like
every stop, so `run.json` and scripts that look for that status keep working;
`RunState.decisions` (a list of `Decision`, the last being the open one) says what's asked.
Only a person picks a choice: `advpipe decide <run-id> CHOICE [--note TEXT]`.

| Kind | When | Choices |
|---|---|---|
| `red_checks` | checks are red and the pipeline has nothing automatic left to try | `fix` (one more pass, optional note) · `takeover` |
| `scan_hits` | the security scan failed below `full` and the policy is `ask` | `ignore` · `block` |
| `spec_cap` | the last allowed spec (or solo) pass still has blocking questions | `begin` · `stop` |

- **Where `red_checks` happens** (`Orchestrator._ask_red`): after the stop's own machinery has
  run. At levels 3 to 5 that's the stage's round cap, then `_settle` (the arbiter where the
  level has one, then the final author pass); also red final gates. At levels 1 and 2 it's
  right after the solo pass (no retry), after Reviewer's review-fix pass, and at the final
  gates. The decision's `stage` is the stage being settled (for a settle that failed in the
  arbiter step, the run stays "in" ARBITER but the decision remembers CODE, which is where the
  fix pass goes).
- **`fix`** (`Orchestrator._person_fix`, `_solo_fix`): one pass by the stage's author (the test
  author for TESTS, the coder for CODE, REVIEW and FINAL_GATES, the solo agent at levels 1
  and 2) with the failing output and the person's note verbatim, via `final_author_pass(...,
  extra=fix_pass_extra(note), rdir="fix-pass-N")`. No critic round: the gates decide. Green
  carries on from that stage (commit, next stage); red asks again. At most
  `limits.max_fix_passes` (default 3) per run; `decide` refuses the next one.
- **`takeover`** (`decisions._take_over`, at once, in `decide`): commits the work as it is
  ("advpipe: work in progress, handed over (checks failing)", skipped when nothing changed),
  removes the worktree (the branch stays), and ends the run (`ended_by = "takeover"`). The run
  can't be decided or resumed any more; `advpipe clean` follows its usual rules.
- **`ignore` / `block`**: set `state.scan_policy` for the rest of the run (see the security
  scan above). The stage that was running starts again with its author's work kept in the
  worktree and `ctx.judge_existing` set, so the stage's first round skips the author call and
  judges the work as it is.
- **`begin` / `stop`**: see *Begin work?* under the spec stage.
- **The worktree at a stop.** `Workspace.reopen` resets the worktree on resume, which would
  throw away the work a person is deciding about. At a decision point the worktree is left
  exactly as it is, and a resume that applies a choice (`decisions.pending_choice`) reopens it
  with `keep_changes=True`: no `reset --hard`, no `clean`. (If the worktree was deleted
  meanwhile, it's recreated from the branch and a note says the uncommitted work was lost.)
- **Resuming.** `check_resumable` refuses a run waiting for a decision and names `advpipe
  decide <id> fix | takeover`; it accepts one whose continuing choice is made (`decision_ready`).
  The choice is marked `applied` (and `fix_passes` counted) only once it has been acted on, so a
  fix pass an interrupt cut short runs again on a plain `advpipe resume`, with the worktree
  kept.
- **Asking at once.** A foreground `advpipe run`/`resume` that stops at a decision point with a
  terminal on stdin shows the explanation and asks right away (`cli._decide_now`): the choices
  plus "later", which leaves the run waiting, as a detached run does. `advpipe decide` with no
  choice does the same, or prints the choices and exits 2 without a terminal.

## The adversarial loop in detail

`adversarial_stage` in `stages.py` is the heart of the system. One round:

```
author runs            round 1: author_initial_prompt (task.md + gate commands)
                       round N: author_findings_prompt (last round's blocking findings + gates)
   │
   ├─ round N>1: parse the author's AuthorResponse → which findings it disputed
   ├─ code stage: test_file_guard (revert test edits → blocking finding)
   ├─ stage_gates          tests stage: red gate      code stage: test/types/lint
   ├─ critic runs          critic_prompt(task.md, diff, gate output); never the author's text
   │
   └─ blocking = guard findings + gate findings + critic's blocking findings
      no blocking → stage passes
      otherwise   → next round, or hand off to the arbiter at the cap
```

Details that matter:

- **IDs include the round.** `_prefix` renames critic finding `F1` from code round 2 to `C2-F1`,
  and gate findings become e.g. `C1-G-test`. Findings from different rounds never collide, and
  authors and the arbiter can refer to them unambiguously.
- **Disputes aren't re-debated.** In round N an author can mark a finding `disputed` with a
  reason. That round's critic sees the new code. If it raises *the same finding again*, the
  finding goes straight to the arbiter instead of back to the author. If the critic doesn't
  raise it again, the dispute is accepted. "The same finding" means the same
  `Finding.match_key()`, which is `(file, criterion)`. Line numbers are left out because they
  move when code is edited.
- **Defective tests go back to the test author, once.** The coder can't edit tests, so a test
  that is itself wrong (invalid SQL, a wrong expected value) would otherwise end the run. If
  the test gate fails and the code critic reports a finding with `category: "test-defect"`,
  `test_fix_pass` gives the **test author** one fix pass for that stage. Changes outside the
  test paths are put back. The fixed tests are committed on their own, and that commit becomes
  the coder's new test-guard baseline (and `state.commits["tests"]`). The gates then rerun in
  the same round. The critic's claim never clears anything by itself: only the rerun gates
  count. Test-defect findings are recorded as minor and never sent to the coder.
- **Gate findings can't be disputed.** `_is_gate` recognises them by their `gate:` / `guard:`
  criterion, and dispute handling skips them.
- **Minor findings never reopen a loop.** They go into the report as-is.
- **The code critic gets context.** `critic_prompt` adds the list of test files written in the
  tests stage. Without it, a critic that only sees the code diff tends to complain that "no
  tests were added". That happened in the first real run.
- **`task.md` is excluded from judged diffs** (`REVIEW_PATHS`). It's the contract, not part of
  the change. Without this, reviewers flagged it as scope creep.

## Gates: ground truth

`gates.py` runs the commands from `[gates]` in `pipeline.toml`, always from the worktree root.

- `run_gate` runs a command in a worker thread (`asyncio.to_thread`) with a timeout
  (`gate_timeout_s`). It keeps only the **tail** of the output (`tail`: last 60 lines, max 4000
  characters). That's what failures usually need, and it keeps prompts small.
- An empty command list means **skipped**. A missing executable means **failed** (exit 127),
  except for gates in `OPTIONAL_GATES` (only `security`), which are skipped instead.
- `gate_findings` turns each failed gate into a blocking `Finding` with
  `criterion="gate:<name>"` and the output tail as evidence.
- `red_gate_findings` is the inverted Tests-stage gate. It produces a blocking finding if:
  - no test files changed (`G-tests-missing`)
  - the test gate isn't configured (`G-tests-skipped`)
  - the tests **pass** before any implementation exists (`G-tests-green`: "tests don't
    exercise the change")

  Whether the failure is for the *right reason* (missing feature rather than a broken test) is
  judged by the test critic, which sees the failure output.

## Guards: rules enforced in code

Prompts ask agents to behave, and guards make sure they did:

| Rule | Where | What happens |
|---|---|---|
| Coder must not touch tests | `test_file_guard` | Every changed or new file under `[paths] tests` is reverted (`Workspace.restore`, which deletes files that didn't exist at the base commit) **before** gates run, and a blocking `scope` finding is raised. |
| Spec writer writes only `task.md` | `Orchestrator._spec` | Other changed files are reverted and noted in the report. |
| Critics are read-only | `ROLE_TOOLS` in `runner.py` | Read-only roles get only `Read`, `Glob`, `Grep`. They have no tool that can change anything. |
| PASS means no blocking findings | `Verdict._enforce_rules` | A "PASS" containing blocking findings is turned into FAIL. |
| Findings must be specific | `Verdict._enforce_rules` | Findings with neither (`file` and `line`) nor `criterion` are moved to `noise`, counted in the report, and otherwise ignored. |

## Data contracts

Defined in `models.py` with Pydantic v2. Every agent reply that needs a structure is parsed
into one of these.

- **`Finding`**: `id`, `severity` (`blocking`/`minor`), `category`, optional `file`/`line`/
  `criterion`, `claim`, `evidence`, `suggested_check`. The `is_grounded` property checks the
  "point at something specific" rule.
- **`Verdict`**: `verdict` (`PASS`/`FAIL`) plus findings. Returned by critics and reviewers.
- **`AuthorResponse`**: authors' replies to findings: `fixed` or `disputed` (with a reason).
  Parsed *leniently* (`parse_author_response`): unparseable means "nothing disputed", because
  retrying an author would mean re-running its edits.
- **`ArbiterRuling`**: a `fix`/`dismiss` decision with a reason for each finding.
- **`RunState`**: everything about a run that's persisted to `run.json`: status, stage, round,
  cost (total, per stage, calls per stage), open and minor findings, rulings, the per-stage
  commits used for resume, notes.

**JSON parsing** (`stages.py`):
- `extract_json` pulls a JSON object out of output that may be wrapped in prose or code fences.
- `call_json_agent` parses the reply. On failure it calls the agent **once more** with the parse
  error and its previous reply attached.
- `call_critic` adds the final fallback: if the second attempt also fails, the result is a
  `FAIL` verdict with one blocking finding, "critic output unparseable". A broken critic can
  never silently pass a change.

## Talking to Claude

`runner.py` keeps the orchestrator independent of the SDK:

```python
class AgentRunner(Protocol):
    async def run(self, request: AgentRequest) -> AgentResult: ...
```

`build_request` creates an `AgentRequest` for a role:
- the role's **system prompt** from `prompts/<role>.md` (via `load_prompt`)
- the **model** from `ROLE_MODEL` mapped through `[models]`: authors and arbiter on Opus,
  critics and reviewers on Sonnet. A different model for the critic gives more independent
  judgement and costs less.
- the **tools** from `ROLE_TOOLS`
- **max turns** from config
- **max budget**: what's left of the task budget

`SdkAgentRunner.run` maps this onto `claude_agent_sdk.query` with `ClaudeAgentOptions`:

| Option | Value | Why |
|---|---|---|
| `tools` | role's tool list | Limits which tools *exist*, so read-only roles really are read-only |
| `allowed_tools` + `permission_mode="dontAsk"` | role's tool list | Pre-approves those tools and denies anything else, so a run never stalls on a permission prompt |
| `cwd` | the run's worktree | Agents work in the isolated copy |
| `max_turns`, `max_budget_usd` | from config / remaining budget | Bounds a single agent |
| `setting_sources=["project"]` | | Loads the *target repo's* `CLAUDE.md` and project settings, not your user-level config |

It returns the final text plus `total_cost_usd` from the SDK's `ResultMessage`.

## Isolation: worktrees and branches

`workspace.py` wraps git. **advpipe never touches your working tree.**

- `Workspace.create` runs `git worktree add -b <branch> .advpipe/worktrees/<run-id> HEAD`. The
  branch comes from `unique_branch`: `advpipe/` plus `slugify` of `--name` or the work item's
  first line (lowercase words, filler words dropped, cut at a word boundary to 40 characters),
  with `-2`, `-3`, ... appended if it already exists. Worktree directories and run logs stay
  keyed by run id.
  `_exclude_advpipe_dir` adds `.advpipe/` to `.git/info/exclude`, a local ignore file that isn't
  tracked, so run artifacts never show up in your `git status`.
- `Workspace.diff` stages new files with `git add --intent-to-add` first, so brand-new files show
  up in the diffs critics see. Diffs over 60,000 characters are truncated.
- `Workspace.commit` commits with a fixed `advpipe` identity and `--no-verify`, so your own
  commit hooks don't interfere with the pipeline's commits.
- When a run completes, `Workspace.remove` deletes the worktree directory. The branch, with one
  commit per stage, is what you review.

## Persistence, resume and locking

**Run directory** (`runlog.py`), at `<repo>/.advpipe/runs/<run-id>/`:

```
run.json        RunState, rewritten at every transition and after every agent call
events.jsonl    every progress event, one JSON object per line (see below)
console.log     stdout and stderr of a run started with --detach
config.json     the config this run runs under (rewritten on resume, e.g. after --budget); its
                [pipeline] table has the level and scan setting from the command line and
                front matter
task.md         copy of the spec (the latest pass, with the Answers section)
spec/author.txt the spec writer's reply (first pass)
spec/pass-N/{task.md, author.txt, answers.json}   each spec pass's task.md; for passes N ≥ 2
                (revisions after answers) also the reply and the questions and answers given
spec/pass-N/bypass.json   the agent's own answers to bypassed questions, for the pass that used them
stage-solo/pass-N/{author.txt, diff.patch, gates.json, task.md}   levels basic and reviewer; also
                answers.json (a revision), bypass.json (Bypass) and request.json (a fix pass you asked for)
stage-tests/round-N/{author.txt, diff.patch, critic.json, gates.json}
stage-code/round-N/{author.txt, diff.patch, critic.json, gates.json}
stage-*/round-N/critic.json is absent at levels without critics (tests)
review/{diff.patch, standards.json, security.json, scanner.json}   (full)
review/{diff.patch, single.json}   (the single review: tests)
decisions/N.json                             (each decision point, as asked and as decided; N from 1)
arbiter.json, arbiter-<stage>.json   (when the arbiter ran)
final-pass-<kind>/{author.txt, gates.json}   (the final author pass after _settle; kind is tests, code or solo)
fix-pass-N/{author.txt, gates.json}          (a fix pass you asked for at a decision point, outside the solo stage)
final-gates.json
report.md       human summary: status, findings, rulings, cost table, notes
run.lock        present only while a process is driving the run
```

**Resume** works at stage boundaries. When a stage finishes, its commit is stored in
`state.commits` (`spec`, `tests`, `code`, `review`, or `solo`). `Orchestrator.resume`:

1. Loads `run.json` (`check_resumable`). Refuses (`NotResumable`) if the run is `complete` or
   `needs_human`, or if another live process holds the lock. Resumable statuses are `running`
   (interrupted), `error` and `budget_exceeded` (`RESUMABLE` in `models.py`), plus a
   `needs_human` run whose blocking spec questions have all been answered (see "Answering and
   resuming" above) or whose decision point has a continuing choice (see
   [Decision points](#decision-points)).
2. Uses the run's saved `config.json` unless you pass one. `--budget` overrides the limit.
   `_open_workspace` writes the config back to `config.json`, so a raised budget sticks: the
   UI shows it, and a later resume uses it.
3. Restores the budget's spent amount and per-stage totals from `run.json`.
4. Keeps the run's level and security-scan setting: `check_same_shape` refuses a `--config`
   whose `[pipeline]` explicitly sets a different level or scan, and the config used (and
   written back to `config.json`) gets the run's own values.

`_run` then calls `Workspace.reopen`:
- if the worktree exists: `git reset --hard` and `git clean -fd`, which throws away the
  interrupted stage's half-done work (not after a person's decision about it: then
  `keep_changes=True` leaves it alone)
- if it was deleted: recreate it from the branch

`_run` then skips every stage already in `state.commits`. The interrupted stage starts over at
round 1.

`diff.patch` is exactly the diff placed in that critic's (or both reviewers') prompt, so you
can see what was judged. It's written before the critic is called.

`RunLog.write_text` (used for `run.json`, `report.md` and the rest) writes a temporary file and
renames it over the target, so a reader polling a live run never sees a half-written file.

**Locking.** `RunLog.acquire_lock` writes the current process ID to `run.lock`.
`RunLog.lock_holder` checks whether that process is still alive (`pid_alive`: `os.kill(pid, 0)`, or
`OpenProcess` on Windows, where signal 0 means Ctrl+C), so a lock
left behind by a crash doesn't block anything. A lock naming this process counts as its own
(`is_this_process`); on Windows that includes the parent, because a venv's `python.exe` is a
launcher that runs the real interpreter as a child, so `start_detached` records the launcher's pid.

**Run ids are path components.** `RunLog.for_run` passes every id through `check_run_id`
(letters, digits, `.`, `_`, `-`; no leading `.` or `-`; no `..`), and every CLI command that
takes a run id validates it the same way, so no id can point outside `.advpipe/runs/`.

## Progress events

Every progress line goes through one function, `events.emit(runlog, state, progress, kind,
message, **data)`. It appends an event to `events.jsonl` (opened in append mode and closed per
event, so each line is on disk before the run moves on) and then hands `message` to the
`progress` callback, which is the CLI's timestamped stderr printer. The terminal text is the
event's `message`, unchanged from before the event log existed. `Orchestrator._emit` and
`Context.emit` are thin wrappers that fill in the run log and state.

```json
{"ts": "2026-10-08T04:41:02.512Z", "kind": "agent_done", "stage": "CODE", "round": 2,
 "message": "coder done in 63s, $0.31 (run total $1.10)", "role": "coder",
 "cost_usd": 0.31, "total_usd": 1.10, "is_error": false}
```

Every event has `ts`, `kind`, `stage`, `round` (from `RunState` at that moment) and `message`.
Extra fields by kind:

| `kind` | When | Extra fields |
|---|---|---|
| `run_start` | new run (branch, worktree) or resume | `branch` or `worktree` (new runs); `level` and `security_scan` (the branch line and resumes) |
| `stage` | `== STAGE`, and a stage passing or going to the arbiter | `passed` (for the latter) |
| `round` | start of an author/critic round, or of a spec revision pass | `cap` |
| `agent_start` / `agent_done` | around every agent call | `role`; `agent_done` adds `cost_usd`, `total_usd`, `is_error` |
| `gates` | check results, scanner result, each blocking gate failure | `gates`: `{name: "pass" \| "fail" \| "skipped"}` (not on the failure lines) |
| `verdict` | a critic or reviewer verdict | `role`, `verdict`, `blocking`, `minor` |
| `guard` | the test-file guard reverted something | |
| `test_fix` | the test-defect fix pass | `files` (when tests were committed) |
| `ruling` | each arbiter ruling | `finding_id`, `decision` |
| `status` | budget exceeded, interrupted, and the final event of every process | `status`; the final one adds `final: true` and `total_usd` |
| `error` | an unexpected exception ended the run | |
| `questions` | the spec stopped on blocking questions (or, with Bypass up front, found some) | `ids`; `answerable` (false once the spec-pass cap is reached, or under Bypass); `bypassed: true` (Bypass up front) |
| `decision` | a run stopped at a decision point, or a choice was recorded or applied | `decision_kind`, `choice` (`asked` or the choice), `decision_stage`; `choices` (when asked) |
| `answers` | answers were recorded (`advpipe answer`, written by that process), or the agent answered bypassed questions | `ids`; `bypassed` (ids a person bypassed, when there are any); `agent: true` (the agent's own answers, under Bypass) |

The final `status` event is written with `echo=False`: it isn't printed, because the CLI prints
its own summary. Resumed runs append to the same file, so a run's whole history is one file. A
reader (like the planned UI) can tail it, and knows the driving process has finished when it
sees `final: true`.

## Background runs, cancel and clean

These live in `control.py` so the CLI stays thin and the UI can share them.

**`advpipe run --detach`.** The parent validates everything it can first (sources, work-item
file, config), picks the run id (`new_run_id`), and calls `start_detached`. That starts
`python -m advpipe run ... --run-id <id>` with `subprocess.Popen` (an argument list, never a
shell) in a new session (`start_new_session=True`; on Windows, a new process group with a hidden
console), with stdin from `/dev/null` and
stdout/stderr appended to `console.log`. It then writes the child's pid to `run.lock`, so
`cancel` works before the child has even started up. The parent prints the run id on stdout
and exits. A work item given as text is passed after `--`, so text starting with `-` isn't read
as an option. `--run-id` is a hidden option, used only for this.

**`advpipe resume --detach`.** The same, for resuming: the parent runs `check_resumable`
first (so a run that can't be resumed is refused at once, with nothing started), then starts
`advpipe resume <id> ... --detached-child` and prints the run id. `--detached-child` is hidden:
it tells the child that `run.lock` already names its own pid (the parent wrote it), which would
otherwise read as "still being driven". Without it, any live holder, this process included, is
refused as before.

**`advpipe cancel`.** `cancel_run` reads `run.lock`, checks the process is alive and (where
`/proc` exists) that its command line mentions `advpipe`, so a stale lock whose pid was reused
after a reboot can't make it signal an unrelated process. It then sends `SIGINT`. In the run's
process, `asyncio.run` turns SIGINT into cancelling the main task, which arrives in
`Orchestrator.run` as `CancelledError`: the existing Ctrl-C path. The run records "Interrupted
during <stage>", keeps status `running`, writes `run.json` and `report.md`, and releases the
lock; then the CLI exits with code 130. Cancellation lands at the next `await` (an agent call
or a gate). Git calls are synchronous, so they're never cut off halfway, and resume resets the
worktree anyway. `run` and `resume` restore the default SIGINT handler if the process was
started with SIGINT ignored (as a shell's `&` does), so cancel always works.

**`advpipe clean`.** `clean_run` does all its checks before deleting anything. It refuses
`complete` runs (the branch is the deliverable) and active runs (cancel first; `--force`
doesn't override this). It refuses a branch with commits that no other local branch contains
(`unmerged_commits` in `workspace.py`: `git rev-list --count B --not --exclude=B --branches`)
unless `--force`. As defence in depth, it only removes a worktree under `.advpipe/worktrees/`
and only deletes branches starting with `advpipe/`, whatever `run.json` says. Then it removes
the worktree, prunes, deletes the branch, and either deletes the run directory (`--logs`) or
adds a "Cleaned up" note to `run.json` and `report.md`.

## Work-item files

`advpipe run --item FILE` reads the task from a file (`workitem.load_work_item`). An optional
front-matter block (a first line of `---`, `key: value` lines, a closing `---`) sets `name`,
`config`, and the run settings `level`, `security_scan` (yes/no), `scan_failures` and
`bypass_questions` (yes/no) (`RUN_KEYS`), whose values are checked as the file is read (an error names the file and line). It's parsed by hand, so there's no YAML dependency, and it's strict: unknown keys,
repeated keys, empty values, malformed lines, a missing closing `---` and an empty body are
all errors, reported with file and line. Command-line options override front matter, and a
front-matter `config` is relative to the repo root. The body (without front matter) becomes
`RunState.work_item`; the file's absolute path is stored in `RunState.work_item_file`.
`format_work_item` writes the same layout back (front matter only for fields that are set, in
`KEYS` order, then the body and one newline); `parse_work_item` reads it back as an equal
`WorkItem`. A body whose first line is `---` gets an empty front-matter block, so it isn't
mistaken for one. `salvage_work_item` pulls what it can out of a file that doesn't parse, so
the UI's editor can open it to fix it. The editor has no fields for the run settings yet, so
`ui.items.save_item` keeps the file's values for them.

## Parallel runs

`run_many(items, make, parallel)` in `orchestrator.py` runs several orchestrators under an
`asyncio.Semaphore(parallel)` and returns their states in input order. Each item gets its own
run ID, worktree, branch, budget and run directory, so runs share nothing but the git object
database. Git calls in `workspace.py` are synchronous and run on the event-loop thread, so two
runs never issue git commands at the same moment in one process. Agent calls and gate commands
are where runs actually overlap.

## Budget

`budget.py`: `Budget.add(cost, stage)` is called by `call_agent` after **every** agent call. It
adds the cost to the total and to the stage's total, counts the call, and raises
`BudgetExceeded` as soon as the total passes the limit. `Orchestrator.run` catches that and ends
the run with `budget_exceeded`. Each agent also gets `max_budget_usd` set to whatever budget is
left, so a single runaway agent is stopped by the SDK too. The per-stage figures feed the cost
table in `report.md` (`_cost_table` in `runlog.py`).

## The web UI

`advpipe ui` serves a local web UI (plan and milestones: [UI_PLAN.md](UI_PLAN.md), all built:
the runs list, the run detail page with its live log and spec & context tab, the work-items
pages, the actions that start, cancel, resume and clean up runs, and the Help page). It lives
in `src/advpipe/ui/` and needs the `ui` extra (FastAPI, uvicorn, Jinja2). `cli.ui` imports it
lazily, so the core CLI works without it. The README's *Using the web UI* has screenshots,
made by `scripts/ui_screenshots.py` (a demo repo built with `tests/fakes.py`, served, and shot
with headless Chromium).

- **Read-only over the files.** The UI reads `.advpipe/runs/*/run.json` and `config.json`; it
  never holds state of its own, and never writes run files itself: actions go through the CLI
  (see *Actions* below). `ui/runs.py` (`load_runs`) turns run directories into rows,
  newest first. A run whose status is `running` but whose `run.lock` holder is dead is shown as
  *stopped*, and a `needs_human` run waiting on answers to blocking spec questions
  (`answers.awaiting_answers`, or answers saved but not resumed yet) as *needs answers*. A
  `needs_human` run at a decision point nobody has chosen at (`decisions.awaiting_decision`) is
  *needs a decision*; one whose continuing choice is made but not acted on yet is *stopped*
  (resuming applies it); `ended_by` makes it *handed over* (takeover) or *ended* (stop). All
  are display-only (`display_status`); `run.json` keeps the real status. Each row also carries
  the run's level (`RunRow.level_label`). An unreadable
  `run.json` is listed as such instead of breaking the page.
- **Pages** are Jinja2 templates (`ui/templates/`, autoescaped, since run data includes agent
  output) plus htmx and its SSE extension, vendored in `ui/static/` (`VENDORED.txt` records
  versions, hashes and licenses). The runs list is a fragment (`/runs/list`) that re-fetches
  itself every 3 seconds with `hx-trigger="every 3s"`. Each row links to its run detail page.
- **Run detail** (`/runs/<id>`, `ui/detail.py` + `routes/run_detail.py`). `load_detail` reads
  `run.json`, `config.json`, `report.md` and the per-stage files, and builds:
  - the **timeline**: the steps of the run's level (`levels.steps_for(state.level)`; `TIMELINE`
    is the Full one), each with a state. A mainline stage is *done* once its commit
    (`COMMIT_KEYS`: `spec`, `solo`, `tests`, `code`, `review`) is in `RunState.commits`. The
    stage the run is in is *in progress* (live) or *stopped here*. During ARBITER, that's both
    the arbiter and the stage it was called from (the first one of the level's steps without a
    commit). Later stages are *not yet* (resumable runs, and runs waiting for an answer or a
    decision) or *not reached*. The arbiter is *not needed* if it never ran, and isn't a step
    at the levels without one: there, each final author pass and the rulings code made sit in
    the step they were for.
  - the **round panels**: for each `stage-*/round-N/` (in numeric order), the author's reply
    (`author.txt`), the checks (`gates.json`), the diff the critic judged (`diff.patch`, cut at
    `MAX_DIFF_LINES`), the critic's verdict as finding cards (`critic.json`, parsed with
    `stages.parse_model`, shown raw if that fails), and any test-fix reply. The same for
    `review/` (both reviewers, scanner, or the single review's `single.json`), the arbiter's
    rulings and `final-pass-*/`, and `final-gates.json`. The solo levels' `stage-solo/pass-N/`
    are panels titled *Pass N*, *Revision N: with your answers to ...* (an `answers.json`) or
    *Fix pass N (you asked)* (a `request.json`); `fix-pass-N/` panels go in the step of the Nth
    `fix` decision; each decision you made is a panel in the step where the run stopped. A
    round without a `critic.json` at a level with no critics says "No critic at this level"
    rather than "missing". A missing or unreadable file shows as such; it never fails the page.
  - the **decision panel** (`RunDetail.decision_view`) for a run *needing a decision*: the
    explanation, the failing checks or scan hits as finding cards, and one control per choice
    (`CHOICE_VIEWS`).
  - **next steps**: commands to paste (`git log`/`merge`/`branch -d` for complete runs;
    `advpipe clean`, `resume` or `cancel` otherwise), with every value `shlex.quote`d. The page
    shows them with copy buttons (`static/app.js`); it never runs them.
  - the **report**: `report.md` rendered by `ui/render.py`: markdown-it with raw HTML escaped,
    unsafe link schemes dropped, and table alignment turned into classes (the CSP blocks
    inline styles).
- **Spec & context tab** (`/runs/<id>/context`, `ui/context.py`). `load_context` gathers what
  the run's agents worked from: the work item (`RunState.work_item` and `work_item_file`),
  `task.md`, `config.json` (shown raw if it doesn't validate) with its check commands, the
  standards doc, and the eight role prompts (`runner.load_prompt`, as installed now) with each
  role's model, tools and prompt inputs. `SEEN_BY` and `ROLE_INPUTS` hold the "who sees this"
  text; they describe what `stages.py` puts in each prompt, so change them together.
  `task.md` goes through `render.task_markdown`, a second markdown-it instance with a core
  rule that adds classes to the *Acceptance criteria* and *Open questions* headings and their
  top-level items (`blocking` for the `BLOCKING:` forms `stages.blocking_open_questions`
  accepts) and counts them. The standards doc is read with `git cat-file blob <rev>:<path>`
  from the run's branch, then its base commit, then the repo's working tree. The revision
  must match a ref-name pattern with no leading `-`, and the path must be relative with no
  `..`, so values from `run.json`/`config.json` can't become git options or leave the repo.
- **Work items** (`/items`, `ui/items.py` + `routes/items.py`). The only part of the UI that
  writes files, and it only writes inside the work-items folder (`--items-dir`). It never
  commits: the list shows each file's git state instead.
  - **Paths.** Every path comes from the browser, so all of them go through `item_path`: a
    relative path of plain names (letters, digits, `-`, `_`, `.`, `+`, spaces; no part
    starting with `.`, so no `..` or hidden files), at most `MAX_DEPTH` folders deep, ending
    in `.md`, and still inside the folder after `resolve()`, so a symlink can't lead out.
    Anything else is 400 on the pages and a form error on create and rename.
  - **The list** (`load_items`) walks the folder (not following symlinked folders, skipping
    hidden ones), groups files by folder, and adds each one's newest run (`last_runs`: runs
    whose `RunState.work_item_file` is the file's resolved path) and git state (one
    `git status --porcelain -z --untracked-files=all --ignored=matching -- <folder>`).
    Files whose path `item_path` refuses are listed by name only, as not manageable here.
  - **The editor** has the front-matter fields (branch name, config, and the run settings
    `level`, `security_scan`, `scan_failures`, `bypass_questions`: each a drop-down whose first
    option, `inherit`, leaves the key out, so the config decides) and the body, with a
    preview pane and inline checks (`check_draft`) that htmx re-fetches from
    `POST /items/preview` as you type. A form that doesn't carry a setting at all (`None` in
    the `Draft`) keeps the file's. Errors (an empty body, a multi-line field, a setting
    `advpipe run` wouldn't accept, checked with its own parsers) block a save;
    warnings don't: a config file that's missing, invalid or outside the repo, and a branch
    `advpipe/<slugify(name or first line)>` that already exists (the run would add `-2`).
    The branch name is the same one `Workspace.create` would make.
  - **Saving** (`save_item`) refuses if the file changed on disk since the editor loaded it
    (the form carries a SHA-256 of the file as loaded). If the fields parse equal to what's on
    disk, the file isn't touched, so front matter round-trips byte for byte; otherwise it's
    written with `format_work_item`. Textarea CRLFs become LF. Creating never overwrites
    (`open("xb")`), and rename refuses an existing target.
  - **Deleting asks first.** The list and editor link to a confirmation page (it says whether
    git still has a copy). Only its form sends `confirm=yes`, and `POST /items/delete`
    refuses without it.
  - **Forms** post with htmx, so they carry the CSRF header. A form with problems comes back
    re-rendered with status 422, which `base.html`'s `responseHandling` lets htmx swap in; other
    errors aren't swapped. Success replies with `HX-Redirect` to the next page.
- **Actions** (`ui/actions.py` + `routes/actions.py`): start, cancel, resume, clean up. Each
  runs the advpipe CLI (`call_advpipe`: `subprocess.run` with an argument list, never a shell,
  `control.child_command()` as the program, a timeout, stdin from `/dev/null`), so the UI does
  exactly what the terminal command does, with its checks. Runs are started and resumed with
  `--detach`, so they're ordinary background processes that outlive the UI.
  - **Which actions, when.** `VALID` maps each displayed status to its actions: *running*:
    cancel; *stopped*, *error*, *over budget*: resume and clean; *needs you*: clean;
    *needs answers*: answer and clean; *needs a decision*: decide and clean; *handed over*,
    *ended*: clean; *complete*: none. Buttons are drawn from it (`RunDetail.actions`), and every action route
    checks it again before running anything: an action that's no longer valid gets 409 with a
    "reload" message; one the CLI refuses gets 422 with the CLI's message (escaped). `base.html`
    lets htmx swap both into the page's `#action-result`.
  - **Answer** (`POST /runs/<id>/answer`). For *needs answers*, the run page's header shows a
    form with a textarea per question (`RunDetail.questions`: those no spec pass has used an
    answer for, prefilled with answers already saved). The route reads only `answer_Q<n>`
    fields, checks them with `answers.check_answers` (so a refused form never starts a
    process; 422 with the reason, and the typed answers stay in the page), then runs
    `advpipe answer <id> --repo <repo> --detach --answer=Q1=<text> ...`
    (`actions.answer_args`; the `--answer=` form keeps an answer that starts with `-` from
    looking like an option). On success it redirects to the run page with an "answered"
    notice; the run goes live there. The SPEC step of the timeline has a panel per spec pass
    (`_spec_rounds`: the first spec, then "Revision N: with your answers to ..."), each with
    the blocking questions it asked and their answers; the Spec & context tab highlights the
    `## Answers` section of `task.md`.
    **Bypass** is in the same form: a *Let the agent decide* checkbox per question
    (`bypass_Q<n>`) and a *Bypass all and continue* button (`bypass_all`). A ticked box beats
    the text in its answer box, and the button beats everything, so what's sent is always what
    the page says; `check_answers(state, answers, bypass)` validates the lot before any process
    (`--bypass=Q2` per bypassed question is added to `answer_args`). At the spec-pass limit
    there's no form: the run is *needs a decision* and the *Begin work?* panel is shown.
  - **Decide** (`POST /runs/<id>/decide`). For *needs a decision* (`VALID`), the run page's
    header shows the decision panel; each choice button posts `choice` (and, for `fix`, the note
    box's `note`). The route checks the choice against `decisions.CHOICES[kind]` for the run's
    open decision, that a note goes with `fix` only and fits `MAX_NOTE_CHARS`, and that `fix`
    is within `max_fix_passes`, all before any process (422 with the reason), then runs
    `advpipe decide <id> CHOICE --repo R [--detach] [--note=TEXT]`: `--detach` for choices that
    carry on, none for `takeover` and `stop`, which end the run in the command itself. Those two
    go through a confirmation page first (`GET /runs/<id>/decide/<choice>`, which says what
    happens), and the route refuses them without the page's `confirm=yes`. The redirect carries
    a notice key (`decided`, `handed_over`, `ended`).
  - **New run** (`/runs/new`). `plan_run` works out what `advpipe run` would do from the
    form (a work item via `item_path` and `load_work_item`, or a typed task; optional branch
    name and config overrides): the effective config (form, then front matter, then
    `pipeline.toml`, then defaults; it must be a file inside the repo and must load), its
    budget, the branch (`advpipe/<slugify(...)>`), problems, and the argument list. Only
    overrides are passed (`--name`, `--config`), so front matter applies as in the terminal;
    a typed task goes after `--`. **How much checking:** the slider (`level`, 1 to 5; a
    `range` input with its labels as visible text and in a `datalist`, `aria-valuetext` set by
    `app.js`), the security scan (`scan`: default, on, off), the scan policy (`scan_failures`)
    and Bypass up front (`bypass`; a hidden `bypass_seen` says the box was in the form).
    `actions._how_much_checking` works out what the run gets the way the CLI does (form over
    front matter over `[pipeline]` over the level's default), so the summary can describe it
    (`levels.py`: label, agents, `max_agent_calls`, scan, `dropped_safeguards`). `--level` is
    always passed; `--security-scan`/`--no-security-scan` only for On/Off, `--scan-failures`
    only when it differs from what the item or config gives, `--bypass-questions`/
    `--no-bypass-questions` likewise. Until the slider is touched, `app.js` moves it to the
    level of the chosen work item (the summary carries it as `data-inherited-level`). The
    summary (`POST /runs/new/check`) is re-fetched as the form changes. It puts the budget it
    shows in a hidden field, and the level, scan, policy and Bypass in another (`shown`), and
    `POST /runs/start` refuses if either isn't what it works out now, so a run never starts on
    a budget or settings nobody saw. Success redirects to the run page with the run id `run --detach` printed
    (checked with `check_run_id`).
  - **Starting up.** Right after `--detach`, the run directory has `run.lock` and
    `console.log` but no `run.json` yet. `/runs/<id>` then shows a starting-up panel
    (`load_starting`) that polls `/runs/<id>/starting` every second, which answers with
    `HX-Refresh` once `run.json` exists. If the process dies first, it shows the end of
    `console.log` instead.
  - **Cancel** asks with `hx-confirm` and runs `advpipe cancel`. **Resume** takes an optional
    new total budget (`parse_budget`: a finite number above what's been spent; required for
    an over-budget run) and runs `advpipe resume --detach [--budget N]`. **Clean up** has a
    confirmation page (`clean_info`: the worktree, the branch and how many commits only it
    has), and only its form sends `confirm=yes`; the options become `--force` and `--logs`.
  - **Work items** link to the dialog (*Run*, and the editor's *Save & run*, which saves and
    then opens it); nothing starts a run without the dialog.
  - After an action, the redirect carries a `notice` key; the page maps it to a fixed text, so
    nothing from the URL is shown.
- **Live updates** (`ui/live.py`). `/runs/<id>/events` is a Server-Sent Events stream of
  `events.jsonl` from a byte `offset` (the page passes the size it rendered, so nothing is
  shown twice). It polls the file, sends only complete lines, and sends three event types:
  `log` (an escaped `<li>`; its SSE `id` is the byte offset after the line, so a reconnecting
  browser's `Last-Event-ID` resumes exactly), `refresh` (after each batch), and `end` once no
  live process holds `run.lock` (after one more read, so no last line is missed). The page
  (htmx SSE extension) appends `log` lines, re-fetches `/runs/<id>/body` on `refresh` (the
  header in place, the timeline and report out of band; `delay:300ms`, not `throttle`, which
  would drop the final refresh), and closes on `end`. `app.js` keeps each `<details>` panel
  open or closed across refreshes. Finished runs get a static page with no stream.
- **Help** (`/help`, `routes/help.py`): the pipeline in plain words. Its tables are built from
  the code the rest uses (`runs.STATUSES`, `detail.STEP_STATES`, `runner.ROLE_TOOLS` and
  `ROLE_MODEL`, `context.ROLE_INPUTS` and `CHECK_HELP`, `Config()` defaults, and `levels.py`
  for the Levels section: steps, agents, call counts, what each level drops, and the
  design-rules table, whose marks come from `SHAPES`), so the round caps, budget, models,
  levels and who-sees-what it quotes can't drift. Its own text (`STATUS_NEXT`, `ROLE_JOBS`)
  must cover every status and role; a test checks that.
- **Errors.** Routes render their own error pages (`error.html`). Errors FastAPI raises itself
  (no such route: 404, wrong method: 405) go through an exception handler in `create_ui_app`
  to the same template, with the nav, never echoing the URL.
- **Look and feel** (`static/app.css`, one file). Every colour is a custom property set with
  `light-dark(light, dark)`, so there's one list of colours and both themes come from it.
  `color-scheme` picks the side: the system's setting, unless `<html data-theme="light|dark">`
  overrides it. The *Theme* button in the top bar (`app.js`) cycles system, dark, light and
  keeps the choice in `localStorage`; `static/theme.js`, loaded in `<head>` *without* `defer`,
  applies it before the first paint so pages don't flash the other theme. (Both are files, as
  the CSP allows no inline script.) The button is `hidden` in the HTML and shown by `app.js`,
  so it never appears where it couldn't work. Keyboard: a *Skip to content* link is the first
  Tab stop, `<main id="content" tabindex="-1">` takes the focus, and `:focus-visible` gives
  every focusable element a ring (nothing removes outlines). One `@media (max-width: 900px)`
  block fits the pages to tablet width (768 px): less padding, a wrapping top bar, and the
  work-items table's actions wrapping instead of overflowing. Wide content (`pre`, commands,
  the Help tables in `.table-wrap`) scrolls inside its box.
- **`create_ui_app(repo, items_dir, token)`** builds the FastAPI app: routes from
  `ui/routes/`, static files, and `GuardMiddleware` (`ui/security.py`), a pure ASGI middleware
  (so it won't buffer the streaming responses later milestones add) that, for every request:
  1. checks the `Host` header is a loopback name (stops DNS-rebinding pages)
  2. on `GET ?token=T` with the right token, sets an HttpOnly, SameSite=Strict cookie and
     redirects to the same local path without the token. Otherwise the cookie is required: 401.
     The cookie name is derived from the token, so two UIs on different ports don't clash.
  3. for anything but GET/HEAD/OPTIONS, requires the per-server CSRF token in an
     `X-CSRF-Token` header: 403. `base.html` sets `hx-headers` so htmx always sends it.
  4. adds a strict Content-Security-Policy (`'self'` only, no inline script or style) and other
     security headers. htmx is configured in a `<meta name="htmx-config">` not to inject styles
     or evaluate code, so the policy holds.
- **`advpipe ui`** binds `127.0.0.1` by default, refuses any non-loopback `--host` without
  `--i-know-this-is-exposed` (and then turns the Host check off and warns), makes a new token
  with `secrets.token_urlsafe`, prints the link, and runs uvicorn.

## The Claude Code version (no Python)

The same pipeline also exists as plain Claude Code configuration, for use inside an interactive
session:

- `.claude/agents/<role>.md`: one subagent per role. The body is the same text as
  `src/advpipe/prompts/<role>.md`, and `test_subagents_in_sync_with_prompts` fails if they
  drift apart. Critics are on `model: sonnet` with read-only tools.
- `.claude/skills/adversarial-build/SKILL.md`: the procedure, written for Claude to follow as
  orchestrator.

The difference matters: in the skill version **a model** follows the procedure, so rule 1 holds
only as far as the model follows instructions. The Python version enforces every rule in code.
Use the skill for quick interactive work, and the CLI when you want guarantees.

## Testing strategy

`tests/` runs the whole orchestrator **without any API calls**:

- `tests/fakes.py`: `FakeAgentRunner` takes a script for each role, a list of steps used in
  order. A step is either a literal reply or a function that edits the worktree, the way a real
  author would (`writes({...})`), and returns a reply. It records every request, so tests can
  assert on what each agent saw (e.g. that no critic prompt contains author reasoning). It
  yields to the event loop on every call so parallel runs really interleave.
- `tests/fixtures/sample_repo/`: a tiny real Python package. Tests copy it into a temporary
  directory, `git init` it, and run real pytest gates against it.
- `test_orchestrator.py`: end-to-end scenarios (pass first time, fail then pass, cap → arbiter
  dismiss / fix / still failing, disputes, malformed JSON, coder editing tests, failed gate, budget
  abort, tests passing too early, blocking questions, review findings, worktree cleanup, cost
  table).
- `test_answers.py`: blocking questions and answers: questions recorded, `record_answers`
  and its refusals, resume after answers (the revision prompt, the Answers section in the
  committed `task.md`, what later agents see), new questions in a revision, the spec-pass cap,
  re-asked questions, interrupted and over-budget revisions, old-format runs, answers files.
- `test_levels.py`: levels as data (shapes, roles, steps, dropped safeguards, parsing, agent
  calls, including the worst case against a run that fails as late as it can at `full` and
  `adversarial`); `[pipeline]` config and front matter; precedence from the command line down
  to the level default; the scan toggle at `full` and below; old runs; resume keeping the level
  and refusing a config that changes it; an `adversarial` run end to end; the summary line,
  `status` and `advpipe levels`.
- `test_bypass.py`: Bypass: recording (all, mixed, both-answered-and-bypassed refused), the
  bypass call and revision on resume (reverted files, retry on bad JSON, the stand-in answer,
  an interrupted revision keeping the agent's answers), Bypass covering one stop, the marked
  Answers section and report, Bypass up front (at and below the cap, precedence of command line
  over front matter over config, a resumed run keeping it), and `advpipe answer --bypass` in
  all its forms.
- `test_answers_cli.py`: `advpipe answer` with `--answer`, `--file`/`--template`, prompting,
  `--no-resume`, `--detach` (through the fake child), refusals and exit codes; what `resume`,
  `status` and the run summary say while a run waits on answers.
- `test_resume.py`, `test_parallel.py`, `test_cli.py`, plus unit tests for models, gates,
  stages, workspace and config/prompt sync.
- `test_events.py` (event log matches the printed progress, ordering, flushing, resume,
  `diff.patch`), `test_workitem.py` (front matter, `--item`), `test_control.py` (detach,
  cancel, clean, run-id traversal). Detach and cancel tests start a real background process:
  `tests/advpipe_fake_child.py` is the real CLI with a `FakeAgentRunner` that can be held at a
  chosen role until a file appears. Tests point `control.child_command` at it.
- `test_ui.py`: the web UI through FastAPI's `TestClient`, with run directories from the real
  orchestrator and fake agents (plus hand-written `run.json` for live statuses): no token → 401,
  missing CSRF → 403, unknown Host → 400, escaped agent text, filters, the polling fragment,
  and the `advpipe ui` command with `uvicorn.run` replaced. Skipped if the `ui` extra isn't
  installed.
- `test_ui_answers.py`: answering from the web UI: *needs answers* in the list and on the page,
  the form (one field per question, prefilled when answers are saved), the exact
  `advpipe answer` argument list, refusals before any process (missing, empty, unknown,
  too long), token/CSRF, escaping of questions and answers on every page, the SPEC passes in
  the timeline, and an end-to-end answer through the fake child.
- `test_ui_detail.py`: the run detail page for runs from the real orchestrator (complete,
  blocked at the spec, stopped in the arbiter's final pass) and for every status; escaping of
  agent text in every file the page reads; quoting in next-step commands; 404s for unknown and
  traversal ids; and the event stream: appended and half-written lines arrive in order,
  keepalives, `end` when the lock is released, `Last-Event-ID`, and offsets mid-line.
- `test_ui_items.py`: work items on real files: a create, edit, rename and delete round trip;
  front matter that round-trips byte for byte; refusing to overwrite changes made elsewhere;
  broken files opening in the editor; the inline checks; the list's grouping, last runs and
  git states; deleting only after confirming; path traversal (`../`, absolute, `%2e%2e`,
  backslashes, symlinks out) on every route; and token and CSRF on every route.
- `test_ui_levels.py`: the New run dialog's slider, scan, policy and Bypass choices (the
  summary for every level and scan choice, the slider starting at the inherited level, the
  exact `advpipe run` argument list for each choice, refusing settings nobody saw and bad
  levels before any process); the editor's run settings round trip; the run page's timeline,
  solo and single-review panels, the runs list's level chip, the context tab's roles and the
  Help page's Levels at every level; no inline script or style on the new pages.
- `test_ui_decisions.py`: the decision panel for each kind (failing checks, scan hits, *Begin
  work?*), `POST /runs/<id>/decide` with the exact argument list for each choice and every
  refusal before any process, the confirmation pages for taking over and stopping, a Basic run
  fixed from the page, handed-over and ended runs, the answer form's Bypass boxes and button
  (and the cap replacing the form), and agent answers with their badge and reason, escaped.
- `test_ui_polish.py`: the Help page (every step, status, role and check, the real defaults,
  token required, GET only); HTML 404/405 pages that don't echo the URL; empty states that say
  what to do next; and on every page the skip link, the theme button and script, and no inline
  script or style. It also checks the stylesheet: every colour has a light and a dark value,
  focus rings are never removed, and the tablet rules exist. The theme button and keyboard path
  were checked in a real browser (headless Chromium) when they were built; there are no
  browser tests in the suite.

## Extending it

- **Change agent behaviour:** edit `src/advpipe/prompts/<role>.md`, then copy the body into
  `.claude/agents/<role>.md`. The sync test makes sure you don't forget.
- **Add a gate:** add a field to `Gates` in `config.py` and include its name where it should run
  (`ALL_GATES` in `stages.py` for the code stage and final gates; `run_gate_names` builds each
  run's list from it).
- **Use a different agent backend:** implement `AgentRunner.run` and pass your runner to
  `Orchestrator`. Nothing else depends on the SDK.
- **Change the pipeline itself:** `Orchestrator._run` is short and linear. Add or reorder
  stages there, and record a `state.commits[...]` entry for each new stage so resume keeps
  working.
