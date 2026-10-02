from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models import (
    AdminUser,
    Quiz,
    QuizParticipant,
    QuizParticipantStatus,
    QuizProblemSnapshot,
    QuizStatus,
    SubmissionSnapshot,
)
from app.models.base import Base
from app.security import hash_secret, verify_secret


@pytest.mark.asyncio
async def test_student_schema_never_leaks_future_questions_or_references(monkeypatch) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import asyncio
    from app.models import RuntimeConfiguration
    async def idle(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr("app.main.SessionLocal", factory)
    for name in ("maintain_attempts", "generation_worker", "grading_worker"):
        monkeypatch.setattr("app.main." + name, idle)
    async with factory() as db:
        now = datetime.now(timezone.utc)
        quiz = Quiz(
            name="Security",
            uoj_contest_id=1,
            quiz_code_hash=hash_secret("ABCDEFGH"),
            start_time=now - timedelta(minutes=1),
            end_time=now + timedelta(hours=1),
            duration_minutes=8,
            submission_cutoff=now - timedelta(days=1),
            status=QuizStatus.PUBLISHED,
            show_score_after_finish=True,
        )
        admin = AdminUser(username="teacher", password_hash=hash_secret("long-test-password"))
        db.add_all([quiz, admin, RuntimeConfiguration(id=1, revision=0)])
        await db.flush()
        problem = QuizProblemSnapshot(
            quiz_id=quiz.id, uoj_problem_id=1, title="P", statement="Statement"
        )
        participant = QuizParticipant(
            quiz_id=quiz.id,
            student_number="231250001",
            eligible_problem_count=1,
            status=QuizParticipantStatus.READY,
        )
        db.add_all([problem, participant])
        await db.flush()
        db.add(
            SubmissionSnapshot(
                quiz_id=quiz.id,
                participant_id=participant.id,
                problem_snapshot_id=problem.id,
                uoj_submission_id=1,
                uoj_problem_id=1,
                source_code="int main(){return 0;}",
                language="C++",
                uoj_score=100,
                uoj_submit_time=now - timedelta(days=1),
            )
        )
        await db.commit()
        quiz_id = quiz.id

        async def override_db():
            yield db

        app.dependency_overrides[get_db] = override_db
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    login = await client.post(
                        f"/api/quiz/{quiz_id}/login",
                        json={"student_number": "231250001", "quiz_code": "ABCDEFGH"},
                    )
                    assert login.status_code == 200
                    started = await client.post(f"/api/quiz/{quiz_id}/start")
                    assert started.status_code == 200
                    payload = started.json()
                    assert payload["question_index"] == 1
                    serialized = started.text
                    assert "reference_answer" not in serialized
                    assert "grading_points" not in serialized
                    assert "generator_raw_response" not in serialized
                    assert "问题 2" not in serialized

                    admin_login = await client.post(
                        "/api/admin/login",
                        json={"username": "teacher", "password": "long-test-password"},
                    )
                    assert admin_login.status_code == 200
                    regenerated = await client.post(
                        f"/api/admin/quizzes/{quiz_id}/regenerate-code"
                    )
                    assert regenerated.status_code == 200
                    new_code = regenerated.json()["quiz_code"]
                    assert len(new_code) == 8
                    await db.refresh(quiz)
                    assert verify_secret(quiz.quiz_code_hash, new_code)
                    assert not verify_secret(quiz.quiz_code_hash, "ABCDEFGH")
        finally:
            app.dependency_overrides.clear()
    await engine.dispose()
