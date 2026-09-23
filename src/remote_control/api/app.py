from __future__ import annotations

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from remote_control.controller.service import ControllerService
from remote_control.transport.protocol import Envelope
from remote_control.transport.runner_ws import RunnerGateway


class RunRequest(BaseModel):
    host: str = "auto"
    requested_by: str = "api"


class ResumeRequest(BaseModel):
    instruction: str | None = None


class SteerRequest(BaseModel):
    instruction: str


class ApprovalResponseRequest(BaseModel):
    user_id: str
    option: str | None = None
    reject: bool = False
    response_text: str | None = None


def create_app(
    controller: ControllerService,
    *,
    runner_gateway: RunnerGateway | None = None,
    runner_token: str = "",
) -> FastAPI:
    app = FastAPI(title="Remote Agent Control", version="0.4.0")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

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
                user_id=request.user_id,
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
                requested_by_user=request.requested_by,
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
                    else:
                        await runner_gateway.handle(host_id, envelope)
            except WebSocketDisconnect:
                pass
            finally:
                if host_id is not None:
                    await runner_gateway.detach(host_id, websocket)
                    await controller.hosts.disconnect(host_id)

    return app


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
