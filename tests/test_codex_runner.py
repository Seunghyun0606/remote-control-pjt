import json
from pathlib import Path

import pytest

from remote_control.process_control import ProcessSafetyError
from remote_control.runners.codex import (
    CODEX_READ_CHUNK_BYTES,
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
        str(Path("/srv/project")),
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


class _AsyncChunkReader:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, size=-1):
        del size
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _AsyncBytes:
    async def read(self):
        return b""


class _FakeProcess:
    def __init__(self, events):
        self.stdout = _AsyncChunkReader(
            [(json.dumps(event) + "\n").encode("utf-8") for event in events]
        )
        self.stderr = _AsyncBytes()
        self.returncode = None
        self.terminated = False
        self.pid = 12345

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.asyncio
async def test_resume_rejects_rebound_thread_identity(monkeypatch):
    from remote_control.runners.codex import CodexRunner

    async def terminate(pid, *, process, timeout_seconds):
        assert pid == 12345
        assert timeout_seconds == 10
        process.terminate()
        return True

    monkeypatch.setattr(
        "remote_control.runners.codex.terminate_process_tree",
        terminate,
    )
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
        self.stdout = _AsyncChunkReader([])
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
async def test_post_spawn_initialization_failure_fails_closed_when_cleanup_unproven(
    monkeypatch,
    tmp_path,
):
    process = _SpawnedProcess()
    process.returncode = None

    class _FailingStdin(_CaptureStdin):
        async def drain(self):
            raise RuntimeError("stdin drain failed")

    process.stdin = _FailingStdin()

    class _Resolution:
        resolved = "codex"

        def build_command(self, args):
            return ["codex", *args]

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    async def cannot_terminate(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        "remote_control.runners.codex.resolve_executable",
        lambda executable: _Resolution(),
    )
    monkeypatch.setattr(
        "remote_control.runners.codex.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        "remote_control.runners.codex.terminate_process_tree",
        cannot_terminate,
    )

    with pytest.raises(ProcessSafetyError, match="could not be terminated") as exc:
        await CodexRunner().start(
            project_id="demo",
            instruction="test",
            working_directory=tmp_path,
        )

    assert exc.value.pid == 999


@pytest.mark.asyncio
async def test_codex_spawn_uses_chunk_reader_without_large_line_limit(monkeypatch, tmp_path):
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
    assert "limit" not in captured["kwargs"]
    assert CODEX_READ_CHUNK_BYTES == 64 * 1024



@pytest.mark.asyncio
async def test_chunk_reader_handles_json_split_across_chunks():
    from remote_control.runners.codex import _iter_jsonl_records

    reader = _AsyncChunkReader([b'{"type":"thread.', b'started","thread_id":"t1"}\n'])
    records = [record async for record in _iter_jsonl_records(reader)]

    assert records == [(b'{"type":"thread.started","thread_id":"t1"}', 0)]


@pytest.mark.asyncio
async def test_chunk_reader_truncates_oversized_record_without_failing(monkeypatch):
    import remote_control.runners.codex as codex_module

    monkeypatch.setattr(codex_module, "CODEX_EVENT_MAX_BYTES", 8)
    reader = _AsyncChunkReader([b"abcdefghijkl", b"mnop\nnext\n"])
    records = [record async for record in codex_module._iter_jsonl_records(reader)]

    assert records[0][0] == b"abcdefgh"
    assert records[0][1] == 8
    assert records[1] == (b"next", 0)


@pytest.mark.asyncio
async def test_read_result_survives_oversized_jsonl_event(monkeypatch):
    import remote_control.runners.codex as codex_module

    monkeypatch.setattr(codex_module, "CODEX_EVENT_MAX_BYTES", 16)
    process = _FakeProcess([])
    process.stdout = _AsyncChunkReader(
        [b'{"type":"tool","text":"', b"x" * 64 + b'"}\n']
    )
    events = []

    async def capture(event):
        events.append(event)

    result = await CodexRunner()._read_result(
        process,
        on_event=capture,
    )

    assert result.returncode == 0
    assert events
    assert events[0]["type"] == "raw_output_truncated"
    assert events[0]["omitted_bytes"] > 0



@pytest.mark.asyncio
async def test_reader_callback_failure_terminates_codex_process_tree(monkeypatch):
    process = _FakeProcess([{"type": "fake.progress", "message": "boom"}])
    terminated = []

    async def terminate(pid, *, process, timeout_seconds):
        terminated.append((pid, timeout_seconds))
        process.terminate()
        return True

    async def fail_callback(_event):
        raise RuntimeError("event callback failed")

    monkeypatch.setattr(
        "remote_control.runners.codex.terminate_process_tree",
        terminate,
    )

    with pytest.raises(RuntimeError, match="event callback failed"):
        await CodexRunner()._read_result(
            process,
            on_event=fail_callback,
        )

    assert terminated == [(12345, 10)]
    assert process.terminated is True
