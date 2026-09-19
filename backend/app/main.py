from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api import admin, student
from app.config import get_settings
from app.database import SessionLocal
from app.services.attempt_maintenance import maintain_attempts
from app.services.generation_service import generation_worker
from app.services.timeout_grading import timeout_worker
from app.integrations.uoj import UOJRepository, UOJSubmissionArchiveClient
from app.services.import_service import ImportService
from app.services.llm_provider import create_llm_provider


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.llm_provider = create_llm_provider(settings)
    app.state.uoj_repository = None
    app.state.import_service = None
    if settings.uoj_database_url:
        repository = UOJRepository(settings.uoj_database_url, settings.business_timezone)
        archive_client = UOJSubmissionArchiveClient(settings)
        app.state.uoj_repository = repository
        app.state.import_service = ImportService(settings, repository, archive_client)
    maintenance = asyncio.create_task(maintain_attempts(SessionLocal, settings))
    timeout_grader = asyncio.create_task(timeout_worker(SessionLocal, settings, app.state.llm_provider))
    generators = [asyncio.create_task(generation_worker(SessionLocal, settings, app.state.llm_provider))
        for _ in range(settings.generation_workers)]
    try:
        yield
    finally:
        for worker in generators:
            worker.cancel()
        await asyncio.gather(*generators, return_exceptions=True)
        timeout_grader.cancel()
        await asyncio.gather(timeout_grader, return_exceptions=True)
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        if app.state.uoj_repository is not None:
            await app.state.uoj_repository.close()
        close = getattr(app.state.llm_provider, "close", None)
        if close is not None:
            await close()


settings = get_settings()
app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.include_router(admin.router)
app.include_router(student.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
