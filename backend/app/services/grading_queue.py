"""Durable grading queue stored on Attempt, shared by all submission paths."""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import selectinload
from app.models import Answer, AnswerDraft, Attempt, AttemptStatus, Question, LLMCallLog, Quiz, QuizParticipant
from app.services.llm_provider import GRADER_VERSION
from app.services.grading_service import grade_attempt

logger = logging.getLogger(__name__)


async def claim_grading(db, now=None):
    now = now or datetime.now(timezone.utc)
    attempt = await db.scalar(select(Attempt).where(or_(Attempt.status == AttemptStatus.EXPIRED,
        and_(Attempt.status == AttemptStatus.IN_PROGRESS, Attempt.deadline_at <= now),
        and_(Attempt.status == AttemptStatus.GRADING,
            or_(Attempt.grading_lease_until <= now, Attempt.grading_lease_until.is_(None)))))
        .order_by(Attempt.created_at).limit(1).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    if attempt is None:
        await db.commit()
        return None
    is_timeout = attempt.status in {AttemptStatus.EXPIRED, AttemptStatus.IN_PROGRESS}
    if is_timeout:
        attempt.timed_out = True
        if attempt.assessment_version == "lightweight_v1":
            from app.services.lightweight_submission import freeze
            drafts = {d.question_id: d for d in (await db.scalars(select(AnswerDraft)
                .where(AnswerDraft.attempt_id == attempt.id))).all()}
            await freeze(db, attempt, source="timeout", now=attempt.deadline_at or now, drafts=drafts)
    if not attempt.questions or (not is_timeout and any(q.answer is None for q in attempt.questions)):
        attempt.status = AttemptStatus.GRADING_ERROR
        attempt.grading_token = None
        attempt.grading_lease_until = None
        await db.commit()
        return None
    for question in attempt.questions:
        if question.answer is None:
            text = (attempt.draft_text or "") if question.question_index == attempt.draft_question_index else ""
            db.add(Answer(question_id=question.id, student_answer=text.strip()))
    attempt.draft_text = None
    attempt.draft_question_index = None
    attempt.status = AttemptStatus.GRADING
    attempt.grading_token = str(uuid.uuid4())
    attempt.grading_lease_until = now + timedelta(seconds=90)
    identity = attempt.id, attempt.grading_token
    await db.commit()
    return identity


async def renew(factory, identity):
    while True:
        await asyncio.sleep(15)
        async with factory() as db:
            result = await db.execute(update(Attempt).where(Attempt.id == identity[0],
                Attempt.grading_token == identity[1], Attempt.status == AttemptStatus.GRADING)
                .values(grading_lease_until=datetime.now(timezone.utc) + timedelta(seconds=90)))
            await db.commit()
            if not result.rowcount:
                return


async def run_grading(factory, provider, identity, timeout_seconds=1200):
    async def grade():
        async with factory() as db:
            await asyncio.wait_for(grade_attempt(db, provider, identity[0], lease_token=identity[1]), timeout=timeout_seconds)
    grading = asyncio.create_task(grade())
    heartbeat = asyncio.create_task(renew(factory, identity))
    try:
        done, _ = await asyncio.wait([grading, heartbeat], return_when=asyncio.FIRST_COMPLETED)
        if grading in done:
            await grading
        else:
            await heartbeat
    except Exception as exc:
        # Includes deadline expiry and failures before the grading service can log.
        # Ownership fencing also protects reset/deletion and replacement workers.
        async with factory() as db:
            result = await db.execute(update(Attempt).where(Attempt.id == identity[0],
                Attempt.grading_token == identity[1], Attempt.status == AttemptStatus.GRADING)
                .values(status=AttemptStatus.GRADING_ERROR, grading_token=None, grading_lease_until=None))
            if result.rowcount:
                db.add(LLMCallLog(attempt_id=identity[0], call_type="grading",
                    model=provider.model_name, prompt_version=GRADER_VERSION, success=False,
                    error="Grading deadline exceeded" if isinstance(exc, TimeoutError) else "Grading executor failed: " + type(exc).__name__))
            await db.commit()
        raise
    finally:
        grading.cancel()
        heartbeat.cancel()
        await asyncio.gather(grading, heartbeat, return_exceptions=True)


async def grading_worker(factory, settings, provider):
    while True:
        try:
            async with factory() as db:
                identity = await claim_grading(db)
            if identity:
                await run_grading(factory, provider, identity, settings.grading_task_timeout_seconds)
                continue
        except Exception as exc:
            logger.error("Durable grading failed (%s)", type(exc).__name__)
        await asyncio.sleep(settings.grading_poll_seconds)


async def enqueue_regrade(db, attempt_id):
    from fastapi import HTTPException
    identity = await db.get(Attempt, attempt_id)
    if identity is None:
        raise HTTPException(404, "Attempt 不存在")
    await db.execute(select(QuizParticipant.id).where(QuizParticipant.id == identity.participant_id).with_for_update())
    quiz = await db.scalar(select(Quiz).where(Quiz.id == identity.quiz_id).with_for_update()
        .execution_options(populate_existing=True))
    if quiz.published_at is not None:
        raise HTTPException(409, "成绩公布后不能自动重新评分")
    attempt = await db.scalar(select(Attempt).where(Attempt.id == attempt_id)
        .with_for_update().execution_options(populate_existing=True)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    if attempt is None:
        raise HTTPException(404, "Attempt 不存在")
    if attempt.status == AttemptStatus.GRADING:
        await db.commit()
        return attempt  # Repeated requests must not invalidate an active lease.
    if attempt.status not in {AttemptStatus.FINISHED, AttemptStatus.GRADING_ERROR}:
        raise HTTPException(409, "当前作答不能重新评分")
    if not attempt.questions or any(q.answer is None for q in attempt.questions):
        raise HTTPException(409, "作答不完整，不能重新评分")
    attempt.status = AttemptStatus.GRADING
    attempt.grading_token = None
    attempt.grading_lease_until = None
    await db.commit()
    return attempt
