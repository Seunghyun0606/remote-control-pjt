from pathlib import Path

import pytest

from remote_control.runners.fake import FakeAgentRunner
from remote_control.runners.hybrid import HybridAgentRunner
from remote_control.transport.runner_ws import RunnerGateway


@pytest.mark.asyncio
async def test_hybrid_uses_local_runner_for_local_host():
    fake = FakeAgentRunner()
    hybrid = HybridAgentRunner(
        local_host_id="lightsail-main",
        local_runner=fake,
        gateway=RunnerGateway(),
    )
    handle = await hybrid.start(
        project_id="demo",
        instruction="continue",
        working_directory=Path("/tmp/demo"),
        host_id="lightsail-main",
    )
    await handle.wait()
    assert fake.started[0]["host_id"] == "lightsail-main"
