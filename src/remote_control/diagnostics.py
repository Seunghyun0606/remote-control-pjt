from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from remote_control.executables import ExecutableResolutionError, resolve_executable
from remote_control.projects.registry import ProjectRegistry
from remote_control.settings import Settings


@dataclass(frozen=True, slots=True)
class DiagnosticItem:
    name: str
    ok: bool
    detail: str


def collect_diagnostics(
    settings: Settings,
    *,
    projects: ProjectRegistry | None = None,
    run_versions: bool = True,
) -> list[DiagnosticItem]:
    items = [
        DiagnosticItem("Host", True, settings.host_id),
        DiagnosticItem("Config", settings.resolved_config_path.exists(), str(settings.resolved_config_path)),
        DiagnosticItem("Database", True, settings.resolved_db_url),
    ]

    items.append(_executable_item("Codex", settings.codex_executable, run_versions))
    items.append(_executable_item("Git", settings.git_executable, run_versions))

    registry = projects
    if registry is None and settings.resolved_config_path.exists():
        try:
            registry = ProjectRegistry.from_yaml(settings.resolved_config_path)
        except Exception as exc:
            items.append(DiagnosticItem("Project Registry", False, str(exc)))
            registry = None

    if registry is not None:
        requires_projectctl = any(
            project.adapter == "project_os"
            and settings.host_id in project.allowed_hosts
            for project in registry.list()
        )
        if requires_projectctl:
            items.append(
                _executable_item(
                    "Projectctl",
                    settings.projectctl_executable,
                    False,
                )
            )

        for project in registry.list():
            if settings.host_id not in project.allowed_hosts:
                continue
            raw_path = project.repository.path.get(settings.host_id)
            if not raw_path:
                items.append(
                    DiagnosticItem(
                        f"Project {project.id}",
                        False,
                        f"no path configured for host {settings.host_id}",
                    )
                )
                continue
            path = Path(raw_path).expanduser()
            items.append(
                DiagnosticItem(
                    f"Project {project.id}",
                    path.exists() and path.is_dir(),
                    str(path),
                )
            )

    home_is_absolute = bool(
        settings.home_path
        and Path(settings.home_path).expanduser().is_absolute()
    )
    if settings.db_url.startswith("sqlite") and not home_is_absolute:
        items.append(
            DiagnosticItem(
                "Runtime Home",
                False,
                "Set REMOTE_CONTROL_HOME to an absolute path so relative DB/config paths "
                "do not depend on the launch directory",
            )
        )
    else:
        items.append(
            DiagnosticItem(
                "Runtime Home",
                True,
                str(settings.resolved_home_path),
            )
        )
    return items


def validate_startup(settings: Settings, projects: ProjectRegistry) -> None:
    failures: list[str] = []
    required_names = {"Config", "Codex", "Git", "Projectctl"}
    for item in collect_diagnostics(settings, projects=projects, run_versions=True):
        if not item.ok and item.name in required_names:
            failures.append(f"{item.name}: {item.detail}")
    if failures:
        joined = "\n- ".join(failures)
        raise RuntimeError(f"startup preflight failed:\n- {joined}")


def format_diagnostics(items: list[DiagnosticItem]) -> str:
    lines = ["Remote Control Doctor"]
    for item in items:
        marker = "✅" if item.ok else "❌"
        lines.append(f"{marker} {item.name}: {item.detail}")
    return "\n".join(lines)


def _executable_item(
    name: str,
    configured: str,
    run_version: bool,
) -> DiagnosticItem:
    try:
        resolution = resolve_executable(configured)
    except ExecutableResolutionError as exc:
        return DiagnosticItem(name, False, str(exc))

    detail = f"{resolution.resolved} ({resolution.kind})"
    if not run_version:
        return DiagnosticItem(name, True, detail)

    try:
        completed = subprocess.run(
            resolution.build_command(["--version"]),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return DiagnosticItem(name, False, f"{detail}; version check failed: {exc}")

    output = (completed.stdout or completed.stderr).strip().splitlines()
    version = output[0] if output else f"exit={completed.returncode}"
    return DiagnosticItem(
        name,
        completed.returncode == 0,
        f"{detail}; {version}",
    )
