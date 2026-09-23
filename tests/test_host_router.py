import pytest

from remote_control.hosts.registry import HostRegistry
from remote_control.hosts.router import HostRouter, HostUnavailable
from remote_control.projects.models import ProjectDefinition, RepositoryConfig
from remote_control.storage.repositories import EventRepository, HostRepository


@pytest.mark.asyncio
async def test_explicit_and_auto_host_routing(database):
    registry = HostRegistry(
        hosts=HostRepository(database),
        events=EventRepository(database),
        local_host_id="lightsail-main",
    )
    await registry.register_local(
        name="Lightsail",
        os_name="linux",
        capabilities={"codex"},
    )
    await registry.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex", "android"},
    )
    project = ProjectDefinition(
        id="demo",
        name="Demo",
        repository=RepositoryConfig(
            path={
                "lightsail-main": "/srv/demo",
                "desktop-main": "C:/dev/demo",
            }
        ),
        allowed_hosts=["lightsail-main", "desktop-main"],
        default_host="lightsail-main",
    )
    router = HostRouter(registry)

    assert await router.choose(project, "auto") == "lightsail-main"
    assert await router.choose(project, "desktop-main") == "desktop-main"

    await registry.disconnect("desktop-main")
    with pytest.raises(HostUnavailable, match="offline"):
        await router.choose(project, "desktop-main")
