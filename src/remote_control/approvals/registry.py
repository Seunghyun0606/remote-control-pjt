from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from remote_control.human_gate import ApprovalOption, HumanGateRequest
from remote_control.storage.models import ApprovalRecord
from remote_control.storage.repositories import ApprovalRepository, EventRepository


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class ApprovalPrompt:
    id: str
    job_id: str
    approval_type: str
    question: str
    details: str | None
    options: tuple[ApprovalOption, ...]
    expires_at: datetime | None


def _approval_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"APPROVAL-{timestamp}-{uuid4().hex[:6].upper()}"


class ApprovalRegistry:
    def __init__(self, *, approvals: ApprovalRepository, events: EventRepository) -> None:
        self.approvals = approvals
        self.events = events

    async def create(
        self,
        *,
        job_id: str,
        project_id: str,
        host_id: str | None,
        requested_by_user: str,
        request: HumanGateRequest,
    ) -> ApprovalRecord:
        current = await self.approvals.pending_for_job(job_id)
        if current is not None:
            return current
        record = ApprovalRecord(
            id=_approval_id(),
            job_id=job_id,
            requested_by_user=requested_by_user,
            approval_type=request.approval_type,
            question=request.question,
            details=request.details,
            options_json=json.dumps(
                [
                    {
                        "key": option.key,
                        "label": option.label,
                        "description": option.description,
                    }
                    for option in request.options
                ],
                ensure_ascii=False,
            ),
            status=ApprovalStatus.PENDING.value,
            expires_at=request.expires_at,
        )
        stored = await self.approvals.add(record)
        await self.events.append(
            "HUMAN_GATE_CREATED",
            job_id=job_id,
            project_id=project_id,
            host_id=host_id,
            payload={
                "approval_id": stored.id,
                "type": stored.approval_type,
                "question": stored.question,
                "options": [option.key for option in self.options(stored)],
            },
        )
        return stored

    async def get(self, approval_id: str) -> ApprovalRecord:
        record = await self.approvals.get(approval_id)
        if record is None:
            raise KeyError(f"unknown approval: {approval_id}")
        return record

    async def list(self, limit: int = 100) -> list[ApprovalRecord]:
        return await self.approvals.list(limit=limit)

    async def pending_for_user(self, user_id: str) -> list[ApprovalRecord]:
        return await self.approvals.pending_for_user(user_id)

    async def pending_for_job(self, job_id: str) -> ApprovalRecord | None:
        return await self.approvals.pending_for_job(job_id)

    async def expired(self, now: datetime) -> list[ApprovalRecord]:
        return await self.approvals.expired_pending(now)

    async def expire(self, approval_id: str) -> ApprovalRecord:
        record = await self.get(approval_id)
        if record.status != ApprovalStatus.PENDING.value:
            return record
        return await self.approvals.update(
            approval_id,
            status=ApprovalStatus.EXPIRED.value,
            resolved_at=datetime.now(timezone.utc),
        )

    async def resolve(
        self,
        approval_id: str,
        *,
        option_key: str | None,
        rejected: bool,
        response_text: str | None = None,
    ) -> ApprovalRecord:
        record = await self.get(approval_id)
        if record.status != ApprovalStatus.PENDING.value:
            raise ValueError(f"approval {approval_id} is not pending")

        selected_option: str | None = None
        if not rejected:
            if not option_key:
                raise ValueError("option is required")
            option_map = {option.key.casefold(): option for option in self.options(record)}
            selected = option_map.get(option_key.casefold())
            if selected is None:
                allowed = ", ".join(option.key for option in self.options(record))
                raise ValueError(f"invalid option; expected one of: {allowed}")
            selected_option = selected.key

        return await self.approvals.update(
            approval_id,
            status=(
                ApprovalStatus.REJECTED.value
                if rejected
                else ApprovalStatus.RESOLVED.value
            ),
            selected_option=selected_option,
            response_text=response_text,
            resolved_at=datetime.now(timezone.utc),
        )

    async def cancel_for_job(self, job_id: str) -> ApprovalRecord | None:
        record = await self.approvals.pending_for_job(job_id)
        if record is None:
            return None
        return await self.approvals.update(
            record.id,
            status=ApprovalStatus.CANCELLED.value,
            resolved_at=datetime.now(timezone.utc),
        )

    def prompt(self, record: ApprovalRecord) -> ApprovalPrompt:
        return ApprovalPrompt(
            id=record.id,
            job_id=record.job_id,
            approval_type=record.approval_type,
            question=record.question,
            details=record.details,
            options=self.options(record),
            expires_at=record.expires_at,
        )

    @staticmethod
    def options(record: ApprovalRecord) -> tuple[ApprovalOption, ...]:
        try:
            raw = json.loads(record.options_json)
        except json.JSONDecodeError:
            return ()
        result: list[ApprovalOption] = []
        if not isinstance(raw, list):
            return ()
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            label = item.get("label")
            description = item.get("description")
            if isinstance(key, str) and isinstance(label, str):
                result.append(
                    ApprovalOption(
                        key=key,
                        label=label,
                        description=description if isinstance(description, str) else None,
                    )
                )
        return tuple(result)
