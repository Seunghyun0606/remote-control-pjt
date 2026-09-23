import pytest

from remote_control.controller.command_router import CommandParseError, CommandRouter, Intent


def test_parse_run_command(project_registry):
    command = CommandRouter(project_registry).parse("/run demo --host lightsail-main")
    assert command.intent == Intent.RUN_PROJECT
    assert command.project_id == "demo"
    assert command.host == "lightsail-main"


def test_parse_natural_korean(project_registry):
    command = CommandRouter(project_registry).parse("Demo Project 다음 작업 진행해")
    assert command.intent == Intent.RUN_PROJECT
    assert command.project_id == "demo"


def test_natural_text_is_not_shell(project_registry):
    with pytest.raises(CommandParseError):
        CommandRouter(project_registry).parse("rm -rf /")
