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
