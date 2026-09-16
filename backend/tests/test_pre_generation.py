import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_attempt_flow import db, seed_participant
from test_import_integration import FakeRepository, FakeArchiveClient, NOW
from app.config import Settings
from app.models import Attempt, GenerationControl, GenerationJob, GenerationRun, QuizStatus, Question, SubmissionSnapshot
from app.services import generation_service as queue
from app.services.import_service import ImportService
from app.services.llm_provider import MockLLMProvider
from app.services.quiz_service import persist_quiz, start_attempt, reset_attempt
from app.schemas.api import QuizCreateRequest
from app.time_utils import ensure_utc


async def prepare_seed(db):
    quiz, participant = await seed_participant(db)
    quiz.pre_generate = True
    quiz.status = QuizStatus.DRAFT
    db.add(GenerationControl(id=1))
    snapshots = (await db.execute(select(SubmissionSnapshot))).scalars().all()
    await queue.enqueue(db, snapshots)
    await db.commit()
    return quiz, participant


async def complete_one(db, settings=None):
    settings = settings or Settings()
    identity = await queue.claim(db, settings, "mock")
    assert identity
    result, raw = await MockLLMProvider().generate_questions()
    assert await queue.finish(db, settings, identity, result=result, raw=raw)
    return identity


class NoGeneration(MockLLMProvider):
    async def generate_questions(self, **kwargs):
        raise AssertionError("Student start must not call LLM")


@pytest.mark.asyncio
async def test_create_atomically_enqueues_only_eligible_submissions(db):
    bundle = await ImportService(Settings(), FakeRepository(), FakeArchiveClient()).build_bundle(7, NOW - timedelta(hours=12))
    quiz, _ = await persist_quiz(db, bundle, QuizCreateRequest(contest_id=7))
    assert quiz.status == QuizStatus.DRAFT and quiz.pre_generate
    p = await queue.progress(db, quiz.id)
    assert p == dict(total=2, completed=0, queued=2, running=0, failed=0, cancelled=0, students_total=1, students_ready=0, ready=False)
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 0


@pytest.mark.asyncio
async def test_partial_progress_open_gate_and_no_llm_on_student_start(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    assert (await queue.progress(db, quiz.id))["completed"] == 1
    with pytest.raises(HTTPException) as denied:
        await queue.open_quiz(db, quiz.id)
    assert denied.value.status_code == 409
    await complete_one(db)
    assert (await queue.progress(db, quiz.id))["students_ready"] == 1
    before = datetime.now(timezone.utc)
    await queue.open_quiz(db, quiz.id)
    original_end = quiz.end_time
    assert ensure_utc(quiz.start_time) >= before
    assert quiz.end_time - quiz.start_time == timedelta(minutes=30)
    await queue.open_quiz(db, quiz.id)
    assert ensure_utc(quiz.end_time) == ensure_utc(original_end)
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 0
    first = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="a")
    assert first.question_count == 4 and first.question_index == 1
    assert "reference_answer" not in first.model_dump()
    attempt = await db.get(Attempt, first.attempt_id)
    assert attempt.deadline_at - attempt.started_at == timedelta(minutes=12)
    again = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="a")
    assert again.attempt_id == first.attempt_id and ensure_utc(again.deadline_at) == ensure_utc(first.deadline_at)
    assert await db.scalar(select(func.count()).select_from(Question)) == 4
    with pytest.raises(HTTPException):
        await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
            student_number=participant.student_number, session_id="b")


@pytest.mark.asyncio
async def test_global_cap_and_stale_lease_fences_late_response(db):
    quiz, _ = await prepare_seed(db)
    settings = Settings(generation_global_concurrency=1)
    first = await queue.claim(db, settings, "mock")
    assert await queue.claim(db, settings, "mock") is None
    job = await db.get(GenerationJob, first[0])
    job.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    second = await queue.claim(db, settings, "mock")
    assert second and second[1] != first[1]
    result, raw = await MockLLMProvider().generate_questions()
    assert not await queue.finish(db, settings, first, result=result, raw=raw)
    assert (await db.get(GenerationRun, first[1])).state == "interrupted"
    assert await queue.finish(db, settings, second, result=result, raw=raw)
    assert (await queue.progress(db, quiz.id))["completed"] == 1


