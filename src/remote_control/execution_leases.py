from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from remote_control.process_control import canonical_working_directory
from remote_control.storage.models import ExecutionLeaseRecord
from remote_control.storage.repositories import EventRepository, ExecutionLeaseRepository


class ExecutionLeaseBusyError(RuntimeError):
    pass


class ExecutionLeaseRegistry:
    def __init__(
        self,
        *,
        leases: ExecutionLeaseRepository,
        events: EventRepository,
    ) -> None:
        self.leases = leases
        self.events = events

    async def acquire(
        self,
        *,
        job_id: str,
        project_id: str,
        host_id: str,
        working_directory: str | Path,
    ) -> ExecutionLeaseRecord:
        normalized = canonical_working_directory(working_directory)
        lease_key = _lease_key(host_id, normalized)

        current = await self.leases.get_for_job(job_id)
        if current is not None:
            if (
                current.host_id != host_id
                or current.working_directory != normalized
            ):
                raise ExecutionLeaseBusyError(
                    "job already owns a different working tree lease: "
                    f"job={job_id} current={current.host_id}:{current.working_directory} "
                    f"requested={host_id}:{normalized}"
                )
            return current

        record = ExecutionLeaseRecord(
            lease_key=lease_key,
            job_id=job_id,
            project_id=project_id,
            host_id=host_id,
            working_directory=normalized,
        )
        try:
            stored = await self.leases.add(record)
        except IntegrityError as exc:
            owner = await self.leases.get(lease_key)
            if owner is not None and owner.job_id == job_id:
                return owner
            detail = (
                f"working tree is already leased by job {owner.job_id}"
                if owner is not None
                else "working tree lease is already held"
            )
            raise ExecutionLeaseBusyError(
                f"{detail}: host={host_id} path={normalized}"
            ) from exc

        await self.events.append(
            "EXECUTION_LEASE_ACQUIRED",
            job_id=job_id,
            project_id=project_id,
            host_id=host_id,
            payload={
                "lease_key": lease_key,
                "working_directory": normalized,
            },
        )
        return stored

    async def release_for_job(self, job_id: str) -> None:
        current = await self.leases.get_for_job(job_id)
        if current is None:
            return
        await self.leases.delete_for_job(job_id)
        await self.events.append(
            "EXECUTION_LEASE_RELEASED",
            job_id=job_id,
            project_id=current.project_id,
            host_id=current.host_id,
            payload={
                "lease_key": current.lease_key,
                "working_directory": current.working_directory,
            },
        )

    async def get_for_job(self, job_id: str) -> ExecutionLeaseRecord | None:
        return await self.leases.get_for_job(job_id)

    async def list(self) -> list[ExecutionLeaseRecord]:
        return await self.leases.list()


def _lease_key(host_id: str, working_directory: str) -> str:
    digest = hashlib.sha256(
        f"{host_id}\0{working_directory}".encode("utf-8")
    ).hexdigest()
    return digest[:64]
