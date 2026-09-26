from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from remote_control.runners.base import AgentRunResult


@dataclass(slots=True)
class RunnerExecutionEntry:
    execution_id: str
    pid: int | None
    working_directory: str
    state: str
    boot_id: str
    process_executable: str | None = None
    process_start_token: str | None = None
    session_id: str | None = None
    returncode: int | None = None
    final_message: str | None = None
    retry_kind: str | None = None
    retry_at: str | None = None
    started_at: str = ""
    completed_at: str | None = None

    def to_result(self) -> AgentRunResult:
        retry_at = None
        if self.retry_at:
            retry_at = datetime.fromisoformat(self.retry_at.replace("Z", "+00:00"))
        return AgentRunResult(
            returncode=self.returncode if self.returncode is not None else 1,
            session_id=self.session_id,
            final_message=self.final_message,
            retry_kind=self.retry_kind,
            retry_at=retry_at,
        )


class RunnerExecutionJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        instance_id, entries = self._load()
        self.instance_id = instance_id or uuid4().hex
        self._entries = entries
        if instance_id is None:
            self._persist()

    def list(self) -> list[RunnerExecutionEntry]:
        return list(self._entries.values())

    def get(self, execution_id: str) -> RunnerExecutionEntry | None:
        return self._entries.get(execution_id)

    def reserve(
        self,
        *,
        execution_id: str,
        working_directory: str,
        boot_id: str,
        session_id: str | None,
    ) -> RunnerExecutionEntry:
        now = datetime.now(timezone.utc).isoformat()
        entry = RunnerExecutionEntry(
            execution_id=execution_id,
            pid=None,
            working_directory=working_directory,
            state="STARTING",
            boot_id=boot_id,
            session_id=session_id,
            started_at=now,
        )
        self._entries[execution_id] = entry
        self._persist()
        return entry

    def attach_pid(
        self,
        execution_id: str,
        pid: int,
        *,
        process_executable: str | None = None,
        process_start_token: str | None = None,
    ) -> RunnerExecutionEntry:
        entry = self._entries.get(execution_id)
        if entry is None:
            raise KeyError(f"unknown runner execution: {execution_id}")
        if pid <= 0:
            raise ValueError("runner execution pid must be positive")
        entry.pid = pid
        entry.process_executable = process_executable
        entry.process_start_token = process_start_token
        entry.state = "RUNNING"
        self._persist()
        return entry

    def update_session(self, execution_id: str, session_id: str) -> None:
        entry = self._entries.get(execution_id)
        if entry is None or entry.session_id == session_id:
            return
        entry.session_id = session_id
        self._persist()

    def complete(
        self,
        execution_id: str,
        result: AgentRunResult,
    ) -> RunnerExecutionEntry:
        entry = self._entries.get(execution_id)
        if entry is None:
            raise KeyError(f"unknown runner execution: {execution_id}")
        entry.state = "COMPLETED"
        entry.session_id = result.session_id or entry.session_id
        entry.returncode = result.returncode
        entry.final_message = result.final_message
        entry.retry_kind = result.retry_kind
        entry.retry_at = result.retry_at.isoformat() if result.retry_at is not None else None
        entry.completed_at = datetime.now(timezone.utc).isoformat()
        self._persist()
        return entry

    def remove(self, execution_id: str) -> None:
        if self._entries.pop(execution_id, None) is not None:
            self._persist()

    def _load(self) -> tuple[str | None, dict[str, RunnerExecutionEntry]]:
        if not self.path.exists():
            return None, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"runner execution journal is unreadable: {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"runner execution journal is invalid: {self.path}")
        raw_instance_id = payload.get("runner_instance_id")
        instance_id = (
            raw_instance_id
            if isinstance(raw_instance_id, str) and raw_instance_id.strip()
            else None
        )
        items = payload.get("executions")
        if not isinstance(items, list):
            raise RuntimeError(f"runner execution journal is invalid: {self.path}")
        entries: dict[str, RunnerExecutionEntry] = {}
        for raw in items:
            if not isinstance(raw, dict):
                raise RuntimeError(f"runner execution journal is invalid: {self.path}")
            entry = _entry_from_dict(raw)
            entries[entry.execution_id] = entry
        return instance_id, entries

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "runner_instance_id": self.instance_id,
            "executions": [
                asdict(entry)
                for entry in sorted(
                    self._entries.values(),
                    key=lambda item: item.execution_id,
                )
            ]
        }
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.path)


def _entry_from_dict(raw: dict[str, Any]) -> RunnerExecutionEntry:
    execution_id = str(raw.get("execution_id") or "")
    pid = raw.get("pid")
    state = str(raw.get("state") or "")
    boot_id = str(raw.get("boot_id") or "")
    working_directory = str(raw.get("working_directory") or "")
    if (
        not execution_id
        or (
            state in {"RUNNING", "COMPLETED"}
            and (not isinstance(pid, int) or pid <= 0)
        )
        or (state == "STARTING" and pid is not None)
        or state not in {"STARTING", "RUNNING", "COMPLETED"}
        or not boot_id
        or not working_directory
    ):
        raise RuntimeError("runner execution journal contains an invalid execution entry")
    return RunnerExecutionEntry(
        execution_id=execution_id,
        pid=pid if isinstance(pid, int) else None,
        working_directory=working_directory,
        state=state,
        boot_id=boot_id,
        process_executable=_optional_str(raw.get("process_executable")),
        process_start_token=_optional_str(raw.get("process_start_token")),
        session_id=_optional_str(raw.get("session_id")),
        returncode=raw.get("returncode") if isinstance(raw.get("returncode"), int) else None,
        final_message=_optional_str(raw.get("final_message")),
        retry_kind=_optional_str(raw.get("retry_kind")),
        retry_at=_optional_str(raw.get("retry_at")),
        started_at=_optional_str(raw.get("started_at")) or "",
        completed_at=_optional_str(raw.get("completed_at")),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
