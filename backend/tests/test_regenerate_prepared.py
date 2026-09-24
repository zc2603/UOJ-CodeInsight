import uuid
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from test_attempt_flow import db
from test_pre_generation import prepare_seed, complete_one, NoGeneration, add_unprepared_student
from app.models import Attempt, GenerationJob, Question
from app.config import Settings
from app.services import generation_service as queue
from app.services.quiz_service import start_attempt, reset_attempt


@pytest.mark.asyncio
async def test_regeneration_preserves_history_and_start_uses_latest_round(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    first_jobs = (await db.execute(select(GenerationJob))).scalars().all()
    old_results = [job.result_json for job in first_jobs]
    await queue.open_quiz(db, quiz.id)
    result = await queue.regenerate_prepared(db, quiz.id, participant.student_number, 1)
    assert result == {"round_no": 2, "queued": 2}
    with pytest.raises(HTTPException):
        await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
            student_number=participant.student_number, session_id="a")
    with pytest.raises(HTTPException):
        await queue.regenerate_prepared(db, quiz.id, participant.student_number, 1)
    with pytest.raises(HTTPException):
        await queue.regenerate_prepared(db, quiz.id, participant.student_number, 2)
    await complete_one(db)
    await complete_one(db)
    assert [job.result_json for job in first_jobs] == old_results
    assert all(job.state == "succeeded" for job in first_jobs)
    assert (await queue.progress(db, quiz.id))["total"] == 2
    response = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="a")
    attempt = await db.get(Attempt, response.attempt_id)
    assert attempt.attempt_no == 2 and response.question_count == 4
    with pytest.raises(HTTPException):
        await queue.regenerate_prepared(db, quiz.id, participant.student_number, 2)
    assert await db.scalar(select(func.count()).select_from(Question)) == 4
    await reset_attempt(db, attempt.id)
    await complete_one(db)
    await complete_one(db)
    assert (await queue.regenerate_prepared(db, quiz.id, participant.student_number, 3))["round_no"] == 4
    await complete_one(db)
    await complete_one(db)
    next_response = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="b")
    assert (await db.get(Attempt, next_response.attempt_id)).attempt_no == 4


@pytest.mark.asyncio
async def test_regenerate_api_auth_scope_and_stale_request(db):
    from app.main import app
    from app.database import get_db
    from app.security import create_token
    quiz, first = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    other = await add_unprepared_student(db, quiz)
    async def test_db():
        yield db
    app.dependency_overrides[get_db] = test_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/admin/quizzes/{quiz.id}/students/{first.student_number}/regenerate-questions"
            assert (await client.post(path,json={"expected_round":1})).status_code == 401
            client.cookies.set("admin_session", create_token(str(uuid.uuid4()), "admin", username="teacher"))
            assert (await client.post(path,json={})).status_code == 422
            response = await client.post(path,json={"expected_round":1})
            assert response.status_code == 200 and response.json()["round_no"] == 2
            assert (await client.post(path,json={"expected_round":1})).status_code == 409
            assert await db.scalar(select(func.max(GenerationJob.round_no)).where(GenerationJob.participant_id == other.id)) == 1
            assert await db.scalar(select(func.count()).select_from(Attempt)) == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_regenerate_one_problem_copies_others_and_blocks_stale_requests(db):
    from app.models import SubmissionSnapshot
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    jobs = (await db.scalars(select(GenerationJob).where(GenerationJob.participant_id == participant.id))).all()
    target = jobs[0]
    other = jobs[1]
    other.quality_state = "done"
    other.quality_result = {"questions": [{"index":1,"verdict":"pass","reason":"ok"}]}
    old_text = other.result_json
    await db.commit()
    await queue.open_quiz(db, quiz.id)
    snapshot = await db.get(SubmissionSnapshot, target.submission_snapshot_id)
    with pytest.raises(HTTPException):
        await queue.regenerate_prepared(db, quiz.id, participant.student_number, 1, 999999)
    result = await queue.regenerate_prepared(db, quiz.id, participant.student_number, 1, snapshot.uoj_problem_id)
    assert result == {"round_no":2,"queued":1}
    new = (await db.scalars(select(GenerationJob).where(GenerationJob.round_no == 2))).all()
    assert len(new) == 2 and sum(j.state == "queued" for j in new) == 1
    copied = next(j for j in new if j.submission_snapshot_id == other.submission_snapshot_id)
    assert copied.result_json == old_text and copied.quality_result == other.quality_result
    assert copied.quality_state == "done"
    assert target.state == "succeeded" and other.result_json == old_text
    with pytest.raises(HTTPException):
        await queue.regenerate_prepared(db, quiz.id, participant.student_number, 1, snapshot.uoj_problem_id)
    with pytest.raises(HTTPException):
        await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    await complete_one(db)
    await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    with pytest.raises(HTTPException):
        await queue.regenerate_prepared(db, quiz.id, participant.student_number, 2, snapshot.uoj_problem_id)
