from __future__ import annotations

import re
import tempfile
from pathlib import Path

import yaml

from remote_control.projects.models import ProjectDefinition, RepositoryConfig

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SUPPORTED_ADAPTERS = {"generic_git", "project_os"}


def add_project(
    config_path: str | Path,
    *,
    project_id: str,
    name: str | None,
    adapter: str,
    host_id: str,
    working_directory: str,
    role: str = "developer",
    actor: str = "remote-control-codex",
) -> ProjectDefinition:
    project_id = _safe(project_id, "project id")
    host_id = _safe(host_id, "host id")
    normalized_adapter = adapter.replace("-", "_").casefold()
    if normalized_adapter not in _SUPPORTED_ADAPTERS:
        allowed = ", ".join(sorted(_SUPPORTED_ADAPTERS))
        raise ValueError(f"unsupported adapter {adapter!r}; expected one of: {allowed}")

    path = Path(config_path)
    if path.exists():
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("project config root must be a mapping")

    projects = payload.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("projects must be a mapping")
    if project_id in projects:
        raise ValueError(f"project {project_id!r} is already registered")

    raw: dict = {
        "name": name or project_id,
        "adapter": normalized_adapter,
        "repository": {"path": {host_id: working_directory}},
        "allowed_hosts": [host_id],
        "default_host": host_id,
    }
    if normalized_adapter == "project_os":
        raw["adapter_config"] = {
            "role": _safe(role, "role"),
            "actor": _safe(actor, "actor"),
        }

    # Validate with the same model used by the runtime before writing.
    project = ProjectDefinition(
        id=project_id,
        name=raw["name"],
        adapter=raw["adapter"],
        adapter_config=dict(raw.get("adapter_config") or {}),
        repository=RepositoryConfig.model_validate(raw["repository"]),
        allowed_hosts=list(raw["allowed_hosts"]),
        default_host=raw["default_host"],
    )
    projects[project_id] = raw

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return project


def _safe(value: str, field: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"invalid {field}: {value!r}")
    return value
