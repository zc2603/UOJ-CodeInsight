import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_attempt_flow import db
from test_pre_generation import prepare_seed, complete_one, NoGeneration
from app.models import GenerationJob
from app.config import Settings
from app.services.generation_service import open_quiz
from app.services.quiz_service import start_attempt
from app.services.prepared_edit import edit_prepared, EditPreparedRequest, revision
from app.api.admin import prepared_questions

@pytest.mark.asyncio
async def test_edit_updates_student_question_and_rejects_stale_or_started(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    detail = await prepared_questions(quiz.id, participant.student_number, db)
    assert detail["can_edit"]
    job = await db.scalar(select(GenerationJob).where(GenerationJob.participant_id == participant.id).order_by(GenerationJob.submission_snapshot_id))
    job.quality_state = "running"
    job.quality_token = "old-audit"
    await db.commit()
    payload = EditPreparedRequest(expected_revision=revision(job.result_json), question="**教师修改** $n^2$",
        question_en="Teacher edited question", reference_answer="教师参考答案", grading_points=["要点"])
    other = job.result_json["questions"][1].copy()
    await edit_prepared(db, quiz.id, participant.student_number, job.id, 1, payload)
    assert job.result_json["questions"][1] == other
    assert job.quality_state == "paused" and job.quality_token is None
    with pytest.raises(HTTPException) as error:
        await edit_prepared(db, quiz.id, participant.student_number, job.id, 1, payload)
    assert error.value.status_code == 409
    await open_quiz(db, quiz.id)
    await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    from app.models import Question, Attempt
    question = await db.scalar(select(Question).join(Attempt).where(Attempt.participant_id == participant.id, Question.submission_snapshot_id == job.submission_snapshot_id, Question.question_text == payload.question))
    assert question is not None and question.reference_answer == payload.reference_answer
    assert not (await prepared_questions(quiz.id, participant.student_number, db))["can_edit"]
    payload.expected_revision = revision(job.result_json)
    with pytest.raises(HTTPException) as error:
        await edit_prepared(db, quiz.id, participant.student_number, job.id, 1, payload)
    assert error.value.status_code == 409

@pytest.mark.asyncio
async def test_edit_endpoint_auth_scope_and_validation(db):
    import httpx, uuid
    from app.main import app
    from app.database import get_db
    from app.security import create_token
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    job = await db.scalar(select(GenerationJob).where(GenerationJob.state == "succeeded"))
    async def test_db(): yield db
    app.dependency_overrides[get_db] = test_db
    payload = dict(expected_revision=revision(job.result_json), question="new", question_en="new", reference_answer="answer", grading_points=["point"])
    path = f"/api/admin/quizzes/{quiz.id}/students/{participant.student_number}/prepared-questions/{job.id}/1"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.patch(path,json=payload)).status_code == 401
            client.cookies.set("admin_session",create_token(str(uuid.uuid4()),"admin",username="teacher"))
            assert (await client.patch(path,json={**payload,"question":"   "})).status_code == 422
            assert (await client.patch(path.replace(str(quiz.id),str(uuid.uuid4())),json=payload)).status_code == 404
            assert (await client.patch(path,json=payload)).status_code == 200
    finally: app.dependency_overrides.clear()