@pytest.mark.asyncio
async def test_retry_only_failed_and_preserve_run_history(db):
    quiz, _ = await prepare_seed(db)
    settings = Settings(generation_max_attempts=1)
    await complete_one(db, settings)
    failed = await queue.claim(db, settings, "mock")
    await queue.finish(db, settings, failed, error="synthetic failure", raw="invalid response")
    assert (await queue.progress(db, quiz.id))["failed"] == 1
    assert await queue.retry_failed(db, quiz.id) == 1
    assert await queue.retry_failed(db, quiz.id) == 0
    assert (await queue.progress(db, quiz.id))["completed"] == 1
    await complete_one(db, settings)
    assert (await queue.progress(db, quiz.id))["ready"]
    assert (await db.get(GenerationRun, failed[1])).raw_response == "invalid response"
    assert await db.scalar(select(func.count()).select_from(GenerationRun)) == 3


@pytest.mark.asyncio
async def test_reset_prepares_new_round_and_is_idempotent(db):
    quiz, participant = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    await queue.open_quiz(db, quiz.id)
    first = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="a")
    await reset_attempt(db, first.attempt_id)
    await reset_attempt(db, first.attempt_id)
    assert await db.scalar(select(func.count()).select_from(GenerationJob)) == 4
    assert await db.scalar(select(func.count()).select_from(Question)) == 4
    assert (await queue.progress(db, quiz.id))["completed"] == 0
    with pytest.raises(HTTPException):
        await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
            student_number=participant.student_number, session_id="a")
    await complete_one(db)
    await complete_one(db)
    second = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=participant.student_number, session_id="a")
    assert first.attempt_id != second.attempt_id
    quiz.end_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    with pytest.raises(HTTPException):
        await reset_attempt(db, second.attempt_id)
    assert await db.scalar(select(func.count()).select_from(GenerationJob)) == 4


@pytest.mark.asyncio
async def test_background_runner_persists_without_any_student_request(db):
    quiz, _ = await prepare_seed(db)
    quiz_id = quiz.id
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    identity = await queue.claim(db, Settings(), "mock")
    await queue.run_claim(factory, Settings(), MockLLMProvider(), identity)
    db.expire_all()
    assert (await queue.progress(db, quiz_id))["completed"] == 1
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 0
    job = await db.get(GenerationJob, identity[0])
    assert job.prompt_version == "question_generator_v12"
    assert job.result_json["questions"][1]["second_kind"] in ("trace", "boundary", "modification")


@pytest.mark.asyncio
async def test_kind_allocation_balanced_across_sessions_and_retries(db):
    from collections import Counter
    from app.models import QuizParticipant, QuizParticipantStatus
    from app.services.question_policy import allocated_kind
    quiz, _ = await prepare_seed(db)
    originals = (await db.execute(select(SubmissionSnapshot).where(SubmissionSnapshot.quiz_id == quiz.id))).scalars().all()
    for student in ("synthetic-2", "synthetic-3"):
        participant = QuizParticipant(quiz_id=quiz.id, student_number=student,
            eligible_problem_count=2, status=QuizParticipantStatus.READY)
        db.add(participant)
        await db.flush()
        for original in originals:
            db.add(SubmissionSnapshot(quiz_id=quiz.id, participant_id=participant.id,
                problem_snapshot_id=original.problem_snapshot_id,
                uoj_submission_id=original.uoj_submission_id, uoj_problem_id=original.uoj_problem_id,
                source_code=original.source_code, language=original.language,
                uoj_score=original.uoj_score, uoj_submit_time=original.uoj_submit_time))
    await db.commit()
    snapshots = (await db.execute(select(SubmissionSnapshot).where(SubmissionSnapshot.quiz_id == quiz.id))).scalars().all()
    before = {s.id: await allocated_kind(db, s) for s in snapshots}
    assert Counter(before.values()) == {"trace": 2, "boundary": 2, "modification": 2}
    identity = await queue.claim(db, Settings(), "mock")
    await queue.finish(db, Settings(), identity, error="synthetic failure")
    await queue.enqueue(db, snapshots, round_no=2)
    await db.commit()
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as reopened:
        loaded = (await reopened.execute(select(SubmissionSnapshot).where(SubmissionSnapshot.quiz_id == quiz.id))).scalars().all()
        assert {s.id: await allocated_kind(reopened, s) for s in reversed(loaded)} == before


