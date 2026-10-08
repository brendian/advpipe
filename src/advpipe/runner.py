"""AgentRunner protocol, role table, and the Claude Agent SDK implementation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Literal, Protocol

from advpipe.config import Config


class Role(StrEnum):
    SPEC_WRITER = "spec-writer"
    TEST_AUTHOR = "test-author"
    TEST_CRITIC = "test-critic"
    CODER = "coder"
    CODE_CRITIC = "code-critic"
    STANDARDS_REVIEWER = "standards-reviewer"
    SECURITY_REVIEWER = "security-reviewer"
    ARBITER = "arbiter"


READ_ONLY = ["Read", "Glob", "Grep"]
WRITER = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]

ModelTier = Literal["author", "critic", "arbiter"]

ROLE_TOOLS: dict[Role, list[str]] = {
    Role.SPEC_WRITER: ["Read", "Glob", "Grep", "Write"],
    Role.TEST_AUTHOR: WRITER,
    Role.TEST_CRITIC: READ_ONLY,
    Role.CODER: WRITER,
    Role.CODE_CRITIC: READ_ONLY,
    Role.STANDARDS_REVIEWER: READ_ONLY,
    Role.SECURITY_REVIEWER: READ_ONLY,
    Role.ARBITER: READ_ONLY,
}

ROLE_MODEL: dict[Role, ModelTier] = {
    Role.SPEC_WRITER: "author",
    Role.TEST_AUTHOR: "author",
    Role.CODER: "author",
    Role.TEST_CRITIC: "critic",
    Role.CODE_CRITIC: "critic",
    Role.STANDARDS_REVIEWER: "critic",
    Role.SECURITY_REVIEWER: "critic",
    Role.ARBITER: "arbiter",
}


def load_prompt(role: Role) -> str:
    return (resources.files("advpipe") / "prompts" / f"{role.value}.md").read_text()


@dataclass(frozen=True)
class AgentRequest:
    role: Role
    prompt: str
    system_prompt: str
    model: str
    tools: list[str]
    cwd: Path
    max_turns: int
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class AgentResult:
    text: str
    cost_usd: float
    is_error: bool = False
    num_turns: int = 0
    session_id: str = ""


def build_request(
    role: Role, prompt: str, config: Config, cwd: Path, remaining_budget: float | None
) -> AgentRequest:
    return AgentRequest(
        role=role,
        prompt=prompt,
        system_prompt=load_prompt(role),
        model=getattr(config.models, ROLE_MODEL[role]),
        tools=list(ROLE_TOOLS[role]),
        cwd=cwd,
        max_turns=config.limits.max_turns_per_agent,
        max_budget_usd=remaining_budget,
    )


class AgentRunner(Protocol):
    async def run(self, request: AgentRequest) -> AgentResult: ...


class SdkAgentRunner:
    """Runs one agent via claude_agent_sdk.query and returns its final text and cost."""

    async def run(self, request: AgentRequest) -> AgentResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        options = ClaudeAgentOptions(
            system_prompt=request.system_prompt,
            model=request.model,
            # `tools` limits which tools exist at all, so read-only roles cannot edit;
            # `allowed_tools` + dontAsk pre-approves them and denies anything else.
            tools=request.tools,
            allowed_tools=request.tools,
            permission_mode="dontAsk",
            cwd=str(request.cwd),
            max_turns=request.max_turns,
            max_budget_usd=request.max_budget_usd,
            # Load the target repo's project settings and CLAUDE.md, not user-level config.
            setting_sources=["project"],
        )
        last_text = ""
        result: ResultMessage | None = None
        async for message in query(prompt=request.prompt, options=options):
            if isinstance(message, AssistantMessage):
                texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                if texts:
                    last_text = "\n".join(texts)
            elif isinstance(message, ResultMessage):
                result = message
        if result is None:
            return AgentResult(text=last_text, cost_usd=0.0, is_error=True)
        return AgentResult(
            text=result.result if result.result is not None else last_text,
            cost_usd=result.total_cost_usd or 0.0,
            is_error=result.is_error,
            num_turns=result.num_turns,
            session_id=result.session_id,
        )
