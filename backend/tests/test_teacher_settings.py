import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from test_attempt_flow import db
from test_lightweight_v1 import bundle, prepared_quiz, enable_v1_for_synthetic_tests
from app.api.dependencies import AdminPrincipal, require_admin
from app.database import get_db
from app.main import app
from app.models import AdminUser, Quiz, QuizProblemSnapshot, QuizStatus
from app.schemas.api import QuizCreateRequest
from app.services.quiz_service import persist_quiz
from app.services.generation_service import open_quiz, reopen_quiz
from app.services.teacher_settings import TeacherSettings, SettingsUpdate, load_settings, save_settings, grade_for
from app.time_utils import ensure_utc


@pytest.mark.asyncio
async def test_settings_http_identity_persistence_and_conflicts(db):
    a = AdminUser(username="settings-a", password_hash="synthetic")
    b = AdminUser(username="settings-b", password_hash="synthetic")
    db.add_all([a, b]); await db.commit()
    async def database():
        yield db
    app.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/admin/settings")).status_code == 401
            app.dependency_overrides[require_admin] = lambda: AdminPrincipal(a.id, a.username)
            response = (await client.get("/api/admin/settings")).json()
            assert response["settings"]["minutes_per_question"] == 4
            settings = response["settings"]
            settings.update(entry_minutes=45, code_wrap=True, question_template="all_short")
            request = {"expected_revision": 0, "settings": settings}
            saved = await client.put("/api/admin/settings", json=request)
            assert saved.status_code == 200 and saved.json()["revision"] == 1
            assert (await client.put("/api/admin/settings", json=request)).status_code == 409
            assert (await client.get("/api/admin/settings")).json()["settings"]["code_wrap"] is True
            from types import SimpleNamespace
            from unittest.mock import AsyncMock, patch
            import app.api.admin as admin
            with patch.object(admin, "_import_service", return_value=SimpleNamespace(build_bundle=AsyncMock(return_value=bundle()))):
                preview = await client.post("/api/admin/quizzes/preview-contest", json={"contest_id": 7})
                assert preview.status_code == 200
                assert not any(p["include_choice"] for p in preview.json()["problems"])
                stale = await client.post("/api/admin/quizzes", json={"contest_id": 7, "expected_settings_revision": 0})
                assert stale.status_code == 409
                created = await client.post("/api/admin/quizzes", json={"contest_id": 7, "expected_settings_revision": 1})
                assert created.status_code == 200
                quiz = await db.get(Quiz, uuid.UUID(created.json()["id"]))
                assert quiz.entry_minutes == 45 and quiz.duration_minutes == 12
                assert quiz.minutes_per_question == 4
            app.dependency_overrides[require_admin] = lambda: AdminPrincipal(b.id, b.username)
            other = (await client.get("/api/admin/settings")).json()
            assert other["revision"] == 0 and other["settings"]["entry_minutes"] == 30
            settings["entry_minutes"] = 0
            assert (await client.put("/api/admin/settings", json=request)).status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("values", [
    {"minutes_per_question": 0}, {"minutes_per_question": 4.5}, {"entry_minutes": 1441},
    {"code_font_size": 40}, {"result_columns": ["grade", "grade"]},
    {"grade_bands": [{"label": "A", "minimum": 2}, {"label": "A", "minimum": 0}]},
    {"grade_bands": [{"label": "A", "minimum": 2}, {"label": "B", "minimum": 1}]},
    {"grade_bands": [{"label": "A", "minimum": 0}, {"label": "B", "minimum": 0}]},
])
def test_invalid_settings(values):
    with pytest.raises(ValidationError):
        TeacherSettings(**values)


def test_grades_raw_score_boundaries_and_custom_rules():
    assert [grade_for(n) for n in range(11)] == ["D", "C", "C", "B", "B", "B+", "B+", "A", "A", "A+", "A+"]
    assert grade_for(None) is None and grade_for(-1) is None
    assert grade_for(100) == "A+"
    assert grade_for(6, [{"label": "理解", "minimum": 6}, {"label": "继续练习", "minimum": 0}]) == "理解"


