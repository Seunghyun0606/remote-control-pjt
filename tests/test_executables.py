from __future__ import annotations

import pytest

from remote_control.executables import ExecutableResolutionError, resolve_executable


def test_windows_cmd_wrapper_is_launched_through_cmd(monkeypatch):
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    def fake_which(name: str):
        if name == "codex":
            return r"C:\Users\tester\AppData\Roaming\npm\codex.CMD"
        return None

    monkeypatch.setattr("remote_control.executables.shutil.which", fake_which)

    resolution = resolve_executable("codex", os_name="Windows")

    assert resolution.kind == "cmd"
    assert resolution.resolved.endswith("codex.CMD")
    command = resolution.build_command(["exec", "--json", "-"])
    assert command[:4] == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
    ]
    assert "codex.CMD" in command[4]
    assert "exec" in command[4]


def test_native_executable_is_used_directly(monkeypatch):
    monkeypatch.setattr(
        "remote_control.executables.shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    resolution = resolve_executable("codex", os_name="Linux")
    assert resolution.kind == "native"
    assert resolution.build_command(["--version"]) == [
        "/usr/local/bin/codex",
        "--version",
    ]


def test_missing_executable_has_actionable_error(monkeypatch):
    monkeypatch.setattr("remote_control.executables.shutil.which", lambda name: None)
    with pytest.raises(ExecutableResolutionError, match="required executable not found: codex"):
        resolve_executable("codex", os_name="Windows")
