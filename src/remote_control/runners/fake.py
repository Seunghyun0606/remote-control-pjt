from __future__ import annotations

import asyncio
from pathlib import Path

from remote_control.runners.base import AgentRunResult, AgentRunner, RunEventCallback, RunHandle


class FakeRunHandle(RunHandle):
    def __init__(self, *, result: AgentRunResult, delay: float = 0) -> None:
        self._result = result
        self._delay = delay
        self._cancelled = False
        self._cancel_event = asyncio.Event()
        self.pid = 4242
        self.session_id = result.session_id

    async def wait(self) -> AgentRunResult:
        if self._delay:
            try:
                await asyncio.wait_for(self._cancel_event.wait(), timeout=self._delay)
            except TimeoutError:
                pass
        if self._cancelled:
            return AgentRunResult(
                returncode=130,
                session_id=self.session_id,
                final_message="cancelled",
            )
        return self._result

    async def cancel(self) -> None:
        self._cancelled = True
        self._cancel_event.set()


class FakeAgentRunner(AgentRunner):
    def __init__(
        self,
        *,
        returncode: int = 0,
        delay: float = 0,
        resume_returncode: int | None = None,
        resume_session_id: str = "fake-session",
        resume_final_message: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.delay = delay
        self.resume_returncode = returncode if resume_returncode is None else resume_returncode
        self.resume_session_id = resume_session_id
        self.resume_final_message = resume_final_message
        self.started: list[dict] = []
        self.resumed: list[dict] = []

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
            await on_event({"type": "thread.started", "thread_id": "fake-session"})
            await on_event({"type": "fake.progress", "message": "started"})
        return FakeRunHandle(
            result=AgentRunResult(
                returncode=self.returncode,
                session_id="fake-session",
                final_message="fake completed" if self.returncode == 0 else "fake failed",
            ),
            delay=self.delay,
        )

    async def resume(
        self,
        *,
        session_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        self.resumed.append(
            {
                "session_id": session_id,
                "instruction": instruction,
                "working_directory": working_directory,
                "host_id": host_id,
            }
        )
        if on_event is not None:
            await on_event(
                {
                    "type": "thread.started",
                    "thread_id": self.resume_session_id,
                }
            )
            await on_event({"type": "fake.progress", "message": "resumed"})
        return FakeRunHandle(
            result=AgentRunResult(
                returncode=self.resume_returncode,
                session_id=self.resume_session_id,
                final_message=(
                    self.resume_final_message
                    if self.resume_final_message is not None
                    else (
                        "fake resumed"
                        if self.resume_returncode == 0
                        else "fake resume failed"
                    )
                ),
            ),
            delay=self.delay,
        )
