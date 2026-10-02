from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api import admin, student
from app.config import get_settings
from app.database import SessionLocal
from app.services.attempt_maintenance import maintain_attempts
from app.services.generation_service import generation_worker
from app.services.grading_queue import grading_worker
from app.services.quality_audit import worker as quality_worker
from app.integrations.uoj import UOJRepository, UOJSubmissionArchiveClient
from app.services.import_service import ImportService
from app.services.runtime_manager import RuntimeManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    runtime = RuntimeManager(SessionLocal, settings)
    await runtime.start()
    app.state.runtime = runtime
    configuration_watch = asyncio.create_task(runtime.watch())
    app.state.llm_provider = None  # RuntimeManager creates task-bound clients.
    app.state.uoj_repository = None
    app.state.import_service = None
    if settings.uoj_database_url:
        repository = UOJRepository(settings.uoj_database_url, settings.business_timezone)
        archive_client = UOJSubmissionArchiveClient(settings)
        app.state.uoj_repository = repository
        app.state.import_service = ImportService(settings, repository, archive_client)
    maintenance = asyncio.create_task(maintain_attempts(SessionLocal, settings))
    graders = [asyncio.create_task(grading_worker(SessionLocal, settings, app.state.llm_provider, runtime))
        for _ in range(settings.grading_workers)]
    generators = [asyncio.create_task(generation_worker(SessionLocal, settings, app.state.llm_provider, runtime))
        for _ in range(settings.generation_workers)]
    quality = asyncio.create_task(quality_worker(SessionLocal, settings)) if settings.quality_audit_enabled else None
    try:
        yield
    finally:
        configuration_watch.cancel()
        await asyncio.gather(configuration_watch, return_exceptions=True)
        if quality is not None:
            quality.cancel()
            await asyncio.gather(quality, return_exceptions=True)
        for worker in generators:
            worker.cancel()
        await asyncio.gather(*generators, return_exceptions=True)
        for worker in graders:
            worker.cancel()
        await asyncio.gather(*graders, return_exceptions=True)
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        if app.state.uoj_repository is not None:
            await app.state.uoj_repository.close()
        close = getattr(app.state.llm_provider, "close", None)
        if close is not None:
            await close()
        app.state.runtime = None


settings = get_settings()
app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.include_router(admin.router)
app.include_router(student.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
