from pathlib import Path

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
