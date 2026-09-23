from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from remote_control.projects.models import ProjectDefinition


class NoProjectWork(RuntimeError):
    """Raised when a project adapter has no eligible work to execute."""


@dataclass(frozen=True, slots=True)
class PreparedWork:
    instruction: str
    task_id: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    message: str | None = None
    next_task_id: str | None = None


class ProjectAdapter(ABC):
    @abstractmethod
    async def prepare(
        self,
        *,
        job_id: str,
        project: ProjectDefinition,
        host_id: str,
        base_instruction: str,
    ) -> PreparedWork:
        raise NotImplementedError

    @abstractmethod
    async def submit_result(
        self,
        *,
        job_id: str,
        project: ProjectDefinition,
        host_id: str,
        final_message: str | None,
    ) -> SubmitOutcome:
        raise NotImplementedError

    @property
    def requires_submission(self) -> bool:
        return False
