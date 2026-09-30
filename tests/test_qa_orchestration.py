from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from remote_control.approvals.registry import ApprovalRegistry
from remote_control.controller.job_manager import JobManager
from remote_control.projects.operations import LocalProjectOperationExecutor, ProjectOperationError
from remote_control.qa import QAContractError, QAOrchestrator, validate_result
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.repositories import (
    ApprovalRepository,
    EventRepository,
    JobRepository,
    QARunRepository,
)


def _result(run_id: str, *, status: str = "PASS") -> dict:
    warnings = 1 if status == "PASS_WITH_WARNINGS" else 0
    failed = 1 if status == "FAIL" else 0
    gates = 1 if status == "HUMAN_GATE_REQUIRED" else 0
    stage_status = (
        "FAIL" if status == "FAIL"
        else "WARN" if status == "PASS_WITH_WARNINGS"
        else "PASS"
    )
    scenario_status = (
        "HUMAN_GATE_REQUIRED" if status == "HUMAN_GATE_REQUIRED"
        else stage_status
    )
    result = {
        "schemaVersion": "2.0",
        "runId": run_id,
        "project": {"id": "demo", "name": "Demo"},
        "status": status,
        "startedAt": "2026-09-29T00:00:00Z",
        "finishedAt": "2026-09-29T00:00:01Z",
        "summary": {
            "total": 1,
            "passed": 1 if status == "PASS" else 0,
            "failed": failed,
            "warnings": warnings,
            "skipped": 0,
            "humanGates": gates,
        },
        "stages": [{"id": "functional", "status": stage_status}],
        "scenarios": [{"id": "main-flow", "status": scenario_status, "required": True}],
        "artifacts": [],
        "visualReviews": [],
        "errors": [],
        "nextAction": {
            "PASS": "NONE",
            "PASS_WITH_WARNINGS": "REVIEW_WARNINGS",
            "FAIL": "FIX_AND_RETRY",
            "HUMAN_GATE_REQUIRED": "HUMAN_GATE",
        }[status],
    }
    if status == "FAIL":
        result["errors"] = [
            {
                "code": "ASSERTION_FAILED",
                "message": "expected work state",
                "stage": "functional",
                "kind": "test",
                "retryable": True,
            }
        ]
    return result


def _legacy_result(run_id: str, *, status: str = "PASS") -> dict:
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
        result["errors"] = [{"code": "ASSERTION_FAILED", "message": "failed", "stage": "functional"}]
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
        {
            "type": "screenshot",
            "name": "bad",
            "path": "../secret.png",
            "caption": "bad",
            "kind": "failure",
            "priority": "failure",
        }
    ]
    with pytest.raises(QAContractError, match="unsafe QA artifact path"):
        validate_result(payload, expected_run_id="QA-test-002")


