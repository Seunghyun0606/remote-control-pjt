import json
from pathlib import Path

import pytest

from remote_control.runners.codex import build_codex_command, build_codex_resume_command


def test_codex_command_is_argument_vector():
    command = build_codex_command(
        executable="codex",
        sandbox="workspace-write",
        approval_policy="never",
        working_directory=Path("/srv/project"),
    )
    assert command[:3] == ["codex", "exec", "--json"]
    assert "--sandbox" in command
    assert "workspace-write" in command
    assert "--cd" in command
    assert command[-1] == "-"


def test_codex_resume_command_uses_explicit_session_id():
    command = build_codex_resume_command(
        executable="codex",
        sandbox="workspace-write",
        approval_policy="never",
        working_directory=Path("/srv/project"),
        session_id="thread-123",
    )
    assert command[:4] == ["codex", "exec", "resume", "thread-123"]
    assert "--json" in command
    assert "--cd" in command
    assert command[-1] == "-"


class _AsyncLines:
    def __init__(self, lines):
        self._lines = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _AsyncBytes:
    async def read(self):
        return b""


class _FakeProcess:
    def __init__(self, events):
        self.stdout = _AsyncLines(
            [(json.dumps(event) + "\n").encode("utf-8") for event in events]
        )
        self.stderr = _AsyncBytes()
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.asyncio
async def test_resume_rejects_rebound_thread_identity():
    from remote_control.runners.codex import CodexRunner

    runner = CodexRunner()
    process = _FakeProcess(
        [{"type": "thread.started", "thread_id": "thread-new"}]
    )

    result = await runner._read_result(
        process,
        on_event=None,
        expected_session_id="thread-old",
    )

    assert process.terminated is True
    assert result.returncode != 0
    assert result.session_id == "thread-new"
    assert "SESSION_IDENTITY_MISMATCH" in (result.final_message or "")
