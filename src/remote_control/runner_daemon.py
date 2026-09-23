from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from remote_control.runners.base import RunHandle
from remote_control.runners.codex import CodexRunner
from remote_control.settings import RunnerSettings
from remote_control.transport.protocol import Envelope, message

logger = logging.getLogger(__name__)


class RunnerDaemon:
    def __init__(self, settings: RunnerSettings) -> None:
        self.settings = settings
        self.runner = CodexRunner(
            executable=settings.codex_executable,
            sandbox=settings.codex_sandbox,
            approval_policy=settings.codex_approval_policy,
        )
        self.running: dict[str, RunHandle] = {}
        self._send_lock = asyncio.Lock()

    async def run_forever(self) -> None:
        while True:
            try:
                await self._run_connection()
            except (OSError, ConnectionClosed) as exc:
                logger.warning("runner connection lost: %s", exc)
            await asyncio.sleep(self.settings.reconnect_seconds)

    async def _run_connection(self) -> None:
        async with connect(
            self.settings.controller_ws,
            additional_headers={"Authorization": f"Bearer {self.settings.token}"},
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            await self._send(
                websocket,
                message(
                    "HOST_REGISTER",
                    host_id=self.settings.host_id,
                    name=self.settings.name,
                    os=self.settings.os_name,
                    capabilities=sorted(self.settings.capabilities),
                ),
            )
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for raw in websocket:
                    envelope = Envelope.model_validate_json(raw)
                    await self._handle(websocket, envelope)
            finally:
                heartbeat.cancel()

    async def _heartbeat(self, websocket: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            await self._send(
                websocket,
                message(
                    "HEARTBEAT",
                    host_id=self.settings.host_id,
                    running_jobs=sorted(self.running),
                ),
            )

    async def _handle(self, websocket: ClientConnection, envelope: Envelope) -> None:
        if envelope.type == "JOB_START":
            await self._start_job(websocket, envelope)
        elif envelope.type == "JOB_CANCEL":
            execution_id = str(envelope.payload.get("execution_id") or "")
            handle = self.running.get(execution_id)
            if handle is not None:
                await handle.cancel()

    async def _start_job(self, websocket: ClientConnection, envelope: Envelope) -> None:
        payload = envelope.payload
        execution_id = str(payload.get("execution_id") or "")
        project_id = str(payload.get("project_id") or "")
        instruction = str(payload.get("instruction") or "")
        working_directory = Path(str(payload.get("working_directory") or ""))

        if not execution_id or not project_id or not instruction or not str(working_directory):
            await self._send(
                websocket,
                message(
                    "JOB_ERROR",
                    execution_id=execution_id,
                    error="invalid JOB_START payload",
                ),
            )
            return

        async def on_event(event: dict) -> None:
            session_id = _session_id(event)
            await self._send(
                websocket,
                message(
                    "JOB_PROGRESS",
                    execution_id=execution_id,
                    event_type=str(event.get("type") or "agent_event"),
                    session_id=session_id,
                ),
            )

        try:
            handle = await self.runner.start(
                project_id=project_id,
                instruction=instruction,
                working_directory=working_directory,
                host_id=self.settings.host_id,
                on_event=on_event,
            )
        except Exception as exc:
            await self._send(
                websocket,
                message("JOB_ERROR", execution_id=execution_id, error=str(exc)),
            )
            return

        self.running[execution_id] = handle
        await self._send(
            websocket,
            message(
                "JOB_ACCEPTED",
                execution_id=execution_id,
                pid=handle.pid,
                session_id=handle.session_id,
            ),
        )
        asyncio.create_task(self._finish_job(websocket, execution_id, handle))

    async def _finish_job(
        self,
        websocket: ClientConnection,
        execution_id: str,
        handle: RunHandle,
    ) -> None:
        try:
            result = await handle.wait()
            await self._send(
                websocket,
                message(
                    "JOB_RESULT",
                    execution_id=execution_id,
                    returncode=result.returncode,
                    session_id=result.session_id,
                    final_message=result.final_message,
                ),
            )
        except Exception as exc:
            await self._send(
                websocket,
                message("JOB_ERROR", execution_id=execution_id, error=str(exc)),
            )
        finally:
            self.running.pop(execution_id, None)

    async def _send(self, websocket: ClientConnection, envelope: Envelope) -> None:
        async with self._send_lock:
            await websocket.send(envelope.model_dump_json())


def _session_id(event: dict) -> str | None:
    value = event.get("session_id") or event.get("thread_id")
    if isinstance(value, str):
        return value
    thread = event.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
        return thread["id"]
    return None
