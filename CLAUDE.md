# advpipe: adversarial multi-agent coding pipeline

Takes one work item and drives it through adversarial author/critic agent pairs with deterministic
gates, leaving a branch ready for human review. Full brief: `BUILD_SPEC.md`.

## Commands

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev,ui]'   # or: uv sync --all-extras
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy --strict src/
```

## Layout

- `.claude/agents/`: subagent definitions, one per role (Claude Code version)
- `.claude/skills/adversarial-build/SKILL.md`: the pipeline procedure for use inside Claude Code
- `src/advpipe/`: the Python orchestrator (Claude Agent SDK)
- `src/advpipe/ui/`: the optional local web UI (`advpipe ui`, FastAPI + Jinja2 + vendored htmx)
- `src/advpipe/prompts/`: role system prompts, **the source of truth**. Keep `.claude/agents/` in sync.
- `tests/`: own tests. `tests/fakes.py` scripts agent responses, so no API calls are made.
- `tests/fixtures/sample_repo/`: tiny target project for tests and dry runs. Don't modify it in
  place; copy it first.
- `docs/ARCHITECTURE.md`: how it works. `README.md`: public usage guide. Update both when
  behaviour or CLI changes.
- `examples/claude-hooks.json`: optional PostToolUse hook running `advpipe gates`.
- `docs/UI_PLAN.md`: plan for the optional local web UI, in milestones (U0–U6), one per session.

## Design rules (do not drop these)

1. **Orchestrator is code, not an agent.** Deterministic state machine; agents never skip stages.
2. **Tests before code.** The test author sees `task.md` only.
3. **One writer at a time.** Critics and reviewers are read-only and return findings, never edits.
4. **Critics get artifacts, not reasoning:** `task.md`, diff, and gate output only.
5. **Every loop is bounded:** round caps, max turns per agent, and a dollar budget per task.
6. **Critics may return PASS.** Findings must cite file + line or an acceptance criterion.
7. **Ground truth beats opinion.** A failed gate is automatically a blocking finding.

Never push, merge, or open PRs from the pipeline. Default models: authors and arbiter on
`claude-opus-5-5`, critics and reviewers on `claude-sonnet-5-5`.
