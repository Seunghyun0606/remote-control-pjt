from __future__ import annotations

from remote_control.hosts.registry import HostRegistry
from remote_control.projects.models import ProjectDefinition


class HostUnavailable(ValueError):
    pass


class HostRouter:
    def __init__(self, registry: HostRegistry) -> None:
        self.registry = registry

    async def choose(self, project: ProjectDefinition, requested_host: str) -> str:
        if requested_host != "auto":
            return await self._require(project, requested_host)

        candidates: list[str] = []
        if project.default_host:
            candidates.append(project.default_host)
        candidates.extend(host for host in project.allowed_hosts if host not in candidates)

        for host_id in candidates:
            if host_id not in project.repository.path:
                continue
            if await self.registry.is_online(host_id):
                return host_id

        raise HostUnavailable(f"no online host is available for project {project.id!r}")

    async def _require(self, project: ProjectDefinition, host_id: str) -> str:
        if host_id not in project.allowed_hosts:
            raise HostUnavailable(f"host {host_id!r} is not allowed for project {project.id!r}")
        project.path_for(host_id)
        if not await self.registry.is_online(host_id):
            raise HostUnavailable(f"host {host_id!r} is offline")
        return host_id
