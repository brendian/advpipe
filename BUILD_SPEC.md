# Build Spec: Adversarial Multi-Agent Coding Pipeline

> **For Claude Code.** This file is the whole brief. Read it end to end before writing anything.
> Build in the phases below, in order. At the end of each phase, stop, summarize what you built and
> how you verified it, and wait for Brendan to say "continue" before starting the next phase.

---

## 1. What we're building

A tool that takes one work item (a feature, a bug fix) and drives it through a pipeline of AI agents
until it is done, then leaves a branch ready for human review.

The pipeline uses **adversarial author/critic pairs**: one agent produces work, a second agent tries
to find concrete reasons it is wrong. Agreement is not a conversation; it's a **structured verdict**
(`PASS`, or a list of findings). Deterministic checks (tests, types, lint, security scan) gate every
stage, every loop has a **round cap**, and an **arbiter** settles anything still disputed.

```
work item
   │
   ▼
[1] Spec writer ──► task.md  (acceptance criteria = the contract every agent is judged against)
   │
   ▼
[2] Test author ⇄ Test critic        max 2 rounds   gate: tests exist and FAIL for the right reason
   │
   ▼
[3] Coder ⇄ Code critic               max 3 rounds   gate: tests + types + lint green, no blocking findings
   │
   ▼
[4] Standards reviewer ∥ Security reviewer   (parallel, one pass each, different lenses)
   │
   ▼
[5] Arbiter (only if blocking findings remain or are disputed) → fix or dismiss, final
   │
   ▼
[6] Done: all gates green, no open blocking findings → branch ready for human review
    Otherwise: status "needs_human" with the open findings
```

The human (Brendan) is always the final reviewer. The pipeline's job is to make that review fast.

### Design rules (do not drop these)

1. **Orchestrator is code, not an agent.** The loop is a deterministic state machine in Python.
   Agents are called *by* it; they never decide whether to skip a stage.
2. **Tests before code.** The test author sees `task.md` only, never an implementation.
3. **One writer at a time.** Critics and reviewers are read-only. They return findings, never edits.
4. **Critics get artifacts, not reasoning.** A critic sees `task.md`, the diff, and gate output.
   It never sees the author's transcript.
5. **Every loop is bounded.** Round caps per stage, a max-turns limit per agent run, and a dollar
   budget per task that aborts the run when exceeded.
6. **Critics may return PASS.** The prompt must explicitly allow it, and must only allow findings that
   point at specific code (file + line) or a specific unmet acceptance criterion.
7. **Ground truth beats opinion.** If a gate fails, that is a blocking finding automatically. Agents
   never argue with the test runner.

---

## 2. Tech choices

