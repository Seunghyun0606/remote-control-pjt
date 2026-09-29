from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from remote_control.projects.operations import ProjectOperationExecutor
from remote_control.storage.models import QARunRecord
from remote_control.storage.repositories import EventRepository, QARunRepository

QA_SCHEMA_VERSION = "1.0"
_RUN_ID = re.compile(r"^QA-[A-Za-z0-9][A-Za-z0-9._-]*$")

class QAContractError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QAOutcome:
    supported: bool
    run: QARunRecord | None
    result: dict[str, Any] | None
    screenshots: tuple[dict[str, Any], ...] = ()


def _run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"QA-{stamp}-{uuid4().hex[:6].upper()}"


def validate_result(payload: object, *, expected_run_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise QAContractError("QA result must be a JSON object")
    required = {
        "schema_version", "run_id", "project", "status", "started_at", "finished_at",
        "preflight", "build", "launch", "smoke", "functional", "ui",
        "artifact_collection", "cleanup", "errors", "artifacts",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise QAContractError(f"QA result missing fields: {', '.join(missing)}")
    if payload.get("schema_version") != QA_SCHEMA_VERSION:
        raise QAContractError(
            f"Unsupported QA schema_version: {payload.get('schema_version')!r}; "
            f"supported={QA_SCHEMA_VERSION}"
        )
    if payload.get("run_id") != expected_run_id or not _RUN_ID.fullmatch(expected_run_id):
        raise QAContractError("QA result run_id does not match requested run")
    project = payload.get("project")
    if not isinstance(project, str) or not project.strip():
        raise QAContractError("QA result project must be a non-empty string")
    for field in ("started_at", "finished_at"):
        value = payload.get(field)
        if not isinstance(value, str):
            raise QAContractError(f"QA result {field} must be an ISO timestamp")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise QAContractError(f"QA result {field} must be an ISO timestamp") from exc

    status = payload.get("status")
    if status not in {"PASS", "FAIL", "UI_REVIEW_REQUIRED"}:
        raise QAContractError(f"invalid QA status: {status!r}")
    next_action = payload.get("next_action")
    if next_action not in {"NONE", "FIX_AND_RETRY", "REQUEST_UI_REVIEW"}:
        raise QAContractError(f"invalid QA next_action: {next_action!r}")

    automated_fields = (
        "preflight", "build", "launch", "smoke", "functional",
        "artifact_collection", "cleanup",
    )
    automated = {"PASS", "FAIL", "SKIPPED"}
    for field in automated_fields:
        if payload.get(field) not in automated:
            raise QAContractError(f"invalid QA stage {field}: {payload.get(field)!r}")
    if payload.get("ui") not in {"PASS", "FAIL", "SKIPPED", "REVIEW_REQUIRED"}:
        raise QAContractError(f"invalid QA ui stage: {payload.get('ui')!r}")

    errors = payload.get("errors")
    artifacts = payload.get("artifacts")
    if not isinstance(errors, list) or not isinstance(artifacts, list):
        raise QAContractError("QA errors/artifacts must be arrays")
    for error in errors:
        _validate_error(error)
    for artifact in artifacts:
        _validate_artifact(artifact)

    if status == "FAIL" and not errors:
        raise QAContractError("FAIL result must contain at least one error")
    if status == "PASS":
        failed = [
            field
            for field in (*automated_fields, "ui")
            if payload.get(field) in {"FAIL", "REVIEW_REQUIRED"}
        ]
        if failed:
            raise QAContractError(f"PASS result contains non-passing stages: {failed}")
        if next_action != "NONE":
            raise QAContractError("PASS result requires next_action=NONE")
    if status == "UI_REVIEW_REQUIRED":
        if payload.get("ui") != "REVIEW_REQUIRED" or next_action != "REQUEST_UI_REVIEW":
            raise QAContractError(
                "UI_REVIEW_REQUIRED requires ui=REVIEW_REQUIRED "
                "and next_action=REQUEST_UI_REVIEW"
            )
    return payload


def _validate_error(error: object) -> None:
    if not isinstance(error, dict):
        raise QAContractError("QA error must be an object")
    for field in ("code", "message", "stage"):
        if not isinstance(error.get(field), str) or not error[field]:
            raise QAContractError(f"QA error {field} is required")
    kind = error.get("kind")
    if kind is not None and kind not in {
        "test", "environment", "tooling", "configuration", "unknown"
    }:
        raise QAContractError(f"invalid QA error kind: {kind!r}")
    retryable = error.get("retryable")
    if retryable is not None and not isinstance(retryable, bool):
        raise QAContractError("QA error retryable must be boolean")


def _validate_artifact(artifact: object) -> None:
    if not isinstance(artifact, dict):
        raise QAContractError("QA artifact must be an object")
    if artifact.get("type") not in {"screenshot", "video", "log", "report", "trace", "other"}:
        raise QAContractError(f"unsupported QA artifact type: {artifact.get('type')!r}")
    for field in ("name", "path"):
        if not isinstance(artifact.get(field), str) or not artifact[field]:
            raise QAContractError(f"QA artifact {field} is required")
    raw = artifact["path"]
    if "\\" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise QAContractError(f"unsafe QA artifact path: {raw}")
    parts = Path(raw).parts
    if ".." in parts:
        raise QAContractError(f"unsafe QA artifact path: {raw}")


class QAOrchestrator:
    def __init__(
        self,
        *,
        runs: QARunRepository,
        events: EventRepository,
        operations: ProjectOperationExecutor,
        enabled: bool = True,
        timeout_seconds: int = 900,
        screenshot_enabled: bool = True,
        max_screenshots: int = 6,
        artifact_max_bytes: int = 8 * 1024 * 1024,
        artifact_total_max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.runs = runs
        self.events = events
        self.operations = operations
        self.enabled = enabled
        self.timeout_seconds = max(timeout_seconds, 1)
        self.screenshot_enabled = screenshot_enabled
        self.max_screenshots = max(max_screenshots, 0)
        self.artifact_max_bytes = max(artifact_max_bytes, 1)
        self.artifact_total_max_bytes = max(artifact_total_max_bytes, 1)

    async def latest_for_job(self, job_id: str) -> QARunRecord | None:
        return await self.runs.latest_for_job(job_id)

    async def execute(
        self,
        *,
        job_id: str,
        project_id: str,
        host_id: str,
        working_directory: Path,
        reuse_run_id: str | None = None,
        collect_only: bool = False,
    ) -> QAOutcome:
        if not self.enabled:
            return QAOutcome(False, None, None)

        contract = await self.operations.execute(
            host_id=host_id,
            project_id=project_id,
            working_directory=working_directory,
            operation="qa_contract",
            payload={},
        )
        if not bool(contract.get("supported")):
            return QAOutcome(False, None, None)
        if contract.get("schema_version") != QA_SCHEMA_VERSION:
            raise QAContractError(
                f"Unsupported QA schema version: {contract.get('schema_version')!r}; "
                f"supported={QA_SCHEMA_VERSION}"
            )

        run_id = reuse_run_id or _run_id()
        run = await self.runs.get(run_id)
        if run is None:
            run = await self.runs.add(
                QARunRecord(
                    run_id=run_id,
                    job_id=job_id,
                    project_id=project_id,
                    host_id=host_id,
                    phase="QA_PREPARING",
                    status=None,
                    result_path=f".qa/runs/{run_id}/result.json",
                )
            )
            await self.events.append(
                "QA_RUN_STARTED", job_id=job_id, project_id=project_id, host_id=host_id,
                payload={"qa_run_id": run_id, "schema_version": QA_SCHEMA_VERSION},
            )

        operation = "qa_collect" if collect_only else "qa_execute"
        await self.runs.update(run_id, phase="QA_COLLECTING" if collect_only else "QA_RUNNING", error=None)
        response = await self.operations.execute(
            host_id=host_id,
            project_id=project_id,
            working_directory=working_directory,
            operation=operation,
            payload={
                "run_id": run_id,
                "timeout_seconds": self.timeout_seconds,
                "max_screenshots": self.max_screenshots if self.screenshot_enabled else 0,
                "artifact_max_bytes": self.artifact_max_bytes,
                "artifact_total_max_bytes": self.artifact_total_max_bytes,
            },
        )
        result = validate_result(response.get("result"), expected_run_id=run_id)
        exit_code = response.get("exit_code")
        expected_exit = {"PASS": 0, "FAIL": 1, "UI_REVIEW_REQUIRED": 2}[result["status"]]
        if isinstance(exit_code, int) and exit_code != expected_exit:
            raise QAContractError(
                f"QA exit code/status mismatch: exit={exit_code} status={result['status']}"
            )

        metadata = result.get("metadata")
        failed_scenarios: list[str] = []
        warning_count = 0
        if isinstance(metadata, dict):
            raw_failed = metadata.get("failed_scenarios")
            if isinstance(raw_failed, list):
                failed_scenarios = sorted({str(item) for item in raw_failed if str(item).strip()})
            raw_warning_count = metadata.get("warning_count")
            if isinstance(raw_warning_count, int) and raw_warning_count >= 0:
                warning_count = raw_warning_count

        screenshots: list[dict[str, Any]] = []
        raw_screenshots = response.get("screenshots")
        if isinstance(raw_screenshots, list):
            for item in raw_screenshots:
                if not isinstance(item, dict):
                    continue
                encoded = item.get("data_base64")
                if not isinstance(encoded, str):
                    continue
                try:
                    base64.b64decode(encoded, validate=True)
                except Exception as exc:
                    raise QAContractError("invalid screenshot payload from QA host") from exc
                screenshots.append(item)

        phase = "QA_REVIEWING" if result["status"] == "UI_REVIEW_REQUIRED" else (
            "QA_DONE" if result["status"] == "PASS" else "QA_FAILED"
        )
        run = await self.runs.update(
            run_id,
            phase=phase,
            status=result["status"],
            result_path=f".qa/runs/{run_id}/result.json",
            failed_scenarios_json=json.dumps(failed_scenarios, ensure_ascii=False),
            warning_count=warning_count,
            artifact_count=len(result["artifacts"]),
            artifacts_json=json.dumps(result["artifacts"], ensure_ascii=False),
            exit_code=exit_code if isinstance(exit_code, int) else None,
            error=None,
            finished_at=datetime.now(timezone.utc),
        )
        await self.events.append(
            "QA_RUN_RESULT", job_id=job_id, project_id=project_id, host_id=host_id,
            payload={
                "qa_run_id": run_id,
                "status": result["status"],
                "phase": phase,
                "artifact_count": len(result["artifacts"]),
                "warning_count": warning_count,
            },
        )
        return QAOutcome(True, run, result, tuple(screenshots))

    async def collect_existing(
        self,
        *,
        run: QARunRecord,
        working_directory: Path,
    ) -> QAOutcome:
        return await self.execute(
            job_id=run.job_id,
            project_id=run.project_id,
            host_id=run.host_id,
            working_directory=working_directory,
            reuse_run_id=run.run_id,
            collect_only=True,
        )
