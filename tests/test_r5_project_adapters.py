from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.projects.adapters import NoProjectWork, ProjectAdapterRegistry
from remote_control.projects.adapters.generic_git import GenericGitAdapter
from remote_control.projects.adapters.project_os import ProjectOSAdapter
from remote_control.projects.config import add_project
from remote_control.projects.models import ProjectDefinition, RepositoryConfig
from remote_control.projects.operations import LocalProjectOperationExecutor, ProjectOperationError
from remote_control.projects.registry import ProjectRegistry
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.models import JobRecord, ProjectWorkRecord
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    ProjectWorkRepository,
    RecoveryRepository,
)
from remote_control.transport.protocol import Envelope, message
from remote_control.transport.runner_ws import RunnerGateway


class FakeProjectOperations:
    def __init__(
        self,
        *,
        next_task: dict[str, Any] | None = None,
        fail_submit: bool = False,
        current_tasks: list[str] | None = None,
    ) -> None:
        self.next_task = next_task
        self.fail_submit = fail_submit
        self.current_tasks = current_tasks or []
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        *,
        host_id: str,
        project_id: str,
        working_directory: Path,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = payload or {}
        self.calls.append(
            {
                "host_id": host_id,
                "project_id": project_id,
                "working_directory": working_directory,
                "operation": operation,
                "payload": data,
            }
        )
        if operation == "git_snapshot":
            return {
                "branch": "feat/demo",
                "status": "## feat/demo",
                "diff_stat": "app.py | 2 ++",
            }
        if operation == "project_os_status":
            return {
                "project_id": project_id,
                "project_status": "active",
                "current_milestone": "M1",
                "current_tasks": list(self.current_tasks),
            }
        if operation == "project_os_next":
            return {"task": self.next_task}
        if operation == "project_os_context":
            return {
                "task": {"id": data["task_id"], "title": "Implement adapter"},
                "role": data["role"],
                "acceptance": ["tests pass"],
                "verification": ["pytest"],
            }
        if operation == "project_os_claim":
            return {"message": f"Claimed {data['task_id']}"}
        if operation == "project_os_submit":
            if self.fail_submit:
                raise RuntimeError("submit unavailable")
            return {"message": f"Stored implementation result for {data['task_id']}"}
        raise AssertionError(f"unexpected operation: {operation}")


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[int] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


def project_os_registry(path: Path, *, hosts: list[str] | None = None) -> ProjectRegistry:
    allowed = hosts or ["lightsail-main"]
    return ProjectRegistry(
        {
            "demo": ProjectDefinition(
                id="demo",
                name="Demo",
                adapter="project_os",
                adapter_config={"role": "developer", "actor": "remote-control-codex"},
                repository=RepositoryConfig(
                    path={host: str(path) for host in allowed},
                ),
                allowed_hosts=allowed,
                default_host=allowed[0],
            )
        }
    )


async def wait_for_terminal(manager: JobManager, job_id: str) -> str:
    for _ in range(300):
        job = await manager.require(job_id)
        if job.state in {"COMPLETED", "FAILED", "CANCELLED"}:
            await manager.wait_until_idle(job_id)
            return job.state
        await asyncio.sleep(0.01)
    return (await manager.require(job_id)).state


@pytest.mark.asyncio
async def test_generic_git_adapter_builds_instruction_from_snapshot(tmp_path):
    operations = FakeProjectOperations()
    adapter = GenericGitAdapter(operations)
    project = ProjectDefinition(
        id="demo",
        name="Demo",
        adapter="generic_git",
        repository=RepositoryConfig(path={"lightsail-main": str(tmp_path)}),
        allowed_hosts=["lightsail-main"],
        default_host="lightsail-main",
    )

    prepared = await adapter.prepare(
        job_id="JOB-1",
        project=project,
        host_id="lightsail-main",
        base_instruction="Continue implementation.",
    )

    assert prepared.task_id is None
    assert "feat/demo" in prepared.instruction
    assert "app.py | 2 ++" in prepared.instruction
    assert [call["operation"] for call in operations.calls] == ["git_snapshot"]


