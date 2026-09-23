from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from remote_control.human_gate import extract_human_gate
from remote_control.runners.base import AgentRunResult, RunHandle
from remote_control.runners.codex import CodexRunner, extract_session_id
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
        self.running_sessions: dict[str, str | None] = {}
        self.completed: dict[str, AgentRunResult] = {}
        self._send_lock = asyncio.Lock()
        self._websocket: ClientConnection | None = None

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
            self._websocket = websocket
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
            await self._send(
                websocket,
                message(
                    "RUNNING_JOBS",
                    host_id=self.settings.host_id,
                    running_jobs=self._running_snapshot(),
                    completed_jobs=self._completed_snapshot(),
                ),
            )
            await self._flush_completed()
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for raw in websocket:
                    envelope = Envelope.model_validate_json(raw)
                    await self._handle(websocket, envelope)
            finally:
                heartbeat.cancel()
                if self._websocket is websocket:
                    self._websocket = None

    async def _heartbeat(self, websocket: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            await self._send(
                websocket,
                message(
                    "HEARTBEAT",
                    host_id=self.settings.host_id,
                    running_jobs=self._running_snapshot(),
                ),
            )

    def _running_snapshot(self) -> list[dict]:
        return [
            {
                "execution_id": execution_id,
                "session_id": self.running_sessions.get(execution_id),
            }
            for execution_id in sorted(self.running)
        ]

    def _completed_snapshot(self) -> list[dict]:
        return [
            {
                "execution_id": execution_id,
                "session_id": result.session_id,
            }
            for execution_id, result in sorted(self.completed.items())
        ]

    async def _handle(self, websocket: ClientConnection, envelope: Envelope) -> None:
        if envelope.type == "JOB_START":
            await self._start_job(websocket, envelope, resume=False)
        elif envelope.type in {"JOB_RESUME", "JOB_STEER"}:
            await self._start_job(websocket, envelope, resume=True)
        elif envelope.type == "JOB_CANCEL":
            execution_id = str(envelope.payload.get("execution_id") or "")
            handle = self.running.get(execution_id)
            if handle is not None:
                await handle.cancel()

    async def _start_job(
        self,
        websocket: ClientConnection,
        envelope: Envelope,
        *,
        resume: bool,
    ) -> None:
        payload = envelope.payload
        execution_id = str(payload.get("execution_id") or "")
        project_id = str(payload.get("project_id") or "resumed-session")
        instruction = str(payload.get("instruction") or "")
        working_directory_text = str(payload.get("working_directory") or "")
        session_id = str(payload.get("session_id") or "")
        working_directory = Path(working_directory_text)

        if (
            not execution_id
            or not instruction
            or not working_directory_text
            or (resume and not session_id)
        ):
            await self._send(
                websocket,
                message(
                    "JOB_ERROR",
                    execution_id=execution_id,
                    error=f"invalid {envelope.type} payload",
                ),
            )
            return

        seen_session: str | None = session_id or None
        self.running_sessions[execution_id] = seen_session

        async def on_event(event: dict) -> None:
            nonlocal seen_session
            current_session = extract_session_id(event)
            if current_session and current_session != seen_session:
                seen_session = current_session
                self.running_sessions[execution_id] = current_session
                await self._send_current(
                    message(
                        "SESSION_STARTED",
                        execution_id=execution_id,
                        session_id=current_session,
                        event={"type": "thread.started", "thread_id": current_session},
                    )
                )
            gate = extract_human_gate(event)
            await self._send_current(
                message(
                    "HUMAN_GATE" if gate is not None else "JOB_PROGRESS",
                    execution_id=execution_id,
                    session_id=current_session or seen_session,
                    event=_sanitize_event(event),
                )
            )

        try:
            if resume:
                handle = await self.runner.resume(
                    session_id=session_id,
                    instruction=instruction,
                    working_directory=working_directory,
                    host_id=self.settings.host_id,
                    on_event=on_event,
                )
            else:
                handle = await self.runner.start(
                    project_id=project_id,
                    instruction=instruction,
                    working_directory=working_directory,
                    host_id=self.settings.host_id,
                    on_event=on_event,
                )
        except Exception as exc:
            self.running_sessions.pop(execution_id, None)
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
                session_id=seen_session or handle.session_id,
            ),
        )
        asyncio.create_task(self._finish_job(execution_id, handle))

    async def _finish_job(
        self,
        execution_id: str,
        handle: RunHandle,
    ) -> None:
        try:
            result = await handle.wait()
        except asyncio.CancelledError:
            result = AgentRunResult(
                returncode=130,
                session_id=self.running_sessions.get(execution_id) or handle.session_id,
                final_message="cancelled",
            )
        except Exception as exc:
            result = AgentRunResult(
                returncode=1,
                session_id=self.running_sessions.get(execution_id) or handle.session_id,
                final_message=str(exc),
            )

        if result.session_id is None:
            result.session_id = self.running_sessions.get(execution_id)
        self.running.pop(execution_id, None)
        self.running_sessions.pop(execution_id, None)
        self.completed[execution_id] = result
        await self._flush_completed()

    async def _flush_completed(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        for execution_id, result in list(self.completed.items()):
            try:
                await self._send(websocket, _result_message(execution_id, result))
            except (OSError, ConnectionClosed):
                return
            else:
                self.completed.pop(execution_id, None)

    async def _send_current(self, envelope: Envelope) -> bool:
        websocket = self._websocket
        if websocket is None:
            return False
        try:
            await self._send(websocket, envelope)
        except (OSError, ConnectionClosed):
            return False
        return True

    async def _send(self, websocket: ClientConnection, envelope: Envelope) -> None:
        async with self._send_lock:
            await websocket.send(envelope.model_dump_json())


def _result_message(execution_id: str, result: AgentRunResult) -> Envelope:
    return message(
        "JOB_RESULT",
        execution_id=execution_id,
        returncode=result.returncode,
        session_id=result.session_id,
        final_message=result.final_message,
        retry_kind=result.retry_kind,
        retry_at=result.retry_at.isoformat() if result.retry_at else None,
    )


def _sanitize_event(event: dict) -> dict:
    result: dict = {"type": str(event.get("type") or "agent_event")}
    session_id = extract_session_id(event)
    if session_id:
        result["thread_id"] = session_id

    for key in ("message", "text", "error"):
        value = event.get(key)
        if isinstance(value, str):
            result[key] = _truncate(value, 1200)

    for key in (
        "question",
        "prompt",
        "details",
        "description",
        "header",
        "approval_type",
    ):
        value = event.get(key)
        if isinstance(value, str):
            result[key] = _truncate(value, 2000)

    for key in (
        "resets_at",
        "reset_at",
        "retry_at",
        "retry_after",
        "retry_after_seconds",
    ):
        value = event.get(key)
        if isinstance(value, (str, int, float)):
            result[key] = value

    options = event.get("options")
    if isinstance(options, list):
        result["options"] = options[:8]

    item = event.get("item")
    if isinstance(item, dict):
        clean_item: dict = {"type": str(item.get("type") or "")}
        for key in (
            "text",
            "content",
            "command",
            "status",
            "question",
            "prompt",
            "details",
            "description",
            "header",
            "approval_type",
        ):
            value = item.get(key)
            if isinstance(value, str):
                clean_item[key] = _truncate(value, 1200 if key != "command" else 400)
        item_options = item.get("options")
        if isinstance(item_options, list):
            clean_item["options"] = item_options[:8]
        exit_code = item.get("exit_code")
        if isinstance(exit_code, int):
            clean_item["exit_code"] = exit_code
        result["item"] = clean_item

    return result


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
