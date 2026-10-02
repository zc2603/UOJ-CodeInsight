import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_attempt_flow import db
from app.api.dependencies import AdminPrincipal, require_admin
from app.config import Settings, get_settings
from app.database import get_db
from app.main import app
from app.models import AdminUser, RuntimeConfiguration, RuntimeConfigurationAudit, RuntimeWorker
from app.services.llm_provider import OpenAICompatibleLLMProvider
from app.services.runtime_manager import RuntimeManager
from app.services.runtime_settings import RuntimeOptions, RuntimeUpdate, runtime_response, save_runtime, task_settings


@pytest.mark.parametrize("values", [
    {"deepseek_model": "unlisted"}, {"openai_model": "gpt-6-sol"}, {"generation_service": "unknown"},
    {"max_tokens": 0}, {"max_tokens": 100001}, {"request_timeout_seconds": 1},
    {"generation_max_attempts": 6}, {"generation_global_concurrency": 101},
    {"grading_timeout_seconds": 0}, {"generation_timeout_seconds": 3.5}, {"api_key": "not-allowed"},
])
def test_runtime_parameter_validation(values):
    with pytest.raises(ValidationError):
        RuntimeOptions(**values)


@pytest.mark.asyncio
async def test_http_global_settings_authorization_cas_and_secret_exclusion(db, monkeypatch):
    teacher = AdminUser(username="runtime-teacher", password_hash="synthetic")
    db.add_all([teacher, RuntimeConfiguration(id=1, revision=0)])
    await db.commit()
    async def database():
        yield db
    app.dependency_overrides[get_db] = database
    monkeypatch.setenv("LLM_API_KEY", "synthetic-deepseek-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-secret")
    get_settings.cache_clear()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/admin/runtime-settings")).status_code == 401
            app.dependency_overrides[require_admin] = lambda: AdminPrincipal(teacher.id, teacher.username)
            response = await client.get("/api/admin/runtime-settings")
            assert response.status_code == 200
            assert "synthetic-deepseek-secret" not in response.text and "synthetic-openai-secret" not in response.text
            assert response.json()["settings"]["openai_model"] == "gpt-5.6-sol"
            assert response.json()["settings"]["deepseek_model"] == "deepseek-flash"
            values = {**response.json()["settings"], "generation_service": "openai", "deepseek_model": "deepseek-pro"}
            payload = {"expected_revision": 0, "settings": values}
            saved = await client.put("/api/admin/runtime-settings", json=payload)
            assert saved.status_code == 200 and saved.json()["revision"] == 1
            assert (await client.put("/api/admin/runtime-settings", json=payload)).status_code == 409
            audits = (await db.scalars(select(RuntimeConfigurationAudit))).all()
            assert len(audits) == 1 and audits[0].actor == teacher.username
            assert audits[0].values_json["generation_service"] == "openai"
            teacher.is_active = False
            await db.commit()
            assert (await client.get("/api/admin/runtime-settings")).status_code == 401
    finally:
        app.dependency_overrides.clear(); get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unconfigured_service_rejected(db):
    teacher = AdminUser(username="no-credential", password_hash="synthetic")
    db.add_all([teacher, RuntimeConfiguration(id=1, revision=0)])
    await db.commit()
    base = Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key="synthetic", openai_api_key="")
    with pytest.raises(HTTPException) as exc:
        await save_runtime(db, base, AdminPrincipal(teacher.id, teacher.username), RuntimeUpdate(expected_revision=0,
            settings=RuntimeOptions(grading_service="openai")))
    assert exc.value.status_code == 422


class ResponsePayload(BaseModel):
    value: int


@pytest.mark.asyncio
@pytest.mark.parametrize("service,model", [("deepseek", "deepseek-flash"), ("deepseek", "deepseek-pro"),
    ("openai", "gpt-5.6-sol"), ("openai", "gpt-6-astra"), ("openai", "gpt-6.1-sol")])
