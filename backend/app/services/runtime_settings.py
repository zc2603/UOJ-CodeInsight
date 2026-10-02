"""Shared model/task configuration. Secrets stay in the deployment environment."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from app.config import Settings
from app.models import RuntimeConfiguration, RuntimeConfigurationAudit, RuntimeWorker
from app.services.teacher_settings import load_settings
from app.services.model_probe import pending_tests, present_tests, probe

Service = Literal["deepseek", "openai"]
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-pro")
OPENAI_MODELS = ("gpt-5.6-sol", "gpt-6-astra", "gpt-6.1-sol")


class RuntimeOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    generation_service: Service = "deepseek"
    grading_service: Service = "deepseek"
    deepseek_model: Literal["deepseek-flash", "deepseek-pro"] = "deepseek-flash"
    openai_model: Literal["gpt-5.6-sol", "gpt-6-astra", "gpt-6.1-sol"] = "gpt-5.6-sol"
    reasoning_effort: Literal["low", "high", "max"] = "max"
    max_tokens: int = Field(default=100000, ge=1000, le=100000, strict=True)
    request_timeout_seconds: int = Field(default=360, ge=10, le=1200, strict=True)
    generation_timeout_seconds: int = Field(default=1200, ge=30, le=3600, strict=True)
    generation_max_attempts: int = Field(default=3, ge=1, le=5, strict=True)
    generation_global_concurrency: int = Field(default=80, ge=1, le=100, strict=True)
    grading_timeout_seconds: int = Field(default=1200, ge=30, le=3600, strict=True)


class RuntimeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    settings: RuntimeOptions


def initial_options(base: Settings) -> RuntimeOptions:
    return RuntimeOptions(deepseek_model=base.llm_model if base.llm_model in DEEPSEEK_MODELS else "deepseek-flash",
        reasoning_effort=base.llm_reasoning_effort, max_tokens=base.llm_max_tokens,
        request_timeout_seconds=int(base.llm_timeout_seconds),
        generation_timeout_seconds=int(base.generation_task_timeout_seconds),
        generation_max_attempts=base.generation_max_attempts,
        generation_global_concurrency=base.generation_global_concurrency,
        grading_timeout_seconds=int(base.grading_task_timeout_seconds))


async def read_runtime(db, base):
    row = await db.get(RuntimeConfiguration, 1, populate_existing=True)
    defaults = initial_options(base)
    return (RuntimeOptions.model_validate({**defaults.model_dump(), **(row.values_json or {})}), row.revision) if row else (defaults, 0)


def task_settings(base: Settings, options: RuntimeOptions, kind: str) -> Settings:
    service = options.generation_service if kind == "generation" else options.grading_service
    return base.model_copy(update={
        "llm_model": options.deepseek_model if service == "deepseek" else options.openai_model,
        "llm_base_url": base.llm_base_url if service == "deepseek" else base.openai_base_url,
        "llm_api_key": base.llm_api_key if service == "deepseek" else base.openai_api_key,
        "llm_api_style": service, "llm_reasoning_effort": options.reasoning_effort,
        "llm_max_tokens": options.max_tokens, "llm_timeout_seconds": options.request_timeout_seconds,
        "generation_task_timeout_seconds": options.generation_timeout_seconds,
        "generation_max_attempts": options.generation_max_attempts,
        "generation_global_concurrency": options.generation_global_concurrency,
        "grading_task_timeout_seconds": options.grading_timeout_seconds,
    })


async def runtime_response(db, base):
    options, revision = await read_runtime(db, base)
    tests = await db.scalar(select(RuntimeConfigurationAudit.model_tests_json).where(RuntimeConfigurationAudit.revision == revision))
    workers = (await db.scalars(select(RuntimeWorker).where(
        RuntimeWorker.seen_at >= datetime.now(timezone.utc) - timedelta(seconds=15)))).all()
    return {"settings": options, "revision": revision, "defaults": initial_options(base), "model_tests": present_tests(tests),
        "services": {
            "deepseek": {"base_url": base.llm_base_url, "configured": bool(base.llm_api_key), "models": DEEPSEEK_MODELS},
            "openai": {"base_url": base.openai_base_url, "configured": bool(base.openai_api_key), "models": OPENAI_MODELS}},
        "runtime": {"active_workers": len(workers), "loaded_workers": sum(w.revision == revision for w in workers),
            "poll_seconds": 3, "mode": base.llm_provider,
            "request_concurrency_per_process": base.llm_max_concurrency,
            "generation_workers_per_process": base.generation_workers,
            "grading_workers_per_process": base.grading_workers}}


async def save_runtime(db, base, principal, payload):
    await load_settings(db, principal.user_id)  # Check active teacher identity.
    row = await db.scalar(select(RuntimeConfiguration).where(RuntimeConfiguration.id == 1)
        .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(503, "运行配置尚未初始化，请联系维护人员")
    if row.revision != payload.expected_revision:
        raise HTTPException(409, "平台运行参数已被其他教师更新，请重新加载")
    if base.llm_provider != "mock":
        for service in {payload.settings.generation_service, payload.settings.grading_service}:
            if not (base.llm_api_key if service == "deepseek" else base.openai_api_key):
                raise HTTPException(422, "所选模型服务尚未配置凭据")
    previous = RuntimeOptions.model_validate({**initial_options(base).model_dump(), **(row.values_json or {})})
    tests = pending_tests(previous, payload.settings)
    row.revision += 1
    saved_revision = row.revision
    row.values_json = payload.settings.model_dump()
    row.updated_by = principal.username
    row.updated_at = datetime.now(timezone.utc)
    db.add(RuntimeConfigurationAudit(revision=row.revision, actor=principal.username, values_json=row.values_json,
        model_tests_json=tests))
    await db.commit()
    response = await runtime_response(db, base)
    # Preserve this save's snapshot even if another teacher saves during the probe.
    response.update(settings=payload.settings, revision=saved_revision, model_tests=tests)
    await db.commit()  # No transaction or configuration lock across the external request.
    if tests:
        results = await asyncio.gather(*(probe(task_settings(base,
            payload.settings.model_copy(update={"generation_service": test["service"]}), "generation"), test) for test in tests))
        await db.execute(update(RuntimeConfigurationAudit).where(RuntimeConfigurationAudit.revision == saved_revision)
            .values(model_tests_json=results))
        await db.commit()
        response["model_tests"] = results
    return response
