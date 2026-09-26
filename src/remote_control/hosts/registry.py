from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from remote_control.hosts.models import HostInfo, HostStatus
from remote_control.storage.models import HostRecord
from remote_control.storage.repositories import EventRepository, HostRepository


class HostRegistry:
    def __init__(
        self,
        *,
        hosts: HostRepository,
        events: EventRepository,
        local_host_id: str,
        heartbeat_timeout_seconds: int = 45,
    ) -> None:
        self.hosts = hosts
        self.events = events
        self.local_host_id = local_host_id
        self.heartbeat_timeout = timedelta(seconds=heartbeat_timeout_seconds)

    async def register_local(
        self,
        *,
        name: str,
        os_name: str,
        capabilities: set[str],
    ) -> HostInfo:
        return await self.register(
            host_id=self.local_host_id,
            name=name,
            os_name=os_name,
            capabilities=capabilities,
            runner_instance_id=None,
            runner_boot_id=None,
        )

    async def register(
        self,
        *,
        host_id: str,
        name: str,
        os_name: str,
        capabilities: set[str],
        runner_instance_id: str | None = None,
        runner_boot_id: str | None = None,
    ) -> HostInfo:
        now = datetime.now(timezone.utc)
        record = HostRecord(
            id=host_id,
            name=name,
            os=os_name,
            status=HostStatus.ONLINE.value,
            capabilities_json=json.dumps(sorted(capabilities)),
            last_heartbeat=now,
            runner_instance_id=runner_instance_id,
            runner_boot_id=runner_boot_id,
        )
        existing = await self.hosts.get(host_id)
        stored = await self.hosts.upsert(record)
        if existing is None or existing.status != HostStatus.ONLINE.value:
            await self.events.append(
                "HOST_ONLINE",
                host_id=host_id,
                payload={"os": os_name, "capabilities": sorted(capabilities)},
            )
        return self._info(stored)

    async def heartbeat(self, host_id: str) -> HostInfo:
        existing = await self.hosts.get(host_id)
        stored = await self.hosts.update(
            host_id,
            status=HostStatus.ONLINE.value,
            last_heartbeat=datetime.now(timezone.utc),
        )
        if existing is not None and existing.status != HostStatus.ONLINE.value:
            await self.events.append("HOST_ONLINE", host_id=host_id)
        return self._info(stored)

    async def disconnect(self, host_id: str) -> None:
        if host_id == self.local_host_id:
            return
        record = await self.hosts.get(host_id)
        if record is None or record.status == HostStatus.OFFLINE.value:
            return
        await self.hosts.update(host_id, status=HostStatus.OFFLINE.value)
        await self.events.append("HOST_OFFLINE", host_id=host_id)

    async def expire_stale(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(timezone.utc)
        expired: list[str] = []
        for record in await self.hosts.list():
            if record.id == self.local_host_id or record.status != HostStatus.ONLINE.value:
                continue
            if record.last_heartbeat is None:
                continue
            heartbeat = record.last_heartbeat
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            if current - heartbeat <= self.heartbeat_timeout:
                continue
            await self.hosts.update(record.id, status=HostStatus.OFFLINE.value)
            await self.events.append(
                "HOST_OFFLINE",
                host_id=record.id,
                payload={"reason": "heartbeat_expired"},
            )
            expired.append(record.id)
        return expired

    async def get(self, host_id: str) -> HostInfo | None:
        record = await self.hosts.get(host_id)
        return None if record is None else self._effective(record)

    async def list(self) -> list[HostInfo]:
        return [self._effective(record) for record in await self.hosts.list()]

    async def is_online(self, host_id: str) -> bool:
        info = await self.get(host_id)
        return info is not None and info.status == HostStatus.ONLINE

    def _effective(self, record: HostRecord) -> HostInfo:
        info = self._info(record)
        if info.id == self.local_host_id:
            return info
        if info.status == HostStatus.ONLINE and info.last_heartbeat is not None:
            now = datetime.now(timezone.utc)
            heartbeat = info.last_heartbeat
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            if now - heartbeat > self.heartbeat_timeout:
                info.status = HostStatus.OFFLINE
        return info

    @staticmethod
    def _info(record: HostRecord) -> HostInfo:
        try:
            capabilities = set(json.loads(record.capabilities_json))
        except (json.JSONDecodeError, TypeError):
            capabilities = set()
        return HostInfo(
            id=record.id,
            name=record.name,
            os=record.os,
            status=HostStatus(record.status),
            capabilities=capabilities,
            last_heartbeat=record.last_heartbeat,
            runner_instance_id=record.runner_instance_id,
            runner_boot_id=record.runner_boot_id,
        )