@pytest.mark.asyncio
async def test_project_os_adapter_selects_context_claims_and_persists(
    tmp_path,
    database,
):
    operations = FakeProjectOperations(
        next_task={"id": "TASK-043", "title": "Save/load", "role": "developer"}
    )
    events = EventRepository(database)
    work = ProjectWorkRepository(database)
    adapter = ProjectOSAdapter(operations=operations, work=work, events=events)
    project = project_os_registry(tmp_path).get("demo")

    prepared = await adapter.prepare(
        job_id="JOB-043",
        project=project,
        host_id="lightsail-main",
        base_instruction="Continue.",
    )

    assert prepared.task_id == "TASK-043"
    assert prepared.role == "developer"
    assert "TASK-043" in prepared.instruction
    assert "tests pass" in prepared.instruction
    assert [call["operation"] for call in operations.calls] == [
        "project_os_status",
        "project_os_next",
        "project_os_context",
        "project_os_claim",
    ]

    record = await work.get("JOB-043")
    assert record is not None
    assert record.host_id == "lightsail-main"
    assert record.task_id == "TASK-043"
    assert record.status == "PREPARED"


@pytest.mark.asyncio
async def test_project_os_adapter_reuses_existing_binding_without_next_or_claim(
    tmp_path,
    database,
):
    operations = FakeProjectOperations(
        next_task={"id": "TASK-043", "title": "Save/load"}
    )
    events = EventRepository(database)
    work = ProjectWorkRepository(database)
    adapter = ProjectOSAdapter(operations=operations, work=work, events=events)
    project = project_os_registry(tmp_path).get("demo")

    await adapter.prepare(
        job_id="JOB-043",
        project=project,
        host_id="lightsail-main",
        base_instruction="First.",
    )
    operations.calls.clear()

    prepared = await adapter.prepare(
        job_id="JOB-043",
        project=project,
        host_id="lightsail-main",
        base_instruction="Resume.",
    )

    assert prepared.task_id == "TASK-043"
    assert [call["operation"] for call in operations.calls] == [
        "project_os_status",
        "project_os_context",
    ]


@pytest.mark.asyncio
async def test_project_os_adapter_refuses_cross_host_continuation(tmp_path, database):
    operations = FakeProjectOperations(next_task={"id": "TASK-043"})
    adapter = ProjectOSAdapter(
        operations=operations,
        work=ProjectWorkRepository(database),
        events=EventRepository(database),
    )
    project = project_os_registry(
        tmp_path,
        hosts=["lightsail-main", "desktop-main"],
    ).get("demo")

    await adapter.prepare(
        job_id="JOB-043",
        project=project,
        host_id="lightsail-main",
        base_instruction="First.",
    )

    with pytest.raises(ValueError, match="pinned to host"):
        await adapter.prepare(
            job_id="JOB-043",
            project=project,
            host_id="desktop-main",
            base_instruction="Resume elsewhere.",
        )




@pytest.mark.asyncio
async def test_project_os_adapter_reconciles_claim_without_duplicate_command(
    tmp_path,
    database,
):
    operations = FakeProjectOperations(
        next_task={"id": "TASK-043"},
        current_tasks=["TASK-043"],
    )
    events = EventRepository(database)
    work = ProjectWorkRepository(database)
    await work.add(
        ProjectWorkRecord(
            job_id="JOB-CLAIM-RECOVERY",
            adapter="project_os",
            host_id="lightsail-main",
            task_id="TASK-043",
            role="developer",
            status="SELECTED",
        )
    )
    adapter = ProjectOSAdapter(operations=operations, work=work, events=events)
    project = project_os_registry(tmp_path).get("demo")

    prepared = await adapter.prepare(
        job_id="JOB-CLAIM-RECOVERY",
        project=project,
        host_id="lightsail-main",
        base_instruction="Resume after restart.",
    )

    assert prepared.task_id == "TASK-043"
    assert "project_os_claim" not in [
        call["operation"] for call in operations.calls
    ]
    record = await work.get("JOB-CLAIM-RECOVERY")
    assert record is not None
    assert record.status == "PREPARED"


@pytest.mark.asyncio
async def test_restart_during_project_submit_recovers_finalization_only(
    tmp_path,
    database,
):
    operations = FakeProjectOperations(next_task=None)
    events = EventRepository(database)
    work = ProjectWorkRepository(database)
    recovery = RecoveryRepository(database)
    runner = FakeAgentRunner()
    manager = JobManager(
        projects=project_os_registry(tmp_path),
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        recovery=recovery,
        project_adapters=ProjectAdapterRegistry(
            operations=operations,
            work=work,
            events=events,
        ),
        project_work=work,
    )
    job = JobRecord(
        id="JOB-FINALIZE-RECOVERY",
        project_id="demo",
        requested_by_channel="test",
        requested_by_user="u1",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="Already implemented.",
        state="WAITING_AGENT",
        result="implementation complete",
    )
    await manager.jobs.add(job)
    await work.add(
        ProjectWorkRecord(
            job_id=job.id,
            adapter="project_os",
            host_id="lightsail-main",
            task_id="TASK-043",
            role="developer",
            status="PREPARED",
        )
    )

    assert await manager.reconcile_startup() == 1
    assert (await manager.require(job.id)).state == "WAITING_HOST"
    recovery_record = await recovery.get(job.id)
    assert recovery_record is not None
    assert recovery_record.mode == "FINALIZE"

    await manager.recover_due()
    assert await wait_for_terminal(manager, job.id) == "COMPLETED"
    assert runner.started == []
    assert runner.resumed == []

    record = await work.get(job.id)
    assert record is not None
    assert record.status == "SUBMITTED"
    assert "project_os_submit" in [
        call["operation"] for call in operations.calls
    ]


