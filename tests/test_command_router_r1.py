from remote_control.controller.command_router import CommandRouter, Intent


def test_hosts_command(project_registry):
    command = CommandRouter(project_registry).parse("/hosts")
    assert command.intent == Intent.HOSTS


def test_natural_desktop_host(project_registry):
    project = project_registry.get("demo")
    project.allowed_hosts.append("desktop-main")
    project.repository.path["desktop-main"] = "C:/dev/demo"

    command = CommandRouter(project_registry).parse(
        "Demo Project desktop에서 다음 작업 진행해"
    )
    assert command.intent == Intent.RUN_PROJECT
    assert command.host == "desktop-main"
