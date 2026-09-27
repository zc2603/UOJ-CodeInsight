"""Database-backed question preparation with leases and fenced completion."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.models import Attempt, AttemptStatus, QuizParticipant, GenerationControl, GenerationJob, GenerationRun, Quiz, QuizStatus, SubmissionSnapshot, QuizProblemSnapshot
from app.services.llm_provider import GENERATOR_VERSION, LIGHTWEIGHT_GENERATOR_VERSION
from app.services.question_policy import allocated_kind
from app.time_utils import ensure_utc

logger = logging.getLogger(__name__)


async def enqueue(db, snapshots, round_no=1):
    if not snapshots:
        return
    quiz = await db.get(Quiz, snapshots[0].quiz_id)
    choice_ids = []
    if quiz.assessment_version == "lightweight_v1":
        choice_ids = (await db.execute(select(SubmissionSnapshot.id)
            .join(QuizProblemSnapshot, QuizProblemSnapshot.id == SubmissionSnapshot.problem_snapshot_id)
            .where(SubmissionSnapshot.quiz_id == quiz.id, QuizProblemSnapshot.include_choice.is_(True),
                SubmissionSnapshot.uoj_score > 0))).scalars().all()
    rank = {str(value): i for i, value in enumerate(sorted(choice_ids, key=str))}
    for snapshot in snapshots:
        second = ("trace", "boundary", "modification")[rank[str(snapshot.id)] % 3] if str(snapshot.id) in rank else None
        db.add(GenerationJob(quiz_id=snapshot.quiz_id, participant_id=snapshot.participant_id,
            submission_snapshot_id=snapshot.id, round_no=round_no,
            second_kind=second, prompt_version=LIGHTWEIGHT_GENERATOR_VERSION if quiz.assessment_version == "lightweight_v1" else GENERATOR_VERSION))


def summarize(jobs):
    # Only the latest round for each student counts; prior rounds remain auditable.
    latest = {}
    for job in jobs:
        latest[job.participant_id] = max(latest.get(job.participant_id, 0), job.round_no)
    current = [j for j in jobs if j.round_no == latest[j.participant_id]]
    counts = {state: sum(j.state == state for j in current) for state in ("queued", "running", "succeeded", "failed", "cancelled")}
    ready = sum(all(j.state == "succeeded" for j in current if j.participant_id == pid) for pid in latest)
    return dict(total=len(current), completed=counts["succeeded"], running=counts["running"],
        queued=counts["queued"], failed=counts["failed"], cancelled=counts["cancelled"], students_total=len(latest), students_ready=ready,
        ready=bool(current) and counts["succeeded"] == len(current))


async def progress(db, quiz_id):
    jobs = (await db.execute(select(GenerationJob).where(GenerationJob.quiz_id == quiz_id))).scalars().all()
    return summarize(jobs)


async def stop_pending(db, quiz_id):
    now = datetime.now(timezone.utc)
    jobs = (await db.execute(select(GenerationJob).where(GenerationJob.quiz_id == quiz_id,
        GenerationJob.state.in_(["queued", "running"])).with_for_update()
        .execution_options(populate_existing=True))).scalars().all()
    for job in jobs:
        if job.lease_token:
            await db.execute(update(GenerationRun).where(GenerationRun.id == job.lease_token)
                .values(state="cancelled", finished_at=now, error="教师终止出题"))
        job.state = "cancelled"
        job.lease_token = None
        job.lease_until = None
        job.error = "教师终止出题"
    await db.flush()
    return len(jobs)


async def stop_preparation(db, quiz_id):
    await db.execute(select(GenerationControl).where(GenerationControl.id == 1).with_for_update())
    quiz = await db.scalar(select(Quiz).where(Quiz.id == quiz_id).with_for_update())
    if quiz is None:
        raise HTTPException(404, "测评不存在")
    count = await stop_pending(db, quiz_id)
    await db.commit()
    return count


async def open_quiz(db, quiz_id, *, confirm_partial=False):
    await db.execute(select(GenerationControl).where(GenerationControl.id == 1).with_for_update())
    quiz = (await db.execute(select(Quiz).where(Quiz.id == quiz_id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if quiz is None:
        raise HTTPException(404, "测评不存在")
    if quiz.status == QuizStatus.PUBLISHED:
        return quiz  # Repeated clicks must never extend the entry window.
    if not quiz.pre_generate or quiz.status != QuizStatus.DRAFT:
        raise HTTPException(409, "当前测评不能开放")
    preparation = await progress(db, quiz_id)
    if not preparation["ready"]:
        if not confirm_partial:
            raise HTTPException(409, "出题尚未全部完成，请确认后再发布")
        if not (preparation["failed"] or preparation["cancelled"]):
            raise HTTPException(409, "请先终止出题，或等待出题完成")
        if not preparation["students_ready"]:
            raise HTTPException(409, "尚无学生的整套问题准备完成，暂不能发布")
        await stop_pending(db, quiz_id)
    now = datetime.now(timezone.utc)
    quiz.status = QuizStatus.PUBLISHED
    quiz.start_time = now
    quiz.end_time = now + timedelta(minutes=30)
    await db.commit()
    return quiz


async def reopen_quiz(db, quiz_id):
    quiz = (await db.execute(select(Quiz).where(Quiz.id == quiz_id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if quiz is None:
        raise HTTPException(404, "测评不存在")
    if quiz.published_at is not None:
        raise HTTPException(409, "成绩已公布，不能重新开放")
    if quiz.status == QuizStatus.DRAFT:
        raise HTTPException(409, "尚未发布的测评请通过开放测评入口发布")
    now = datetime.now(timezone.utc)
    if ensure_utc(quiz.end_time) > now:
        raise HTTPException(409, "测评仍在开放中，请刷新页面")
    quiz.status = QuizStatus.PUBLISHED
    quiz.start_time = now
    quiz.end_time = now + timedelta(minutes=30)
    await db.commit()
    return quiz


async def regenerate_prepared(db, quiz_id, student_number, expected_round, problem_id=None):
    await db.execute(select(GenerationControl).where(GenerationControl.id == 1).with_for_update())
    participant = await db.scalar(select(QuizParticipant).where(
        QuizParticipant.quiz_id == quiz_id, QuizParticipant.student_number == student_number).with_for_update())
    if participant is None:
        raise HTTPException(404, "学生不属于本场测评")
    quiz = await db.get(Quiz, quiz_id)
    if quiz.published_at is not None:
        raise HTTPException(409, "成绩已公布，不能重新生成")
    if not quiz.pre_generate:
        raise HTTPException(409, "本场测评不支持预生成题目")
    latest = await db.scalar(select(Attempt).where(Attempt.participant_id == participant.id)
        .order_by(Attempt.attempt_no.desc()).limit(1))
    if latest is not None and latest.status != AttemptStatus.RESET:
        raise HTTPException(409, "学生已开始或提交作答，不能重新生成预生成题目")
    round_no = await db.scalar(select(func.max(GenerationJob.round_no)).where(GenerationJob.participant_id == participant.id))
    if round_no is None or round_no != expected_round:
        raise HTTPException(409, "题目已更新，请刷新后重试")
    pending = await db.scalar(select(GenerationJob.id).where(GenerationJob.participant_id == participant.id,
        GenerationJob.round_no == round_no, GenerationJob.state.in_(["queued", "running"])).limit(1))
    if pending is not None:
        raise HTTPException(409, "该学生的问题正在生成，请等待完成后再操作")
    snapshots = (await db.execute(select(SubmissionSnapshot).where(
        SubmissionSnapshot.participant_id == participant.id, SubmissionSnapshot.uoj_score > 0))).scalars().all()
    if not snapshots:
        raise HTTPException(409, "没有可用于出题的有效提交")
    targets = snapshots if problem_id is None else [s for s in snapshots if s.uoj_problem_id == problem_id]
    if not targets:
        raise HTTPException(404, "该学生没有这道题的有效提交")
    if problem_id is not None:
        # Keep a complete next round so starting, progress and reset use one consistent set.
        jobs = (await db.scalars(select(GenerationJob).where(GenerationJob.participant_id == participant.id,
            GenerationJob.round_no == round_no).with_for_update().execution_options(populate_existing=True))).all()
        by_snapshot = {j.submission_snapshot_id: j for j in jobs}
        for snapshot in snapshots:
            if snapshot.uoj_problem_id == problem_id:
                continue
            old = by_snapshot.get(snapshot.id)
            if old is None:
                raise HTTPException(409, "题目记录不完整，请刷新后重试")
            fields = {key: getattr(old, key) for key in ("state", "attempts", "result_json", "raw_response",
                "model", "prompt_version", "second_kind", "error")}
            # Completed audits still apply to unchanged text; never duplicate a paid in-flight audit.
            if old.quality_state in ("done", "failed", "not_requested"):
                fields.update({key: getattr(old, key) for key in ("quality_state", "quality_result", "quality_raw",
                    "quality_error", "quality_model", "quality_version", "quality_acknowledged", "quality_finished_at")})
            db.add(GenerationJob(quiz_id=quiz_id, participant_id=participant.id,
                submission_snapshot_id=snapshot.id, round_no=round_no + 1, **fields))
    await enqueue(db, targets, round_no=round_no + 1)
    await db.commit()
    return {"round_no": round_no + 1, "queued": len(targets)}


async def retry_failed(db, quiz_id):
    await db.execute(select(GenerationControl).where(GenerationControl.id == 1).with_for_update())
    quiz = (await db.execute(select(Quiz).where(Quiz.id == quiz_id).with_for_update())).scalar_one_or_none()
    if quiz is None:
        raise HTTPException(404, "测评不存在")
    if quiz.published_at is not None:
        raise HTTPException(409, "成绩已公布，不能重新生成")
    jobs = (await db.execute(select(GenerationJob).where(GenerationJob.quiz_id == quiz_id))).scalars().all()
    latest = {}
    for job in jobs:
        latest[job.participant_id] = max(latest.get(job.participant_id, 0), job.round_no)
    ids = [j.id for j in jobs if j.state in ("failed", "cancelled") and j.round_no == latest[j.participant_id]]
    if ids:
        await db.execute(update(GenerationJob).where(GenerationJob.id.in_(ids), GenerationJob.state.in_(["failed", "cancelled"]))
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
    db.add(GenerationRun(id=token, job_id=job.id, model=model, prompt_version=job.prompt_version))
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
            .values(raw_response=raw, error="Late result ignored after cancellation or lease reassignment"))
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
        if settings.quality_audit_enabled and job.prompt_version != "lightweight_v1":
            job.quality_state = "queued"
    await db.commit()
    return True


async def heartbeat(factory, settings, identity):
    while True:
        await asyncio.sleep(min(3, settings.generation_lease_seconds / 3))
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
            job = await db.scalar(select(GenerationJob).where(GenerationJob.id == identity[0],
                GenerationJob.lease_token == identity[1], GenerationJob.state == "running"))
            if job is None:
                raise RuntimeError("Generation cancelled before request")
            snapshot = (await db.execute(select(SubmissionSnapshot).where(SubmissionSnapshot.id == identity[2])
                .options(selectinload(SubmissionSnapshot.problem)))).scalar_one()
            payload = dict(title=snapshot.problem.title, statement=snapshot.problem.statement,
                language=snapshot.language, source_code=snapshot.source_code,
                second_question_kind=job.second_kind if job.prompt_version == LIGHTWEIGHT_GENERATOR_VERSION else await allocated_kind(db, snapshot))
            lightweight = job.prompt_version == LIGHTWEIGHT_GENERATOR_VERSION
        call = provider.generate_lightweight if lightweight else provider.generate_questions
        return await asyncio.wait_for(call(**payload), settings.generation_task_timeout_seconds)

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
