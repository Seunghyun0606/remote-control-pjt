from __future__ import annotations

import asyncio
import json
import ntpath
import os
import posixpath
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProcessSafetyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        pid: int,
        process_executable: str | None = None,
        process_start_token: str | None = None,
    ) -> None:
        super().__init__(message)
        self.pid = pid
        self.process_executable = process_executable
        self.process_start_token = process_start_token


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    executable: str
    start_token: str


def subprocess_group_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def terminate_persisted_codex_process(
    pid: int | None,
    *,
    working_directory: str | Path,
    expected_executable: str | None,
    expected_start_token: str | None,
    timeout_seconds: float = 10.0,
) -> bool:
    if pid is None or pid <= 0 or not process_exists(pid):
        return True
    if not expected_executable or not expected_start_token:
        return False

    identity = await process_identity(pid)
    if identity is None:
        return False
    if (
        _normalize_executable(identity.executable)
        != _normalize_executable(expected_executable)
        or identity.start_token != expected_start_token
    ):
        return False

    command_line = await process_command_line(pid)
    if not command_line:
        return False
    normalized_command = command_line.strip().replace("\\", "/")
    normalized_directory = canonical_working_directory(working_directory)
    if os.name == "nt":
        normalized_command = normalized_command.casefold()
    plain = f"--cd {normalized_directory}"
    quoted = (
        f'--cd "{normalized_directory}"',
        f"--cd '{normalized_directory}'",
    )
    plain_match = (
        normalized_command.endswith(plain)
        or f"{plain} " in normalized_command
    )
    quoted_match = any(pattern in normalized_command for pattern in quoted)
    if not plain_match and not quoted_match:
        return False
    return await terminate_process_tree(
        pid,
        timeout_seconds=timeout_seconds,
    )


async def process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        return await _windows_process_identity(pid)

    proc_exe = Path(f"/proc/{pid}/exe")
    proc_stat = Path(f"/proc/{pid}/stat")
    if not proc_exe.exists() or not proc_stat.exists():
        return None
    try:
        executable = os.readlink(proc_exe)
        raw_stat = proc_stat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    closing_paren = raw_stat.rfind(")")
    if closing_paren < 0:
        return None
    fields_after_comm = raw_stat[closing_paren + 2 :].split()
    # /proc/<pid>/stat field 22 is process starttime in clock ticks since boot.
    # fields_after_comm begins at field 3, so starttime is index 19.
    if len(fields_after_comm) <= 19:
        return None
    starttime = fields_after_comm[19]
    if not starttime:
        return None
    return ProcessIdentity(
        executable=_normalize_executable(executable),
        start_token=f"linux:{starttime}",
    )


def _normalize_executable(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if os.name == "nt":
        normalized = normalized.casefold()
    return normalized


async def process_command_line(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        return await _windows_process_command_line(pid)

    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():
        try:
            raw = proc_path.read_bytes()
        except OSError:
            return None
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()

    try:
        process = await asyncio.create_subprocess_exec(
            "ps",
            "-p",
            str(pid),
            "-o",
            "command=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except OSError:
        return None
    if process.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


async def terminate_process_tree(
    pid: int | None,
    *,
    process: asyncio.subprocess.Process | None = None,
    timeout_seconds: float = 10.0,
) -> bool:
    if pid is None or pid <= 0:
        return True
    if process is not None and process.returncode is not None:
        return True

    if os.name == "nt":
        await _terminate_windows_tree(pid)
    else:
        _signal_posix_tree(pid, signal.SIGTERM)

    if await _wait_stopped(pid, process=process, timeout_seconds=timeout_seconds):
        return True

    if os.name == "nt":
        await _terminate_windows_tree(pid)
    else:
        _signal_posix_tree(pid, signal.SIGKILL)

    return await _wait_stopped(pid, process=process, timeout_seconds=2.0)


async def _windows_process_identity(pid: int) -> ProcessIdentity | None:
    script = (
        "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = "
        + str(pid)
        + "' -ErrorAction SilentlyContinue; "
        "if ($null -ne $p -and $null -ne $p.CreationDate "
        "-and -not [string]::IsNullOrWhiteSpace([string]$p.ExecutablePath)) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$o = [pscustomobject]@{ executable = [string]$p.ExecutablePath; "
        "start_token = $p.CreationDate.ToUniversalTime().Ticks.ToString() }; "
        "$o | ConvertTo-Json -Compress }"
    )
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return None
    if process.returncode != 0:
        return None
    raw = stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    executable = payload.get("executable")
    start_token = payload.get("start_token")
    if not isinstance(executable, str) or not executable.strip():
        return None
    if not isinstance(start_token, str) or not start_token.strip():
        return None
    return ProcessIdentity(
        executable=_normalize_executable(executable),
        start_token=f"windows:{start_token.strip()}",
    )


async def _windows_process_command_line(pid: int) -> str | None:
    script = (
        "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = "
        + str(pid)
        + "' -ErrorAction SilentlyContinue; "
        "if ($null -ne $p) { [Console]::OutputEncoding = "
        "[System.Text.Encoding]::UTF8; $p.CommandLine }"
    )
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return None
    if process.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


async def _terminate_windows_tree(pid: int) -> None:
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return
    try:
        await asyncio.wait_for(killer.wait(), timeout=5)
    except TimeoutError:
        killer.kill()
        await killer.wait()


def _signal_posix_tree(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return


async def _wait_stopped(
    pid: int,
    *,
    process: asyncio.subprocess.Process | None,
    timeout_seconds: float,
) -> bool:
    if process is not None:
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            return True
        except TimeoutError:
            pass

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not process_exists(pid):
            return True
        await asyncio.sleep(0.1)
    return not process_exists(pid)


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def canonical_working_directory(value: str | Path) -> str:
    raw = str(value).strip()
    windows_like = (
        (len(raw) >= 2 and raw[1] == ":")
        or raw.startswith("\\\\")
        or raw.startswith("//")
    )
    if windows_like:
        return ntpath.normcase(ntpath.normpath(raw)).replace("\\", "/")
    return posixpath.normpath(raw.replace("\\", "/"))
