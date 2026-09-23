from pathlib import Path

import pytest

from remote_control.transport.protocol import message
from remote_control.transport.runner_ws import RunnerGateway


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000):
        self.closed.append(code)


@pytest.mark.asyncio
async def test_remote_runner_gateway_result_flow():
    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    handle = await gateway.start_remote(
        host_id="desktop-main",
        project_id="demo",
        instruction="continue",
        working_directory=Path("C:/dev/demo"),
    )
    assert websocket.sent

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_ACCEPTED",
            execution_id=handle.execution_id,
            pid=123,
            session_id="thread-1",
        ),
    )
    assert handle.pid == 123

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_RESULT",
            execution_id=handle.execution_id,
            returncode=0,
            session_id="thread-1",
            final_message="done",
        ),
    )
    result = await handle.wait()
    assert result.returncode == 0
    assert result.session_id == "thread-1"
    assert result.final_message == "done"
