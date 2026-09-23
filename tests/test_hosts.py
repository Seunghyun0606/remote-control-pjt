import pytest

from remote_control.hosts.models import HostStatus
from remote_control.hosts.registry import HostRegistry
from remote_control.storage.repositories import EventRepository, HostRepository


@pytest.mark.asyncio
async def test_host_register_heartbeat_and_disconnect(database):
    registry = HostRegistry(
        hosts=HostRepository(database),
        events=EventRepository(database),
        local_host_id="lightsail-main",
    )
    local = await registry.register_local(
        name="Lightsail",
        os_name="linux",
        capabilities={"codex", "git"},
    )
    assert local.status == HostStatus.ONLINE

    desktop = await registry.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex", "git", "android"},
    )
    assert desktop.status == HostStatus.ONLINE
    assert "android" in desktop.capabilities

    await registry.heartbeat("desktop-main")
    await registry.disconnect("desktop-main")
    current = await registry.get("desktop-main")
    assert current is not None
    assert current.status == HostStatus.OFFLINE
