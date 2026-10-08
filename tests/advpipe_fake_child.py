"""The real `advpipe` CLI with a fake agent runner, for tests of detached runs and cancel.

Tests point `advpipe.control.child_command` at this script. Environment:

- ADVPIPE_FAKE_BLOCK=<role>: that role's call waits until the file named by
  ADVPIPE_FAKE_RELEASE exists (forever if it's unset), so a test can catch the run mid-way.

The file name contains "advpipe" so `advpipe cancel` recognises the process as advpipe's.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeAgentRunner, happy_scripts  # noqa: E402

import advpipe.cli as cli  # noqa: E402
from advpipe.runner import AgentRequest, AgentResult  # noqa: E402


class BlockingRunner(FakeAgentRunner):
    async def run(self, request: AgentRequest) -> AgentResult:
        release = os.environ.get("ADVPIPE_FAKE_RELEASE")
        if os.environ.get("ADVPIPE_FAKE_BLOCK") == request.role.value:
            while not (release and Path(release).exists()):
                await asyncio.sleep(0.05)
        return await super().run(request)


cli.SdkAgentRunner = lambda: BlockingRunner(happy_scripts())  # type: ignore[assignment,misc]
cli.app(prog_name="advpipe")
