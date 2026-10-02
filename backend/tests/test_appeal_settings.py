import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from test_attempt_flow import db
from test_lightweight_v1 import bundle, prepared_quiz, enable_v1_for_synthetic_tests
from app.config import Settings
from app.models import AdminUser, Answer, Attempt, AttemptStatus
from app.schemas.api import QuizCreateRequest
from app.services.llm_provider import MockLLMProvider
from app.services.publication import student_result, request_appeal, resolve_appeal
from app.services.quiz_service import persist_quiz, start_attempt
from app.services.teacher_settings import TeacherSettings, SettingsUpdate, load_settings, save_settings


@pytest.mark.parametrize("values", [{"appeal_window_days": 0}, {"appeal_window_days": 366},
    {"appeal_window_days": 2.5}, {"appeal_prompt": "  "}, {"appeal_prompt": "x" * 501}])
def test_invalid_appeal_defaults(values):
    with pytest.raises(ValidationError):
        TeacherSettings(**values)


@pytest.mark.asyncio
async def test_appeal_defaults_frozen_and_old_client_preserves_new_fields(db):
    teacher = AdminUser(username="appeal-defaults", password_hash="synthetic", settings_json={
        "appeal_window_days": 3, "appeal_prompt": "说明理由"})
    db.add(teacher); await db.commit()
    await save_settings(db, teacher.id, SettingsUpdate(expected_revision=0, settings=TeacherSettings(minutes_per_question=6)))
    defaults, _ = await load_settings(db, teacher.id)
    assert defaults.appeal_window_days == 3 and defaults.appeal_prompt == "说明理由"
    quiz, _ = await persist_quiz(db, bundle(), QuizCreateRequest(contest_id=7), defaults)
    assert quiz.appeal_window_days == 3 and quiz.appeal_prompt == "说明理由"
    defaults.appeal_window_days = 7
    assert quiz.appeal_window_days == 3


@pytest.mark.asyncio
async def test_appeal_cutoff_idempotent_retry_and_pending_resolution(db):
    quiz = await prepared_quiz(db)
    quiz.appeal_window_days = 1
    quiz.appeal_prompt = "用自己的话说明需要检查的地方"
    current = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250001", session_id="synthetic")
    attempt = await db.get(Attempt, current.attempt_id)
    attempt.status = AttemptStatus.FINISHED
    for q in current.questions:
        db.add(Answer(question_id=uuid.UUID(q["id"]), student_answer="synthetic", auto_score=2))
    quiz.published_at = datetime.now(timezone.utc) - timedelta(hours=12)
    await db.commit()
    quiz_id = quiz.id
    db.expire_all()
    result = await student_result(db, quiz_id, "231250001")
    assert result["appeals_open"] and result["appeal_prompt"] == "用自己的话说明需要检查的地方"
    q1, q2 = (uuid.UUID(q["id"]) for q in current.questions[:2])
    first = await request_appeal(db, quiz_id, "231250001", q1, reason="synthetic reason", request_key="request-1")
    from app.models import Quiz
    quiz = await db.get(Quiz, quiz_id)
    quiz.published_at = datetime.now(timezone.utc) - timedelta(days=1, seconds=1)
    await db.commit()
    assert not (await student_result(db, quiz_id, "231250001"))["appeals_open"]
    assert await request_appeal(db, quiz_id, "231250001", q1, reason="synthetic reason", request_key="request-1") == first
    with pytest.raises(HTTPException) as exc:
        await request_appeal(db, quiz_id, "231250001", q2, reason="late", request_key="request-2")
    assert exc.value.status_code == 403
    resolved = await resolve_appeal(db, uuid.UUID(first["id"]), actor="teacher", resolution="维持原分", expected_version=0, score=None)
    assert resolved["state"] == "resolved"
    # Historical records with no window remain unlimited, even long after publication.
    quiz.appeal_window_days = None; await db.commit()
    assert (await student_result(db, quiz_id, "231250001"))["appeals_open"]
    await request_appeal(db, quiz_id, "231250001", q2, reason="allowed", request_key="request-2")
