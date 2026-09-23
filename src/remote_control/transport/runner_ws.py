from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import WebSocket

from remote_control.runners.base import AgentRunResult, RunEventCallback, RunHandle
from remote_control.transport.protocol import Envelope, message


class RemoteRunHandle(RunHandle):
    def __init__(
        self,
        *,
        execution_id: str,
        host_id: str,
        gateway: "RunnerGateway",
        result_future: asyncio.Future[AgentRunResult],
    ) -> None:
        self.execution_id = execution_id
        self.host_id = host_id
        self.gateway = gateway
        self._result_future = result_future
        self.pid: int | None = None
        self.session_id: str | None = None

    async def wait(self) -> AgentRunResult:
        result = await self._result_future
        self.session_id = result.session_id
        return result

    async def cancel(self) -> None:
        if self._result_future.done():
            return
        await self.gateway.send(
            self.host_id,
            message("JOB_CANCEL", execution_id=self.execution_id),
        )


@dataclass(slots=True)
class _PendingRun:
    host_id: str
    future: asyncio.Future[AgentRunResult]
    handle: RemoteRunHandle
    on_event: RunEventCallback | None


class RunnerGateway:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._pending: dict[str, _PendingRun] = {}
        self._lock = asyncio.Lock()

    async def attach(self, host_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            previous = self._connections.get(host_id)
            self._connections[host_id] = websocket
        if previous is not None and previous is not websocket:
            await previous.close(code=1012)

    async def detach(self, host_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            if self._connections.get(host_id) is websocket:
                self._connections.pop(host_id, None)
        for execution_id, pending in list(self._pending.items()):
            if pending.host_id == host_id and not pending.future.done():
                pending.future.set_result(
                    AgentRunResult(returncode=1, final_message="runner disconnected")
                )
                self._pending.pop(execution_id, None)

    def is_connected(self, host_id: str) -> bool:
        return host_id in self._connections

    async def send(self, host_id: str, envelope: Envelope) -> None:
        websocket = self._connections.get(host_id)
        if websocket is None:
            raise ConnectionError(f"runner {host_id!r} is not connected")
        await websocket.send_text(envelope.model_dump_json())

    async def start_remote(
        self,
        *,
        host_id: str,
        project_id: str,
        instruction: str,
        working_directory: Path,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        if not self.is_connected(host_id):
            raise ConnectionError(f"runner {host_id!r} is not connected")
        execution_id = uuid4().hex
        future: asyncio.Future[AgentRunResult] = asyncio.get_running_loop().create_future()
        handle = RemoteRunHandle(
            execution_id=execution_id,
            host_id=host_id,
            gateway=self,
            result_future=future,
        )
        self._pending[execution_id] = _PendingRun(
            host_id=host_id,
            future=future,
            handle=handle,
            on_event=on_event,
        )
        try:
            await self.send(
                host_id,
                message(
                    "JOB_START",
                    execution_id=execution_id,
                    project_id=project_id,
                    instruction=instruction,
                    working_directory=str(working_directory),
                ),
            )
        except Exception:
            self._pending.pop(execution_id, None)
            raise
        return handle

    async def handle(self, host_id: str, envelope: Envelope) -> None:
        payload = envelope.payload
        execution_id = str(payload.get("execution_id") or "")
        pending = self._pending.get(execution_id)
        if pending is None or pending.host_id != host_id:
            return

        if envelope.type == "JOB_ACCEPTED":
            pid = payload.get("pid")
            pending.handle.pid = int(pid) if isinstance(pid, int) else None
            session_id = payload.get("session_id")
            if isinstance(session_id, str):
                pending.handle.session_id = session_id
            return

        if envelope.type in {"JOB_PROGRESS", "SESSION_STARTED"}:
            session_id = payload.get("session_id")
            if isinstance(session_id, str):
                pending.handle.session_id = session_id
            if pending.on_event is not None:
                await pending.on_event({"type": envelope.type, **payload})
            return

        if envelope.type == "JOB_RESULT":
            if not pending.future.done():
                pending.future.set_result(
                    AgentRunResult(
                        returncode=int(payload.get("returncode", 0)),
                        session_id=_string_or_none(payload.get("session_id")),
                        final_message=_string_or_none(payload.get("final_message")),
                    )
                )
            self._pending.pop(execution_id, None)
            return

        if envelope.type == "JOB_ERROR":
            if not pending.future.done():
                pending.future.set_result(
                    AgentRunResult(
                        returncode=int(payload.get("returncode", 1)),
                        session_id=_string_or_none(payload.get("session_id")),
                        final_message=_string_or_none(payload.get("error")) or "remote runner error",
                    )
                )
            self._pending.pop(execution_id, None)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
