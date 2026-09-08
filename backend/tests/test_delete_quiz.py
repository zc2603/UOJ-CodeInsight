import uuid
import httpx
import pytest
from sqlalchemy import select, func, text

from test_attempt_flow import db, seed_participant
from app.api.admin import delete_quiz
from app.config import Settings
from app.models import Quiz, QuizParticipant, QuizProblemSnapshot, SubmissionSnapshot, Attempt, Question, Answer, LLMCallLog, GenerationControl, GenerationJob, GenerationRun
from app.services.quiz_service import start_attempt
from app.services.llm_provider import MockLLMProvider
from app.services import generation_service as queue


@pytest.mark.asyncio
async def test_delete_cascades_all_owned_data_preserves_other_quiz_and_fences_result(db):
    await db.execute(text("PRAGMA foreign_keys=ON"))
    quiz, participant = await seed_participant(db)
    other, _ = await seed_participant(db)
    quiz_id, other_id = quiz.id, other.id
    first = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz_id,
        student_number=participant.student_number, session_id="delete-test")
    question = await db.scalar(select(Question).where(Question.attempt_id == first.attempt_id).limit(1))
    db.add(Answer(question_id=question.id, student_answer="Synthetic answer"))
    db.add(GenerationControl(id=1))
    snapshots = (await db.execute(select(SubmissionSnapshot).where(SubmissionSnapshot.quiz_id == quiz_id))).scalars().all()
    await queue.enqueue(db, snapshots)
    await db.commit()
    identity = await queue.claim(db, Settings(), "mock")
    assert identity
    assert (await delete_quiz(quiz_id, db))["deleted"]
    assert not (await delete_quiz(quiz_id, db))["deleted"]
    result, raw = await MockLLMProvider().generate_questions()
    assert not await queue.finish(db, Settings(), identity, result=result, raw=raw)
    for model in [Attempt, Question, Answer, LLMCallLog, GenerationJob, GenerationRun]:
        assert await db.scalar(select(func.count()).select_from(model)) == 0
    for model in [QuizParticipant, QuizProblemSnapshot, SubmissionSnapshot]:
        assert await db.scalar(select(func.count()).select_from(model).where(model.quiz_id == quiz_id)) == 0
        assert await db.scalar(select(func.count()).select_from(model).where(model.quiz_id == other_id)) > 0
    assert await db.scalar(select(func.count()).select_from(Quiz)) == 1


@pytest.mark.asyncio
async def test_delete_endpoint_requires_teacher(db):
    from app.main import app
    from app.database import get_db
    from app.security import create_token
    quiz, _ = await seed_participant(db)
    quiz_id = quiz.id
    async def get_test_db():
        yield db
    app.dependency_overrides[get_db] = get_test_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.delete(f"/api/admin/quizzes/{quiz_id}")).status_code == 401
            client.cookies.set("student_session", create_token("20260001", "student", quiz_id=str(quiz_id), sid="a"))
            assert (await client.delete(f"/api/admin/quizzes/{quiz_id}")).status_code == 401
            assert await db.scalar(select(func.count()).select_from(Quiz)) == 1
            client.cookies.set("admin_session", create_token(str(uuid.uuid4()), "admin", username="teacher"))
            response = await client.delete(f"/api/admin/quizzes/{quiz_id}")
            assert response.status_code == 200 and response.json()["deleted"]
    finally:
        app.dependency_overrides.clear()
