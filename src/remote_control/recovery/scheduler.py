from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from remote_control.controller.job_manager import JobManager
from remote_control.hosts.registry import HostRegistry

logger = logging.getLogger(__name__)


class RecoveryScheduler:
    def __init__(
        self,
        *,
        jobs: JobManager,
        hosts: HostRegistry,
        interval_seconds: int = 15,
    ) -> None:
        self.jobs = jobs
        self.hosts = hosts
        self.interval_seconds = max(interval_seconds, 1)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="recovery-scheduler")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def tick(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        await self.hosts.expire_stale(now=current)
        await self.jobs.expire_approvals(now=current)
        await self.jobs.recover_due(now=current)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("recovery scheduler tick failed")
            await asyncio.sleep(self.interval_seconds)
