from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from test_attempt_flow import db
from test_pre_generation import prepare_seed, complete_one
from app.models import QuizStatus
from app.services.generation_service import open_quiz
from app.services.scheduled_open import set_schedule, open_due_quizzes
from app.time_utils import ensure_utc


@pytest.mark.asyncio
async def test_schedule_opens_once_at_fixed_window(db):
    quiz, _ = await prepare_seed(db)
    at = datetime.now(timezone.utc) + timedelta(hours=1)
    await set_schedule(db, quiz.id, at)
    await complete_one(db)
    await complete_one(db)
    await open_due_quizzes(db, now=at - timedelta(seconds=1))
    assert quiz.status == QuizStatus.DRAFT
    await open_due_quizzes(db, now=at + timedelta(seconds=2))
    assert quiz.status == QuizStatus.PUBLISHED
    assert ensure_utc(quiz.start_time) == at
    assert ensure_utc(quiz.end_time) == at + timedelta(minutes=30)
    assert quiz.scheduled_open_at is None
    await open_due_quizzes(db, now=at + timedelta(minutes=1))
    assert ensure_utc(quiz.end_time) == at + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_not_ready_failure_is_sticky_until_teacher_reschedules(db):
    quiz, _ = await prepare_seed(db)
    at = datetime.now(timezone.utc) + timedelta(hours=1)
    await set_schedule(db, quiz.id, at)
    await open_due_quizzes(db, now=at)
    assert quiz.status == QuizStatus.DRAFT
    assert "尚未全部就绪" in quiz.scheduled_open_error
    await complete_one(db)
    await complete_one(db)
    await open_due_quizzes(db, now=at + timedelta(minutes=1))
    assert quiz.status == QuizStatus.DRAFT
    await set_schedule(db, quiz.id, at + timedelta(hours=1))
    assert quiz.scheduled_open_error is None
    await open_due_quizzes(db, now=at + timedelta(hours=1))
    assert quiz.status == QuizStatus.PUBLISHED


@pytest.mark.asyncio
async def test_cancel_immediate_and_expired_recovery(db):
    quiz, _ = await prepare_seed(db)
    at = datetime.now(timezone.utc) + timedelta(hours=1)
    await set_schedule(db, quiz.id, at)
    await set_schedule(db, quiz.id, None)
    await open_due_quizzes(db, now=at)
    assert quiz.status == QuizStatus.DRAFT
    await set_schedule(db, quiz.id, at)
    await complete_one(db)
    await complete_one(db)
    await open_due_quizzes(db, now=at + timedelta(minutes=30))
    assert "窗口已结束" in quiz.scheduled_open_error
    await open_quiz(db, quiz.id)
    assert quiz.scheduled_open_at is None and quiz.scheduled_open_error is None
    with pytest.raises(HTTPException) as error:
        await set_schedule(db, quiz.id, at)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_reject_past_and_naive_times(db):
    quiz, _ = await prepare_seed(db)
    for at in [datetime.now(), datetime.now(timezone.utc) - timedelta(seconds=1)]:
        with pytest.raises(HTTPException) as error:
            await set_schedule(db, quiz.id, at)
        assert error.value.status_code == 422
    assert quiz.scheduled_open_at is None
