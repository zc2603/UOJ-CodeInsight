"""Durable timeout submissions, claimed once across workers with renewable leases."""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import selectinload
from app.models import Answer, Attempt, AttemptStatus, Question
from app.services.grading_service import grade_attempt

logger = logging.getLogger(__name__)


async def claim_timeout(db, now=None):
    now = now or datetime.now(timezone.utc)
    attempt = await db.scalar(select(Attempt).where(or_(Attempt.status == AttemptStatus.EXPIRED,
        and_(Attempt.status == AttemptStatus.IN_PROGRESS, Attempt.deadline_at <= now),
        and_(Attempt.status == AttemptStatus.GRADING, Attempt.timed_out.is_(True),
            or_(Attempt.grading_lease_until <= now, Attempt.grading_lease_until.is_(None)))))
        .order_by(Attempt.created_at).limit(1).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    if attempt is None:
        await db.commit()
        return None
    attempt.timed_out = True
    if not attempt.questions:
        attempt.status = AttemptStatus.GRADING_ERROR
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


async def run_timeout(factory, provider, identity):
    async def grade():
        async with factory() as db:
            await grade_attempt(db, provider, identity[0], lease_token=identity[1])
    grading = asyncio.create_task(grade())
    heartbeat = asyncio.create_task(renew(factory, identity))
    try:
        done, _ = await asyncio.wait([grading, heartbeat], return_when=asyncio.FIRST_COMPLETED)
        if grading in done:
            await grading
        else:
            await heartbeat
    finally:
        grading.cancel()
        heartbeat.cancel()
        await asyncio.gather(grading, heartbeat, return_exceptions=True)


async def timeout_worker(factory, settings, provider):
    while True:
        try:
            async with factory() as db:
                identity = await claim_timeout(db)
            if identity:
                await run_timeout(factory, provider, identity)
                continue
        except Exception as exc:
            logger.error("Timeout grading failed (%s)", type(exc).__name__)
        await asyncio.sleep(settings.attempt_maintenance_interval_seconds)
