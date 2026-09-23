from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ExecutableResolutionError(FileNotFoundError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutableResolution:
    configured: str
    resolved: str
    kind: str
    launcher: tuple[str, ...]

    def build_command(self, args: list[str]) -> list[str]:
        if self.kind == "cmd":
            command = subprocess.list2cmdline([self.resolved, *args])
            return [*self.launcher, command]
        if self.kind == "powershell":
            return [*self.launcher, self.resolved, *args]
        return [self.resolved, *args]


def resolve_executable(
    configured: str,
    *,
    os_name: str | None = None,
) -> ExecutableResolution:
    configured = configured.strip()
    if not configured:
        raise ExecutableResolutionError("executable is empty")

    system = (os_name or platform.system()).casefold()
    direct = Path(configured).expanduser()
    resolved: str | None = None

    if direct.is_absolute() or direct.parent != Path("."):
        if direct.exists() and direct.is_file():
            resolved = str(direct.resolve())
    else:
        resolved = shutil.which(configured)

    if resolved is None:
        raise ExecutableResolutionError(
            f"required executable not found: {configured}"
        )

    suffix = Path(resolved).suffix.casefold()
    if system == "windows" and suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not comspec:
            raise ExecutableResolutionError(
                f"Windows command wrapper found but cmd.exe is unavailable: {resolved}"
            )
        return ExecutableResolution(
            configured=configured,
            resolved=resolved,
            kind="cmd",
            launcher=(comspec, "/d", "/s", "/c"),
        )

    if system == "windows" and suffix == ".ps1":
        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if not powershell:
            raise ExecutableResolutionError(
                f"PowerShell wrapper found but PowerShell is unavailable: {resolved}"
            )
        return ExecutableResolution(
            configured=configured,
            resolved=resolved,
            kind="powershell",
            launcher=(
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
            ),
        )

    return ExecutableResolution(
        configured=configured,
        resolved=resolved,
        kind="native",
        launcher=(),
    )
