from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from remote_control.controller.service import ControllerService


class RunRequest(BaseModel):
    host: str = "auto"
    requested_by: str = "api"


def create_app(controller: ControllerService) -> FastAPI:
    app = FastAPI(title="Remote Agent Control", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/projects")
    async def projects() -> list[dict]:
        return [project.model_dump() for project in controller.projects.list()]

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