@pytest.mark.asyncio
async def test_project_os_adapter_no_work_raises(tmp_path, database):
    operations = FakeProjectOperations(next_task=None)
    adapter = ProjectOSAdapter(
        operations=operations,
        work=ProjectWorkRepository(database),
        events=EventRepository(database),
    )
    project = project_os_registry(tmp_path).get("demo")

    with pytest.raises(NoProjectWork, match="no eligible task"):
        await adapter.prepare(
            job_id="JOB-NONE",
            project=project,
            host_id="lightsail-main",
            base_instruction="Continue.",
        )


@pytest.mark.asyncio
async def test_job_manager_project_os_flow_claims_runs_and_submits(
    tmp_path,
    database,
):
    operations = FakeProjectOperations(
        next_task={"id": "TASK-043", "title": "Save/load", "role": "developer"}
    )
    events = EventRepository(database)
    work = ProjectWorkRepository(database)
    adapters = ProjectAdapterRegistry(
        operations=operations,
        work=work,
        events=events,
    )
    runner = FakeAgentRunner()
    manager = JobManager(
        projects=project_os_registry(tmp_path),
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        project_adapters=adapters,
        project_work=work,
    )

    job = await manager.create(
        project_id="demo",
        instruction="Continue the next task.",
        requested_by_channel="test",
        requested_by_user="u1",
    )

    assert await wait_for_terminal(manager, job.id) == "COMPLETED"
    assert len(runner.started) == 1
    assert "TASK-043" in runner.started[0]["instruction"]

    record = await work.get(job.id)
    assert record is not None
    assert record.status == "SUBMITTED"
    assert record.task_id == "TASK-043"
    assert record.host_id == "lightsail-main"

    operations_used = [call["operation"] for call in operations.calls]
    assert operations_used[:4] == [
        "project_os_status",
        "project_os_next",
        "project_os_context",
        "project_os_claim",
    ]
    assert "project_os_submit" in operations_used


@pytest.mark.asyncio
async def test_job_manager_no_project_os_work_completes_without_agent(
    tmp_path,
    database,
):
    operations = FakeProjectOperations(next_task=None)
    events = EventRepository(database)
    work = ProjectWorkRepository(database)
    runner = FakeAgentRunner()
    manager = JobManager(
        projects=project_os_registry(tmp_path),
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        project_adapters=ProjectAdapterRegistry(
            operations=operations,
            work=work,
            events=events,
        ),
        project_work=work,
    )

    job = await manager.create(
        project_id="demo",
        instruction="Continue.",
        requested_by_channel="test",
        requested_by_user="u1",
    )

    assert await wait_for_terminal(manager, job.id) == "COMPLETED"
    assert runner.started == []


@pytest.mark.asyncio
async def test_job_manager_submit_failure_marks_job_failed(tmp_path, database):
    operations = FakeProjectOperations(
        next_task={"id": "TASK-043"},
        fail_submit=True,
    )
    events = EventRepository(database)
    work = ProjectWorkRepository(database)
    manager = JobManager(
        projects=project_os_registry(tmp_path),
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        project_adapters=ProjectAdapterRegistry(
            operations=operations,
            work=work,
            events=events,
        ),
        project_work=work,
    )

    job = await manager.create(
        project_id="demo",
        instruction="Continue.",
        requested_by_channel="test",
        requested_by_user="u1",
    )

    assert await wait_for_terminal(manager, job.id) == "FAILED"
    record = await work.get(job.id)
    assert record is not None
    assert record.status == "SUBMIT_FAILED"
    assert "submit unavailable" in (record.error or "")


