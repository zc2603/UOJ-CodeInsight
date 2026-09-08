"""Database-backed question preparation with leases and fenced completion."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.models import GenerationControl, GenerationJob, GenerationRun, Quiz, QuizStatus, SubmissionSnapshot
from app.services.llm_provider import GENERATOR_VERSION
from app.services.question_policy import allocated_kind

logger = logging.getLogger(__name__)


async def enqueue(db, snapshots, round_no=1):
    for snapshot in snapshots:
        db.add(GenerationJob(quiz_id=snapshot.quiz_id, participant_id=snapshot.participant_id,
            submission_snapshot_id=snapshot.id, round_no=round_no, prompt_version=GENERATOR_VERSION))


def summarize(jobs):
    # Only the latest round for each student counts; prior rounds remain auditable.
    latest = {}
    for job in jobs:
        latest[job.participant_id] = max(latest.get(job.participant_id, 0), job.round_no)
    current = [j for j in jobs if j.round_no == latest[j.participant_id]]
    counts = {state: sum(j.state == state for j in current) for state in ("queued", "running", "succeeded", "failed")}
    ready = sum(all(j.state == "succeeded" for j in current if j.participant_id == pid) for pid in latest)
    return dict(total=len(current), completed=counts["succeeded"], running=counts["running"],
        queued=counts["queued"], failed=counts["failed"], students_total=len(latest), students_ready=ready,
        ready=bool(current) and counts["succeeded"] == len(current))


async def progress(db, quiz_id):
    jobs = (await db.execute(select(GenerationJob).where(GenerationJob.quiz_id == quiz_id))).scalars().all()
    return summarize(jobs)


async def open_quiz(db, quiz_id):
    quiz = (await db.execute(select(Quiz).where(Quiz.id == quiz_id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if quiz is None:
        raise HTTPException(404, "测评不存在")
    if quiz.status == QuizStatus.PUBLISHED:
        return quiz  # Repeated clicks must never extend the entry window.
    if not quiz.pre_generate or quiz.status != QuizStatus.DRAFT:
        raise HTTPException(409, "当前测评不能开放")
    if not (await progress(db, quiz_id))["ready"]:
        raise HTTPException(409, "请等待全部问题准备完成后再开放")
    now = datetime.now(timezone.utc)
    quiz.status = QuizStatus.PUBLISHED
    quiz.start_time = now
    quiz.end_time = now + timedelta(minutes=30)
    await db.commit()
    return quiz


async def retry_failed(db, quiz_id):
    quiz = (await db.execute(select(Quiz).where(Quiz.id == quiz_id).with_for_update())).scalar_one_or_none()
    if quiz is None:
        raise HTTPException(404, "测评不存在")
    jobs = (await db.execute(select(GenerationJob).where(GenerationJob.quiz_id == quiz_id))).scalars().all()
    latest = {}
    for job in jobs:
        latest[job.participant_id] = max(latest.get(job.participant_id, 0), job.round_no)
    ids = [j.id for j in jobs if j.state == "failed" and j.round_no == latest[j.participant_id]]
    if ids:
        await db.execute(update(GenerationJob).where(GenerationJob.id.in_(ids), GenerationJob.state == "failed")
            .values(state="queued", attempts=0, error=None, available_at=datetime.now(timezone.utc)))
    await db.commit()
    return len(ids)


async def claim(db, settings, model, *, now=None):
    now = now or datetime.now(timezone.utc)
    # Every worker locks the same row before counting/claiming; no process-local limit race.
    (await db.execute(select(GenerationControl).where(GenerationControl.id == 1).with_for_update())).scalar_one()
    stale = (await db.execute(select(GenerationJob).where(GenerationJob.state == "running",
        GenerationJob.lease_until <= now).with_for_update().execution_options(populate_existing=True))).scalars().all()
    for job in stale:
        await db.execute(update(GenerationRun).where(GenerationRun.id == job.lease_token)
            .values(state="interrupted", finished_at=now, error="Worker lease expired"))
        job.state = "failed" if job.attempts >= settings.generation_max_attempts else "queued"
        job.error = "出题任务中断，重试次数已用尽" if job.state == "failed" else None
        job.lease_token = None
        job.lease_until = None
        job.available_at = now
    await db.flush()
    running = await db.scalar(select(func.count()).select_from(GenerationJob).where(GenerationJob.state == "running"))
    if running >= settings.generation_global_concurrency:
        await db.commit()
        return None
    job = (await db.execute(select(GenerationJob).where(GenerationJob.state == "queued", GenerationJob.available_at <= now)
        .order_by(GenerationJob.available_at, GenerationJob.created_at, GenerationJob.id)
        .limit(1).with_for_update(skip_locked=True).execution_options(populate_existing=True))).scalar_one_or_none()
    if job is None:
        await db.commit()
        return None
    token = uuid.uuid4()
    job.state = "running"
    job.attempts += 1
    job.lease_token = token
    job.lease_until = now + timedelta(seconds=settings.generation_lease_seconds)
    job.model = model
    job.prompt_version = GENERATOR_VERSION
    db.add(GenerationRun(id=token, job_id=job.id, model=model, prompt_version=GENERATOR_VERSION))
    identity = job.id, token, job.submission_snapshot_id
    await db.commit()
    return identity


async def finish(db, settings, identity, *, result=None, raw=None, error=None):
    job_id, token, _ = identity
    now = datetime.now(timezone.utc)
    job = (await db.execute(select(GenerationJob).where(GenerationJob.id == job_id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if job is None:  # The teacher deleted this quiz while the model was running.
        await db.commit()
        return False
    # A late response from a dead/replaced worker cannot overwrite a new run.
    if job.lease_token != token or job.state != "running":
        await db.execute(update(GenerationRun).where(GenerationRun.id == token)
            .values(raw_response=raw, error="Late result ignored after lease reassignment"))
        await db.commit()
        return False
    await db.execute(update(GenerationRun).where(GenerationRun.id == token).values(
        state="failed" if error else "succeeded", finished_at=now, raw_response=raw, error=error))
    job.lease_token = None
    job.lease_until = None
    job.error = error
    if error:
        job.state = "failed" if job.attempts >= settings.generation_max_attempts else "queued"
        job.available_at = now + timedelta(seconds=min(60, 5 * 2 ** (job.attempts - 1)))
    else:
        job.state = "succeeded"
        job.result_json = result.model_dump(mode="json")
        job.raw_response = raw
    await db.commit()
    return True


async def heartbeat(factory, settings, identity):
    while True:
        await asyncio.sleep(settings.generation_lease_seconds / 3)
        async with factory() as db:
            result = await db.execute(update(GenerationJob).where(GenerationJob.id == identity[0],
                GenerationJob.lease_token == identity[1], GenerationJob.state == "running")
                .values(lease_until=datetime.now(timezone.utc) + timedelta(seconds=settings.generation_lease_seconds)))
            await db.commit()
            if result.rowcount == 0:
                raise RuntimeError("Generation lease lost")


async def run_claim(factory, settings, provider, identity):
    async def generate():
        async with factory() as db:
            snapshot = (await db.execute(select(SubmissionSnapshot).where(SubmissionSnapshot.id == identity[2])
                .options(selectinload(SubmissionSnapshot.problem)))).scalar_one()
            payload = dict(title=snapshot.problem.title, statement=snapshot.problem.statement,
                language=snapshot.language, source_code=snapshot.source_code,
                second_question_kind=await allocated_kind(db, snapshot))
        return await asyncio.wait_for(provider.generate_questions(**payload), settings.generation_task_timeout_seconds)

    generation = asyncio.create_task(generate())
    renew = asyncio.create_task(heartbeat(factory, settings, identity))
    try:
        done, _ = await asyncio.wait([generation, renew], return_when=asyncio.FIRST_COMPLETED)
        if renew in done:
            await renew  # Lease failure cancels generation; it must not commit stale work.
        result, raw = await generation
        async with factory() as db:
            await finish(db, settings, identity, result=result, raw=raw)
    except asyncio.CancelledError:
        # No claim cleanup needed: durable lease permits recovery even after hard kill.
        raise
    except Exception as exc:
        async with factory() as db:
            await finish(db, settings, identity, error=f"出题未完成（{type(exc).__name__}）",
                raw=getattr(exc, "raw_response", None))
    finally:
        generation.cancel()
        renew.cancel()
        await asyncio.gather(generation, renew, return_exceptions=True)


async def generation_worker(factory, settings, provider):
    while True:
        try:
            async with factory() as db:
                identity = await claim(db, settings, provider.model_name)
            if identity:
                await run_claim(factory, settings, provider, identity)
                continue
        except Exception as exc:
            logger.error("Generation worker retrying after %s", type(exc).__name__)
        await asyncio.sleep(settings.generation_poll_seconds)