async def test_provider_protocols_and_credential_routing(service, model):
    base = Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key="synthetic-deepseek",
        openai_api_key="synthetic-openai", llm_base_url="https://deepseek.test", openai_base_url="http://openai.test/v1")
    options = RuntimeOptions(generation_service=service, **{service + "_model": model})
    provider = OpenAICompatibleLLMProvider(task_settings(base, options, "generation"))
    await provider.client.aclose()
    async def respond(request):
        payload = json.loads(request.content)
        assert payload["model"] == model
        assert request.headers["authorization"] == "Bearer synthetic-" + service
        assert request.url.path == ("/v1/chat/completions" if service == "openai" else "/chat/completions")
        if service == "deepseek":
            assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "max"
            assert payload["max_tokens"] == 100000 and "max_completion_tokens" not in payload
        else:
            assert "thinking" not in payload and "max_tokens" not in payload
            assert payload["reasoning_effort"] == "xhigh" and payload["max_completion_tokens"] == 100000
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"value": 1}'}}]})
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond),
        base_url=provider.settings.llm_base_url.rstrip("/") + "/", headers={"Authorization": "Bearer " + provider.settings.llm_api_key})
    try:
        response, _ = await provider._request_json("json", "synthetic", ResponsePayload)
        assert response.value == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_refresh_new_tasks_keep_inflight_snapshot_and_semaphore(db):
    base = Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key="synthetic-a", openai_api_key="synthetic-b")
    row = RuntimeConfiguration(id=1, revision=0)
    db.add(row); await db.commit()
    manager = RuntimeManager(async_sessionmaker(db.bind, expire_on_commit=False), base)
    await manager.start()
    first = await manager.snapshot("generation")
    async with manager.provider(first) as old:
        row.values_json = RuntimeOptions(generation_service="openai", generation_timeout_seconds=99,
            grading_timeout_seconds=88, generation_global_concurrency=7).model_dump()
        row.revision = 1; await db.commit()
        manager.last_refresh = float("-inf")
        second = await manager.snapshot("generation")
        assert second.llm_model == "gpt-5.6-sol" and second.generation_task_timeout_seconds == 99
        assert first.llm_model == "deepseek-flash" and first.generation_task_timeout_seconds == 1200
        async with manager.provider(second) as new:
            assert old.model_name == "deepseek-flash" and new.model_name == "gpt-5.6-sol"
            assert old.semaphore is new.semaphore and not old.client.is_closed
        assert new.client.is_closed and not old.client.is_closed
    assert old.client.is_closed
    response = await runtime_response(db, base)
    assert response["runtime"]["loaded_workers"] == response["runtime"]["active_workers"] == 1
    assert response["revision"] == 1
    # Expired process receipts never claim that a stopped worker loaded settings.
    await db.execute(__import__("sqlalchemy").update(RuntimeWorker).values(seen_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await db.commit()
    assert (await runtime_response(db, base))["runtime"]["active_workers"] == 0


@pytest.mark.asyncio
async def test_workers_use_runtime_limits_and_models(db, monkeypatch):
    from app.services import generation_service, grading_queue
    base = Settings(_env_file=None, llm_provider="openai-compatible", llm_api_key="synthetic-a", openai_api_key="synthetic-b")
    db.add(RuntimeConfiguration(id=1, revision=1, values_json=RuntimeOptions(generation_service="openai",
        grading_service="openai", generation_timeout_seconds=99, grading_timeout_seconds=88,
        generation_global_concurrency=7, generation_max_attempts=2).model_dump()))
    await db.commit()
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    manager = RuntimeManager(factory, base); await manager.start()
    async def claim(session, settings, model):
        assert settings.generation_global_concurrency == 7 and settings.generation_max_attempts == 2
        assert model == "gpt-5.6-sol"
        return ("synthetic",)
    async def run(factory, settings, provider, identity):
        assert settings.generation_task_timeout_seconds == 99 and provider.model_name == "gpt-5.6-sol"
        raise asyncio.CancelledError
    monkeypatch.setattr(generation_service, "claim", claim)
    monkeypatch.setattr(generation_service, "run_claim", run)
    with pytest.raises(asyncio.CancelledError):
        await generation_service.generation_worker(factory, base, None, manager)
    async def grade_claim(session):
        return ("synthetic",)
    async def grade_run(factory, provider, identity, timeout):
        assert timeout == 88 and provider.model_name == "gpt-5.6-sol"
        raise asyncio.CancelledError
    monkeypatch.setattr(grading_queue, "claim_grading", grade_claim)
    monkeypatch.setattr(grading_queue, "run_grading", grade_run)
    with pytest.raises(asyncio.CancelledError):
        await grading_queue.grading_worker(factory, base, None, manager)