@pytest.mark.asyncio
async def test_defaults_snapshot_explicit_empty_and_fixed_duration(db):
    defaults = TeacherSettings(entry_minutes=45, reopen_minutes=12, time_mode="fixed", fixed_minutes=19,
        question_template="all_choice")
    quiz, _ = await persist_quiz(db, bundle(), QuizCreateRequest(contest_id=7), defaults)
    assert quiz.time_mode == "fixed" and quiz.duration_minutes == 19
    assert quiz.entry_minutes == 45 and quiz.reopen_minutes == 12
    snapshots = (await db.scalars(select(QuizProblemSnapshot).where(QuizProblemSnapshot.quiz_id == quiz.id))).all()
    assert all(p.include_choice for p in snapshots)
    defaults.grade_bands[0].minimum = 12
    defaults.entry_minutes = 60
    assert quiz.grade_bands[0]["minimum"] == 9 and quiz.entry_minutes == 45
    second, _ = await persist_quiz(db, bundle(), QuizCreateRequest(contest_id=7,
        choice_problem_ids=[], time_mode="per_question", minutes_per_question=7, entry_minutes=15), defaults)
    assert second.duration_minutes == 21 and second.entry_minutes == 15
    assert not any((await db.scalars(select(QuizProblemSnapshot.include_choice).where(QuizProblemSnapshot.quiz_id == second.id))).all())
    with pytest.raises(ValueError, match="180"):
        await persist_quiz(db, bundle(), QuizCreateRequest(contest_id=7, minutes_per_question=40))


@pytest.mark.asyncio
async def test_open_reopen_frozen_windows_and_duplicate_open(db):
    quiz = await prepared_quiz(db, QuizCreateRequest(contest_id=7, entry_minutes=45, reopen_minutes=12))
    assert (ensure_utc(quiz.end_time) - ensure_utc(quiz.start_time)).total_seconds() == 45 * 60
    end = quiz.end_time
    await open_quiz(db, quiz.id)
    assert ensure_utc(quiz.end_time) == ensure_utc(end)
    quiz.end_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    await reopen_quiz(db, quiz.id)
    assert (ensure_utc(quiz.end_time) - ensure_utc(quiz.start_time)).total_seconds() == 12 * 60
    quiz.end_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    await reopen_quiz(db, quiz.id, minutes=8)
    assert (ensure_utc(quiz.end_time) - ensure_utc(quiz.start_time)).total_seconds() == 8 * 60
    assert quiz.reopen_minutes == 12


@pytest.mark.asyncio
async def test_published_grade_shared_by_student_teacher_and_export(db):
    from app.config import Settings
    from app.models import Attempt, AttemptStatus, Answer
    from app.services.quiz_service import start_attempt
    from app.services.llm_provider import MockLLMProvider
    from app.services.publication import student_result
    from app.api.admin import _result_rows, export_csv
    quiz = await prepared_quiz(db)
    quiz.grade_bands = [{"label": "理解", "minimum": 6}, {"label": "练习", "minimum": 0}]
    current = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250002", session_id="synthetic")
    attempt = await db.get(Attempt, current.attempt_id)
    attempt.status = AttemptStatus.FINISHED
    for q in current.questions:
        db.add(Answer(question_id=uuid.UUID(q["id"]), student_answer="synthetic", auto_score=2))
    await db.commit()
    assert (await student_result(db, quiz.id, "231250002")) == {"published": False, "message": "成绩尚未公布"}
    quiz.published_at = datetime.now(timezone.utc)
    await db.commit()
    quiz_id = quiz.id
    db.expire_all()
    result = await student_result(db, quiz_id, "231250002")
    assert result["score"] == result["max_score"] == 6 and result["grade"] == "理解"
    rows = await _result_rows(db, quiz_id)
    assert next(r for r in rows if r.student_number == "231250002").grade == "理解"
    response = await export_csv(quiz_id, db)
    text = "".join([chunk async for chunk in response.body_iterator])
    assert "grade" in text and "理解" in text
