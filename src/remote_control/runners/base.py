from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

RunEventCallback = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class AgentRunResult:
    returncode: int
    session_id: str | None = None
    final_message: str | None = None
    retry_kind: str | None = None
    retry_at: datetime | None = None


class RunHandle(ABC):
    pid: int | None = None
    session_id: str | None = None
    execution_id: str | None = None

    @abstractmethod
    async def wait(self) -> AgentRunResult:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self) -> None:
        raise NotImplementedError

    async def acknowledge_result(self) -> None:
        """Acknowledge a durably processed result when the transport requires it."""
        return None


class AgentRunner(ABC):
    @abstractmethod
    async def start(
        self,
        *,
        project_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        raise NotImplementedError

    async def resume(
        self,
        *,
        session_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        raise NotImplementedError("session resume is not supported by this runner")

    async def steer(
        self,
        *,
        session_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        return await self.resume(
            session_id=session_id,
            instruction=instruction,
            working_directory=working_directory,
            host_id=host_id,
            on_event=on_event,
        )