@pytest.mark.asyncio
async def test_local_qa_collect_reads_only_registered_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "qa.ps1").write_text("# legacy QA test entrypoint\n", encoding="utf-8")
    run_id = "QA-local-001"
    run_dir = root / ".qa" / "runs" / run_id
    screenshot_dir = run_dir / "screenshots"
    screenshot_dir.mkdir(parents=True)
    image_bytes = b"not-a-real-png-but-a-file"
    (screenshot_dir / "main.png").write_bytes(image_bytes)
    payload = _legacy_result(run_id)
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
async def test_v2_manifest_collects_repository_relative_screenshot_by_priority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = "QA-v2-001"
    qa_dir = root / ".qa"
    run_dir = qa_dir / "runs" / run_id
    screenshot_dir = run_dir / "screenshots"
    screenshot_dir.mkdir(parents=True)
    (qa_dir / "manifest.yaml").write_text(
        """
schemaVersion: "2.0"
qa:
  command:
    unix: "./.qa/scripts/run-qa.sh --run-id {runId}"
  stages: [smoke, ui]
artifacts:
  result: ".qa/runs/{runId}/result.json"
  screenshots: ".qa/runs/{runId}/screenshots"
  logs: ".qa/runs/{runId}/logs"
  visual: ".qa/runs/{runId}/visual"
""".strip(),
        encoding="utf-8",
    )
    normal = b"normal"
    failure = b"failure"
    (screenshot_dir / "normal.png").write_bytes(normal)
    (screenshot_dir / "failure.png").write_bytes(failure)
    payload = _result(run_id)
    payload["artifacts"] = [
        {
            "type": "screenshot",
            "name": "normal",
            "path": f".qa/runs/{run_id}/screenshots/normal.png",
            "caption": "normal",
            "kind": "checkpoint",
            "priority": "normal",
        },
        {
            "type": "screenshot",
            "name": "failure",
            "path": f".qa/runs/{run_id}/screenshots/failure.png",
            "caption": "failure",
            "kind": "failure",
            "priority": "failure",
        },
    ]
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    executor = LocalProjectOperationExecutor()
    contract = await executor.execute(
        host_id="local",
        project_id="demo",
        working_directory=root,
        operation="qa_contract",
        payload={},
    )
    assert contract["contract_version"] == "2.0"

    collected = await executor.execute(
        host_id="local",
        project_id="demo",
        working_directory=root,
        operation="qa_collect",
        payload={"run_id": run_id, "max_screenshots": 1},
    )
    assert collected["contract_version"] == "2.0"
    assert collected["screenshots"][0]["name"] == "failure"
    assert base64.b64decode(collected["screenshots"][0]["data_base64"]) == failure


