import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy import update
from fastapi import HTTPException
from test_attempt_flow import db
from test_timeout_submission import setup
from app.models import Attempt, AttemptStatus
from app.services.question_service import submit_answer
from app.services.grading_queue import claim_grading, run_grading, enqueue_regrade
from app.services.grading_service import grade_attempt
from app.services.llm_provider import MockLLMProvider

async def submitted(db):
    first, args = await setup(db)
    for index in range(1, 5):
        await submit_answer(db, **args, question_index=index, student_answer='synthetic answer')
    return first.attempt_id

@pytest.mark.asyncio
async def test_manual_submission_survives_session_restart_and_regrade(db):
    attempt_id = await submitted(db)
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async with factory() as fresh:
        identity = await claim_grading(fresh)
        assert identity[0] == attempt_id
        assert await claim_grading(fresh) is None
    await run_grading(factory, MockLLMProvider(), identity)
    db.expire_all()
    attempt = await db.get(Attempt, attempt_id)
    assert attempt.status == AttemptStatus.FINISHED and not attempt.timed_out
    await enqueue_regrade(db, attempt_id)
    identity = await claim_grading(db)
    await enqueue_regrade(db, attempt_id)
    assert attempt.grading_token == identity[1]
    await run_grading(factory, MockLLMProvider(), identity)
    db.expire_all()
    assert (await db.get(Attempt, attempt_id)).status == AttemptStatus.FINISHED

@pytest.mark.asyncio
async def test_shutdown_leaves_recoverable_lease(db):
    attempt_id = await submitted(db)
    identity = await claim_grading(db)
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    started = asyncio.Event()
    class Blocking(MockLLMProvider):
        async def grade_answers(self, **kwargs):
            started.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(run_grading(factory, Blocking(), identity))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    db.expire_all()
    attempt = await db.get(Attempt, attempt_id)
    assert attempt.status == AttemptStatus.GRADING
    assert await claim_grading(db) is None
    replacement = await claim_grading(db, datetime.now(timezone.utc) + timedelta(seconds=91))
    assert replacement[1] != identity[1]
    await run_grading(factory, MockLLMProvider(), replacement)
    db.expire_all()
    assert (await db.get(Attempt, attempt_id)).status == AttemptStatus.FINISHED

@pytest.mark.asyncio
async def test_whole_task_deadline_stops_infinite_renewal(db):
    attempt_id = await submitted(db)
    identity = await claim_grading(db)
    class Blocking(MockLLMProvider):
        async def grade_answers(self, **kwargs):
            await asyncio.Event().wait()
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    with pytest.raises(TimeoutError):
        await run_grading(factory, Blocking(), identity, timeout_seconds=0.05)
    db.expire_all()
    attempt = await db.get(Attempt, attempt_id)
    assert attempt.status == AttemptStatus.GRADING_ERROR
    assert attempt.grading_token is None
    assert await claim_grading(db) is None

@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_reset_fences_late_success_and_failure(db, fail):
    attempt_id = await submitted(db)
    identity = await claim_grading(db)
    reset_lock = asyncio.Lock()
    class Resetting(MockLLMProvider):
        reset_done = False
        async def grade_answers(self, **kwargs):
            async with reset_lock:
                if not self.reset_done:
                    await db.execute(update(Attempt).where(Attempt.id == attempt_id).values(status=AttemptStatus.RESET))
                    await db.commit()
                    self.reset_done = True
            if fail:
                raise RuntimeError('synthetic failure')
            return await super().grade_answers(**kwargs)
    await grade_attempt(db, Resetting(), attempt_id, lease_token=identity[1])
    assert (await db.get(Attempt, attempt_id)).status == AttemptStatus.RESET
    with pytest.raises(HTTPException):
        await enqueue_regrade(db, attempt_id)

@pytest.mark.asyncio
async def test_incomplete_legacy_grading_is_error_not_fabricated_answers(db):
    first, _ = await setup(db)
    attempt = await db.get(Attempt, first.attempt_id)
    attempt.status = AttemptStatus.GRADING
    await db.commit()
    assert await claim_grading(db) is None
    assert attempt.status == AttemptStatus.GRADING_ERROR
    assert not attempt.timed_out
