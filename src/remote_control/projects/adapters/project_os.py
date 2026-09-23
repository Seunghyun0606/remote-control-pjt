from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from remote_control.projects.adapters.base import (
    NoProjectWork,
    PreparedWork,
    ProjectAdapter,
    SubmitOutcome,
)
from remote_control.projects.models import ProjectDefinition
from remote_control.projects.operations import ProjectOperationExecutor
from remote_control.storage.models import ProjectWorkRecord
from remote_control.storage.repositories import EventRepository, ProjectWorkRepository


class ProjectOSAdapter(ProjectAdapter):
    def __init__(
        self,
        *,
        operations: ProjectOperationExecutor,
        work: ProjectWorkRepository,
        events: EventRepository,
    ) -> None:
        self.operations = operations
        self.work = work
        self.events = events

    @property
    def requires_submission(self) -> bool:
        return True

    async def prepare(
        self,
        *,
        job_id: str,
        project: ProjectDefinition,
        host_id: str,
        base_instruction: str,
    ) -> PreparedWork:
        role = str(project.adapter_config.get("role") or "developer")
        working_directory = Path(project.path_for(host_id))
        current = await self.work.get(job_id)

        status = await self.operations.execute(
            host_id=host_id,
            project_id=project.id,
            working_directory=working_directory,
            operation="project_os_status",
        )

        if current is None:
            next_payload = await self.operations.execute(
                host_id=host_id,
                project_id=project.id,
                working_directory=working_directory,
                operation="project_os_next",
                payload={"role": role},
            )
            task = next_payload.get("task")
            if task is None:
                await self.events.append(
                    "PROJECT_OS_NO_WORK",
                    job_id=job_id,
                    project_id=project.id,
                    host_id=host_id,
                    payload={"role": role},
                )
                raise NoProjectWork(
                    f"Project OS has no eligible task for role {role!r}"
                )
            if not isinstance(task, dict) or not isinstance(task.get("id"), str):
                raise ValueError("projectctl next returned an invalid task")
            task_id = task["id"]
            current = await self.work.add(
                ProjectWorkRecord(
                    job_id=job_id,
                    adapter="project_os",
                    host_id=host_id,
                    task_id=task_id,
                    role=role,
                    status="SELECTED",
                )
            )
            await self.events.append(
                "PROJECT_OS_TASK_SELECTED",
                job_id=job_id,
                project_id=project.id,
                host_id=host_id,
                payload={"task_id": task_id, "role": role},
            )
        else:
            if current.host_id and current.host_id != host_id:
                raise ValueError(
                    f"Project OS task {current.task_id or '-'} is pinned to host "
                    f"{current.host_id!r}; refusing continuation on {host_id!r}"
                )
            task_id = current.task_id
            role = current.role or role
            if not task_id:
                raise ValueError("persisted Project OS work has no task id")

        context = await self.operations.execute(
            host_id=host_id,
            project_id=project.id,
            working_directory=working_directory,
            operation="project_os_context",
            payload={"task_id": task_id, "role": role},
        )

        if current.status == "SELECTED":
            await self.operations.execute(
                host_id=host_id,
                project_id=project.id,
                working_directory=working_directory,
                operation="project_os_claim",
                payload={"task_id": task_id, "role": role},
            )
            current = await self.work.update(
                job_id,
                status="CLAIMED",
                claimed_at=datetime.now(timezone.utc),
                error=None,
            )
            await self.events.append(
                "PROJECT_OS_TASK_CLAIMED",
                job_id=job_id,
                project_id=project.id,
                host_id=host_id,
                payload={"task_id": task_id, "role": role},
            )

        await self.work.update(job_id, status="PREPARED", error=None)
        instruction = _build_instruction(
            base_instruction=base_instruction,
            task_id=task_id,
            role=role,
            status=status,
            context=context,
        )
        return PreparedWork(
            instruction=instruction,
            task_id=task_id,
            role=role,
        )

    async def submit_result(
        self,
        *,
        job_id: str,
        project: ProjectDefinition,
        host_id: str,
        final_message: str | None,
    ) -> SubmitOutcome:
        current = await self.work.get(job_id)
        if current is None or current.adapter != "project_os" or not current.task_id:
            raise ValueError(f"missing Project OS work binding for job {job_id}")
        if current.status == "SUBMITTED":
            return SubmitOutcome(
                message=current.result_path,
                next_task_id=current.next_task_id,
            )

        role = current.role or "developer"
        actor = str(
            project.adapter_config.get("actor")
            or "remote-control-codex"
        )
        working_directory = Path(project.path_for(host_id))
        payload = {
            "task": current.task_id,
            "status": "submitted",
            "summary": (final_message or "Codex completed the implementation turn.").strip(),
            "files_changed": [],
            "verification": {
                "remote_control_job": job_id,
                "agent_returncode": 0,
            },
            "review": None,
            "followups": [],
            "commit": None,
        }
        try:
            submitted = await self.operations.execute(
                host_id=host_id,
                project_id=project.id,
                working_directory=working_directory,
                operation="project_os_submit",
                payload={
                    "task_id": current.task_id,
                    "role": role,
                    "actor": actor,
                    "result": payload,
                },
            )
        except Exception as exc:
            await self.work.update(job_id, status="SUBMIT_FAILED", error=str(exc))
            await self.events.append(
                "PROJECT_OS_SUBMIT_FAILED",
                job_id=job_id,
                project_id=project.id,
                host_id=host_id,
                payload={"task_id": current.task_id, "error": str(exc)},
            )
            raise

        next_payload = await self.operations.execute(
            host_id=host_id,
            project_id=project.id,
            working_directory=working_directory,
            operation="project_os_next",
            payload={"role": role},
        )
        next_task = next_payload.get("task")
        next_task_id = (
            str(next_task.get("id"))
            if isinstance(next_task, dict) and next_task.get("id")
            else None
        )
        message = str(submitted.get("message") or "")
        await self.work.update(
            job_id,
            status="SUBMITTED",
            result_path=message or None,
            next_task_id=next_task_id,
            submitted_at=datetime.now(timezone.utc),
            error=None,
        )
        await self.events.append(
            "PROJECT_OS_RESULT_SUBMITTED",
            job_id=job_id,
            project_id=project.id,
            host_id=host_id,
            payload={
                "task_id": current.task_id,
                "next_task_id": next_task_id,
            },
        )
        return SubmitOutcome(
            message=message or None,
            next_task_id=next_task_id,
        )


def _build_instruction(
    *,
    base_instruction: str,
    task_id: str,
    role: str,
    status: dict,
    context: dict,
) -> str:
    return (
        f"You are executing Project OS task {task_id} as role {role}.\n"
        "Remote Control already selected and claimed this task through projectctl. "
        "Do not claim or submit the task yourself. Project OS remains the canonical project state.\n\n"
        f"Project OS status snapshot:\n{json.dumps(status, ensure_ascii=False, indent=2)}\n\n"
        f"Task context package from projectctl context:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        f"Execution instruction:\n{base_instruction.rstrip()}\n\n"
        "Implement only the claimed task and follow the context package, AGENTS.md, quality gates, "
        "and repository-local instructions. Run relevant verification. In the final response, "
        "summarize the implementation, tests, notable files, and any remaining blocker. "
        "Do not mark the task complete or self-approve it; Remote Control will submit the "
        "implementation handoff through projectctl after this turn succeeds."
    )
