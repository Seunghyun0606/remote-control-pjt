from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path


def subprocess_group_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def terminate_persisted_codex_process(
    pid: int | None,
    *,
    working_directory: str | Path,
    timeout_seconds: float = 10.0,
) -> bool:
    if pid is None or pid <= 0 or not process_exists(pid):
        return True
    command_line = await process_command_line(pid)
    if not command_line:
        return False
    normalized_command = command_line.strip().replace("\\", "/")
    normalized_directory = canonical_working_directory(working_directory)
    if os.name == "nt":
        normalized_command = normalized_command.casefold()
    if "--cd" not in normalized_command or normalized_directory not in normalized_command:
        return False
    return await terminate_process_tree(
        pid,
        timeout_seconds=timeout_seconds,
    )


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
    text = str(value).strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    if len(text) >= 2 and text[1] == ":":
        return text.casefold()
    if text.startswith("//"):
        return text.casefold()
    return text
