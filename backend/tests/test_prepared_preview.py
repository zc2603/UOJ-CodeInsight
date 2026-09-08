import uuid
import httpx
import pytest
from sqlalchemy import select, func
from test_pre_generation import db, prepare_seed, complete_one, NoGeneration
from app.models import Attempt, Question, GenerationRun
from app.main import app
from app.database import get_db
from app.security import create_token
from app.config import Settings
from app.services import generation_service as queue
from app.services.quiz_service import start_attempt


@pytest.mark.asyncio
async def test_teacher_preview_before_start_is_read_only_and_matches_student_questions(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    async def get_test_db():
        yield db
    app.dependency_overrides[get_db] = get_test_db
    path = f"/api/admin/quizzes/{quiz.id}/students/{participant.student_number}/prepared-questions"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(path)).status_code == 401
            client.cookies.set("student_session", create_token(participant.student_number, "student", quiz_id=str(quiz.id), sid="a"))
            assert (await client.get(path)).status_code == 401
            client.cookies.set("admin_session", create_token(str(uuid.uuid4()), "admin", username="teacher"))
            partial = (await client.get(path)).json()
            assert partial["prepared_problem_count"] == 1 and partial["preparation_total"] == 2
            assert len(partial["questions"]) == 2
            await complete_one(db)
            data = (await client.get(path)).json()
            assert len(data["questions"]) == 4 and all(q["reference_answer"] for q in data["questions"])
            rows = (await client.get(f"/api/admin/quizzes/{quiz.id}/results")).json()
            assert rows[0]["attempt_id"] is None
            assert rows[0]["prepared_problem_count"] == 2
            assert rows[0]["question_count"] == 4 and rows[0]["problem_count"] == 2
            assert (await client.get(path.replace(str(quiz.id), str(uuid.uuid4())))).status_code == 404
            assert await db.scalar(select(func.count()).select_from(Attempt)) == 0
            assert await db.scalar(select(func.count()).select_from(Question)) == 0
            assert await db.scalar(select(func.count()).select_from(GenerationRun)) == 2
            await queue.open_quiz(db, quiz.id)
            first = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
                student_number=participant.student_number, session_id="a")
            assert first.question_text == data["questions"][0]["question"]
            assert first.source_code == data["questions"][0]["source_code"]
    finally:
        app.dependency_overrides.clear()
