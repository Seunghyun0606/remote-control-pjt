from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.projects.operations import LocalProjectOperationExecutor, ProjectOperationError
from remote_control.qa import QAContractError, QAOrchestrator, validate_result
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    QARunRepository,
)


def _result(run_id: str, *, status: str = "PASS") -> dict:
    result = {
        "schema_version": "1.0",
        "run_id": run_id,
        "project": "demo",
        "status": status,
        "started_at": "2026-09-29T00:00:00Z",
        "finished_at": "2026-09-29T00:00:01Z",
        "preflight": "PASS",
        "build": "PASS",
        "launch": "PASS",
        "smoke": "PASS",
        "functional": "PASS",
        "ui": "PASS",
        "artifact_collection": "PASS",
        "cleanup": "PASS",
        "next_action": "NONE",
        "errors": [],
        "artifacts": [],
    }
    if status == "FAIL":
        result["functional"] = "FAIL"
        result["next_action"] = "FIX_AND_RETRY"
        result["errors"] = [
            {
                "code": "ASSERTION_FAILED",
                "message": "expected work state",
                "stage": "functional",
                "kind": "test",
                "retryable": True,
            }
        ]
    elif status == "UI_REVIEW_REQUIRED":
        result["ui"] = "REVIEW_REQUIRED"
        result["next_action"] = "REQUEST_UI_REVIEW"
    return result


def test_validate_project_os_qa_contract() -> None:
    payload = _result("QA-test-001")
    assert validate_result(payload, expected_run_id="QA-test-001")["status"] == "PASS"


def test_validate_rejects_unsafe_artifact_path() -> None:
    payload = _result("QA-test-002")
    payload["artifacts"] = [
        {"type": "screenshot", "name": "bad", "path": "../secret.png"}
    ]
    with pytest.raises(QAContractError, match="unsafe QA artifact path"):
        validate_result(payload, expected_run_id="QA-test-002")


@pytest.mark.asyncio
async def test_local_qa_collect_reads_only_registered_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = "QA-local-001"
    run_dir = root / ".qa" / "runs" / run_id
    screenshot_dir = run_dir / "screenshots"
    screenshot_dir.mkdir(parents=True)
    image_bytes = b"not-a-real-png-but-a-file"
    (screenshot_dir / "main.png").write_bytes(image_bytes)
    payload = _result(run_id)
    payload["artifacts"] = [
        {
            "type": "screenshot",
            "name": "main",
            "path": "screenshots/main.png",
            "scenario": "main-flow",
        }
    ]
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    executor = LocalProjectOperationExecutor()
    collected = await executor.execute(
        host_id="local",
        project_id="demo",
        working_directory=root,
        operation="qa_collect",
        payload={"run_id": run_id, "max_screenshots": 6},
    )

    assert collected["result"]["status"] == "PASS"
    assert base64.b64decode(collected["screenshots"][0]["data_base64"]) == image_bytes


@pytest.mark.asyncio
async def test_local_qa_collect_rejects_workspace_escape(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = "QA-local-002"
    run_dir = root / ".qa" / "runs" / run_id
    run_dir.mkdir(parents=True)
    payload = _result(run_id)
    payload["artifacts"] = [
        {"type": "screenshot", "name": "escape", "path": "../escape.png"}
    ]
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    executor = LocalProjectOperationExecutor()
    with pytest.raises(ProjectOperationError, match="unsafe QA artifact path"):
        await executor.execute(
            host_id="local",
            project_id="demo",
            working_directory=root,
            operation="qa_collect",
            payload={"run_id": run_id},
        )


class _FakeQAOperations:
    def __init__(self, status: str) -> None:
        self.status = status

    async def execute(
        self,
        *,
        host_id: str,
        project_id: str,
        working_directory: Path,
        operation: str,
        payload: dict | None = None,
    ) -> dict:
        del host_id, project_id, working_directory
        if operation == "qa_contract":
            return {
                "supported": True,
                "entrypoint": "scripts/qa.ps1",
                "schema_version": "1.0",
            }
        assert payload is not None
        run_id = str(payload["run_id"])
        result = _result(run_id, status=self.status)
        return {
            "result": result,
            "screenshots": [],
            "result_path": f".qa/runs/{run_id}/result.json",
            "exit_code": {"PASS": 0, "FAIL": 1, "UI_REVIEW_REQUIRED": 2}[self.status],
        }


@pytest.mark.asyncio
async def test_job_completion_waits_for_qa_pass(project_registry, database) -> None:
    events = EventRepository(database)
    qa_runs = QARunRepository(database)
    qa = QAOrchestrator(
        runs=qa_runs,
        events=events,
        operations=_FakeQAOperations("PASS"),
    )
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        qa=qa,
    )
    job = await manager.create(
        project_id="demo",
        instruction="do the work",
        requested_by_channel="test",
        requested_by_user="u1",
    )

    for _ in range(200):
        current = await manager.require(job.id)
        if current.state in {"COMPLETED", "FAILED"}:
            await manager.wait_until_idle(job.id)
            break
        await asyncio.sleep(0.01)

    current = await manager.require(job.id)
    qa_run = await qa_runs.latest_for_job(job.id)
    assert current.state == "COMPLETED"
    assert qa_run is not None
    assert qa_run.status == "PASS"
    assert qa_run.phase == "QA_DONE"


@pytest.mark.asyncio
async def test_qa_fail_fails_job_but_keeps_codex_session_reusable(
    project_registry,
    database,
) -> None:
    events = EventRepository(database)
    qa_runs = QARunRepository(database)
    qa = QAOrchestrator(
        runs=qa_runs,
        events=events,
        operations=_FakeQAOperations("FAIL"),
    )
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        qa=qa,
    )
    job = await manager.create(
        project_id="demo",
        instruction="do the work",
        requested_by_channel="test",
        requested_by_user="u1",
    )

    for _ in range(200):
        current = await manager.require(job.id)
        if current.state == "FAILED":
            await manager.wait_until_idle(job.id)
            break
        await asyncio.sleep(0.01)

    current = await manager.require(job.id)
    qa_run = await qa_runs.latest_for_job(job.id)
    assert current.state == "FAILED"
    assert "ASSERTION_FAILED" in (current.error or "")
    assert current.external_session_id == "fake-session"
    assert qa_run is not None and qa_run.status == "FAIL"
