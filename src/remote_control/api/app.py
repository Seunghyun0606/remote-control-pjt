from __future__ import annotations

import secrets

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from remote_control.api.dashboard import DashboardService, render_dashboard_html
from remote_control.controller.service import ControllerService
from remote_control.transport.protocol import Envelope
from remote_control.transport.runner_ws import RunnerGateway


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "auto"


class ResumeRequest(BaseModel):
    instruction: str | None = None


class SteerRequest(BaseModel):
    instruction: str


class ApprovalResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str | None = None
    reject: bool = False
    response_text: str | None = None


def create_app(
    controller: ControllerService,
    *,
    runner_gateway: RunnerGateway | None = None,
    runner_token: str = "",
    api_token: str = "",
    api_principal: str = "api:local",
    web_ui_enabled: bool = True,
) -> FastAPI:
    api_principal = api_principal.strip()
    if not api_principal.startswith("api:") or any(
        character.isspace() for character in api_principal
    ):
        raise ValueError("api_principal must be an api:... identifier without whitespace")

    app = FastAPI(title="Remote Agent Control", version="0.11.2")
    dashboard = DashboardService(controller)

    if api_token:
        @app.middleware("http")
        async def require_control_api_auth(request: Request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            provided = _control_api_token(request)
            if provided is None or not secrets.compare_digest(provided, api_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "control API authentication required"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/dashboard")
    async def dashboard_snapshot() -> dict:
        return await dashboard.snapshot()

    if web_ui_enabled:

        @app.get("/ui", response_class=HTMLResponse)
        async def dashboard_ui() -> str:
            return render_dashboard_html()

    @app.get("/projects")
    async def projects() -> list[dict]:
        return [project.model_dump() for project in controller.projects.list()]

    @app.get("/hosts")
    async def hosts() -> list[dict]:
        if controller.hosts is None:
            return []
        return [
            {
                "id": host.id,
                "name": host.name,
                "os": host.os,
                "status": host.status.value,
                "capabilities": sorted(host.capabilities),
                "last_heartbeat": host.last_heartbeat,
            }
            for host in await controller.hosts.list()
        ]

    @app.get("/sessions")
    async def sessions() -> list[dict]:
        if controller.jobs.sessions is None:
            return []
        return [
            {
                "id": session.id,
                "job_id": session.job_id,
                "project_id": session.project_id,
                "host_id": session.host_id,
                "agent_type": session.agent_type,
                "external_session_id": session.external_session_id,
                "status": session.status,
                "created_at": session.created_at,
                "last_active_at": session.last_active_at,
            }
            for session in await controller.jobs.sessions.list()
        ]

    @app.get("/project-sessions")
    async def project_sessions() -> list[dict]:
        if controller.jobs.project_sessions is None:
            return []
        return [
            {
                "id": session.id,
                "project_id": session.project_id,
                "owner_user_id": session.owner_user_id,
                "external_session_id": session.external_session_id,
                "host_id": session.host_id,
                "status": session.status,
                "last_job_id": session.last_job_id,
                "locked_by_job_id": session.locked_by_job_id,
                "created_at": session.created_at,
                "last_active_at": session.last_active_at,
                "closed_at": session.closed_at,
            }
            for session in await controller.jobs.project_sessions.sessions.list()
        ]

    @app.get("/recovery")
    async def recovery() -> list[dict]:
        if controller.jobs.recovery is None:
            return []
        return [
            {
                "job_id": record.job_id,
                "kind": record.kind,
                "mode": record.mode,
                "attempt_count": record.attempt_count,
                "next_retry_at": record.next_retry_at,
                "execution_id": record.execution_id,
                "last_error": record.last_error,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
            for record in await controller.jobs.recovery.list()
        ]

    @app.get("/project-work")
    async def project_work() -> list[dict]:
        if controller.jobs.project_work is None:
            return []
        return [
            _project_work_view(record)
            for record in await controller.jobs.project_work.list()
        ]

    @app.get("/jobs/{job_id}/project-work")
    async def project_work_for_job(job_id: str) -> dict:
        if controller.jobs.project_work is None:
            raise HTTPException(status_code=404, detail="project work registry is not enabled")
        record = await controller.jobs.project_work.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="project work not found")
        return _project_work_view(record)

    @app.get("/approvals")
    async def approvals() -> list[dict]:
        if controller.jobs.approvals is None:
            return []
        return [
            _approval_view(controller.jobs.approvals, record)
            for record in await controller.jobs.approvals.list()
        ]

    @app.get("/approvals/{approval_id}")
    async def approval(approval_id: str) -> dict:
        if controller.jobs.approvals is None:
            raise HTTPException(status_code=404, detail="approval registry is not enabled")
        try:
            record = await controller.jobs.approvals.get(approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _approval_view(controller.jobs.approvals, record)

    @app.post("/approvals/{approval_id}/respond", status_code=202)
    async def respond_approval(
        approval_id: str,
        request: ApprovalResponseRequest,
    ) -> dict:
        try:
            record = await controller.jobs.respond_approval(
                approval_id,
                user_id=api_principal,
                option_key=request.option,
                rejected=request.reject,
                response_text=request.response_text,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        assert controller.jobs.approvals is not None
        return _approval_view(controller.jobs.approvals, record)

    @app.get("/jobs")
    async def jobs() -> list[dict]:
        return [_job_view(job) for job in await controller.jobs.list()]

    @app.get("/jobs/{job_id}")
    async def job(job_id: str) -> dict:
        try:
            record = await controller.jobs.require(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _job_view(record)

    @app.post("/projects/{project_id}/run", status_code=202)
    async def run(project_id: str, request: RunRequest) -> dict:
        try:
            record = await controller.jobs.create(
                project_id=project_id,
                instruction=(
                    "Continue the next appropriate implementation task for this project. "
                    "Inspect repository state, make a coherent change, run tests, and summarize."
                ),
                requested_by_channel="api",
                requested_by_user=api_principal,
                requested_host=request.host,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _job_view(record)

    @app.post("/jobs/{job_id}/pause")
    async def pause(job_id: str) -> dict:
        try:
            return _job_view(await controller.jobs.pause(job_id))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/jobs/{job_id}/resume", status_code=202)
    async def resume(job_id: str, request: ResumeRequest) -> dict:
        try:
            if request.instruction:
                record = await controller.jobs.resume(job_id, instruction=request.instruction)
            else:
                record = await controller.jobs.resume(job_id)
            return _job_view(record)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/jobs/{job_id}/steer", status_code=202)
    async def steer(job_id: str, request: SteerRequest) -> dict:
        try:
            return _job_view(await controller.jobs.steer(job_id, request.instruction))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str) -> dict:
        try:
            record = await controller.jobs.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _job_view(record)

    if runner_gateway is not None and controller.hosts is not None:

        @app.websocket("/ws/runner")
        async def runner_socket(websocket: WebSocket) -> None:
            authorization = websocket.headers.get("authorization", "")
            if not runner_token or authorization != f"Bearer {runner_token}":
                await websocket.close(code=1008)
                return

            await websocket.accept()
            host_id: str | None = None
            try:
                first = Envelope.model_validate_json(await websocket.receive_text())
                if first.type != "HOST_REGISTER":
                    await websocket.close(code=1008)
                    return

                host_id = str(first.payload.get("host_id") or "")
                if not host_id:
                    await websocket.close(code=1008)
                    return

                await controller.hosts.register(
                    host_id=host_id,
                    name=str(first.payload.get("name") or host_id),
                    os_name=str(first.payload.get("os") or "unknown"),
                    capabilities={
                        str(value)
                        for value in first.payload.get("capabilities", [])
                        if isinstance(value, str)
                    },
                )
                await runner_gateway.attach(host_id, websocket)

                while True:
                    envelope = Envelope.model_validate_json(await websocket.receive_text())
                    if envelope.type == "HEARTBEAT":
                        await controller.hosts.heartbeat(host_id)
                        await controller.jobs.reconcile_runner(
                            host_id=host_id,
                            running_jobs=_dict_list(envelope.payload.get("running_jobs")),
                            completed_jobs=[],
                            gateway=runner_gateway,
                            snapshot_complete=False,
                        )
                    elif envelope.type == "RUNNING_JOBS":
                        await controller.hosts.heartbeat(host_id)
                        await controller.jobs.reconcile_runner(
                            host_id=host_id,
                            running_jobs=_dict_list(envelope.payload.get("running_jobs")),
                            completed_jobs=_dict_list(envelope.payload.get("completed_jobs")),
                            gateway=runner_gateway,
                            snapshot_complete=True,
                        )
                    else:
                        await runner_gateway.handle(host_id, envelope)
            except WebSocketDisconnect:
                pass
            finally:
                if host_id is not None:
                    detached = await runner_gateway.detach(host_id, websocket)
                    if detached:
                        await controller.hosts.disconnect(host_id)

    return app


def _control_api_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    token = request.headers.get("x-remote-control-token", "").strip()
    return token or None


def _job_view(job) -> dict:
    return {
        "id": job.id,
        "project_id": job.project_id,
        "state": job.state,
        "requested_host": job.requested_host,
        "assigned_host": job.assigned_host,
        "external_session_id": job.external_session_id,
        "pid": job.pid,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _project_work_view(record) -> dict:
    return {
        "job_id": record.job_id,
        "adapter": record.adapter,
        "host_id": record.host_id,
        "task_id": record.task_id,
        "role": record.role,
        "status": record.status,
        "result_path": record.result_path,
        "next_task_id": record.next_task_id,
        "error": record.error,
        "claimed_at": record.claimed_at,
        "submitted_at": record.submitted_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _approval_view(registry, record) -> dict:
    return {
        "id": record.id,
        "job_id": record.job_id,
        "requested_by_user": record.requested_by_user,
        "type": record.approval_type,
        "question": record.question,
        "details": record.details,
        "options": [
            {
                "key": option.key,
                "label": option.label,
                "description": option.description,
            }
            for option in registry.options(record)
        ],
        "status": record.status,
        "selected_option": record.selected_option,
        "response_text": record.response_text,
        "expires_at": record.expires_at,
        "resolved_at": record.resolved_at,
        "created_at": record.created_at,
    }


def _dict_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
