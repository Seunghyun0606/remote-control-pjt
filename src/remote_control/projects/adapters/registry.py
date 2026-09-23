from __future__ import annotations

from remote_control.projects.adapters.base import ProjectAdapter
from remote_control.projects.adapters.generic_git import GenericGitAdapter
from remote_control.projects.adapters.project_os import ProjectOSAdapter
from remote_control.projects.models import ProjectDefinition
from remote_control.projects.operations import ProjectOperationExecutor
from remote_control.storage.repositories import EventRepository, ProjectWorkRepository


class ProjectAdapterRegistry:
    def __init__(
        self,
        *,
        operations: ProjectOperationExecutor,
        work: ProjectWorkRepository,
        events: EventRepository,
    ) -> None:
        self.generic = GenericGitAdapter(operations)
        self.project_os = ProjectOSAdapter(
            operations=operations,
            work=work,
            events=events,
        )

    def get(self, project: ProjectDefinition) -> ProjectAdapter:
        adapter = project.adapter.replace("-", "_").casefold()
        if adapter == "generic_git":
            return self.generic
        if adapter == "project_os":
            return self.project_os
        raise ValueError(
            f"unsupported project adapter {project.adapter!r} for project {project.id!r}"
        )