@pytest.mark.asyncio
async def test_local_qa_collect_rejects_workspace_escape(tmp_path: Path) -> None:
    root = tmp_path / "project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "qa.ps1").write_text("# legacy QA test entrypoint\n", encoding="utf-8")
    run_id = "QA-local-002"
    run_dir = root / ".qa" / "runs" / run_id
    run_dir.mkdir(parents=True)
    payload = _legacy_result(run_id)
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
                "entrypoint": ".qa/manifest.yaml",
                "schema_version": "2.0",
                "contract_version": "2.0",
            }
        assert payload is not None
        run_id = str(payload["run_id"])
        result = _result(run_id, status=self.status)
        return {
            "result": result,
            "screenshots": [],
            "result_path": f".qa/runs/{run_id}/result.json",
            "exit_code": {
                "PASS": 0,
                "PASS_WITH_WARNINGS": 0,
                "FAIL": 1,
                "HUMAN_GATE_REQUIRED": 2,
            }[self.status],
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
async def test_pass_with_warnings_completes_job_and_persists_warning_count(
    project_registry,
    database,
) -> None:
    events = EventRepository(database)
    qa_runs = QARunRepository(database)
    qa = QAOrchestrator(
        runs=qa_runs,
        events=events,
        operations=_FakeQAOperations("PASS_WITH_WARNINGS"),
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
    assert qa_run.status == "PASS_WITH_WARNINGS"
    assert qa_run.warning_count == 1


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


@pytest.mark.asyncio
async def test_qa_contract_is_optional_without_entrypoint(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    executor = LocalProjectOperationExecutor()
    contract = await executor.execute(
        host_id="local",
        project_id="demo",
        working_directory=root,
        operation="qa_contract",
        payload={},
    )
    assert contract == {
        "supported": False,
        "contract_version": None,
        "schema_version": None,
        "entrypoint": None,
        "manifest": None,
    }


@pytest.mark.asyncio
async def test_local_qa_collect_requires_result_json(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "qa.ps1").write_text("# legacy QA test entrypoint\n", encoding="utf-8")
    executor = LocalProjectOperationExecutor()
    with pytest.raises(ProjectOperationError, match="result.json was not produced"):
        await executor.execute(
            host_id="local",
            project_id="demo",
            working_directory=root,
            operation="qa_collect",
            payload={"run_id": "QA-missing-001"},
        )


@pytest.mark.asyncio
async def test_local_qa_collect_rejects_malformed_result(tmp_path: Path) -> None:
    root = tmp_path / "project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "qa.ps1").write_text("# legacy QA test entrypoint\n", encoding="utf-8")
    run_dir = root / ".qa" / "runs" / "QA-malformed-001"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text("{bad json", encoding="utf-8")
    executor = LocalProjectOperationExecutor()
    with pytest.raises(ProjectOperationError, match="malformed QA result.json"):
        await executor.execute(
            host_id="local",
            project_id="demo",
            working_directory=root,
            operation="qa_collect",
            payload={"run_id": "QA-malformed-001"},
        )


class _ExitMismatchOperations(_FakeQAOperations):
    async def execute(self, **kwargs) -> dict:
        response = await super().execute(**kwargs)
        if kwargs["operation"] != "qa_contract":
            response["exit_code"] = 1
        return response


@pytest.mark.asyncio
async def test_qa_status_exit_code_mismatch_fails_closed(database, project_dir: Path) -> None:
    events = EventRepository(database)
    qa = QAOrchestrator(
        runs=QARunRepository(database),
        events=events,
        operations=_ExitMismatchOperations("PASS"),
    )
    with pytest.raises(QAContractError, match="exit code/status mismatch"):
        await qa.execute(
            job_id="JOB-test",
            project_id="demo",
            host_id="lightsail-main",
            working_directory=project_dir,
        )


@pytest.mark.asyncio
async def test_ui_review_uses_existing_human_gate_and_approval(
    project_registry,
    database,
) -> None:
    events = EventRepository(database)
    qa_runs = QARunRepository(database)
    qa = QAOrchestrator(
        runs=qa_runs,
        events=events,
        operations=_FakeQAOperations("HUMAN_GATE_REQUIRED"),
    )
    approvals = ApprovalRegistry(
        approvals=ApprovalRepository(database),
        events=events,
    )
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        approvals=approvals,
        qa=qa,
    )
    job = await manager.create(
        project_id="demo",
        instruction="do the work",
        requested_by_channel="test",
        requested_by_user="u1",
    )

    approval = None
    for _ in range(200):
        current = await manager.require(job.id)
        if current.state == "WAITING_HUMAN":
            approval = await approvals.pending_for_job(job.id)
            break
        await asyncio.sleep(0.01)

    assert approval is not None
    assert approval.approval_type == "qa_human_gate"
    await manager.respond_approval(
        approval.id,
        user_id="u1",
        option_key="APPROVE",
    )
    for _ in range(200):
        current = await manager.require(job.id)
        if current.state == "COMPLETED":
            await manager.wait_until_idle(job.id)
            break
        await asyncio.sleep(0.01)

    current = await manager.require(job.id)
    qa_run = await qa_runs.latest_for_job(job.id)
    assert current.state == "COMPLETED"
    assert qa_run is not None
    assert qa_run.status == "HUMAN_GATE_REQUIRED"
    assert qa_run.review_status == "UI_APPROVED"


@pytest.mark.asyncio
async def test_ui_review_rejection_fails_job(project_registry, database) -> None:
    events = EventRepository(database)
    qa_runs = QARunRepository(database)
    qa = QAOrchestrator(
        runs=qa_runs,
        events=events,
        operations=_FakeQAOperations("HUMAN_GATE_REQUIRED"),
    )
    approvals = ApprovalRegistry(
        approvals=ApprovalRepository(database),
        events=events,
    )
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        approvals=approvals,
        qa=qa,
    )
    job = await manager.create(
        project_id="demo",
        instruction="do the work",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    approval = None
    for _ in range(200):
        if (await manager.require(job.id)).state == "WAITING_HUMAN":
            approval = await approvals.pending_for_job(job.id)
            break
        await asyncio.sleep(0.01)
    assert approval is not None
    await manager.respond_approval(
        approval.id,
        user_id="u1",
        option_key="REJECT",
    )
    current = await manager.require(job.id)
    qa_run = await qa_runs.latest_for_job(job.id)
    assert current.state == "FAILED"
    assert qa_run is not None and qa_run.review_status == "UI_REJECTED"