@pytest.mark.asyncio
async def test_generation_timeout_becomes_visible_failure(db):
    quiz, _ = await prepare_seed(db)
    quiz_id = quiz.id
    class Slow(MockLLMProvider):
        async def generate_questions(self, **kwargs):
            await asyncio.sleep(60)
    settings = Settings(generation_task_timeout_seconds=.01, generation_max_attempts=1)
    identity = await queue.claim(db, settings, "mock")
    await queue.run_claim(async_sessionmaker(db.bind, expire_on_commit=False), settings, Slow(), identity)
    db.expire_all()
    assert (await queue.progress(db, quiz_id))["failed"] == 1


@pytest.mark.asyncio
async def test_api_teacher_auth_and_student_draft_block(db):
    from app.main import app
    from app.database import get_db
    from app.security import hash_secret, create_token
    quiz, participant = await prepare_seed(db)
    quiz.quiz_code_hash = hash_secret("TEST1234")
    await db.commit()
    async def get_test_db():
        yield db
    app.dependency_overrides[get_db] = get_test_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post(f"/api/admin/quizzes/{quiz.id}/open")).status_code == 401
            denied = await client.post(f"/api/quiz/{quiz.id}/login", json={"student_number":participant.student_number,"quiz_code":"TEST1234"})
            assert denied.status_code == 403 and "尚未开放" in denied.json()["detail"]
            client.cookies.set("admin_session", create_token(str(uuid.uuid4()), "admin", username="teacher"))
            listing = await client.get("/api/admin/quizzes")
            assert listing.status_code == 200
            assert listing.json()[0]["preparation"]["total"] == 2
            assert "result_json" not in listing.text and "reference_answer" not in listing.text
            assert (await client.post(f"/api/admin/quizzes/{quiz.id}/open")).status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_heartbeat_prevents_reclaim_during_slow_reasoning(db):
    quiz, _ = await prepare_seed(db)
    settings = Settings(generation_global_concurrency=1, generation_lease_seconds=.12)
    identity = await queue.claim(db, settings, "mock")
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    task = asyncio.create_task(queue.heartbeat(factory, settings, identity))
    try:
        await asyncio.sleep(.28)
        async with factory() as observer:
            assert await queue.claim(observer, settings, "mock") is None
            job = await observer.get(GenerationJob, identity[0])
            assert ensure_utc(job.lease_until) > datetime.now(timezone.utc)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_file_database_recovers_after_engine_restart():
    from pathlib import Path
    tmp_path = Path(__file__).resolve().parents[2] / "artifacts" / "preparation-tests"
    tmp_path.mkdir(parents=True, exist_ok=True)
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.models.base import Base
    url = "sqlite+aiosqlite:///" + (tmp_path / f"recovery-{uuid.uuid4()}.db").as_posix()
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings()
    async with factory() as session:
        quiz, _ = await prepare_seed(session)
        quiz_id = quiz.id
        await complete_one(session)
        lost = await queue.claim(session, settings, "mock")
        job = await session.get(GenerationJob, lost[0])
        job.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
    await engine.dispose()
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            recovered = await queue.claim(session, settings, "mock")
            assert recovered[0] == lost[0] and recovered[1] != lost[1]
        await queue.run_claim(factory, settings, MockLLMProvider(), recovered)
        async with factory() as session:
            assert (await queue.progress(session, quiz_id))["ready"]
            assert await queue.claim(session, settings, "mock") is None
            assert await session.scalar(select(func.count()).select_from(Attempt)) == 0
    finally:
        await engine.dispose()


