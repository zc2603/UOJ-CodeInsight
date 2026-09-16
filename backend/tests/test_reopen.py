from datetime import datetime, timedelta, timezone
import uuid
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from test_attempt_flow import db, seed_participant
from test_pre_generation import prepare_seed, complete_one, NoGeneration
from app.models import Attempt, QuizStatus, GenerationJob
from app.config import Settings
from app.services.generation_service import reopen_quiz, open_quiz
from app.services.quiz_service import start_attempt
from app.time_utils import ensure_utc


@pytest.mark.asyncio
async def test_reopen_preserves_attempt_and_prepared_questions(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    await open_quiz(db, quiz.id)
    response = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="same-device")
    attempt = await db.get(Attempt, response.attempt_id)
    deadline = ensure_utc(attempt.deadline_at)
    quiz.end_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    before = datetime.now(timezone.utc)
    await reopen_quiz(db, quiz.id)
    assert ensure_utc(quiz.end_time) >= before + timedelta(minutes=30)
    assert quiz.end_time - quiz.start_time == timedelta(minutes=30)
    assert quiz.status == QuizStatus.PUBLISHED
    assert ensure_utc(attempt.deadline_at) == deadline
    again = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="same-device")
    assert again.attempt_id == response.attempt_id
    assert await db.scalar(select(func.count()).select_from(GenerationJob)) == 2
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 1
    end = ensure_utc(quiz.end_time)
    with pytest.raises(HTTPException):
        await reopen_quiz(db, quiz.id)
    assert ensure_utc(quiz.end_time) == end


@pytest.mark.asyncio
async def test_reopen_api_requires_admin_and_rejects_draft(db):
    from app.main import app
    from app.database import get_db
    from app.security import create_token
    quiz, _ = await seed_participant(db)
    quiz.status = QuizStatus.CLOSED
    quiz.end_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()
    async def test_db():
        yield db
    app.dependency_overrides[get_db] = test_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/admin/quizzes/{quiz.id}/reopen"
            assert (await client.post(path)).status_code == 401
            client.cookies.set("admin_session", create_token(str(uuid.uuid4()), "admin", username="teacher"))
            assert (await client.post(path)).status_code == 200
            assert (await client.post(path)).status_code == 409
            assert (await client.post(f"/api/admin/quizzes/{uuid.uuid4()}/reopen")).status_code == 404
            quiz.status = QuizStatus.DRAFT
            quiz.end_time = datetime.now(timezone.utc) - timedelta(minutes=1)
            await db.commit()
            assert (await client.post(path)).status_code == 409
    finally:
        app.dependency_overrides.clear()
