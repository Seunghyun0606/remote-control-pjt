from __future__ import annotations

import json
from pathlib import Path

from remote_control.projects.adapters.base import PreparedWork, ProjectAdapter, SubmitOutcome
from remote_control.projects.models import ProjectDefinition
from remote_control.projects.operations import ProjectOperationExecutor


class GenericGitAdapter(ProjectAdapter):
    def __init__(self, operations: ProjectOperationExecutor) -> None:
        self.operations = operations

    async def prepare(
        self,
        *,
        job_id: str,
        project: ProjectDefinition,
        host_id: str,
        base_instruction: str,
    ) -> PreparedWork:
        del job_id
        working_directory = Path(project.path_for(host_id))
        snapshot = await self.operations.execute(
            host_id=host_id,
            project_id=project.id,
            working_directory=working_directory,
            operation="git_snapshot",
        )
        instruction = (
            f"{base_instruction.rstrip()}\n\n"
            "Repository snapshot captured by the Remote Control generic Git adapter:\n"
            f"{json.dumps(snapshot, ensure_ascii=False, indent=2)}\n\n"
            "Treat the repository filesystem and its project instructions as the source of truth. "
            "Do not assume conversation history is current."
        )
        return PreparedWork(instruction=instruction)

    async def submit_result(
        self,
        *,
        job_id: str,
        project: ProjectDefinition,
        host_id: str,
        final_message: str | None,
    ) -> SubmitOutcome:
        del job_id, project, host_id, final_message
        return SubmitOutcome()