| Item | Choice |
|---|---|
| Language | Python 3.11+ |
| Agent runtime | `claude-agent-sdk` (Claude Agent SDK for Python) |
| Package / env | `uv` (fall back to `pip` + `venv` if `uv` isn't installed) |
| CLI | `typer` |
| Config | `pipeline.toml` (read with `tomllib`) |
| Data models | `pydantic` v2 for verdicts, findings, run state |
| Own tests | `pytest`, `pytest-asyncio` |
| Lint / types | `ruff`, `mypy --strict` on the package |

**Models (configurable, these are defaults):**
- Authors (spec writer, test author, coder) and arbiter: `claude-opus-5-5`
- Critics and reviewers: `claude-sonnet-5-5` (a different model adds real independence and costs less)

**Before writing any SDK code:** check the installed `claude-agent-sdk` version and its docs
(https://code.claude.com/docs/en/agent-sdk) for the current names of `query`, `ClaudeAgentOptions`,
`ResultMessage`, and the options for system prompt, allowed tools, working directory, model,
permission mode, and max turns. Do not guess API names; the sketch in this spec is illustrative.

---

## 3. Repository layout

```
adversarial-pipeline/
├── BUILD_SPEC.md                 ← this file
├── CLAUDE.md                     ← short project instructions (create in Phase 0)
├── README.md                     ← usage docs (create in Phase 3)
├── pyproject.toml
├── pipeline.example.toml
├── .claude/
│   ├── agents/                   ← Phase 0: subagent definitions
│   │   ├── spec-writer.md
│   │   ├── test-author.md
│   │   ├── test-critic.md
│   │   ├── coder.md
│   │   ├── code-critic.md
│   │   ├── standards-reviewer.md
│   │   ├── security-reviewer.md
│   │   └── arbiter.md
│   └── skills/
│       └── adversarial-build/
│           └── SKILL.md          ← Phase 0: the procedure, for use inside Claude Code
├── src/advpipe/
│   ├── __init__.py
│   ├── cli.py                    ← `advpipe run`, `advpipe status`, `advpipe report`
│   ├── config.py                 ← load + validate pipeline.toml
│   ├── models.py                 ← Finding, Verdict, AuthorResponse, StageResult, RunState
│   ├── prompts/                  ← one .md system prompt per role (single source of truth)
│   ├── runner.py                 ← AgentRunner protocol + SDK implementation
│   ├── gates.py                  ← run configured check commands, turn failures into findings
│   ├── stages.py                 ← adversarial_stage(), review_stage(), arbiter_stage()
│   ├── orchestrator.py           ← the state machine
│   ├── workspace.py              ← git worktree + branch per run, diff helpers
│   ├── budget.py                 ← cost tracking + abort
│   └── runlog.py                 ← writes .advpipe/runs/<run-id>/...
└── tests/
    ├── fakes.py                  ← FakeAgentRunner with scripted responses
    ├── test_models.py
    ├── test_gates.py
    ├── test_stages.py
    ├── test_orchestrator.py
    └── fixtures/sample_repo/     ← a tiny Python project used as a target in tests
```

The role prompts in `src/advpipe/prompts/` and the subagent files in `.claude/agents/` should say
the same thing. Treat `prompts/` as the source of truth and keep the subagents in sync.

---

## 4. Data contracts

### 4.1 `task.md` (written by the spec writer into the target worktree)

```markdown
# Task: <short title>

## Goal
<one paragraph>

## Acceptance criteria
- [ ] AC1: <observable, testable statement>
- [ ] AC2: ...

## Constraints
- <e.g. no new dependencies; don't change public API X>

## In scope
- <files / modules expected to change>

## Out of scope
- <things not to touch>

## Open questions
- <anything ambiguous; the spec writer flags rather than guesses>
```

If the spec writer lists open questions that block the work, the orchestrator stops with status
`needs_human` before any code is written.

### 4.2 Finding and Verdict (critics, reviewers)

```json
{
  "verdict": "PASS | FAIL",
  "findings": [
    {
      "id": "F1",
      "severity": "blocking | minor",
      "category": "correctness | coverage | standards | security | scope",
      "file": "billing/refund.py",
      "line": 42,
      "criterion": "AC2",
      "claim": "Refund larger than the original charge is accepted",
      "evidence": "No comparison against charge.amount before issuing",
      "suggested_check": "test_refund_exceeds_charge_rejected"
    }
  ]
}
```

Rules enforced in code (`models.py` validation):
- `verdict == "PASS"` implies no `blocking` findings.
- Each finding needs either (`file` and `line`) or `criterion`. Findings with neither are dropped
  and logged as noise.
- Only `blocking` findings reopen a loop. `minor` findings are logged and included in the final
  report, not fixed.

### 4.3 AuthorResponse (author replying to findings)

```json
{
  "responses": [
    { "finding_id": "F1", "action": "fixed", "note": "Added guard + test" },
    { "finding_id": "F2", "action": "disputed", "reason": "AC3 explicitly allows this" }
  ]
}
```

Disputed findings are not re-debated. If the critic still flags them in the next round, they go
straight to the arbiter.

### 4.4 Arbiter ruling

```json
{ "rulings": [ { "finding_id": "F2", "decision": "dismiss | fix", "reason": "..." } ] }
```

`fix` rulings get one final author pass with no further critic round. Anything still failing after
that ends the run as `needs_human`.

### 4.5 Run log (`.advpipe/runs/<run-id>/` in the target repo)

```
run.json            ← RunState: status, stage, round, cost so far, timestamps
task.md             ← copy of the spec
stage-tests/round-1/{author.txt, critic.json, gates.json}
stage-code/round-1/...
review/{standards.json, security.json}
arbiter.json        ← if used
report.md           ← human-readable summary: status, open findings, minor findings, cost, time
```

Agent output is parsed as JSON. On malformed JSON: retry that agent once with the parse error
appended; on a second failure, record the raw output and treat it as a `FAIL` with one blocking
finding ("critic output unparseable") so the run can't silently pass.

---

## 5. Roles

Each role prompt lives in `src/advpipe/prompts/<role>.md`. Key points each must cover:

| Role | Tools | Must do | Must not do |
|---|---|---|---|
| spec-writer | Read, Glob, Grep, Write (task.md only) | Explore relevant code; write `task.md` with testable acceptance criteria; flag ambiguity under Open questions | Write code or tests |
| test-author | Read, Glob, Grep, Edit, Write, Bash | Write tests from `task.md` only, one or more per acceptance criterion; run them and confirm they fail for the right reason | Implement the feature; look at implementation code beyond existing interfaces |
| test-critic | Read, Glob, Grep | Check every AC is covered; look for missing edge cases; ask "would a trivially wrong implementation pass these?" | Edit files |
| coder | Read, Glob, Grep, Edit, Write, Bash | Make the tests pass with the minimal change; respond to findings with an AuthorResponse | Edit or delete tests (if a test is wrong, dispute it) |
| code-critic | Read, Glob, Grep | Judge the diff against `task.md` and gate output; return a Verdict | Edit files; see the coder's reasoning |
| standards-reviewer | Read, Glob, Grep | Check conventions (`CLAUDE.md`, configured style guide), scope creep, readability, test quality | Re-litigate design choices that `task.md` settles |
| security-reviewer | Read, Glob, Grep | Triage scanner output; check injection, authz, secrets, unsafe input, dependency risks | Report a scanner hit without checking it's reachable |
| arbiter | Read, Glob, Grep | Rule fix or dismiss on each disputed finding, with a one-line reason | Introduce new findings |

Critic and reviewer prompts all end with the shared rules:

> Find the strongest reasons this fails an acceptance criterion in task.md or would break in
> production. Report only findings you can point to by file and line, or by acceptance criterion.
> Mark each finding blocking or minor. If you find nothing blocking, return PASS.
> Reply with JSON only, matching the Verdict schema.

The coder must not modify test files. Enforce this in code: after each coder run, diff test paths;
if changed, revert them and add a blocking finding.

---

## 6. Orchestrator behaviour

State machine (in `orchestrator.py`):

```
INIT → SPEC → [needs_human if blocking open questions]
     → TESTS (adversarial, cap 2) → CODE (adversarial, cap 3)
     → REVIEW (parallel) → [ARBITER if needed] → FINAL_GATES
     → COMPLETE | NEEDS_HUMAN | BUDGET_EXCEEDED | ERROR
```

- Every transition writes `run.json`, so a run can be inspected at any point and resumed later
  (resume is Phase 2).
- Each run happens in its own git worktree and branch (`advpipe/<run-id>`) off the target repo's
  current HEAD. The orchestrator never touches the user's working tree.
- Gate commands come from config. A failed gate becomes a blocking finding with
  `category: correctness` and the tail of the command output as evidence.
- The TESTS stage gate is inverted: tests must exist and the new tests must **fail** before
  implementation. If they pass already, that's a blocking finding ("tests don't exercise the change").
- Budget: sum `total_cost_usd` (or the SDK's equivalent) from every agent run; abort with
  `BUDGET_EXCEEDED` as soon as the configured limit is crossed.
- The orchestrator never pushes, merges, or opens PRs. It leaves a branch and a `report.md`.

---

## 7. Configuration (`pipeline.example.toml`)

```toml
[models]
author = "claude-opus-5-5"
critic = "claude-sonnet-5-5"
arbiter = "claude-opus-5-5"

[limits]
max_rounds_tests = 2
max_rounds_code = 3
max_turns_per_agent = 40
budget_usd_per_task = 15.0

[gates]
# Commands run from the worktree root. Empty list = gate skipped.
test = ["pytest", "-q"]
types = ["mypy", "."]
lint = ["ruff", "check", "."]
security = ["semgrep", "--error", "--config", "auto"]   # optional; skipped if not installed

[paths]
tests = ["tests/"]           # coder may not modify these
standards_doc = "CLAUDE.md"  # given to the standards reviewer
```

---

## 8. Phases

### Phase 0: The no-code version (Claude Code subagents + skill)

Goal: a working version Brendan can use inside Claude Code today, before any Python exists.

- Create the eight subagent files in `.claude/agents/` with the tools and rules from section 5.
  Critics use `model: sonnet`; authors inherit the session model.
- Create `.claude/skills/adversarial-build/SKILL.md` describing the procedure in section 1 with
  explicit round caps, the Verdict JSON format, and "stop and summarize open findings for the user"
  when a cap is hit.
- Create a short `CLAUDE.md` for this repo (build/test commands, layout, the design rules from
  section 1).

**Done when:** the files exist, frontmatter is valid, and you've dry-run the skill on
`tests/fixtures/sample_repo` with a tiny work item (for example "add a `clamp(x, lo, hi)` function")
and shown the transcript summary.

### Phase 1: SDK orchestrator MVP

- `models.py`, `config.py`, `gates.py`, `runner.py` (an `AgentRunner` protocol plus the SDK
  implementation), `stages.py`, `orchestrator.py`, minimal `cli.py` with `advpipe run --repo PATH "work item"`.
- `FakeAgentRunner` in `tests/fakes.py` that returns scripted outputs per role and round, so the
  whole orchestrator is tested **without API calls**.

**Done when:**
- `pytest`, `ruff`, `mypy --strict src/` all pass.
- Tests cover: PASS on first round; FAIL then PASS; cap hit leading to arbiter; arbiter dismiss;
  arbiter fix then success; malformed JSON retry; coder edits a test file (reverted + finding);
  failed gate becomes blocking finding; budget exceeded aborts; tests passing before implementation
  flagged.
- One real end-to-end run on `tests/fixtures/sample_repo` completes and produces `report.md`.
  Ask Brendan before running it, since it spends API credit.

### Phase 2: Hardening

- Per-run git worktrees and branches (`workspace.py`), with cleanup on success.
- Run log as in section 4.5; `advpipe status <run-id>` and `advpipe report <run-id>`.
- Resume an interrupted run from `run.json`.
- Run several work items concurrently (`advpipe run --from-file items.txt --parallel 3`), each in
  its own worktree.

**Done when:** tests cover resume and worktree isolation, and two fake runs in parallel don't
interfere.

### Phase 3: Polish

- `README.md` with install, config, a worked example, cost expectations, and when *not* to use the
  pipeline (routine changes: use a single agent plus one reviewer instead).
- Cost summary per stage in `report.md`.
- Optional: a Claude Code hook config that runs the gates after every edit.

---

## 9. Non-goals

- No web UI. CLI and files only.
- No pushing, merging, or PR creation. A human does that.
- No multi-repo or monorepo-aware task splitting (the orchestrator handles one work item at a time;
  parallelism is across independent items).
- No support for languages other than Python in the gate defaults. Gates are just commands, so
  other languages work via config, but don't build language-specific logic.

---

## 10. Defaults chosen (tell Brendan if you change any)

- Python + Claude Agent SDK, because the existing projects here are Python.
- Critics on Sonnet, authors on Opus.
- Round caps 2 / 3, budget $15 per task.
- Semgrep is optional; if not installed, the security reviewer works from the diff alone and the
  report says so.

## 11. Background reading

The reasoning behind this design (risks, cost estimates, tradeoffs) is in the feasibility guide:
https://claude.ai/code/artifact/7d6fc4b8-0831-412a-886a-b83fbf01b3a9