def test_preparation_progress_for_65_students_three_problems():
    from types import SimpleNamespace
    jobs = [SimpleNamespace(participant_id=student, round_no=1,
        state="succeeded" if student < 28 else "queued") for student in range(65) for _ in range(3)]
    result = queue.summarize(jobs)
    assert result["total"] == 195 and result["completed"] == 84
    assert result["students_ready"] == 28 and result["students_total"] == 65
    jobs.extend(SimpleNamespace(participant_id=0, round_no=2, state="queued") for _ in range(3))
    result = queue.summarize(jobs)
    assert result["total"] == 195 and result["completed"] == 81 and result["students_ready"] == 27


def test_additive_migration_preserves_old_quizzes_and_initializes_queue():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    spec = importlib.util.spec_from_file_location("preparation_migration", Path(__file__).parents[1] / "alembic/versions/0003_pre_generation.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE quizzes (id CHAR(32) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO quizzes(id) VALUES ('old-quiz')"))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            assert connection.execute(text("SELECT id, pre_generate FROM quizzes")).one() == ("old-quiz", 0)
            assert connection.scalar(text("SELECT id FROM generation_control")) == 1
            assert connection.scalar(text("SELECT COUNT(*) FROM generation_jobs")) == 0
    finally:
        engine.dispose()


async def add_unprepared_student(db, quiz):
    from app.models import QuizParticipant, QuizParticipantStatus
    participant = QuizParticipant(quiz_id=quiz.id, student_number="231250002",
        eligible_problem_count=2, status=QuizParticipantStatus.READY)
    db.add(participant)
    await db.flush()
    originals = (await db.execute(select(SubmissionSnapshot).where(SubmissionSnapshot.quiz_id == quiz.id))).scalars().all()
    snapshots = [SubmissionSnapshot(quiz_id=quiz.id, participant_id=participant.id,
        problem_snapshot_id=item.problem_snapshot_id, uoj_submission_id=900 + index,
        uoj_problem_id=item.uoj_problem_id, source_code="int main() {}", language="C++",
        uoj_score=100, uoj_submit_time=item.uoj_submit_time) for index, item in enumerate(originals)]
    db.add_all(snapshots)
    await db.flush()
    await queue.enqueue(db, snapshots)
    await db.commit()
    return participant


@pytest.mark.asyncio
async def test_stop_is_durable_idempotent_and_fences_late_results(db):
    quiz, _ = await prepare_seed(db)
    finished = await complete_one(db)
    running = await queue.claim(db, Settings(), "mock")
    assert await queue.stop_preparation(db, quiz.id) == 1
    assert await queue.stop_preparation(db, quiz.id) == 0
    result, raw = await MockLLMProvider().generate_questions()
    assert not await queue.finish(db, Settings(), running, result=result, raw=raw)
    assert (await db.get(GenerationJob, finished[0])).state == "succeeded"
    assert (await db.get(GenerationRun, running[1])).state == "cancelled"
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async with factory() as reopened:
        assert await queue.claim(reopened, Settings(), "mock") is None
        assert (await queue.progress(reopened, quiz.id))["cancelled"] == 1
    assert await queue.retry_failed(db, quiz.id) == 1
    await complete_one(db)
    assert (await queue.progress(db, quiz.id))["ready"]


