from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
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
        await self.gateway.cancel_remote(
            host_id=self.host_id,
            execution_id=self.execution_id,
            session_id=self.session_id,
        )


@dataclass(slots=True)
class _PendingRun:
    host_id: str
    future: asyncio.Future[AgentRunResult]
    handle: RemoteRunHandle
    on_event: RunEventCallback | None


@dataclass(slots=True)
class _PendingProjectOperation:
    host_id: str
    future: asyncio.Future[dict[str, Any]]


class RunnerGateway:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._pending: dict[str, _PendingRun] = {}
        self._project_pending: dict[str, _PendingProjectOperation] = {}
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
                    AgentRunResult(
                        returncode=75,
                        session_id=pending.handle.session_id,
                        final_message="runner disconnected",
                        retry_kind="host",
                    )
                )
                self._pending.pop(execution_id, None)
        for request_id, pending in list(self._project_pending.items()):
            if pending.host_id == host_id and not pending.future.done():
                pending.future.set_exception(
                    ConnectionError(f"runner {host_id!r} disconnected")
                )
                self._project_pending.pop(request_id, None)

    def is_connected(self, host_id: str) -> bool:
        return host_id in self._connections

    async def send(self, host_id: str, envelope: Envelope) -> None:
        websocket = self._connections.get(host_id)
        if websocket is None:
            raise ConnectionError(f"runner {host_id!r} is not connected")
        await websocket.send_text(envelope.model_dump_json())

    async def project_operation(
        self,
        *,
        host_id: str,
        project_id: str,
        working_directory: Path,
        operation: str,
        payload: dict[str, Any],
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if not self.is_connected(host_id):
            raise ConnectionError(f"runner {host_id!r} is not connected")
        request_id = uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._project_pending[request_id] = _PendingProjectOperation(
            host_id=host_id,
            future=future,
        )
        try:
            await self.send(
                host_id,
                message(
                    "PROJECT_OPERATION_REQUEST",
                    request_id=request_id,
                    project_id=project_id,
                    working_directory=str(working_directory),
                    operation=operation,
                    payload=payload,
                ),
            )
            return await asyncio.wait_for(future, timeout=max(timeout_seconds, 1))
        except TimeoutError as exc:
            raise TimeoutError(
                f"remote project operation {operation!r} timed out"
            ) from exc
        finally:
            self._project_pending.pop(request_id, None)

    async def cancel_remote(
        self,
        *,
        host_id: str,
        execution_id: str,
        session_id: str | None,
    ) -> None:
        pending = self._pending.pop(execution_id, None)
        try:
            await self.send(
                host_id,
                message("JOB_CANCEL", execution_id=execution_id),
            )
        finally:
            if pending is not None and not pending.future.done():
                pending.future.set_result(
                    AgentRunResult(
                        returncode=130,
                        session_id=session_id,
                        final_message="cancelled by controller",
                    )
                )

    async def start_remote(
        self,
        *,
        host_id: str,
        project_id: str,
        instruction: str,
        working_directory: Path,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        return await self._start_remote_operation(
            message_type="JOB_START",
            host_id=host_id,
            instruction=instruction,
            working_directory=working_directory,
            on_event=on_event,
            project_id=project_id,
        )

    async def resume_remote(
        self,
        *,
        host_id: str,
        session_id: str,
        instruction: str,
        working_directory: Path,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        return await self._start_remote_operation(
            message_type="JOB_RESUME",
            host_id=host_id,
            instruction=instruction,
            working_directory=working_directory,
            on_event=on_event,
            session_id=session_id,
        )

    async def steer_remote(
        self,
        *,
        host_id: str,
        session_id: str,
        instruction: str,
        working_directory: Path,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        return await self._start_remote_operation(
            message_type="JOB_STEER",
            host_id=host_id,
            instruction=instruction,
            working_directory=working_directory,
            on_event=on_event,
            session_id=session_id,
        )

    def adopt_remote(
        self,
        *,
        host_id: str,
        execution_id: str,
        session_id: str | None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        existing = self._pending.get(execution_id)
        if existing is not None:
            return existing.handle
        future: asyncio.Future[AgentRunResult] = asyncio.get_running_loop().create_future()
        handle = RemoteRunHandle(
            execution_id=execution_id,
            host_id=host_id,
            gateway=self,
            result_future=future,
        )
        handle.session_id = session_id
        self._pending[execution_id] = _PendingRun(
            host_id=host_id,
            future=future,
            handle=handle,
            on_event=on_event,
        )
        return handle

    async def _start_remote_operation(
        self,
        *,
        message_type: str,
        host_id: str,
        instruction: str,
        working_directory: Path,
        on_event: RunEventCallback | None,
        project_id: str | None = None,
        session_id: str | None = None,
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
        handle.session_id = session_id
        self._pending[execution_id] = _PendingRun(
            host_id=host_id,
            future=future,
            handle=handle,
            on_event=on_event,
        )
        payload = {
            "execution_id": execution_id,
            "instruction": instruction,
            "working_directory": str(working_directory),
        }
        if project_id is not None:
            payload["project_id"] = project_id
        if session_id is not None:
            payload["session_id"] = session_id
        try:
            await self.send(host_id, message(message_type, **payload))
        except Exception:
            self._pending.pop(execution_id, None)
            raise
        return handle

    async def handle(self, host_id: str, envelope: Envelope) -> None:
        payload = envelope.payload

        if envelope.type in {"PROJECT_OPERATION_RESULT", "PROJECT_OPERATION_ERROR"}:
            request_id = str(payload.get("request_id") or "")
            pending_operation = self._project_pending.get(request_id)
            if pending_operation is None or pending_operation.host_id != host_id:
                return
            if envelope.type == "PROJECT_OPERATION_ERROR":
                if not pending_operation.future.done():
                    pending_operation.future.set_exception(
                        RuntimeError(
                            str(payload.get("error") or "remote project operation failed")
                        )
                    )
            else:
                result = payload.get("result")
                if not isinstance(result, dict):
                    result = {}
                if not pending_operation.future.done():
                    pending_operation.future.set_result(result)
            return

        execution_id = str(payload.get("execution_id") or "")
        pending = self._pending.get(execution_id)
        if pending is None or pending.host_id != host_id:
            return

        if envelope.type == "JOB_ACCEPTED":
            pid = payload.get("pid")
            pending.handle.pid = int(pid) if isinstance(pid, int) else None
            session_id = payload.get("session_id")
            if isinstance(session_id, str) and session_id:
                pending.handle.session_id = session_id
            return

        if envelope.type in {"JOB_PROGRESS", "SESSION_STARTED", "HUMAN_GATE"}:
            session_id = payload.get("session_id")
            if isinstance(session_id, str) and session_id:
                pending.handle.session_id = session_id
            if pending.on_event is not None:
                event = payload.get("event")
                if isinstance(event, dict):
                    await pending.on_event(event)
                else:
                    await pending.on_event({"type": envelope.type, **payload})
            return

        if envelope.type == "JOB_RESULT":
            if not pending.future.done():
                pending.future.set_result(
                    AgentRunResult(
                        returncode=int(payload.get("returncode", 0)),
                        session_id=_string_or_none(payload.get("session_id")),
                        final_message=_string_or_none(payload.get("final_message")),
                        retry_kind=_string_or_none(payload.get("retry_kind")),
                        retry_at=_datetime_or_none(payload.get("retry_at")),
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
                        retry_kind=_string_or_none(payload.get("retry_kind")),
                        retry_at=_datetime_or_none(payload.get("retry_at")),
                    )
                )
            self._pending.pop(execution_id, None)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _datetime_or_none(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
