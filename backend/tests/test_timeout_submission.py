from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from test_attempt_flow import db, seed_participant
from app.config import Settings
from app.models import Attempt, AttemptStatus, Question
from app.services.quiz_service import start_attempt, _attempt_view
from app.services.question_service import save_draft, submit_answer
from app.services.timeout_grading import claim_timeout
from app.services.grading_service import grade_attempt
from app.services.llm_provider import MockLLMProvider

async def setup(db):
    quiz, participant = await seed_participant(db)
    first = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="a")
    args = dict(quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    return first, args

@pytest.mark.asyncio
async def test_draft_revision_resume_and_boundaries(db):
    first, args = await setup(db)
    await save_draft(db, **args, question_index=1, student_answer="new", revision=2)
    await save_draft(db, **args, question_index=1, student_answer="old", revision=1)
    assert (await _attempt_view(db, first.attempt_id)).draft_answer == "new"
    with pytest.raises(HTTPException) as err:
        await save_draft(db, **{**args, "session_id":"other"}, question_index=1, student_answer="bad", revision=3)
    assert err.value.status_code == 403
    with pytest.raises(HTTPException):
        await save_draft(db, **args, question_index=2, student_answer="bad", revision=3)
    await submit_answer(db, **args, question_index=1, student_answer="submitted")
    assert (await _attempt_view(db, first.attempt_id)).draft_answer == ""
    attempt = await db.get(Attempt, first.attempt_id)
    attempt.deadline_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    await db.commit()
    with pytest.raises(HTTPException) as err:
        await save_draft(db, **args, question_index=2, student_answer="late", revision=4)
    assert err.value.status_code == 410

@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
async def test_timeout_saves_draft_and_grades_without_page_visit(db, historical):
    first, args = await setup(db)
    await submit_answer(db, **args, question_index=1, student_answer="previous submitted answer")
    await save_draft(db, **args, question_index=2, student_answer="saved draft answer", revision=1)
    attempt = await db.get(Attempt, first.attempt_id)
    attempt.deadline_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    if historical: attempt.status = AttemptStatus.EXPIRED
    await db.commit()
    identity = await claim_timeout(db)
    assert identity and await claim_timeout(db) is None
    class Recording(MockLLMProvider):
        calls = []
        async def grade_answers(self, **kwargs):
            self.calls.append(kwargs["question_payload"])
            return await super().grade_answers(**kwargs)
    provider = Recording()
    await grade_attempt(db, provider, identity[0], lease_token=identity[1])
    db.expire_all()
    result = await db.scalar(select(Attempt).where(Attempt.id == first.attempt_id)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    assert result.status == AttemptStatus.FINISHED and result.timed_out
    assert [q.answer.student_answer for q in result.questions] == ["previous submitted answer", "saved draft answer", "", ""]
    assert [q.answer.auto_score for q in result.questions[2:]] == [0, 0]
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 2
    assert await claim_timeout(db) is None

@pytest.mark.asyncio
async def test_expired_lease_reclaimed_and_stale_result_fenced(db):
    first, args = await setup(db)
    attempt = await db.get(Attempt, first.attempt_id)
    attempt.status = AttemptStatus.EXPIRED
    await db.commit()
    old = await claim_timeout(db)
    attempt.grading_lease_until = datetime.now(timezone.utc)-timedelta(seconds=1)
    await db.commit()
    new = await claim_timeout(db)
    assert new[1] != old[1]
    class NoCall(MockLLMProvider):
        async def grade_answers(self, **kwargs): raise AssertionError("empty answers must not call model")
    await grade_attempt(db, NoCall(), old[0], lease_token=old[1])
    assert attempt.status == AttemptStatus.GRADING
    await grade_attempt(db, NoCall(), new[0], lease_token=new[1])
    assert attempt.status == AttemptStatus.FINISHED and attempt.auto_score == 0


@pytest.mark.asyncio
async def test_grading_failure_keeps_answers_for_teacher_retry(db):
    first, args = await setup(db)
    await save_draft(db, **args, question_index=1, student_answer="only first question answered", revision=1)
    attempt = await db.get(Attempt, first.attempt_id)
    attempt.status = AttemptStatus.EXPIRED
    await db.commit()
    identity = await claim_timeout(db)
    class Failing(MockLLMProvider):
        async def grade_answers(self, **kwargs): raise RuntimeError("offline failure")
    with pytest.raises(RuntimeError):
        await grade_attempt(db, Failing(), identity[0], lease_token=identity[1])
    assert attempt.status == AttemptStatus.GRADING_ERROR
    assert attempt.grading_token is None and await claim_timeout(db) is None
    await grade_attempt(db, MockLLMProvider(), identity[0])
    assert attempt.status == AttemptStatus.FINISHED


def test_migration_preserves_legacy_attempts_and_is_repeatable(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    path = Path(__file__).parents[1] / "alembic/versions/0005_timeout_submission.py"
    spec = importlib.util.spec_from_file_location("timeout_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.sqlite"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE attempts (id INTEGER PRIMARY KEY, status TEXT)"))
        conn.execute(text("INSERT INTO attempts VALUES (1, 'EXPIRED')"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
        assert conn.execute(text("SELECT status, timed_out, draft_revision FROM attempts")).one() == ("EXPIRED", 0, 0)
    engine.dispose()


@pytest.mark.asyncio
async def test_late_grade_cannot_overwrite_new_owner(db):
    from sqlalchemy import update
    first, args = await setup(db)
    await save_draft(db, **args, question_index=1, student_answer="saved answer", revision=1)
    attempt = await db.get(Attempt, first.attempt_id)
    attempt.status = AttemptStatus.EXPIRED
    await db.commit()
    identity = await claim_timeout(db)
    class LosingLease(MockLLMProvider):
        async def grade_answers(self, **kwargs):
            await db.execute(update(Attempt).where(Attempt.id == identity[0]).values(grading_token="new-owner"))
            await db.commit()
            return await super().grade_answers(**kwargs)
    await grade_attempt(db, LosingLease(), identity[0], lease_token=identity[1])
    assert attempt.status == AttemptStatus.GRADING and attempt.auto_score is None
    assert attempt.grading_token == "new-owner"
