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

QA_SCHEMA_VERSION = "2.0"
LEGACY_QA_SCHEMA_VERSION = "1.0"
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


def _iso_timestamp(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise QAContractError(f"QA result {field} must be an ISO timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QAContractError(f"QA result {field} must be an ISO timestamp") from exc


def _relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise QAContractError(f"QA {field} must be a non-empty relative path")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value) or ".." in Path(value).parts:
        raise QAContractError(f"unsafe QA {field}: {value}")
    return value


def validate_result(payload: object, *, expected_run_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise QAContractError("QA result must be a JSON object")
    if not _RUN_ID.fullmatch(expected_run_id):
        raise QAContractError("invalid requested QA run id")

    if payload.get("schemaVersion") == QA_SCHEMA_VERSION:
        return _validate_v2_result(payload, expected_run_id=expected_run_id)
    if payload.get("schema_version") == LEGACY_QA_SCHEMA_VERSION:
        return _validate_v1_result(payload, expected_run_id=expected_run_id)
    raise QAContractError(
        "Unsupported QA result schema version: "
        f"{payload.get('schemaVersion', payload.get('schema_version'))!r}; "
        "supported=2.0 (legacy 1.0)"
    )


def _validate_v2_result(payload: dict[str, Any], *, expected_run_id: str) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "project", "status", "startedAt", "finishedAt",
        "summary", "stages", "scenarios", "artifacts", "visualReviews", "errors",
        "nextAction",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise QAContractError(f"QA result missing fields: {', '.join(missing)}")
    if payload.get("runId") != expected_run_id:
        raise QAContractError("QA result runId does not match requested run")

    project = payload.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("id"), str) or not project["id"].strip():
        raise QAContractError("QA result project.id must be a non-empty string")
    _iso_timestamp(payload.get("startedAt"), "startedAt")
    _iso_timestamp(payload.get("finishedAt"), "finishedAt")

    status = payload.get("status")
    if status not in {"PASS", "PASS_WITH_WARNINGS", "FAIL", "HUMAN_GATE_REQUIRED"}:
        raise QAContractError(f"invalid QA status: {status!r}")
    next_action = payload.get("nextAction")
    if next_action not in {
        "NONE", "REVIEW_WARNINGS", "FIX_AND_RETRY", "HUMAN_GATE",
        "INVESTIGATE_ENVIRONMENT",
    }:
        raise QAContractError(f"invalid QA nextAction: {next_action!r}")

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise QAContractError("QA summary must be an object")
    for field in ("total", "passed", "failed", "warnings", "skipped", "humanGates"):
        value = summary.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise QAContractError(f"QA summary.{field} must be a non-negative integer")

    stages = payload.get("stages")
    scenarios = payload.get("scenarios")
    artifacts = payload.get("artifacts")
    visual_reviews = payload.get("visualReviews")
    errors = payload.get("errors")
    if not all(isinstance(value, list) for value in (stages, scenarios, artifacts, visual_reviews, errors)):
        raise QAContractError("QA stages/scenarios/artifacts/visualReviews/errors must be arrays")

    stage_statuses = {"PENDING", "RUNNING", "PASS", "WARN", "FAIL", "SKIPPED"}
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("id"), str) or stage.get("status") not in stage_statuses:
            raise QAContractError("invalid QA stage entry")
    scenario_statuses = stage_statuses | {"HUMAN_GATE_REQUIRED"}
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str) or scenario.get("status") not in scenario_statuses:
            raise QAContractError("invalid QA scenario entry")

    for artifact in artifacts:
        _validate_v2_artifact(artifact)
    for review in visual_reviews:
        _validate_visual_review(review)
    for error in errors:
        _validate_v2_error(error)

    if status == "PASS":
        if summary["failed"] or summary["warnings"] or summary["humanGates"] or errors or next_action != "NONE":
            raise QAContractError("PASS result contains failures, warnings, human gates, errors, or invalid nextAction")
    elif status == "PASS_WITH_WARNINGS":
        if summary["failed"] or summary["humanGates"] or summary["warnings"] < 1 or next_action != "REVIEW_WARNINGS":
            raise QAContractError("PASS_WITH_WARNINGS requires warnings and no failures/human gates")
    elif status == "HUMAN_GATE_REQUIRED":
        if summary["failed"] or summary["humanGates"] < 1 or next_action != "HUMAN_GATE":
            raise QAContractError("HUMAN_GATE_REQUIRED requires a human gate and no deterministic failure")
    elif status == "FAIL":
        if summary["failed"] < 1 and not errors:
            raise QAContractError("FAIL result must contain failed summary count or errors")
        if next_action not in {"FIX_AND_RETRY", "INVESTIGATE_ENVIRONMENT"}:
            raise QAContractError("FAIL result requires a failure nextAction")

    return payload


def _validate_v2_artifact(artifact: object) -> None:
    if not isinstance(artifact, dict):
        raise QAContractError("QA artifact must be an object")
    if artifact.get("type") not in {
        "screenshot", "visual_diff", "log", "report", "trace", "video", "metadata", "other"
    }:
        raise QAContractError(f"unsupported QA artifact type: {artifact.get('type')!r}")
    if not isinstance(artifact.get("name"), str) or not artifact["name"]:
        raise QAContractError("QA artifact name is required")
    _relative_path(artifact.get("path"), "artifact path")
    if artifact.get("type") == "screenshot":
        if artifact.get("kind") not in {"initial", "checkpoint", "result", "failure", "visual_diff"}:
            raise QAContractError("screenshot artifact requires valid kind")
        if artifact.get("priority") not in {"normal", "important", "failure"}:
            raise QAContractError("screenshot artifact requires valid priority")
        if not isinstance(artifact.get("caption"), str):
            raise QAContractError("screenshot artifact requires caption")
    if artifact.get("type") == "visual_diff":
        if artifact.get("kind") != "visual_diff":
            raise QAContractError("visual_diff artifact requires kind=visual_diff")
        if artifact.get("priority") not in {"normal", "important", "failure"}:
            raise QAContractError("visual_diff artifact requires valid priority")


