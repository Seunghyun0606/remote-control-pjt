from __future__ import annotations

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from remote_control.controller.service import ControllerService
from remote_control.transport.protocol import Envelope
from remote_control.transport.runner_ws import RunnerGateway


class RunRequest(BaseModel):
    host: str = "auto"
    requested_by: str = "api"


def create_app(
    controller: ControllerService,
    *,
    runner_gateway: RunnerGateway | None = None,
    runner_token: str = "",
) -> FastAPI:
    app = FastAPI(title="Remote Agent Control", version="0.2.0")

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
