from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Protocol

import yaml

from remote_control.transport.runner_ws import RunnerGateway

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ProjectOperationError(RuntimeError):
    pass


class ProjectOperationExecutor(Protocol):
    async def execute(
        self,
        *,
        host_id: str,
        project_id: str,
        working_directory: Path,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


class LocalProjectOperationExecutor:
    def __init__(
        self,
        *,
        projectctl_executable: str = "projectctl",
        git_executable: str = "git",
        timeout_seconds: int = 30,
    ) -> None:
        self.projectctl_executable = projectctl_executable
        self.git_executable = git_executable
        self.timeout_seconds = max(timeout_seconds, 1)

    async def execute(
        self,
        *,
        host_id: str,
        project_id: str,
        working_directory: Path,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del host_id, project_id
        root = working_directory.expanduser()
        if not root.exists() or not root.is_dir():
            raise ProjectOperationError(f"working directory not found: {root}")
        data = payload or {}

        if operation == "git_snapshot":
            return await self._git_snapshot(root)
        if operation == "project_os_status":
            return await self._projectctl_json(root, ["status", "--json"])
        if operation == "project_os_next":
            role = _safe_token(data.get("role"), "role")
            result = await self._run(
                root,
                [self.projectctl_executable, "next", "--role", role, "--json"],
                allowed_returncodes={0, 2},
            )
            if result["returncode"] == 2:
                return {"task": None}
            parsed = _json_object(result["stdout"], "projectctl next")
            return {"task": parsed}
        if operation == "project_os_context":
            task_id = _safe_token(data.get("task_id"), "task_id")
            role = _safe_token(data.get("role"), "role")
            return await self._projectctl_json(
                root,
                ["context", task_id, "--role", role],
            )
        if operation == "project_os_claim":
            task_id = _safe_token(data.get("task_id"), "task_id")
            role = _safe_token(data.get("role"), "role")
            result = await self._run(
                root,
                [self.projectctl_executable, "claim", task_id, "--role", role],
            )
            return {"message": result["stdout"].strip()}
        if operation == "project_os_submit":
            task_id = _safe_token(data.get("task_id"), "task_id")
            role = _safe_token(data.get("role"), "role")
            actor = _safe_token(data.get("actor"), "actor")
            result_payload = data.get("result")
            if not isinstance(result_payload, dict):
                raise ProjectOperationError("project_os_submit requires a mapping result")
            return await self._projectctl_submit(
                root,
                task_id=task_id,
                role=role,
                actor=actor,
                result_payload=result_payload,
            )
        raise ProjectOperationError(f"unsupported project operation: {operation}")

    async def _git_snapshot(self, root: Path) -> dict[str, Any]:
        branch = await self._run(
            root,
            [self.git_executable, "branch", "--show-current"],
        )
        status = await self._run(
            root,
            [self.git_executable, "status", "--short", "--branch"],
        )
        diff = await self._run(
            root,
            [self.git_executable, "diff", "--stat"],
        )
        return {
            "branch": branch["stdout"].strip(),
            "status": status["stdout"].strip(),
            "diff_stat": diff["stdout"].strip(),
        }

    async def _projectctl_json(self, root: Path, args: list[str]) -> dict[str, Any]:
        result = await self._run(root, [self.projectctl_executable, *args])
        return _json_object(result["stdout"], f"projectctl {' '.join(args)}")

    async def _projectctl_submit(
        self,
        root: Path,
        *,
        task_id: str,
        role: str,
        actor: str,
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".yaml",
                prefix="remote-control-project-os-",
                delete=False,
            ) as handle:
                yaml.safe_dump(
                    result_payload,
                    handle,
                    sort_keys=False,
                    allow_unicode=True,
                )
                temp_path = Path(handle.name)

            result = await self._run(
                root,
                [
                    self.projectctl_executable,
                    "submit",
                    task_id,
                    str(temp_path),
                    "--role",
                    role,
                    "--actor",
                    actor,
                ],
            )
            return {"message": result["stdout"].strip()}
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    async def _run(
        self,
        root: Path,
        command: list[str],
        *,
        allowed_returncodes: set[int] | None = None,
    ) -> dict[str, Any]:
        allowed = allowed_returncodes or {0}
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ProjectOperationError(
                f"required executable not found: {command[0]}"
            ) from exc

        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProjectOperationError(
                f"project operation timed out after {self.timeout_seconds}s"
            ) from exc

        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")
        if process.returncode not in allowed:
            detail = (stderr or stdout).strip()[-4000:]
            raise ProjectOperationError(
                f"{command[0]} operation failed with code {process.returncode}: {detail}"
            )
        return {
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }


class HybridProjectOperationExecutor:
    def __init__(
        self,
        *,
        local_host_id: str,
        local: LocalProjectOperationExecutor,
        gateway: RunnerGateway,
        timeout_seconds: int = 30,
    ) -> None:
        self.local_host_id = local_host_id
        self.local = local
        self.gateway = gateway
        self.timeout_seconds = max(timeout_seconds, 1)

    async def execute(
        self,
        *,
        host_id: str,
        project_id: str,
        working_directory: Path,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if host_id == self.local_host_id:
            return await self.local.execute(
                host_id=host_id,
                project_id=project_id,
                working_directory=working_directory,
                operation=operation,
                payload=payload,
            )
        return await self.gateway.project_operation(
            host_id=host_id,
            project_id=project_id,
            working_directory=working_directory,
            operation=operation,
            payload=payload or {},
            timeout_seconds=self.timeout_seconds,
        )


def _safe_token(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
        raise ProjectOperationError(f"invalid {field}")
    return value


def _json_object(text: str, source: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectOperationError(f"{source} did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProjectOperationError(f"{source} did not return a JSON object")
    return payload
