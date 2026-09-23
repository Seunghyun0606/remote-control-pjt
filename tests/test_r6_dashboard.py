from __future__ import annotations

import platform

import httpx
import pytest

from remote_control.api.app import create_app
from remote_control.approvals.registry import ApprovalRegistry
from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.hosts.registry import HostRegistry
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.models import JobRecord, ProjectWorkRecord
from remote_control.storage.repositories import (
    ApprovalRepository,
    EventRepository,
    HostRepository,
    JobRepository,
    ProjectWorkRepository,
)


async def build_controller(project_registry, database):
    events = EventRepository(database)
    hosts = HostRegistry(
        hosts=HostRepository(database),
        events=events,
        local_host_id="lightsail-main",
    )
    await hosts.register_local(
        name="Controller",
        os_name=platform.system().lower(),
        capabilities={"codex", "git"},
    )
    work = ProjectWorkRepository(database)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        hosts=hosts,
        approvals=ApprovalRegistry(
            approvals=ApprovalRepository(database),
            events=events,
        ),
        project_work=work,
    )
    return ControllerService(projects=project_registry, jobs=manager, hosts=hosts), work


@pytest.mark.asyncio
async def test_dashboard_snapshot_and_html(project_registry, database):
    controller, work = await build_controller(project_registry, database)
    job = JobRecord(
        id="JOB-DASH",
        project_id="demo",
        requested_by_channel="slack",
        requested_by_user="U123",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="dashboard test",
        state="RUNNING",
    )
    await controller.jobs.jobs.add(job)
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

    app = create_app(controller, web_ui_enabled=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        snapshot = await client.get("/dashboard")
        page = await client.get("/ui")

    assert snapshot.status_code == 200
    payload = snapshot.json()
    assert payload["metrics"]["projects"] == 1
    assert payload["metrics"]["active_hosts"] == 1
    assert payload["metrics"]["active_jobs"] == 1
    assert payload["jobs"][0]["task_id"] == "TASK-043"

    assert page.status_code == 200
    assert "Remote Agent Control" in page.text
    assert "Read-only runtime dashboard" in page.text
    assert "fetch('/dashboard'" in page.text


@pytest.mark.asyncio
async def test_web_ui_can_be_disabled(project_registry, database):
    controller, _ = await build_controller(project_registry, database)
    app = create_app(controller, web_ui_enabled=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        page = await client.get("/ui")
        snapshot = await client.get("/dashboard")

    assert page.status_code == 404
    assert snapshot.status_code == 200
