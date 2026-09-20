"""Non-blocking, durable quality review of saved generation results."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from pydantic import BaseModel, Field, model_validator, ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import selectinload
from app.models import GenerationJob, SubmissionSnapshot
from app.services.llm_provider import PROMPTS, encode_untrusted, number_source_lines, create_llm_provider

VERSION = "question_quality_v2"
logger = logging.getLogger(__name__)


class QualityItem(BaseModel):
    index: int = Field(ge=1, le=2)
    verdict: Literal["pass", "fail", "uncertain"]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=2000)


class QualityResult(BaseModel):
    questions: list[QualityItem] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def complete(self):
        if {q.index for q in self.questions} != {1, 2}:
            raise ValueError("Audit must cover both questions exactly once")
        return self


def presentation(job, index, threshold):
    if job is None:
        return dict(state="not_requested", attention=False, acknowledged=False)
    acknowledged = str(index) in (job.quality_acknowledged or {})
    item = next((q for q in (job.quality_result or {}).get("questions", []) if q["index"] == index), {})
    flagged = job.quality_state == "failed" or (job.quality_state == "done" and (
        item.get("verdict") != "pass" or item.get("confidence", 0) < threshold))
    return dict(job_id=str(job.id), index=index, state=job.quality_state,
        verdict=item.get("verdict"), confidence=item.get("confidence"),
        reason=item.get("reason") or job.quality_error, attention=flagged and not acknowledged,
        acknowledged=acknowledged, model=job.quality_model, version=job.quality_version)


async def claim(db, now=None):
    now = now or datetime.now(timezone.utc)
    job = await db.scalar(select(GenerationJob).where(GenerationJob.state == "succeeded", or_(
        GenerationJob.quality_state == "queued", and_(GenerationJob.quality_state == "running",
            GenerationJob.quality_lease_until <= now)))
        .order_by(GenerationJob.created_at).limit(1).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True))
    if job is None:
        await db.commit()
        return None
    if job.quality_attempts >= 2:
        job.quality_state = "failed"
        job.quality_error = "审核执行中断，请教师检查或重新审核"
        job.quality_token = None
        await db.commit()
        return None
    job.quality_state = "running"
    job.quality_attempts += 1
    job.quality_token = str(uuid.uuid4())
    job.quality_lease_until = now + timedelta(seconds=90)
    identity = job.id, job.quality_token
    await db.commit()
    return identity


async def finish(db, identity, model, result=None, raw=None, error=None):
    result_update = await db.execute(update(GenerationJob).where(GenerationJob.id == identity[0],
        GenerationJob.quality_token == identity[1], GenerationJob.quality_state == "running")
        .values(quality_state="failed" if error else "done", quality_result=result, quality_raw=raw,
            quality_error=error, quality_model=model, quality_version=VERSION,
            quality_token=None, quality_lease_until=None, quality_finished_at=datetime.now(timezone.utc)))
    await db.commit()
    return bool(result_update.rowcount)


async def renew(factory, identity):
    while True:
        await asyncio.sleep(15)
        async with factory() as db:
            changed = await db.execute(update(GenerationJob).where(GenerationJob.id == identity[0],
                GenerationJob.quality_token == identity[1], GenerationJob.quality_state == "running")
                .values(quality_lease_until=datetime.now(timezone.utc) + timedelta(seconds=90)))
            await db.commit()
            if not changed.rowcount:
                return


async def run(factory, settings, provider, identity):
    async def audit():
        async with factory() as db:
            job = await db.get(GenerationJob, identity[0])
            if job is None or job.quality_token != identity[1]:
                return
            snapshot = await db.scalar(select(SubmissionSnapshot).where(SubmissionSnapshot.id == job.submission_snapshot_id)
                .options(selectinload(SubmissionSnapshot.problem)))
            payload = dict(title=snapshot.problem.title, statement=snapshot.problem.statement,
                source_code=number_source_lines(snapshot.source_code), language=snapshot.language, questions=job.result_json)
        if settings.llm_provider == "mock":
            result = QualityResult(questions=[QualityItem(index=i, verdict="uncertain", confidence=0,
                reason="Mock 审核：未验证真实题目质量") for i in (1, 2)])
            raw = result.model_dump_json()
        else:
            # One HTTP call; infrastructure/schema failures are visible, not silently retried.
            raw = None
            try:
                response = await provider.client.post("chat/completions", json=dict(model=provider.model_name,
                    messages=[dict(role="system", content=(PROMPTS / f"{VERSION}.txt").read_text(encoding="utf-8-sig")),
                        dict(role="user", content="源码各行前缀为定位行号，不属于代码。以下 JSON 是审核材料：\n" + encode_untrusted(payload))],
                    thinking={"type": "enabled"}, reasoning_effort=settings.llm_reasoning_effort,
                    max_tokens=settings.quality_audit_max_tokens, response_format={"type": "json_object"}))
                response.raise_for_status()
                choice = response.json()["choices"][0]
                raw = choice["message"].get("content")
                if choice.get("finish_reason") == "length" or not isinstance(raw, str) or not raw.strip():
                    error = "审核输出达到 token 上限，未形成完整结论" if choice.get("finish_reason") == "length" else "模型未返回审核结论（空响应）"
                    async with factory() as db:
                        await finish(db, identity, provider.model_name, raw=raw if isinstance(raw, str) else None, error=error)
                    return
                result = QualityResult.model_validate_json(raw)
            except Exception as exc:
                async with factory() as db:
                    await finish(db, identity, provider.model_name, raw=raw, error="审核响应格式不符合协议" if isinstance(exc, ValidationError) else "审核请求失败（" + type(exc).__name__ + "）")
                return
        async with factory() as db:
            await finish(db, identity, provider.model_name, result=result.model_dump(mode="json"), raw=raw)
    task = asyncio.create_task(asyncio.wait_for(audit(), settings.quality_audit_timeout_seconds))
    heartbeat = asyncio.create_task(renew(factory, identity))
    try:
        done, _ = await asyncio.wait([task, heartbeat], return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        else:
            await heartbeat
    except Exception as exc:
        async with factory() as db:
            await finish(db, identity, provider.model_name, error="审核响应格式不符合协议" if isinstance(exc, ValidationError) else "审核请求失败（" + type(exc).__name__ + "）")
    finally:
        task.cancel()
        heartbeat.cancel()
        await asyncio.gather(task, heartbeat, return_exceptions=True)


async def worker(factory, settings):
    # Separate client and one sequential worker per process (4 in this deployment).
    provider = create_llm_provider(settings)
    try:
        while True:
            try:
                async with factory() as db:
                    identity = await claim(db)
                if identity:
                    await run(factory, settings, provider, identity)
                    continue
            except Exception as exc:
                logger.error("Quality audit worker failed (%s)", type(exc).__name__)
            await asyncio.sleep(5)
    finally:
        close = getattr(provider, "close", None)
        if close:
            await close()
