from __future__ import annotations

import asyncio
import json
from pathlib import Path

from remote_control.recovery.quota import detect_quota_event, detect_quota_text
from remote_control.runners.base import AgentRunResult, AgentRunner, RunEventCallback, RunHandle


def build_codex_command(
    *,
    executable: str,
    sandbox: str,
    approval_policy: str,
    working_directory: Path,
) -> list[str]:
    return [
        executable,
        "exec",
        "--json",
        "--sandbox",
        sandbox,
        "--cd",
        str(working_directory),
        "--config",
        f'approval_policy="{approval_policy}"',
        "-",
    ]


def build_codex_resume_command(
    *,
    executable: str,
    sandbox: str,
    approval_policy: str,
    working_directory: Path,
    session_id: str,
) -> list[str]:
    return [
        executable,
        "exec",
        "resume",
        session_id,
        "--json",
        "--sandbox",
        sandbox,
        "--cd",
        str(working_directory),
        "--config",
        f'approval_policy="{approval_policy}"',
        "-",
    ]


class CodexRunHandle(RunHandle):
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        reader_task: asyncio.Task[AgentRunResult],
    ) -> None:
        self._process = process
        self._reader_task = reader_task
        self.pid = process.pid
        self.session_id: str | None = None
        self.execution_id: str | None = None

    async def wait(self) -> AgentRunResult:
        result = await self._reader_task
        self.session_id = result.session_id
        return result

    async def cancel(self) -> None:
        if self._process.returncode is not None:
            return
        self._process.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=10)
        except TimeoutError:
            self._process.kill()
            await self._process.wait()
        if not self._reader_task.done():
            self._reader_task.cancel()


class CodexRunner(AgentRunner):
    def __init__(
        self,
        *,
        executable: str = "codex",
        sandbox: str = "workspace-write",
        approval_policy: str = "never",
    ) -> None:
        self.executable = executable
        self.sandbox = sandbox
        self.approval_policy = approval_policy

    async def start(
        self,
        *,
        project_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        del project_id, host_id
        self._validate_working_directory(working_directory)
        return await self._spawn(
            build_codex_command(
                executable=self.executable,
                sandbox=self.sandbox,
                approval_policy=self.approval_policy,
                working_directory=working_directory,
            ),
            instruction=instruction,
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
        del host_id
        self._validate_working_directory(working_directory)
        return await self._spawn(
            build_codex_resume_command(
                executable=self.executable,
                sandbox=self.sandbox,
                approval_policy=self.approval_policy,
                working_directory=working_directory,
                session_id=session_id,
            ),
            instruction=instruction,
            on_event=on_event,
        )

    @staticmethod
    def _validate_working_directory(working_directory: Path) -> None:
        if not working_directory.exists() or not working_directory.is_dir():
            raise FileNotFoundError(f"working directory not found: {working_directory}")

    async def _spawn(
        self,
        command: list[str],
        *,
        instruction: str,
        on_event: RunEventCallback | None,
    ) -> RunHandle:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        process.stdin.write(instruction.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        reader_task = asyncio.create_task(self._read_result(process, on_event=on_event))
        return CodexRunHandle(process, reader_task)

    async def _read_result(
        self,
        process: asyncio.subprocess.Process,
        *,
        on_event: RunEventCallback | None,
    ) -> AgentRunResult:
        assert process.stdout is not None
        assert process.stderr is not None

        session_id: str | None = None
        final_message: str | None = None
        quota_signal = None
        stderr_task = asyncio.create_task(process.stderr.read())

        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = {"type": "raw_output", "text": line}

            session_id = session_id or extract_session_id(event)
            final_message = extract_final_message(event) or final_message
            quota_signal = quota_signal or detect_quota_event(event)
            if on_event is not None:
                await on_event(event)

        stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
        returncode = await process.wait()
        stderr_quota = detect_quota_text(stderr)
        quota_signal = quota_signal or stderr_quota
        if returncode != 0 and stderr:
            final_message = stderr[-4000:]

        return AgentRunResult(
            returncode=returncode,
            session_id=session_id,
            final_message=final_message,
            retry_kind="quota" if quota_signal is not None else None,
            retry_at=quota_signal.reset_at if quota_signal is not None else None,
        )


def extract_session_id(event: dict) -> str | None:
    for key in ("thread_id", "session_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    thread = event.get("thread")
    if isinstance(thread, dict):
        value = thread.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def extract_final_message(event: dict) -> str | None:
    event_type = str(event.get("type", ""))
    if event_type not in {"item.completed", "turn.completed", "message.completed"}:
        return None
    item = event.get("item")
    if isinstance(item, dict):
        text = item.get("text") or item.get("content")
        if isinstance(text, str):
            return text
    text = event.get("message") or event.get("text")
    return text if isinstance(text, str) else None
