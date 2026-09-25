import json
from pathlib import Path

import pytest

from remote_control.runners.codex import (
    CODEX_STREAM_LIMIT_BYTES,
    CodexRunner,
    build_codex_command,
    build_codex_environment,
    build_codex_resume_command,
    prepare_codex_instruction,
)


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


def test_codex_resume_command_places_exec_options_before_resume():
    command = build_codex_resume_command(
        executable="codex",
        sandbox="workspace-write",
        approval_policy="never",
        working_directory=Path("/srv/project"),
        session_id="thread-123",
    )
    assert command == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--cd",
        "/srv/project",
        "--config",
        'approval_policy="never"',
        "resume",
        "thread-123",
        "-",
    ]


def test_windows_instruction_adds_utf8_guard_without_changing_user_text():
    original = "README의 한글 문구를 확인해줘"
    prepared = prepare_codex_instruction(original, os_name="Windows")

    assert prepared.startswith(original)
    assert "repository text is UTF-8" in prepared
    assert "-Encoding UTF8" in prepared
    assert "mojibake" in prepared


def test_non_windows_instruction_is_unchanged():
    original = "README의 한글 문구를 확인해줘"
    assert prepare_codex_instruction(original, os_name="Linux") == original


def test_windows_codex_environment_forces_python_utf8(monkeypatch):
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp949")

    env = build_codex_environment(
        codex_home=r"C:\Users\tester\.codex",
        os_name="Windows",
    )

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["CODEX_HOME"] == r"C:\Users\tester\.codex"


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



class _CaptureStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _SpawnedProcess:
    def __init__(self):
        self.stdin = _CaptureStdin()
        self.stdout = _AsyncLines([])
        self.stderr = _AsyncBytes()
        self.pid = 999
        self.returncode = 0

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


@pytest.mark.asyncio
async def test_codex_spawn_uses_large_stream_limit(monkeypatch, tmp_path):
    captured = {}
    process = _SpawnedProcess()

    class _Resolution:
        resolved = "codex"

        def build_command(self, args):
            return ["codex", *args]

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(
        "remote_control.runners.codex.resolve_executable",
        lambda executable: _Resolution(),
    )
    monkeypatch.setattr(
        "remote_control.runners.codex.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    runner = CodexRunner()
    handle = await runner.start(
        project_id="demo",
        instruction="test",
        working_directory=tmp_path,
    )
    result = await handle.wait()

    assert result.returncode == 0
    assert captured["kwargs"]["limit"] == CODEX_STREAM_LIMIT_BYTES
    assert CODEX_STREAM_LIMIT_BYTES >= 16 * 1024 * 1024
