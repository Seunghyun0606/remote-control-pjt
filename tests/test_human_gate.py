from __future__ import annotations

from pathlib import Path

import pytest

from remote_control.approvals.registry import ApprovalPrompt
from remote_control.human_gate import (
    ApprovalOption,
    HUMAN_GATE_MARKER,
    extract_human_gate,
    extract_human_gate_from_text,
)
from remote_control.messaging.telegram import (
    build_approval_markup,
    parse_approval_callback,
)
from remote_control.transport.protocol import message
from remote_control.transport.runner_ws import RunnerGateway


def test_extracts_codex_human_gate_marker():
    text = (
        "A migration decision is required.\n"
        f"{HUMAN_GATE_MARKER}\n"
        '{"type":"architecture_change","question":"Migrate schema?",'
        '"details":"This changes persisted data.",'
        '"options":[{"key":"A","label":"Keep v2"},'
        '{"key":"B","label":"Migrate to v3"}]}'
    )
    gate = extract_human_gate_from_text(text)

    assert gate is not None
    assert gate.approval_type == "architecture_change"
    assert gate.question == "Migrate schema?"
    assert [option.key for option in gate.options] == ["A", "B"]


def test_extracts_request_user_input_shape():
    gate = extract_human_gate(
        {
            "type": "request_user_input",
            "questions": [
                {
                    "question": "Choose storage",
                    "options": [
                        {"label": "SQLite", "description": "Local"},
                        {"label": "Postgres", "description": "Remote"},
                    ],
                }
            ],
        }
    )

    assert gate is not None
    assert gate.approval_type == "human_decision"
    assert [option.key for option in gate.options] == ["A", "B"]
    assert gate.options[1].label == "Postgres"


def test_telegram_approval_buttons_are_compact():
    prompt = ApprovalPrompt(
        id="APPROVAL-20260923-230000-ABC123",
        job_id="JOB-1",
        approval_type="architecture_change",
        question="Migrate?",
        details="schema change",
        options=(
            ApprovalOption("A", "Keep"),
            ApprovalOption("B", "Migrate"),
        ),
        expires_at=None,
    )

    markup = build_approval_markup(prompt)
    callback_values = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert callback_values
    assert all(len(value.encode("utf-8")) <= 64 for value in callback_values)
    assert parse_approval_callback(
        "approval:APPROVAL-20260923-230000-ABC123:choose:B"
    ) == ("APPROVAL-20260923-230000-ABC123", "choose", "B")


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        del code


@pytest.mark.asyncio
async def test_runner_gateway_forwards_human_gate_event():
    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    events: list[dict] = []
    await gateway.attach("desktop-main", websocket)

    async def on_event(event: dict) -> None:
        events.append(event)

    handle = await gateway.start_remote(
        host_id="desktop-main",
        project_id="demo",
        instruction="continue",
        working_directory=Path("C:/dev/demo"),
        on_event=on_event,
    )
    await gateway.handle(
        "desktop-main",
        message(
            "HUMAN_GATE",
            execution_id=handle.execution_id,
            session_id="thread-1",
            event={
                "type": "HUMAN_GATE",
                "approval_type": "architecture_change",
                "question": "Proceed?",
                "options": [{"key": "A", "label": "Yes"}],
            },
        ),
    )

    assert events
    assert events[0]["question"] == "Proceed?"
    await handle.cancel()
