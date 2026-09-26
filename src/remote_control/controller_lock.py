from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class ControllerAlreadyRunningError(RuntimeError):
    pass


class RunnerAlreadyRunningError(RuntimeError):
    pass


class ControllerRuntimeLock:
    """Process-scoped cross-platform singleton lock for one Controller runtime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            # Do not read the byte that another Windows process may already
            # hold with msvcrt.locking(). A competing reader receives
            # PermissionError before we get a chance to translate lock
            # contention into ControllerAlreadyRunningError.
            #
            # fstat() inspects file metadata without touching the locked byte.
            # msvcrt.locking() needs at least one byte in the file, so seed an
            # empty lock file before attempting the non-blocking byte-range
            # lock. Concurrent seed writes are harmless; the lock below is the
            # authoritative ownership decision.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            _lock_file(handle)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
        except Exception:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            _unlock_file(handle)
        finally:
            handle.close()

    def __enter__(self) -> "ControllerRuntimeLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def _lock_file(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise ControllerAlreadyRunningError(
            "another Remote Control Controller already owns this runtime: "
            f"{Path(handle.name)}"
        ) from exc


def _unlock_file(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # OS releases advisory locks when the descriptor/process exits.
        pass


class RunnerRuntimeLock(ControllerRuntimeLock):
    def acquire(self) -> None:
        try:
            super().acquire()
        except ControllerAlreadyRunningError as exc:
            raise RunnerAlreadyRunningError(
                "another Remote Control Runner already owns this journal runtime: "
                f"{self.path}"
            ) from exc