@pytest.mark.asyncio
async def test_project_adapter_registry_accepts_project_os_aliases(tmp_path, database):
    operations = FakeProjectOperations()
    registry = ProjectAdapterRegistry(
        operations=operations,
        work=ProjectWorkRepository(database),
        events=EventRepository(database),
    )
    for value in ("project_os", "project-os"):
        project = ProjectDefinition(
            id=value,
            name=value,
            adapter=value,
            repository=RepositoryConfig(path={"lightsail-main": str(tmp_path)}),
            allowed_hosts=["lightsail-main"],
        )
        assert isinstance(registry.get(project), ProjectOSAdapter)


@pytest.mark.asyncio
async def test_remote_project_operation_rpc_round_trip():
    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    task = asyncio.create_task(
        gateway.project_operation(
            host_id="desktop-main",
            project_id="demo",
            working_directory=Path("C:/dev/demo"),
            operation="project_os_status",
            payload={},
            timeout_seconds=2,
        )
    )
    await asyncio.sleep(0)

    envelope = Envelope.model_validate_json(websocket.sent[-1])
    assert envelope.type == "PROJECT_OPERATION_REQUEST"
    request_id = envelope.payload["request_id"]
    assert envelope.payload["operation"] == "project_os_status"

    await gateway.handle(
        "desktop-main",
        message(
            "PROJECT_OPERATION_RESULT",
            request_id=request_id,
            result={"project_status": "active"},
        ),
    )

    assert await task == {"project_status": "active"}


@pytest.mark.asyncio
async def test_remote_project_operation_error_is_propagated():
    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    task = asyncio.create_task(
        gateway.project_operation(
            host_id="desktop-main",
            project_id="demo",
            working_directory=Path("C:/dev/demo"),
            operation="project_os_context",
            payload={"task_id": "TASK-1", "role": "developer"},
            timeout_seconds=2,
        )
    )
    await asyncio.sleep(0)

    envelope = Envelope.model_validate_json(websocket.sent[-1])
    await gateway.handle(
        "desktop-main",
        message(
            "PROJECT_OPERATION_ERROR",
            request_id=envelope.payload["request_id"],
            error="projectctl missing",
        ),
    )

    with pytest.raises(RuntimeError, match="projectctl missing"):
        await task


@pytest.mark.asyncio
async def test_local_project_operation_executor_rejects_unknown_and_unsafe_inputs(tmp_path):
    executor = LocalProjectOperationExecutor()

    with pytest.raises(ProjectOperationError, match="unsupported project operation"):
        await executor.execute(
            host_id="lightsail-main",
            project_id="demo",
            working_directory=tmp_path,
            operation="shell",
            payload={"command": "rm -rf /"},
        )

    with pytest.raises(ProjectOperationError, match="invalid task_id"):
        await executor.execute(
            host_id="lightsail-main",
            project_id="demo",
            working_directory=tmp_path,
            operation="project_os_context",
            payload={"task_id": "TASK 1; rm -rf /", "role": "developer"},
        )


def test_project_registry_loads_adapter_config(tmp_path):
    config = tmp_path / "projects.yaml"
    config.write_text(
        """
projects:
  demo:
    name: Demo
    adapter: project_os
    adapter_config:
      role: architect
      actor: remote-control-architect
    repository:
      path:
        lightsail-main: /tmp/demo
    allowed_hosts:
      - lightsail-main
""".strip(),
        encoding="utf-8",
    )

    project = ProjectRegistry.from_yaml(config).get("demo")
    assert project.adapter == "project_os"
    assert project.adapter_config == {
        "role": "architect",
        "actor": "remote-control-architect",
    }



def test_add_project_writes_remote_control_registry_atomically(tmp_path):
    config = tmp_path / "projects.yaml"
    project = add_project(
        config,
        project_id="dailytown",
        name="DailyTown",
        adapter="project-os",
        host_id="lightsail-main",
        working_directory="/srv/dailytown",
        role="developer",
        actor="remote-control-codex",
    )

    assert project.adapter == "project_os"
    loaded = ProjectRegistry.from_yaml(config).get("dailytown")
    assert loaded.path_for("lightsail-main") == "/srv/dailytown"
    assert loaded.adapter_config["role"] == "developer"

    with pytest.raises(ValueError, match="already registered"):
        add_project(
            config,
            project_id="dailytown",
            name=None,
            adapter="generic_git",
            host_id="lightsail-main",
            working_directory="/srv/duplicate",
        )


def test_add_project_rejects_unsafe_identifier(tmp_path):
    with pytest.raises(ValueError, match="invalid project id"):
        add_project(
            tmp_path / "projects.yaml",
            project_id="../escape",
            name=None,
            adapter="generic_git",
            host_id="lightsail-main",
            working_directory="/srv/demo",
        )
