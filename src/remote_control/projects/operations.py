from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
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
        manifest_path = root / ".qa" / "manifest.yaml"
        if manifest_path.is_file():
            manifest = _load_qa_manifest(manifest_path)
            return {
                "supported": True,
                "contract_version": "2.0",
                "schema_version": "2.0",
                "entrypoint": ".qa/manifest.yaml",
                "manifest": manifest,
            }

        entrypoint = root / "scripts" / "qa.ps1"
        return {
            "supported": entrypoint.is_file(),
            "contract_version": "1.0" if entrypoint.is_file() else None,
            "schema_version": "1.0" if entrypoint.is_file() else None,
            "entrypoint": "scripts/qa.ps1" if entrypoint.is_file() else None,
            "manifest": None,
        }

    async def _qa_execute(self, root: Path, data: dict[str, Any]) -> dict[str, Any]:
        run_id = _qa_run_id(data.get("run_id"))
        contract = self._qa_contract(root)
        if not contract["supported"]:
            raise ProjectOperationError("Project OS QA contract not found")

        if contract["contract_version"] == "2.0":
            manifest = contract["manifest"]
            assert isinstance(manifest, dict)
            qa = manifest["qa"]
            commands = qa["command"]
            command = commands.get("windows" if os.name == "nt" else "unix")
            if not isinstance(command, str) or not command.strip():
                raise ProjectOperationError(
                    f"QA manifest does not define a command for {'windows' if os.name == 'nt' else 'unix'}"
                )
            manifest_timeout = qa.get("timeoutSeconds")
            requested_timeout = _positive_int(
                data.get("timeout_seconds"), "timeout_seconds", 900
            )
            timeout_seconds = (
                min(requested_timeout, manifest_timeout)
                if isinstance(manifest_timeout, int) and manifest_timeout > 0
                else requested_timeout
            )
            argv = _manifest_command_argv(root, command, run_id=run_id)
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_group_kwargs(),
            )
        else:
            timeout_seconds = _positive_int(
                data.get("timeout_seconds"), "timeout_seconds", 900
            )
            entrypoint = root / "scripts" / "qa.ps1"
            shell_name = "powershell.exe" if os.name == "nt" else "pwsh"
            try:
                resolution = resolve_executable(shell_name)
            except ExecutableResolutionError:
                if os.name != "nt":
                    try:
                        resolution = resolve_executable("powershell")
                    except ExecutableResolutionError as exc:
                        raise ProjectOperationError(
                            "PowerShell is required to run legacy Project OS scripts/qa.ps1"
                        ) from exc
                else:
                    raise
            args = ["-NoProfile", "-NonInteractive"]
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
        contract = self._qa_contract(root)
        if not contract["supported"]:
            raise ProjectOperationError("Project OS QA contract not found")

        if contract["contract_version"] == "2.0":
            manifest = contract["manifest"]
            assert isinstance(manifest, dict)
            result_template = manifest["artifacts"]["result"]
            result_relative = _substitute_run_id(result_template, run_id)
            result_path = _safe_workspace_relative_path(root, result_relative)
            schema_version = "2.0"
        else:
            result_relative = f".qa/runs/{run_id}/result.json"
            result_path = _safe_workspace_relative_path(root, result_relative)
            schema_version = "1.0"

        if not result_path.is_file():
            raise ProjectOperationError(
                f"QA result.json was not produced: {result_relative}"
            )
        try:
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectOperationError(f"malformed QA result.json: {exc}") from exc
        if not isinstance(result, dict):
            raise ProjectOperationError("QA result.json must contain a JSON object")

        if schema_version == "2.0":
            if result.get("schemaVersion") != "2.0":
                raise ProjectOperationError(
                    f"Unsupported QA schemaVersion: {result.get('schemaVersion')!r}; supported=2.0"
                )
            if result.get("runId") != run_id:
                raise ProjectOperationError("QA result runId does not match requested run")
        else:
            if result.get("schema_version") != "1.0":
                raise ProjectOperationError(
                    f"Unsupported QA schema_version: {result.get('schema_version')!r}; supported=1.0"
                )
            if result.get("run_id") != run_id:
                raise ProjectOperationError("QA result run_id does not match requested run")

        artifacts = result.get("artifacts")
        if not isinstance(artifacts, list):
            raise ProjectOperationError("QA result artifacts must be an array")

        candidates: list[tuple[tuple[int, int, int], dict[str, Any], Path]] = []
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                raise ProjectOperationError("QA artifact entry must be an object")
            raw_path = artifact.get("path")
            if not isinstance(raw_path, str):
                raise ProjectOperationError("QA artifact path must be a string")
            artifact_path = (
                _safe_workspace_relative_path(root, raw_path)
                if schema_version == "2.0"
                else _safe_legacy_qa_artifact_path(root, run_id, raw_path)
            )
            if not artifact_path.is_file():
                raise ProjectOperationError(f"QA artifact not found: {raw_path}")
            if artifact.get("type") != "screenshot":
                continue

            priority = artifact.get("priority") if schema_version == "2.0" else None
            kind = artifact.get("kind") if schema_version == "2.0" else None
            priority_rank = {"failure": 0, "important": 1, "normal": 2}.get(priority, 2)
            kind_rank = {
                "failure": 0,
                "result": 1,
                "checkpoint": 2,
                "initial": 3,
                "visual_diff": 4,
            }.get(kind, 5)
            candidates.append(((priority_rank, kind_rank, index), artifact, artifact_path))

        candidates.sort(key=lambda item: item[0])
        screenshots: list[dict[str, Any]] = []
        screenshot_bytes = 0
        for _rank, artifact, artifact_path in candidates[:max_screenshots]:
            raw_path = str(artifact["path"])
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
                    "scenario": artifact.get("scenarioId") or artifact.get("scenario"),
                    "description": artifact.get("caption") or artifact.get("description"),
                    "caption": artifact.get("caption"),
                    "kind": artifact.get("kind"),
                    "priority": artifact.get("priority"),
                    "media_type": artifact.get("mediaType")
                    or artifact.get("media_type")
                    or _image_media_type(artifact_path),
                    "size_bytes": size,
                    "data_base64": base64.b64encode(artifact_path.read_bytes()).decode("ascii"),
                }
            )
        return {
            "result": result,
            "screenshots": screenshots,
            "result_path": result_relative,
            "exit_code": None,
            "contract_version": schema_version,
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


def _load_qa_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProjectOperationError(f"malformed QA manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectOperationError("QA manifest must contain a mapping")
    if payload.get("schemaVersion") != "2.0":
        raise ProjectOperationError(
            f"Unsupported QA manifest schemaVersion: {payload.get('schemaVersion')!r}; supported=2.0"
        )
    qa = payload.get("qa")
    artifacts = payload.get("artifacts")
    if not isinstance(qa, dict) or not isinstance(artifacts, dict):
        raise ProjectOperationError("QA manifest requires qa and artifacts mappings")
    command = qa.get("command")
    stages = qa.get("stages")
    if not isinstance(command, dict) or not command:
        raise ProjectOperationError("QA manifest qa.command must be a non-empty mapping")
    if not isinstance(stages, list) or not stages or not all(isinstance(x, str) and x for x in stages):
        raise ProjectOperationError("QA manifest qa.stages must be a non-empty string array")
    for key in ("result", "screenshots", "logs", "visual"):
        value = artifacts.get(key)
        if not isinstance(value, str) or not value:
            raise ProjectOperationError(f"QA manifest artifacts.{key} is required")
        _validate_relative_contract_path(value, f"artifacts.{key}")
    optional_metadata = artifacts.get("metadata")
    if optional_metadata is not None:
        if not isinstance(optional_metadata, str) or not optional_metadata:
            raise ProjectOperationError("QA manifest artifacts.metadata must be a path")
        _validate_relative_contract_path(optional_metadata, "artifacts.metadata")
    for os_key in ("windows", "unix"):
        value = command.get(os_key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ProjectOperationError(f"QA manifest qa.command.{os_key} must be a non-empty string")
    timeout = qa.get("timeoutSeconds")
    if timeout is not None and (
        not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0
    ):
        raise ProjectOperationError("QA manifest qa.timeoutSeconds must be a positive integer")
    return payload


def _manifest_command_argv(root: Path, command: str, *, run_id: str) -> list[str]:
    substituted = command.replace("{runId}", run_id)
    try:
        argv = shlex.split(substituted, posix=os.name != "nt")
    except ValueError as exc:
        raise ProjectOperationError(f"invalid QA command: {exc}") from exc
    if not argv:
        raise ProjectOperationError("QA command is empty")

    executable = argv[0]
    if "/" in executable or "\\" in executable:
        if "\\" in executable or executable.startswith("/") or re.match(r"^[A-Za-z]:", executable):
            raise ProjectOperationError("QA command executable must be workspace-relative or on PATH")
        resolved = _safe_workspace_relative_path(root, executable.removeprefix("./"))
        if not resolved.is_file():
            raise ProjectOperationError(f"QA command executable not found: {executable}")
        argv[0] = str(resolved)
    else:
        try:
            resolution = resolve_executable(executable)
        except ExecutableResolutionError as exc:
            raise ProjectOperationError(str(exc)) from exc
        argv = resolution.build_command(argv[1:])
    return argv


def _substitute_run_id(value: str, run_id: str) -> str:
    return value.replace("{runId}", run_id)


def _validate_relative_contract_path(raw_path: str, field: str) -> None:
    if (
        "\\" in raw_path
        or raw_path.startswith("/")
        or re.match(r"^[A-Za-z]:", raw_path)
        or ".." in Path(raw_path).parts
    ):
        raise ProjectOperationError(f"unsafe QA manifest path {field}: {raw_path}")


def _safe_workspace_relative_path(root: Path, raw_path: str) -> Path:
    _validate_relative_contract_path(raw_path, "path")
    root_resolved = root.resolve()
    resolved = (root_resolved / Path(raw_path)).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ProjectOperationError(f"QA path escapes workspace: {raw_path}") from exc
    return resolved


def _safe_legacy_qa_artifact_path(root: Path, run_id: str, raw_path: str) -> Path:
    if (
        not raw_path
        or "\\" in raw_path
        or raw_path.startswith("/")
        or re.match(r"^[A-Za-z]:", raw_path)
        or ".." in Path(raw_path).parts
    ):
        raise ProjectOperationError(f"unsafe QA artifact path: {raw_path}")
    run_dir = (root / ".qa" / "runs" / run_id).resolve()
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
