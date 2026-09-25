from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from remote_control.event_payloads import sanitize_agent_event
from remote_control.human_gate import extract_human_gate
from remote_control.process_control import terminate_process_tree
from remote_control.projects.operations import LocalProjectOperationExecutor
from remote_control.runner_journal import RunnerExecutionJournal
from remote_control.runners.base import AgentRunResult, RunHandle
from remote_control.runners.codex import CodexRunner, extract_session_id
from remote_control.settings import RunnerSettings
from remote_control.transport.protocol import Envelope, message

logger = logging.getLogger(__name__)


class RunnerDaemon:
    def __init__(
        self,
        settings: RunnerSettings,
        *,
        journal: RunnerExecutionJournal | None = None,
    ) -> None:
        self.settings = settings
        self.boot_id = uuid4().hex
        self.journal = journal or RunnerExecutionJournal(settings.resolved_state_path)
        self._journal_recovered = False
        self.runner = CodexRunner(
            executable=settings.codex_executable,
            sandbox=settings.codex_sandbox,
            approval_policy=settings.codex_approval_policy,
            codex_home=settings.codex_home,
        )
        self.project_operations = LocalProjectOperationExecutor(
            projectctl_executable=settings.projectctl_executable,
            git_executable=settings.git_executable,
            timeout_seconds=settings.project_operation_timeout_seconds,
        )
        self.running: dict[str, RunHandle] = {}
        self.running_sessions: dict[str, str | None] = {}
        self.completed: dict[str, AgentRunResult] = {}
        self._send_lock = asyncio.Lock()
        self._websocket: ClientConnection | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def run_forever(self) -> None:
        await self._recover_persisted_executions()
        while True:
            try:
                await self._run_connection()
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionClosed) as exc:
                logger.warning("runner connection lost: %s", exc)
            except Exception:
                logger.exception("runner connection loop failed; reconnecting")
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
                    runner_boot_id=self.boot_id,
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
            heartbeat = asyncio.create_task(
                self._heartbeat(websocket),
                name="runner-heartbeat",
            )
            receiver = asyncio.create_task(
                self._receive_loop(websocket),
                name="runner-receiver",
            )
            try:
                done, pending = await asyncio.wait(
                    {heartbeat, receiver},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    await task
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                for task in (heartbeat, receiver):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(heartbeat, receiver, return_exceptions=True)
                if self._websocket is websocket:
                    self._websocket = None

    async def _receive_loop(self, websocket: ClientConnection) -> None:
        async for raw in websocket:
            envelope = Envelope.model_validate_json(raw)
            await self._handle(websocket, envelope)

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
            elif execution_id in self.completed:
                await self._send(
                    websocket,
                    _result_message(execution_id, self.completed[execution_id]),
                )
            else:
                await self._send(
                    websocket,
                    message(
                        "JOB_ERROR",
                        execution_id=execution_id,
                        error="execution not found on runner; termination is unconfirmed",
                        retry_kind="cancel_unconfirmed",
                    ),
                )
        elif envelope.type == "JOB_RESULT_ACK":
            execution_id = str(envelope.payload.get("execution_id") or "")
            self.completed.pop(execution_id, None)
            self.journal.remove(execution_id)
        elif envelope.type == "PROJECT_OPERATION_REQUEST":
            await self._project_operation(websocket, envelope)

    async def _project_operation(
        self,
        websocket: ClientConnection,
        envelope: Envelope,
    ) -> None:
        payload = envelope.payload
        request_id = str(payload.get("request_id") or "")
        project_id = str(payload.get("project_id") or "")
        working_directory_text = str(payload.get("working_directory") or "")
        operation = str(payload.get("operation") or "")
        operation_payload = payload.get("payload")
        if not isinstance(operation_payload, dict):
            operation_payload = {}
        if not request_id or not project_id or not working_directory_text or not operation:
            await self._send(
                websocket,
                message(
                    "PROJECT_OPERATION_ERROR",
                    request_id=request_id,
                    error="invalid project operation payload",
                ),
            )
            return
        try:
            result = await self.project_operations.execute(
                host_id=self.settings.host_id,
                project_id=project_id,
                working_directory=Path(working_directory_text),
                operation=operation,
                payload=operation_payload,
            )
        except Exception as exc:
            await self._send(
                websocket,
                message(
                    "PROJECT_OPERATION_ERROR",
                    request_id=request_id,
                    error=str(exc),
                ),
            )
            return
        await self._send(
            websocket,
            message(
                "PROJECT_OPERATION_RESULT",
                request_id=request_id,
                result=result,
            ),
        )

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
                self.journal.update_session(execution_id, current_session)
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

        if handle.pid is None:
            await handle.cancel()
            await self._send(
                websocket,
                message(
                    "JOB_ERROR",
                    execution_id=execution_id,
                    error="runner started Codex without a process id",
                ),
            )
            return
        try:
            self.journal.start(
                execution_id=execution_id,
                pid=handle.pid,
                working_directory=working_directory_text,
                boot_id=self.boot_id,
                session_id=seen_session or handle.session_id,
            )
        except Exception:
            await handle.cancel()
            raise

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
        self._start_background_task(
            self._finish_job(execution_id, handle),
            name=f"runner-job:{execution_id}",
        )

    def _start_background_task(
        self,
        coroutine,
        *,
        name: str,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)

        def cleanup(done_task: asyncio.Task[None]) -> None:
            self._background_tasks.discard(done_task)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error(
                    "runner background task failed task=%s",
                    done_task.get_name(),
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(cleanup)
        return task

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
        self.journal.complete(execution_id, result)
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
                # Keep the completed result until the Controller sends JOB_RESULT_ACK.
                # This makes completion delivery durable across Runner reconnect/restart.
                continue

    async def _recover_persisted_executions(self) -> None:
        if self._journal_recovered:
            return
        for entry in self.journal.list():
            if entry.state == "COMPLETED":
                self.completed[entry.execution_id] = entry.to_result()
                continue

            stopped = await terminate_process_tree(entry.pid, timeout_seconds=10)
            if not stopped:
                raise RuntimeError(
                    "refusing to start Runner because an orphan Codex process "
                    f"could not be terminated: execution={entry.execution_id} pid={entry.pid}"
                )
            result = AgentRunResult(
                returncode=130,
                session_id=entry.session_id,
                final_message=(
                    "Runner restarted while this execution was active; "
                    "the persisted Codex process tree was terminated before reconnect."
                ),
            )
            self.journal.complete(entry.execution_id, result)
            self.completed[entry.execution_id] = result
        self._journal_recovered = True

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


def _sanitize_event(event: dict, *, now: datetime | None = None) -> dict:
    return sanitize_agent_event(event, now=now)
