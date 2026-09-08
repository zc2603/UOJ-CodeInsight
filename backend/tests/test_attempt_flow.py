from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select, update
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.models import (
    Attempt,
    AttemptStatus,
    Question,
    Quiz,
    QuizParticipant,
    QuizParticipantStatus,
    QuizProblemSnapshot,
    QuizStatus,
    SubmissionSnapshot,
)
from app.models.base import Base
from app.services.llm_provider import MockLLMProvider
from app.services.grading_service import grade_attempt
from app.services.question_service import submit_answer
from app.services.quiz_service import reset_attempt, start_attempt
from app.time_utils import ensure_utc
from app.services.attempt_maintenance import reconcile_attempts
from app.models import LLMCallLog


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def seed_participant(db):
    now = datetime.now(timezone.utc)
    quiz = Quiz(
        name="Flow",
        uoj_contest_id=1,
        quiz_code_hash="unused",
        start_time=now - timedelta(minutes=1),
        end_time=now + timedelta(hours=1),
        duration_minutes=8,
        submission_cutoff=now - timedelta(days=1),
        status=QuizStatus.PUBLISHED,
        show_score_after_finish=True,
    )
    db.add(quiz)
    await db.flush()
    problems = [
        QuizProblemSnapshot(
            quiz_id=quiz.id, uoj_problem_id=10 + index, title=f"P{index}", statement="Statement"
        )
        for index in range(2)
    ]
    participant = QuizParticipant(
        quiz_id=quiz.id,
        student_number="231250001",
        eligible_problem_count=2,
        status=QuizParticipantStatus.READY,
    )
    db.add_all([*problems, participant])
    await db.flush()
    for index in range(2):
        db.add(
            SubmissionSnapshot(
                quiz_id=quiz.id,
                participant_id=participant.id,
                problem_snapshot_id=problems[index].id,
                uoj_submission_id=100 + index,
                uoj_problem_id=10 + index,
                source_code=f"int main(){{return {index};}}",
                language="C++",
                uoj_score=100,
                uoj_submit_time=now - timedelta(days=1),
            )
        )
    await db.commit()
    return quiz, participant


@pytest.mark.asyncio
async def test_every_positive_problem_gets_two_questions_and_dynamic_timing(db) -> None:
    quiz, participant = await seed_participant(db)
    provider = MockLLMProvider()
    settings = Settings()
    quiz_id = quiz.id
    student_number = participant.student_number
    first = await start_attempt(
        db,
        settings,
        provider,
        quiz_id=quiz_id,
        student_number=student_number,
        session_id="device-a",
    )
    assert first.question_index == 1
    assert first.question_count == 4
    assert "reference_answer" not in first.model_dump()
    assert first.deadline_at is not None
    created_attempt = await db.get(Attempt, first.attempt_id)
    assert created_attempt is not None and created_attempt.started_at is not None
    assert created_attempt.deadline_at - created_attempt.started_at == timedelta(minutes=12)
    generated_questions = (
        await db.execute(
            select(Question).where(Question.attempt_id == first.attempt_id)
        )
    ).scalars().all()
    assert len({item.submission_snapshot_id for item in generated_questions}) == 2
    assert all(item.question_text_en for item in generated_questions)
    first_problem_id = first.problem_id

    second = await submit_answer(
        db,
        quiz_id=quiz_id,
        student_number=student_number,
        session_id="device-a",
        question_index=1,
        student_answer="这是第一题的足够长测试回答，用于说明代码整体流程。",
    )
    assert second.question_index == 2
    assert second.problem_id == first_problem_id
    third = await submit_answer(
        db,
        quiz_id=quiz_id,
        student_number=student_number,
        session_id="device-a",
        question_index=2,
        student_answer="这是第二题的足够长测试回答，用于追踪所有关键状态。",
    )
    assert third.question_index == 3
    assert third.problem_id != first_problem_id
    fourth = await submit_answer(
        db,
        quiz_id=quiz_id,
        student_number=student_number,
        session_id="device-a",
        question_index=3,
        student_answer="这是下一道正分题第一问的足够长测试回答，用于解释边界情况行为。",
    )
    assert fourth.question_index == 4
    submitted = await submit_answer(
        db,
        quiz_id=quiz_id,
        student_number=student_number,
        session_id="device-a",
        question_index=4,
        student_answer="这是第四题的足够长测试回答，用于解释第二份代码行为。",
    )
    assert isinstance(submitted, Attempt)
    assert submitted.status == AttemptStatus.GRADING
    assert submitted.auto_score is None

    finished = await grade_attempt(db, provider, submitted.id)
    assert finished.status == AttemptStatus.FINISHED
    assert finished.auto_score == 8

    await reset_attempt(db, finished.id)
    restarted = await start_attempt(
        db,
        settings,
        provider,
        quiz_id=quiz_id,
        student_number=student_number,
        session_id="device-b",
    )
    assert restarted.question_index == 1
    assert restarted.question_count == 4
    assert restarted.problem_id == first_problem_id


