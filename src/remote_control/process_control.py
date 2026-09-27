from __future__ import annotations

import asyncio
import ctypes
import ntpath
import os
import posixpath
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    if not command_line_matches_working_directory(
        command_line,
        working_directory,
    ):
        return False
    return await terminate_process_tree(
        pid,
        timeout_seconds=timeout_seconds,
    )


async def legacy_persisted_process_is_gone_or_reused(
    pid: int | None,
    *,
    working_directory: str | Path,
    persisted_at: datetime | None = None,
) -> bool:
    """Safely reconcile pre-identity persisted PIDs.

    Legacy rows created before executable/start-token persistence cannot prove
    ownership of a live PID. They may still be cleared when the PID is gone,
    when the current process provably started after the legacy row was last
    persisted, or when its command line demonstrably targets another working
    directory. Ambiguous cases remain fail-closed.
    """
    if pid is None or pid <= 0 or not process_exists(pid):
        return True

    if persisted_at is not None:
        started_at = await process_started_at(pid)
        if started_at is not None:
            persisted_utc = _as_utc(persisted_at)
            # The original process is spawned before its PID is persisted on
            # the Job row. A process that started materially later therefore
            # cannot be that original Codex process; the numeric PID was reused.
            if started_at > persisted_utc + timedelta(seconds=1):
                return True

    command_line = await process_command_line(pid)
    if not command_line:
        return False
    return not command_line_matches_working_directory(
        command_line,
        working_directory,
    )


async def process_started_at(pid: int) -> datetime | None:
    identity = await process_identity(pid)
    if identity is None or not identity.start_token.startswith("windows:"):
        return None
    try:
        ticks = int(identity.start_token.split(":", 1)[1])
    except (ValueError, IndexError):
        return None
    if ticks <= 0:
        return None
    # .NET DateTime ticks are 100 ns intervals since 0001-01-01 UTC.
    return datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(
        microseconds=ticks // 10
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def command_line_matches_working_directory(
    command_line: str,
    working_directory: str | Path,
) -> bool:
    normalized_command = command_line.strip().replace("\\", "/")
    normalized_directory = canonical_working_directory(working_directory)
    windows_like = (
        (len(normalized_directory) >= 2 and normalized_directory[1] == ":")
        or normalized_directory.startswith("//")
    )
    if os.name == "nt" or windows_like:
        normalized_command = normalized_command.casefold()
        normalized_directory = normalized_directory.casefold()

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
    return plain_match or quoted_match


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
    if pid <= 0:
        return None

    from ctypes import wintypes

    process_query_limited_information = 0x1000
    dotnet_ticks_at_filetime_epoch = 504_911_232_000_000_000

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query_image.restype = wintypes.BOOL
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    get_process_times.restype = wintypes.BOOL

    handle = open_process(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return None

    try:
        size = wintypes.DWORD(32768)
        image_buffer = ctypes.create_unicode_buffer(size.value)
        if not query_image(handle, 0, image_buffer, ctypes.byref(size)):
            return None
        executable = image_buffer.value.strip()
        if not executable:
            return None

        creation = FileTime()
        exit_time = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None

        filetime_ticks = (
            int(creation.dwHighDateTime) << 32
        ) | int(creation.dwLowDateTime)
        if filetime_ticks <= 0:
            return None

        # Preserve the existing persisted token format produced by
        # PowerShell's DateTime.ToUniversalTime().Ticks: 100 ns ticks since
        # 0001-01-01, while FILETIME starts at 1601-01-01.
        start_ticks = filetime_ticks + dotnet_ticks_at_filetime_epoch
        return ProcessIdentity(
            executable=_normalize_executable(executable),
            start_token=f"windows:{start_ticks}",
        )
    finally:
        close_handle(handle)


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
    if os.name == "nt":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_exists(pid: int) -> bool:
    """Check PID liveness from the system's active process enumeration."""
    if pid <= 0:
        return False

    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    enum_processes = psapi.EnumProcesses
    enum_processes.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    enum_processes.restype = wintypes.BOOL

    capacity = 4096
    while capacity <= 65536:
        process_ids = (wintypes.DWORD * capacity)()
        bytes_returned = wintypes.DWORD()
        buffer_size = ctypes.sizeof(process_ids)

        if not enum_processes(
            process_ids,
            buffer_size,
            ctypes.byref(bytes_returned),
        ):
            # Enumeration failure is unusual. Preserve fail-closed behavior
            # rather than claiming a potentially live process is gone.
            return True

        count = bytes_returned.value // ctypes.sizeof(wintypes.DWORD)
        if bytes_returned.value < buffer_size:
            return pid in process_ids[:count]

        capacity *= 2

    # If the process table unexpectedly exceeds the maximum buffer, remain
    # fail-closed instead of declaring the PID absent.
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
