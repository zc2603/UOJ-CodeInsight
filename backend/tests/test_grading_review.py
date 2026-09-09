import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text, select
from alembic.migration import MigrationContext
from alembic.operations import Operations

from test_attempt_flow import db, seed_participant
from app.config import Settings
from app.models import Attempt, Answer
from app.schemas.api import ManualOverrideRequest
from app.services.llm_provider import MockLLMProvider
from app.services.quiz_service import start_attempt
from app.services.question_service import submit_answer
from app.services.grading_service import grade_attempt
from app.api.admin import _result_rows, attempt_detail, override_score, list_quizzes


@pytest.mark.asyncio
async def test_review_retains_suggestions_hides_final_and_requires_teacher_resolution(db):
    quiz, participant = await seed_participant(db)
    quiz_id, student = quiz.id, participant.student_number
    started = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz_id, student_number=student, session_id="a")
    for index in range(1, 5):
        await submit_answer(db, quiz_id=quiz_id, student_number=student, session_id="a", question_index=index, student_answer="合成的足够长的答案用于完整走通本地评分和复核流程")
    class ReviewProvider(MockLLMProvider):
        async def grade_answers(self, **kwargs):
            result, raw = await super().grade_answers(**kwargs)
            result.grades[0].review_required = True
            result.grades[0].review_reason = "题目存在缺陷"
            result.grades[0].question_validity = "invalid"
            return result, raw
    attempt = await grade_attempt(db, ReviewProvider(), started.attempt_id)
    assert attempt.review_required and attempt.auto_score == 8
    db.expire_all()
    rows = await _result_rows(db, quiz_id)
    assert rows[0].review_required and rows[0].auto_score == 8 and rows[0].final_score is None
    details = await attempt_detail(started.attempt_id, db)
    assert details["review_required"] and details["final_percent"] is None
    assert any(q["review_reason"] == "题目存在缺陷" for q in details["questions"])
    stored = (await db.scalars(select(Answer).join(Answer.question).where(Answer.question.has(attempt_id=started.attempt_id)))).all()
    assert len(stored) == 4 and all(answer.auto_score == 2 for answer in stored)
    assert (await list_quizzes(db))[0].finished_count == 0
    from app.api.student import result as student_result
    from app.api.dependencies import StudentPrincipal
    public_result = await student_result(StudentPrincipal(quiz_id, student, "a"), db)
    assert public_result.submitted and not public_result.score_visible and public_result.total_score is None
    # Even a later clean model result cannot dismiss an outstanding review.
    await grade_attempt(db, MockLLMProvider(), started.attempt_id)
    assert (await db.get(Attempt, started.attempt_id)).review_required
    await override_score(started.attempt_id, ManualOverrideRequest(score=7, reason="教师已复核并调整总分"), db)
    db.expire_all()
    row = (await _result_rows(db, quiz_id))[0]
    assert not row.review_required and row.final_score == 7
    assert (await list_quizzes(db))[0].finished_count == 1
    # A new flagged regrade requires fresh review, even with a prior manual score.
    await grade_attempt(db, ReviewProvider(), started.attempt_id)
    db.expire_all()
    assert (await _result_rows(db, quiz_id))[0].final_score is None


def test_additive_migration_retains_legacy_questions_and_allows_new_types(tmp_path):
    migration_path = Path(__file__).parents[1] / "alembic/versions/0004_grading_review.py"
    spec = importlib.util.spec_from_file_location("review_migration", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite:///" + str(tmp_path / "old.sqlite"))
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE attempts (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE answers (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE questions (id INTEGER PRIMARY KEY, question_type VARCHAR(24), CONSTRAINT questiontype CHECK (question_type IN ('explanation','trace','boundary_or_modification')))"))
        connection.execute(text("INSERT INTO questions VALUES (1, 'boundary_or_modification')"))
        connection.execute(text("INSERT INTO attempts VALUES (1)"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
        connection.execute(text("INSERT INTO questions VALUES (2, 'boundary'), (3, 'modification')"))
        assert connection.scalar(text("SELECT COUNT(*) FROM questions")) == 3
        assert connection.scalar(text("SELECT review_required FROM attempts WHERE id=1")) == 0
    engine.dispose()
