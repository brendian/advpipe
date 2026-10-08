from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from advpipe.config import Config, load_config
from advpipe.runner import Role, load_prompt

ROOT = Path(__file__).parent.parent


def test_example_config_matches_defaults() -> None:
    assert load_config(ROOT / "pipeline.example.toml") == Config()


def test_repo_config_discovered(tmp_path: Path) -> None:
    (tmp_path / "pipeline.toml").write_text("[limits]\nmax_rounds_code = 5\n")
    assert load_config(repo=tmp_path).limits.max_rounds_code == 5
    assert load_config(repo=tmp_path / "missing") == Config()


def test_unknown_config_key_rejected(tmp_path: Path) -> None:
    (tmp_path / "pipeline.toml").write_text("[limits]\nmax_round_code = 5\n")
    with pytest.raises(ValidationError):
        load_config(repo=tmp_path)


@pytest.mark.parametrize("role", list(Role))
def test_subagents_in_sync_with_prompts(role: Role) -> None:
    """src/advpipe/prompts/ is the source of truth; .claude/agents/ must say the same thing."""
    agent = (ROOT / ".claude" / "agents" / f"{role.value}.md").read_text()
    _, frontmatter, body = agent.split("---\n", 2)
    assert f"name: {role.value}" in frontmatter
    assert body.strip() == load_prompt(role).strip()


@pytest.mark.parametrize(
    "role", [Role.TEST_CRITIC, Role.CODE_CRITIC, Role.STANDARDS_REVIEWER, Role.SECURITY_REVIEWER]
)
def test_critic_prompts_carry_shared_rules(role: Role) -> None:
    prompt = load_prompt(role)
    assert "If you find nothing blocking, return PASS." in prompt
    assert "Reply with JSON only, matching the Verdict schema." in prompt
