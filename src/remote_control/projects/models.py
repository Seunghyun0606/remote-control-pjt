from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RepositoryConfig(BaseModel):
    path: dict[str, str] = Field(default_factory=dict)
    url: str | None = None


class ProjectDefinition(BaseModel):
    id: str
    name: str
    adapter: str = "generic_git"
    adapter_config: dict[str, Any] = Field(default_factory=dict)
    repository: RepositoryConfig
    allowed_hosts: list[str]
    default_host: str | None = None

    def path_for(self, host_id: str) -> str:
        if host_id not in self.allowed_hosts:
            raise ValueError(f"host {host_id!r} is not allowed for project {self.id!r}")
        path = self.repository.path.get(host_id)
        if not path:
            raise ValueError(f"project {self.id!r} has no working directory for host {host_id!r}")
        return path
