from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

HUMAN_GATE_MARKER = "REMOTE_CONTROL_HUMAN_GATE"
_OPTION_KEY = re.compile(r"^[A-Za-z0-9_-]{1,12}$")


@dataclass(frozen=True, slots=True)
class ApprovalOption:
    key: str
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class HumanGateRequest:
    approval_type: str
    question: str
    options: tuple[ApprovalOption, ...]
    details: str | None = None
    expires_at: datetime | None = None


def human_gate_protocol_instruction() -> str:
    return """
Remote Control Human Gate protocol:
If you reach a decision that materially changes architecture, data/schema migration,
destructive state, production behavior, credentials/security posture, or another choice
that should be made by a human, do not guess. Finish the current safe step, then emit
exactly this marker followed by one JSON object and stop the turn:

REMOTE_CONTROL_HUMAN_GATE
{"type":"architecture_change","question":"Question for the human","details":"Why this decision is required","options":[{"key":"A","label":"First option"},{"key":"B","label":"Second option"}]}

Use short option keys containing only letters, numbers, underscore or hyphen.
Do not continue the gated change after emitting the marker. Wait for the next user turn.
""".strip()


def extract_human_gate(event: dict[str, Any]) -> HumanGateRequest | None:
    event_type = str(event.get("type") or event.get("event_type") or "").casefold()

    if event_type in {"human_gate", "human.gate", "request_user_input", "request.user_input"}:
        payload = event.get("human_gate") or event.get("payload") or event
        if isinstance(payload, dict):
            return _from_payload(payload)

    item = event.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type") or "").casefold()
        if item_type in {"human_gate", "request_user_input", "request.user_input"}:
            return _from_payload(item)
        for key in ("text", "content"):
            text = item.get(key)
            if isinstance(text, str):
                parsed = extract_human_gate_from_text(text)
                if parsed is not None:
                    return parsed

    for key in ("message", "text"):
        text = event.get(key)
        if isinstance(text, str):
            parsed = extract_human_gate_from_text(text)
            if parsed is not None:
                return parsed
    return None


def extract_human_gate_from_text(text: str) -> HumanGateRequest | None:
    marker_index = text.find(HUMAN_GATE_MARKER)
    if marker_index < 0:
        return None
    tail = text[marker_index + len(HUMAN_GATE_MARKER) :]
    object_index = tail.find("{")
    if object_index < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(tail[object_index:])
    except json.JSONDecodeError:
        return None
    return _from_payload(payload) if isinstance(payload, dict) else None


def _from_payload(payload: dict[str, Any]) -> HumanGateRequest | None:
    if isinstance(payload.get("questions"), list) and payload["questions"]:
        first = payload["questions"][0]
        if isinstance(first, dict):
            payload = {**payload, **first}

    question = payload.get("question") or payload.get("prompt")
    if not isinstance(question, str) or not question.strip():
        return None

    approval_type = payload.get("approval_type") or payload.get("gate_type") or payload.get("type")
    if (
        not isinstance(approval_type, str)
        or not approval_type.strip()
        or approval_type.casefold()
        in {"human_gate", "human.gate", "request_user_input", "request.user_input"}
    ):
        approval_type = "human_decision"

    details = payload.get("details") or payload.get("description") or payload.get("header")
    if not isinstance(details, str) or not details.strip():
        details = None

    options = _parse_options(payload.get("options"))
    if not options:
        options = (
            ApprovalOption("APPROVE", "Approve"),
            ApprovalOption("REJECT", "Reject"),
        )

    return HumanGateRequest(
        approval_type=approval_type.strip()[:64],
        question=question.strip()[:2000],
        options=options,
        details=details.strip()[:4000] if details else None,
    )


def _parse_options(raw: Any) -> tuple[ApprovalOption, ...]:
    if not isinstance(raw, list):
        return ()
    parsed: list[ApprovalOption] = []
    for index, item in enumerate(raw[:8]):
        key: str
        label: str
        description: str | None = None
        if isinstance(item, str):
            key = item.strip()
            label = item.strip()
        elif isinstance(item, dict):
            raw_key = item.get("key") or item.get("id") or chr(ord("A") + index)
            raw_label = item.get("label") or item.get("value") or item.get("text") or raw_key
            key = str(raw_key).strip()
            label = str(raw_label).strip()
            raw_description = item.get("description")
            if isinstance(raw_description, str) and raw_description.strip():
                description = raw_description.strip()[:1000]
        else:
            continue
        if not _OPTION_KEY.fullmatch(key) or not label:
            continue
        parsed.append(
            ApprovalOption(
                key=key,
                label=label[:200],
                description=description,
            )
        )
    return tuple(parsed)
