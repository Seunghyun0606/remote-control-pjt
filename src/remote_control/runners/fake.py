from __future__ import annotations

import asyncio
from pathlib import Path

from remote_control.runners.base import AgentRunResult, AgentRunner, RunEventCallback, RunHandle


class FakeRunHandle(RunHandle):
    def __init__(self, *, result: AgentRunResult, delay: float = 0) -> None:
        self._result = result
        self._delay = delay
        self._cancelled = False
        self.pid = 4242
        self.session_id = result.session_id

    async def wait(self) -> AgentRunResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._cancelled:
            return AgentRunResult(returncode=130, final_message="cancelled")
        return self._result

    async def cancel(self) -> None:
        self._cancelled = True


class FakeAgentRunner(AgentRunner):
    def __init__(self, *, returncode: int = 0, delay: float = 0) -> None:
        self.returncode = returncode
        self.delay = delay
        self.started: list[dict] = []

    async def start(
        self,
        *,
        project_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        self.started.append(
            {
                "project_id": project_id,
                "instruction": instruction,
                "working_directory": working_directory,
                "host_id": host_id,
            }
        )
        if on_event is not None:
            await on_event({"type": "fake.progress", "message": "started"})
        return FakeRunHandle(
            result=AgentRunResult(
                returncode=self.returncode,
                session_id="fake-session",
                final_message="fake completed" if self.returncode == 0 else "fake failed",
            ),
            delay=self.delay,
        )
