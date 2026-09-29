from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Protocol

import yaml

from remote_control.executables import ExecutableResolutionError, resolve_executable
from remote_control.process_control import subprocess_group_kwargs, terminate_process_tree
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
        if operation == "qa_contract":
            return self._qa_contract(root)
        if operation == "qa_execute":
            return await self._qa_execute(root, data)
        if operation == "qa_collect":
            return self._qa_collect(root, data)
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

    def _qa_contract(self, root: Path) -> dict[str, Any]:
        entrypoint = root / "scripts" / "qa.ps1"
        return {
            "supported": entrypoint.is_file(),
            "entrypoint": "scripts/qa.ps1" if entrypoint.is_file() else None,
            "schema_version": "1.0",
        }

    async def _qa_execute(self, root: Path, data: dict[str, Any]) -> dict[str, Any]:
        run_id = _qa_run_id(data.get("run_id"))
        timeout_seconds = _positive_int(data.get("timeout_seconds"), "timeout_seconds", 900)
        entrypoint = root / "scripts" / "qa.ps1"
        if not entrypoint.is_file():
            raise ProjectOperationError("Project OS QA entrypoint not found: scripts/qa.ps1")

        shell_name = "powershell.exe" if os.name == "nt" else "pwsh"
        try:
            resolution = resolve_executable(shell_name)
        except ExecutableResolutionError:
            if os.name != "nt":
                try:
                    resolution = resolve_executable("powershell")
                except ExecutableResolutionError as exc:
                    raise ProjectOperationError(
                        "PowerShell is required to run Project OS scripts/qa.ps1"
                    ) from exc
            else:
                raise
        args = [
            "-NoProfile",
            "-NonInteractive",
        ]
        if os.name == "nt":
            args.extend(["-ExecutionPolicy", "Bypass"])
        args.extend(["-File", str(entrypoint), "-RunId", run_id])
        process = await asyncio.create_subprocess_exec(
            *resolution.build_command(args),
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_group_kwargs(),
        )
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.CancelledError:
            await terminate_process_tree(process.pid, process=process)
            raise
        except TimeoutError as exc:
            await terminate_process_tree(process.pid, process=process)
            raise ProjectOperationError(
                f"QA command timed out after {timeout_seconds}s"
            ) from exc

        collected = self._qa_collect(root, data)
        collected["exit_code"] = int(process.returncode or 0)
        collected["stdout"] = stdout_raw.decode("utf-8", errors="replace")[-32768:]
        collected["stderr"] = stderr_raw.decode("utf-8", errors="replace")[-32768:]
        return collected

    def _qa_collect(self, root: Path, data: dict[str, Any]) -> dict[str, Any]:
        run_id = _qa_run_id(data.get("run_id"))
        max_screenshots = _nonnegative_int(
            data.get("max_screenshots"), "max_screenshots", 6
        )
        max_bytes = _positive_int(
            data.get("artifact_max_bytes"), "artifact_max_bytes", 8 * 1024 * 1024
        )
        total_max_bytes = _positive_int(
            data.get("artifact_total_max_bytes"),
            "artifact_total_max_bytes",
            8 * 1024 * 1024,
        )
        run_dir = (root / ".qa" / "runs" / run_id).resolve()
        result_path = run_dir / "result.json"
        if not result_path.is_file():
            raise ProjectOperationError(
                f"QA result.json was not produced: .qa/runs/{run_id}/result.json"
            )
        try:
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectOperationError(f"malformed QA result.json: {exc}") from exc
        if not isinstance(result, dict):
            raise ProjectOperationError("QA result.json must contain a JSON object")
        if result.get("schema_version") != "1.0":
            raise ProjectOperationError(
                f"Unsupported QA schema_version: {result.get('schema_version')!r}; supported=1.0"
            )
        if result.get("run_id") != run_id:
            raise ProjectOperationError("QA result run_id does not match requested run")

        screenshots: list[dict[str, Any]] = []
        screenshot_bytes = 0
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, list):
            raise ProjectOperationError("QA result artifacts must be an array")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ProjectOperationError("QA artifact entry must be an object")
            raw_path = artifact.get("path")
            if not isinstance(raw_path, str):
                raise ProjectOperationError("QA artifact path must be a string")
            artifact_path = _safe_qa_artifact_path(run_dir, raw_path)
            if not artifact_path.is_file():
                raise ProjectOperationError(f"QA artifact not found: {raw_path}")
            if artifact.get("type") != "screenshot" or len(screenshots) >= max_screenshots:
                continue
            if artifact_path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ProjectOperationError(
                    f"unsupported screenshot file type: {artifact_path.suffix}"
                )
            size = artifact_path.stat().st_size
            if size > max_bytes:
                raise ProjectOperationError(
                    f"QA screenshot exceeds size limit: {raw_path} ({size} > {max_bytes})"
                )
            if screenshot_bytes + size > total_max_bytes:
                raise ProjectOperationError(
                    "QA screenshot set exceeds total size limit: "
                    f"{screenshot_bytes + size} > {total_max_bytes}"
                )
            screenshot_bytes += size
            screenshots.append(
                {
                    "name": str(artifact.get("name") or artifact_path.name),
                    "path": raw_path,
                    "scenario": artifact.get("scenario"),
                    "description": artifact.get("description"),
                    "media_type": artifact.get("media_type") or _image_media_type(artifact_path),
                    "size_bytes": size,
                    "data_base64": base64.b64encode(artifact_path.read_bytes()).decode("ascii"),
                }
            )
        return {
            "result": result,
            "screenshots": screenshots,
            "result_path": f".qa/runs/{run_id}/result.json",
            "exit_code": None,
        }

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
            resolution = resolve_executable(command[0])
            process_command = resolution.build_command(command[1:])
            process = await asyncio.create_subprocess_exec(
                *process_command,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except ExecutableResolutionError as exc:
            raise ProjectOperationError(str(exc)) from exc
        except OSError as exc:
            raise ProjectOperationError(
                f"failed to launch {command[0]!r}: {exc}"
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
        operation_payload = payload or {}
        remote_timeout = self.timeout_seconds
        if operation == "qa_execute":
            requested = operation_payload.get("timeout_seconds")
            if isinstance(requested, int) and not isinstance(requested, bool):
                remote_timeout = max(remote_timeout, requested + 30)
        return await self.gateway.project_operation(
            host_id=host_id,
            project_id=project_id,
            working_directory=working_directory,
            operation=operation,
            payload=operation_payload,
            timeout_seconds=remote_timeout,
        )



def _qa_run_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"QA-[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ProjectOperationError("invalid QA run_id")
    return value


def _positive_int(value: object, field: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProjectOperationError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectOperationError(f"{field} must be a non-negative integer")
    return value


def _safe_qa_artifact_path(run_dir: Path, raw_path: str) -> Path:
    if (
        not raw_path
        or "\\" in raw_path
        or raw_path.startswith("/")
        or re.match(r"^[A-Za-z]:", raw_path)
        or ".." in Path(raw_path).parts
    ):
        raise ProjectOperationError(f"unsafe QA artifact path: {raw_path}")
    resolved = (run_dir / Path(raw_path)).resolve()
    try:
        resolved.relative_to(run_dir)
    except ValueError as exc:
        raise ProjectOperationError(f"QA artifact escapes run directory: {raw_path}") from exc
    return resolved


def _image_media_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")


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
