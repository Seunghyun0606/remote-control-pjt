from __future__ import annotations

import asyncio
import json
import os
import platform
from pathlib import Path

from remote_control.executables import ExecutableResolutionError, resolve_executable
from remote_control.recovery.quota import detect_quota_event, detect_quota_text
from remote_control.runners.base import AgentRunResult, AgentRunner, RunEventCallback, RunHandle


WINDOWS_UTF8_GUIDANCE = (
    "Windows UTF-8 I/O rule: repository text is UTF-8. "
    "Avoid PowerShell text aliases/cmdlets that can fall back to the legacy ANSI code page. "
    "Prefer rg, git, or Python with explicit UTF-8 for text reads/writes. "
    "Use a UTF-8-safe read on the first attempt; do not probe with legacy PowerShell and then retry. "
    "If Windows PowerShell text cmdlets are necessary, pass -Encoding UTF8 explicitly. "
    "Do not rewrite files merely because shell output is mojibake."
)


def prepare_codex_instruction(
    instruction: str,
    *,
    os_name: str | None = None,
) -> str:
    system = (os_name or platform.system()).casefold()
    if system != "windows":
        return instruction
    return f"{instruction.rstrip()}\n\n---\n\n{WINDOWS_UTF8_GUIDANCE}\n"


def build_codex_environment(
    *,
    codex_home: str | None,
    os_name: str | None = None,
) -> dict[str, str]:
    child_env = os.environ.copy()
    system = (os_name or platform.system()).casefold()
    if system == "windows":
        # These cover Python-based fallbacks and keep the launched Codex process
        # in a UTF-8-oriented environment. PowerShell file cmdlets still need
        # explicit UTF-8 handling, which is enforced by the instruction guard.
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
    if codex_home:
        child_env["CODEX_HOME"] = codex_home
    return child_env


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
        codex_home: str | None = None,
    ) -> None:
        self.executable = executable
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.codex_home = codex_home

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
            expected_session_id=session_id,
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
        expected_session_id: str | None = None,
    ) -> RunHandle:
        try:
            resolution = resolve_executable(command[0])
            process_command = resolution.build_command(command[1:])
            child_env = build_codex_environment(codex_home=self.codex_home)
            process = await asyncio.create_subprocess_exec(
                *process_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
        except ExecutableResolutionError:
            raise
        except OSError as exc:
            resolved = (
                resolution.resolved
                if "resolution" in locals()
                else command[0]
            )
            raise RuntimeError(
                "Codex executable launch failed: "
                f"configured={command[0]!r}, resolved={resolved!r}, "
                f"error={exc}"
            ) from exc
        assert process.stdin is not None
        prepared_instruction = prepare_codex_instruction(instruction)
        process.stdin.write(prepared_instruction.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        reader_task = asyncio.create_task(
            self._read_result(
                process,
                on_event=on_event,
                expected_session_id=expected_session_id,
            )
        )
        return CodexRunHandle(process, reader_task)

    async def _read_result(
        self,
        process: asyncio.subprocess.Process,
        *,
        on_event: RunEventCallback | None,
        expected_session_id: str | None = None,
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

            event_session_id = extract_session_id(event)
            if (
                expected_session_id
                and event_session_id
                and event_session_id != expected_session_id
            ):
                session_id = event_session_id
                final_message = (
                    "SESSION_IDENTITY_MISMATCH "
                    f"expected={expected_session_id} actual={event_session_id}"
                )
                if process.returncode is None:
                    process.terminate()
                break

            session_id = session_id or event_session_id
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

        if (
            expected_session_id
            and session_id
            and session_id != expected_session_id
        ):
            returncode = returncode if returncode != 0 else 65
            final_message = (
                final_message
                or "SESSION_IDENTITY_MISMATCH "
                f"expected={expected_session_id} actual={session_id}"
            )

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
