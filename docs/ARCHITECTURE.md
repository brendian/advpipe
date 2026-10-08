# How advpipe works

This document explains the internals: what happens during a run, which file and function does
each part, and why it's built that way. For installing and using the tool, see the
[README](../README.md).

- [The big picture](#the-big-picture)
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

## Module map

All code is in `src/advpipe/`.

| File | Responsibility | Key names |
|---|---|---|
| `cli.py` | Typer CLI: `run`, `resume`, `cancel`, `clean`, `status`, `report`, `gates` | `run`, `resume`, `cancel`, `clean`, `status`, `report`, `gates` |
| `orchestrator.py` | The state machine; resume; parallel runs | `Orchestrator`, `Orchestrator._run`, `Orchestrator._settle`, `Orchestrator.resume`, `run_many` |
| `stages.py` | What each stage does; prompt building; JSON parsing and retry | `adversarial_stage`, `review_stage`, `arbiter_stage`, `final_author_pass`, `call_critic`, `test_file_guard`, `Context` |
| `models.py` | Pydantic data contracts and validation rules | `Finding`, `Verdict`, `AuthorResponse`, `ArbiterRuling`, `RunState`, `Stage`, `Status` |
| `runner.py` | The agent abstraction and the Claude Agent SDK implementation; role → tools/model table | `AgentRunner`, `SdkAgentRunner`, `Role`, `ROLE_TOOLS`, `ROLE_MODEL`, `build_request` |
| `gates.py` | Runs check commands; turns failures into findings | `run_gates`, `gate_findings`, `red_gate_findings`, `GateResult` |
| `workspace.py` | Git worktree per run; diffs; reverting files | `Workspace.create`, `.reopen`, `.remove`, `.diff`, `.changed_files`, `.restore` |
| `budget.py` | Cost tracking with a hard stop | `Budget`, `BudgetExceeded` |
| `runlog.py` | The on-disk run directory, lock file, event log and `report.md`; run-id validation | `RunLog`, `check_run_id`, `render_report`, `list_runs` |
| `events.py` | One `emit()` for progress: appends to `events.jsonl` and prints | `emit`, `EventKind`, `gate_statuses` |
| `control.py` | Acting on runs from outside: detached start, cancel, clean | `start_detached`, `cancel_run`, `clean_run`, `ControlError` |
| `workitem.py` | Work-item files with optional front matter | `parse_work_item`, `load_work_item`, `WorkItem` |
| `config.py` | `pipeline.toml` loading and validation | `Config`, `load_config` |
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
ran out of money.

`_run` then does the following:

### 0. Set up the workspace (`Orchestrator._open_workspace`)
A new run calls `Workspace.create`. That makes a git worktree at
`<repo>/.advpipe/worktrees/<run-id>` on a new, human-readable branch (see
[isolation](#isolation-worktrees-and-branches)), starting from the
repo's current `HEAD`. It also saves the effective config to `config.json` in the run directory.
A resumed run calls `Workspace.reopen` instead (see [resume](#persistence-resume-and-locking)).

### 1. Spec (`Orchestrator._spec`)
- Calls the **spec-writer** with the work item (`spec_prompt`).
- **Guard:** the spec writer may only create `task.md`. Any other file it touched is reverted
  with `Workspace.restore` and noted in the report.
- If `task.md` is missing, the run stops with `needs_human`.
- `blocking_open_questions` scans the `## Open questions` section for items starting with
  `BLOCKING:`. If there are any, the run stops with `needs_human` **before any code is
  written**. The idea is that a human answers a question once instead of agents guessing and
  arguing.
- Commits `task.md`. That commit becomes `state.commits["spec"]`.

### 2. Tests (`adversarial_stage(ctx, "tests")`)
The test-author / test-critic loop, capped at `max_rounds_tests` (default 2). Its gate is
*inverted*: the new tests must exist and must **fail**. See
[the loop](#the-adversarial-loop-in-detail). Then `_settle` handles anything still open, and
the result is committed (`state.commits["tests"]`).

### 3. Code (`adversarial_stage(ctx, "code")`)
The coder / code-critic loop, capped at `max_rounds_code` (default 3). Its gate requires tests,
types and lint to be green. The test-file guard runs after every coder pass. Then `_settle`, and
a commit (`state.commits["code"]`).

### 4. Review (`review_stage`)
- Runs the security scanner gate (semgrep by default). It's optional: if the tool isn't
  installed, the gate is skipped and the report says so.
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
3. If gates still fail, the run stops with `needs_human` and those failures as open findings.

### 6. Final gates
All gates run one last time on the finished branch. Green means `complete`, and the worktree
is removed (the branch stays). Red means `needs_human`.

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
config.json     the config this run started with
task.md         copy of the spec
spec/author.txt
stage-tests/round-N/{author.txt, diff.patch, critic.json, gates.json}
stage-code/round-N/{author.txt, diff.patch, critic.json, gates.json}
review/{diff.patch, standards.json, security.json, scanner.json}
arbiter.json, arbiter-<stage>.json, final-pass-<kind>/...   (when the arbiter ran)
final-gates.json
report.md       human summary: status, findings, rulings, cost table, notes
run.lock        present only while a process is driving the run
```

**Resume** works at stage boundaries. When a stage finishes, its commit is stored in
`state.commits` (`spec`, `tests`, `code`, `review`). `Orchestrator.resume`:

1. Loads `run.json`. Refuses (`NotResumable`) if the run is `complete` or `needs_human`, or if
   another live process holds the lock. Resumable statuses are `running` (interrupted),
   `error` and `budget_exceeded` (`RESUMABLE` in `models.py`).
2. Uses the run's saved `config.json` unless you pass one. `--budget` overrides the limit.
3. Restores the budget's spent amount and per-stage totals from `run.json`.

`_run` then calls `Workspace.reopen`:
- if the worktree exists: `git reset --hard` and `git clean -fd`, which throws away the
  interrupted stage's half-done work
- if it was deleted: recreate it from the branch

`_run` then skips every stage already in `state.commits`. The interrupted stage starts over at
round 1.

`diff.patch` is exactly the diff placed in that critic's (or both reviewers') prompt, so you
can see what was judged. It's written before the critic is called.

**Locking.** `RunLog.acquire_lock` writes the current process ID to `run.lock`.
`RunLog.lock_holder` checks whether that process is still alive (`os.kill(pid, 0)`), so a lock
left behind by a crash doesn't block anything.

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
| `run_start` | new run (branch, worktree) or resume | `branch` or `worktree` (new runs) |
| `stage` | `== STAGE`, and a stage passing or going to the arbiter | `passed` (for the latter) |
| `round` | start of an author/critic round | `cap` |
| `agent_start` / `agent_done` | around every agent call | `role`; `agent_done` adds `cost_usd`, `total_usd`, `is_error` |
| `gates` | check results, scanner result, each blocking gate failure | `gates`: `{name: "pass" \| "fail" \| "skipped"}` (not on the failure lines) |
| `verdict` | a critic or reviewer verdict | `role`, `verdict`, `blocking`, `minor` |
| `guard` | the test-file guard reverted something | |
| `test_fix` | the test-defect fix pass | `files` (when tests were committed) |
| `ruling` | each arbiter ruling | `finding_id`, `decision` |
| `status` | budget exceeded, interrupted, and the final event of every process | `status`; the final one adds `final: true` and `total_usd` |
| `error` | an unexpected exception ended the run | |

The final `status` event is written with `echo=False`: it isn't printed, because the CLI prints
its own summary. Resumed runs append to the same file, so a run's whole history is one file. A
reader (like the planned UI) can tail it, and knows the driving process has finished when it
sees `final: true`.

## Background runs, cancel and clean

These live in `control.py` so the CLI stays thin and the UI can share them.

**`advpipe run --detach`.** The parent validates everything it can first (sources, work-item
file, config), picks the run id (`new_run_id`), and calls `start_detached`. That starts
`python -m advpipe run ... --run-id <id>` with `subprocess.Popen` (an argument list, never a
shell) in a new session (`start_new_session=True`), with stdin from `/dev/null` and
stdout/stderr appended to `console.log`. It then writes the child's pid to `run.lock`, so
`cancel` works before the child has even started up. The parent prints the run id on stdout
and exits. A work item given as text is passed after `--`, so text starting with `-` isn't read
as an option. `--run-id` is a hidden option, used only for this.

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
front-matter block (a first line of `---`, `key: value` lines, a closing `---`) sets `name` and
`config`. It's parsed by hand, so there's no YAML dependency, and it's strict: unknown keys,
repeated keys, empty values, malformed lines, a missing closing `---` and an empty body are
all errors, reported with file and line. Command-line options override front matter, and a
front-matter `config` is relative to the repo root. The body (without front matter) becomes
`RunState.work_item`; the file's absolute path is stored in `RunState.work_item_file`.

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
- `test_resume.py`, `test_parallel.py`, `test_cli.py`, plus unit tests for models, gates,
  stages, workspace and config/prompt sync.
- `test_events.py` (event log matches the printed progress, ordering, flushing, resume,
  `diff.patch`), `test_workitem.py` (front matter, `--item`), `test_control.py` (detach,
  cancel, clean, run-id traversal). Detach and cancel tests start a real background process:
  `tests/advpipe_fake_child.py` is the real CLI with a `FakeAgentRunner` that can be held at a
  chosen role until a file appears. Tests point `control.child_command` at it.

## Extending it

- **Change agent behaviour:** edit `src/advpipe/prompts/<role>.md`, then copy the body into
  `.claude/agents/<role>.md`. The sync test makes sure you don't forget.
- **Add a gate:** add a field to `Gates` in `config.py` and include its name where it should run
  (`ALL_GATES` in `stages.py` for the code stage and final gates).
- **Use a different agent backend:** implement `AgentRunner.run` and pass your runner to
  `Orchestrator`. Nothing else depends on the SDK.
- **Change the pipeline itself:** `Orchestrator._run` is short and linear. Add or reorder
  stages there, and record a `state.commits[...]` entry for each new stage so resume keeps
  working.
