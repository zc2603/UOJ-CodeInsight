"""Each process refreshes shared settings; each claimed task keeps one snapshot."""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import update

from app.models import RuntimeConfiguration, RuntimeWorker
from app.services.llm_provider import create_llm_provider
from app.services.runtime_settings import initial_options, read_runtime, task_settings

logger = logging.getLogger(__name__)


class RuntimeManager:
    def __init__(self, factory, base):
        self.factory, self.base = factory, base
        self.options, self.revision = initial_options(base), 0
        self.worker_id = str(uuid.uuid4())
        self.lock = asyncio.Lock()
        self.last_refresh = float("-inf")
        # One shared semaphore across all old/new task clients in this process.
        self.semaphore = asyncio.Semaphore(base.llm_max_concurrency)

    async def start(self):
        async with self.factory() as db:
            self.options, self.revision = await read_runtime(db, self.base)
            if await db.get(RuntimeConfiguration, 1) is None:
                raise RuntimeError("Runtime configuration migration is required")
            db.add(RuntimeWorker(id=self.worker_id, revision=self.revision, seen_at=datetime.now(timezone.utc)))
            await db.commit()
        self.last_refresh = time.monotonic()

    async def refresh(self):
        async with self.lock:
            if time.monotonic() - self.last_refresh < 3:
                return
            async with self.factory() as db:
                options, revision = await read_runtime(db, self.base)
                await db.execute(update(RuntimeWorker).where(RuntimeWorker.id == self.worker_id)
                    .values(revision=revision, seen_at=datetime.now(timezone.utc)))
                await db.commit()
            self.options, self.revision = options, revision
            self.last_refresh = time.monotonic()

    async def watch(self):
        while True:
            await asyncio.sleep(3)
            try:
                await self.refresh()
            except Exception as exc:
                logger.error("Runtime configuration refresh failed (%s)", type(exc).__name__)

    async def snapshot(self, kind):
        await self.refresh()
        return task_settings(self.base, self.options, kind)

    @asynccontextmanager
    async def provider(self, settings):
        provider = create_llm_provider(settings)
        if hasattr(provider, "semaphore"):
            provider.semaphore = self.semaphore
        try:
            yield provider
        finally:
            close = getattr(provider, "close", None)
            if close:
                await close()
