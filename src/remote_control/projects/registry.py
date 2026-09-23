from __future__ import annotations

from pathlib import Path

import yaml

from remote_control.projects.models import ProjectDefinition, RepositoryConfig


class ProjectRegistry:
    def __init__(self, projects: dict[str, ProjectDefinition]) -> None:
        self._projects = projects

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ProjectRegistry":
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"project config not found: {config_path}")
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        raw_projects = payload.get("projects") or {}
        projects: dict[str, ProjectDefinition] = {}
        for project_id, raw in raw_projects.items():
            raw = raw or {}
            projects[project_id] = ProjectDefinition(
                id=project_id,
                name=raw.get("name", project_id),
                adapter=raw.get("adapter", "generic_git"),
                repository=RepositoryConfig.model_validate(raw.get("repository") or {}),
                allowed_hosts=list(raw.get("allowed_hosts") or []),
                default_host=raw.get("default_host"),
            )
        return cls(projects)

    def list(self) -> list[ProjectDefinition]:
        return sorted(self._projects.values(), key=lambda project: project.id)

    def get(self, project_id: str) -> ProjectDefinition:
        try:
            return self._projects[project_id]
        except KeyError as exc:
            raise KeyError(f"unknown project: {project_id}") from exc

    def resolve_name(self, text: str) -> ProjectDefinition | None:
        normalized = text.casefold()
        matches = [
            project
            for project in self._projects.values()
            if project.id.casefold() in normalized or project.name.casefold() in normalized
        ]
        if len(matches) == 1:
            return matches[0]
        return None