@pytest.mark.asyncio
async def test_stop_cancels_active_provider_and_prevents_unstarted_call(db):
    quiz, _ = await prepare_seed(db)
    quiz_id = quiz.id
    started, cancelled = asyncio.Event(), asyncio.Event()
    class Slow(MockLLMProvider):
        async def generate_questions(self, **kwargs):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
    settings = Settings(generation_lease_seconds=.12)
    identity = await queue.claim(db, settings, "mock")
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    task = asyncio.create_task(queue.run_claim(factory, settings, Slow(), identity))
    await asyncio.wait_for(started.wait(), 2)
    assert await queue.stop_preparation(db, quiz_id) == 2
    await asyncio.wait_for(task, 2)
    assert cancelled.is_set()
    # A claim stopped before its runner starts must never send a model request.
    class CountCalls(MockLLMProvider):
        calls = 0
        async def generate_questions(self, **kwargs):
            self.calls += 1
            return await super().generate_questions(**kwargs)
    provider = CountCalls()
    await queue.run_claim(factory, settings, provider, identity)
    assert provider.calls == 0
    db.expire_all()
    assert (await queue.progress(db, quiz_id))["cancelled"] == 2


@pytest.mark.asyncio
async def test_partial_publish_requires_confirmation_and_complete_student_set(db):
    quiz, first = await prepare_seed(db)
    await complete_one(db)
    await queue.stop_preparation(db, quiz.id)
    with pytest.raises(HTTPException) as denied:
        await queue.open_quiz(db, quiz.id, confirm_partial=True)
    assert denied.value.status_code == 409
    await queue.retry_failed(db, quiz.id)
    await complete_one(db)
    second = await add_unprepared_student(db, quiz)
    with pytest.raises(HTTPException):
        await queue.open_quiz(db, quiz.id, confirm_partial=True)  # Must stop first.
    failure = await queue.claim(db, Settings(), "mock")
    await queue.finish(db, Settings(generation_max_attempts=1), failure, error="synthetic failure")
    running = await queue.claim(db, Settings(), "mock")
    with pytest.raises(HTTPException):
        await queue.open_quiz(db, quiz.id)
    await queue.open_quiz(db, quiz.id, confirm_partial=True)
    assert (await db.get(GenerationJob, running[0])).state == "cancelled"
    original_end = quiz.end_time
    await queue.open_quiz(db, quiz.id, confirm_partial=True)
    assert ensure_utc(quiz.end_time) == ensure_utc(original_end)
    response = await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
        student_number=first.student_number, session_id="ready")
    assert response.question_count == 4
    with pytest.raises(HTTPException):
        await start_attempt(db, Settings(), NoGeneration(), quiz_id=quiz.id,
            student_number=second.student_number, session_id="not-ready")
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 1


@pytest.mark.asyncio
async def test_partial_publish_api_and_student_login_gate(db):
    from app.main import app
    from app.database import get_db
    from app.security import hash_secret, create_token
    quiz, first = await prepare_seed(db)
    await complete_one(db)
    await complete_one(db)
    second = await add_unprepared_student(db, quiz)
    quiz.quiz_code_hash = hash_secret("TEST1234")
    await db.commit()
    async def get_test_db():
        yield db
    app.dependency_overrides[get_db] = get_test_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/admin/quizzes/{quiz.id}"
            assert (await client.post(path + "/stop-preparation")).status_code == 401
            client.cookies.set("admin_session", create_token(str(uuid.uuid4()), "admin", username="teacher"))
            assert (await client.post(path + "/stop-preparation")).json() == {"cancelled": 2}
            assert (await client.post(path + "/open")).status_code == 409
            assert (await client.post(path + "/open", json={"confirm_partial": True})).status_code == 200
            listing = (await client.get("/api/admin/quizzes")).json()[0]["preparation"]
            assert listing["cancelled"] == 2 and listing["students_ready"] == 1
            for student, expected in [(second, 409), (first, 200)]:
                response = await client.post(f"/api/quiz/{quiz.id}/login", json={"student_number":student.student_number,"quiz_code":"TEST1234"})
                assert response.status_code == expected, response.text
    finally:
        app.dependency_overrides.clear()