@pytest.mark.asyncio
async def test_second_device_is_rejected(db) -> None:
    quiz, participant = await seed_participant(db)
    await start_attempt(
        db,
        Settings(),
        MockLLMProvider(),
        quiz_id=quiz.id,
        student_number=participant.student_number,
        session_id="device-a",
    )
    with pytest.raises(HTTPException) as error:
        await start_attempt(
            db,
            Settings(),
            MockLLMProvider(),
            quiz_id=quiz.id,
            student_number=participant.student_number,
            session_id="device-b",
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_entry_deadline_does_not_cut_off_an_existing_attempt(db) -> None:
    quiz, participant = await seed_participant(db)
    provider = MockLLMProvider()
    started = await start_attempt(
        db,
        Settings(),
        provider,
        quiz_id=quiz.id,
        student_number=participant.student_number,
        session_id="device-a",
    )
    original_deadline = started.deadline_at

    quiz.end_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    resumed = await start_attempt(
        db,
        Settings(),
        provider,
        quiz_id=quiz.id,
        student_number=participant.student_number,
        session_id="device-a",
    )
    assert resumed.attempt_id == started.attempt_id
    assert ensure_utc(resumed.deadline_at) == ensure_utc(original_deadline)

    await reset_attempt(db, started.attempt_id)
    with pytest.raises(HTTPException, match="进入时间已结束") as error:
        await start_attempt(
            db,
            Settings(),
            provider,
            quiz_id=quiz.id,
            student_number=participant.student_number,
            session_id="device-b",
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_existing_quiz_with_only_zero_score_snapshots_is_rejected(db) -> None:
    quiz, participant = await seed_participant(db)
    await db.execute(
        update(SubmissionSnapshot)
        .where(SubmissionSnapshot.participant_id == participant.id)
        .values(uoj_score=0)
    )
    await db.commit()

    with pytest.raises(HTTPException, match="得分大于 0") as error:
        await start_attempt(
            db,
            Settings(),
            MockLLMProvider(),
            quiz_id=quiz.id,
            student_number=participant.student_number,
            session_id="device-a",
        )

    assert error.value.status_code == 409
    refreshed = (
        await db.execute(select(QuizParticipant).where(QuizParticipant.id == participant.id))
    ).scalar_one()
    assert refreshed.status == QuizParticipantStatus.NO_ELIGIBLE_SUBMISSION
    assert refreshed.eligible_problem_count == 0


@pytest.mark.asyncio
async def test_maintenance_expires_idle_and_resets_stale_preparation(db):
    quiz, participant = await seed_participant(db)
    response = await start_attempt(db, Settings(), MockLLMProvider(),
        quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    attempt = await db.get(Attempt, response.attempt_id)
    now = datetime.now(timezone.utc)
    attempt.deadline_at = now
    await db.commit()
    assert await reconcile_attempts(db, Settings(), now=now) == (1, 0)
    await db.refresh(attempt)
    assert attempt.status == AttemptStatus.EXPIRED
    assert len((await db.execute(select(Question))).scalars().all()) == 4
    attempt.status = AttemptStatus.PREPARING
    attempt.created_at = now - timedelta(seconds=1800)
    await db.commit()
    assert await reconcile_attempts(db, Settings(), now=now) == (0, 1)
    assert await reconcile_attempts(db, Settings(), now=now) == (0, 0)
    await db.refresh(attempt)
    assert attempt.status == AttemptStatus.RESET
    logs = (await db.execute(select(LLMCallLog).where(LLMCallLog.success == False))).scalars().all()
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_maintenance_preserves_live_and_grading_attempts(db):
    quiz, participant = await seed_participant(db)
    response = await start_attempt(db, Settings(), MockLLMProvider(),
        quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    attempt = await db.get(Attempt, response.attempt_id)
    for state in (AttemptStatus.IN_PROGRESS, AttemptStatus.PREPARING, AttemptStatus.GRADING, AttemptStatus.FINISHED):
        attempt.status = state
        await db.commit()
        assert await reconcile_attempts(db, Settings()) == (0, 0)


@pytest.mark.asyncio
async def test_late_generation_cannot_resurrect_reset(db):
    quiz, participant = await seed_participant(db)
    class ResetDuringGeneration(MockLLMProvider):
        async def generate_questions(self, **kwargs):
            # Simulate another transaction resetting the database row while the
            # original request still holds a stale PREPARING ORM instance.
            await db.execute(update(Attempt).values(status=AttemptStatus.RESET)
                .execution_options(synchronize_session=False))
            await db.commit()
            return await super().generate_questions(**kwargs)
    # One input avoids concurrent operations on this test's shared session.
    snapshots = (await db.execute(select(SubmissionSnapshot))).scalars().all()
    snapshots[1].uoj_score = 0
    await db.commit()
    with pytest.raises(HTTPException, match="出题已取消"):
        await start_attempt(db, Settings(), ResetDuringGeneration(),
            quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    assert not (await db.execute(select(Question))).scalars().all()
    assert (await db.execute(select(Attempt.status))).scalar_one() == AttemptStatus.RESET


@pytest.mark.asyncio
async def test_maintenance_runs_without_http_and_retries_database_failure(db):
    import asyncio
    from contextlib import asynccontextmanager, suppress
    from app.services.attempt_maintenance import maintain_attempts
    quiz, participant = await seed_participant(db)
    response = await start_attempt(db, Settings(), MockLLMProvider(),
        quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    attempt = await db.get(Attempt, response.attempt_id)
    attempt.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    passes = 0
    swept = asyncio.Event()
    @asynccontextmanager
    async def factory():
        nonlocal passes
        passes += 1
        if passes == 1:
            raise ConnectionError("simulated database outage")
        yield db
        swept.set()
    task = asyncio.create_task(maintain_attempts(factory,
        Settings(attempt_maintenance_interval_seconds=0.01)))
    try:
        await asyncio.wait_for(swept.wait(), timeout=2)
        await db.refresh(attempt)
        assert attempt.status == AttemptStatus.EXPIRED
        assert passes >= 2
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_preparation_timeout_cancels_provider_and_preserves_retry(db):
    import asyncio
    quiz, participant = await seed_participant(db)
    class SlowProvider(MockLLMProvider):
        async def generate_questions(self, **kwargs):
            await asyncio.sleep(5)
            return await super().generate_questions(**kwargs)
    with pytest.raises(HTTPException) as error:
        await start_attempt(db, Settings(attempt_preparing_timeout_seconds=0.02), SlowProvider(),
            quiz_id=quiz.id, student_number=participant.student_number, session_id="a")
    assert error.value.status_code == 503
    assert (await db.execute(select(Attempt.status))).scalar_one() == AttemptStatus.RESET


@pytest.mark.asyncio
async def test_rejected_whitespace_grade_retains_answers_and_raw_evidence(db):
    from app.services.llm_provider import WhitespaceGradingError
    quiz, participant = await seed_participant(db)
    quiz_id, student = quiz.id, participant.student_number
    started = await start_attempt(db, Settings(), MockLLMProvider(),
        quiz_id=quiz_id, student_number=student, session_id="a")
    for index in range(1, 5):
        await submit_answer(db, quiz_id=quiz_id, student_number=student,
            session_id="a", question_index=index, student_answer="1 2")
    class RejectingProvider(MockLLMProvider):
        async def grade_answers(self, **kwargs):
            raise WhitespaceGradingError('original rejected response')
    with pytest.raises(WhitespaceGradingError):
        await grade_attempt(db, RejectingProvider(), started.attempt_id)
    attempt = await db.get(Attempt, started.attempt_id)
    await db.refresh(attempt)
    assert attempt.status == AttemptStatus.GRADING_ERROR
    assert attempt.auto_score is None
    from app.models import Answer
    answers = (await db.execute(select(Answer))).scalars().all()
    assert len(answers) == 4
    assert all(a.student_answer == "1 2" and a.auto_score is None for a in answers)
    log = (await db.execute(select(LLMCallLog).where(LLMCallLog.success == False))).scalar_one()
    assert log.raw_response == 'original rejected response'
    assert log.prompt_version == 'grader_v7'


@pytest.mark.asyncio
async def test_overview_counts_unique_students_across_quizzes(db):
    from app.api.admin import overview
    assert await overview(db) == {"student_count": 0}
    await seed_participant(db)
    quiz, _ = await seed_participant(db)
    assert await overview(db) == {"student_count": 1}
    db.add(QuizParticipant(quiz_id=quiz.id, student_number="231250002",
        eligible_problem_count=0, status=QuizParticipantStatus.NO_ELIGIBLE_SUBMISSION))
    await db.commit()
    assert await overview(db) == {"student_count": 2}
