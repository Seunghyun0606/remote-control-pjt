from __future__ import annotations

from pathlib import Path

from remote_control.runners.base import AgentRunner, RunEventCallback, RunHandle
from remote_control.transport.runner_ws import RunnerGateway


class HybridAgentRunner(AgentRunner):
    def __init__(
        self,
        *,
        local_host_id: str,
        local_runner: AgentRunner,
        gateway: RunnerGateway,
    ) -> None:
        self.local_host_id = local_host_id
        self.local_runner = local_runner
        self.gateway = gateway

    async def start(
        self,
        *,
        project_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        target = host_id or self.local_host_id
        if target == self.local_host_id:
            return await self.local_runner.start(
                project_id=project_id,
                instruction=instruction,
                working_directory=working_directory,
                host_id=target,
                on_event=on_event,
            )
        return await self.gateway.start_remote(
            host_id=target,
            project_id=project_id,
            instruction=instruction,
            working_directory=working_directory,
            on_event=on_event,
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
        target = host_id or self.local_host_id
        if target == self.local_host_id:
            return await self.local_runner.resume(
                session_id=session_id,
                instruction=instruction,
                working_directory=working_directory,
                host_id=target,
                on_event=on_event,
            )
        return await self.gateway.resume_remote(
            host_id=target,
            session_id=session_id,
            instruction=instruction,
            working_directory=working_directory,
            on_event=on_event,
        )

    async def steer(
        self,
        *,
        session_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        target = host_id or self.local_host_id
        if target == self.local_host_id:
            return await self.local_runner.steer(
                session_id=session_id,
                instruction=instruction,
                working_directory=working_directory,
                host_id=target,
                on_event=on_event,
            )
        return await self.gateway.steer_remote(
            host_id=target,
            session_id=session_id,
            instruction=instruction,
            working_directory=working_directory,
            on_event=on_event,
        )