def _validate_visual_review(review: object) -> None:
    if not isinstance(review, dict) or review.get("type") != "visual_review":
        raise QAContractError("invalid QA visual review")
    if review.get("status") not in {"PASS", "WARN", "FAIL", "HUMAN_GATE_REQUIRED"}:
        raise QAContractError("invalid QA visual review status")
    issues = review.get("issues")
    if not isinstance(issues, list):
        raise QAContractError("QA visual review issues must be an array")
    categories = {
        "clipping", "overlap", "alignment", "readability", "missing_asset",
        "unexpected_layout", "visual_regression", "hierarchy", "obstruction",
    }
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or issue.get("severity") not in {"info", "warning", "error", "critical"}
            or issue.get("category") not in categories
            or not isinstance(issue.get("message"), str)
            or not issue["message"]
        ):
            raise QAContractError("invalid QA visual review issue")
        if issue.get("artifact") is not None:
            _relative_path(issue.get("artifact"), "visual review artifact")


def _validate_v2_error(error: object) -> None:
    if not isinstance(error, dict):
        raise QAContractError("QA error must be an object")
    for field in ("code", "message"):
        if not isinstance(error.get(field), str) or not error[field]:
            raise QAContractError(f"QA error {field} is required")
    if error.get("kind") is not None and error.get("kind") not in {
        "test", "environment", "tooling", "configuration", "unknown"
    }:
        raise QAContractError("invalid QA error kind")
    if error.get("retryable") is not None and not isinstance(error.get("retryable"), bool):
        raise QAContractError("QA error retryable must be boolean")


def _validate_v1_result(payload: dict[str, Any], *, expected_run_id: str) -> dict[str, Any]:
    required = {
        "schema_version", "run_id", "project", "status", "started_at", "finished_at",
        "preflight", "build", "launch", "smoke", "functional", "ui",
        "artifact_collection", "cleanup", "errors", "artifacts",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise QAContractError(f"legacy QA result missing fields: {', '.join(missing)}")
    if payload.get("run_id") != expected_run_id:
        raise QAContractError("legacy QA result run_id does not match requested run")
    if payload.get("status") not in {"PASS", "FAIL", "UI_REVIEW_REQUIRED"}:
        raise QAContractError(f"invalid legacy QA status: {payload.get('status')!r}")
    if not isinstance(payload.get("errors"), list) or not isinstance(payload.get("artifacts"), list):
        raise QAContractError("legacy QA errors/artifacts must be arrays")
    return payload


def result_run_id(result: dict[str, Any]) -> str:
    return str(result.get("runId") or result.get("run_id") or "")


def result_warning_count(result: dict[str, Any]) -> int:
    if result.get("schemaVersion") == "2.0":
        summary = result.get("summary")
        return int(summary.get("warnings", 0)) if isinstance(summary, dict) else 0
    metadata = result.get("metadata")
    raw = metadata.get("warning_count") if isinstance(metadata, dict) else 0
    return int(raw) if isinstance(raw, int) and raw >= 0 else 0


def result_failed_scenarios(result: dict[str, Any]) -> list[str]:
    if result.get("schemaVersion") == "2.0":
        scenarios = result.get("scenarios")
        if not isinstance(scenarios, list):
            return []
        return sorted({
            str(item["id"])
            for item in scenarios
            if isinstance(item, dict)
            and item.get("status") == "FAIL"
            and isinstance(item.get("id"), str)
            and item["id"]
        })
    metadata = result.get("metadata")
    raw = metadata.get("failed_scenarios") if isinstance(metadata, dict) else None
    return sorted({str(item) for item in raw if str(item).strip()}) if isinstance(raw, list) else []


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
        contract_version = contract.get("schema_version")
        if contract_version not in {QA_SCHEMA_VERSION, LEGACY_QA_SCHEMA_VERSION}:
            raise QAContractError(
                f"Unsupported QA schema version: {contract_version!r}; "
                f"supported={QA_SCHEMA_VERSION} (legacy {LEGACY_QA_SCHEMA_VERSION})"
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
                    result_path=None,
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
        expected_exit = (
            {"PASS": 0, "PASS_WITH_WARNINGS": 0, "FAIL": 1, "HUMAN_GATE_REQUIRED": 2}[result["status"]]
            if result.get("schemaVersion") == "2.0"
            else {"PASS": 0, "FAIL": 1, "UI_REVIEW_REQUIRED": 2}[result["status"]]
        )
        if isinstance(exit_code, int) and exit_code != expected_exit:
            raise QAContractError(
                f"QA exit code/status mismatch: exit={exit_code} status={result['status']}"
            )

        failed_scenarios = result_failed_scenarios(result)
        warning_count = result_warning_count(result)

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

        phase = (
            "QA_REVIEWING"
            if result["status"] in {"HUMAN_GATE_REQUIRED", "UI_REVIEW_REQUIRED"}
            else "QA_DONE"
            if result["status"] in {"PASS", "PASS_WITH_WARNINGS"}
            else "QA_FAILED"
        )
        run = await self.runs.update(
            run_id,
            phase=phase,
            status=result["status"],
            result_path=(
                str(response.get("result_path"))
                if isinstance(response.get("result_path"), str)
                else run.result_path
            ),
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
