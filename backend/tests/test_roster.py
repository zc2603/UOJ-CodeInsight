import pytest
import httpx
from fastapi import HTTPException
from sqlalchemy import func, select

from test_attempt_flow import db
from test_import_integration import FakeRepository, FakeArchiveClient, NOW
from app.config import Settings
from app.models import Quiz, QuizParticipant, SubmissionSnapshot, GenerationJob
from app.schemas.api import QuizCreateRequest
from app.services.import_service import ImportService
from app.services.roster_service import select_roster
from app.services.quiz_service import persist_quiz, preview_from_bundle, start_attempt
from app.services.llm_provider import MockLLMProvider


class Repository(FakeRepository):
    async def get_contest(self, contest_id):
        contest = await super().get_contest(contest_id)
        return contest.model_copy(update={"end_time": NOW})

    async def get_contest_students(self, contest_id):
        return ["231250001", "231250002", "231250003"]


class Archive(FakeArchiveClient):
    async def read_source(self, submission, problem):
        return "int main() {}"


async def bundle():
    return await ImportService(Settings(), Repository(), Archive()).build_bundle(7)


@pytest.mark.asyncio
async def test_roster_exact_intersection_duplicates_and_preview():
    original = await bundle()
    selected, report = select_roster(original, "\ufeff231250002\r\n231250002，231250003;231259999")
    assert report.requested_count == 3 and report.duplicate_count == 1
    assert report.matched_students == ["231250002"]
    assert report.unavailable_students == ["231250003"]
    assert report.unknown_students == ["231259999"]
    assert selected.students == ["231250002"]
    assert len(selected.selected) == 1
    assert len(original.selected) == 3
    preview = preview_from_bundle(original, "231250002")
    assert preview.students_with_eligible_problem == 2
    assert preview.roster.selected_submission_snapshots == 1
    assert select_roster(original, None) == (original, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", " \n", "学号\n231250001", "- 231250001", "231250001\x00", "1" * 65537], ids=["empty", "whitespace", "heading", "bullet", "binary", "oversize"])
async def test_invalid_roster_never_falls_back_to_everyone(text):
    with pytest.raises(ValueError):
        select_roster(await bundle(), text)


@pytest.mark.asyncio
async def test_create_only_persists_and_queues_matched_students(db):
    quiz, _ = await persist_quiz(db, await bundle(), QuizCreateRequest(contest_id=7,
        roster_text="231250002 231250003 231259999"))
    names = (await db.execute(select(QuizParticipant.student_number).where(QuizParticipant.quiz_id == quiz.id))).scalars().all()
    assert names == ["231250002"]
    assert await db.scalar(select(func.count()).select_from(SubmissionSnapshot)) == 1
    assert await db.scalar(select(func.count()).select_from(GenerationJob)) == 1
    with pytest.raises(HTTPException):
        await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
            student_number="231250001", session_id="excluded")


@pytest.mark.asyncio
async def test_no_match_refuses_create_without_writing_quiz(db):
    imported = await bundle()
    assert preview_from_bundle(imported, "231259999").roster.matched_students == []
    with pytest.raises(ValueError, match="没有可参与"):
        await persist_quiz(db, imported, QuizCreateRequest(contest_id=7, roster_text="231259999"))
    assert await db.scalar(select(func.count()).select_from(Quiz)) == 0


@pytest.mark.asyncio
async def test_roster_api_rechecks_on_create_and_excludes_login(db):
    from app.main import app
    from app.database import get_db
    from app.api.dependencies import require_admin
    import app.api.admin as admin
    from unittest.mock import patch
    async def get_test_db():
        yield db
    app.dependency_overrides[get_db] = get_test_db
    from app.api.dependencies import AdminPrincipal
    from app.models import AdminUser
    teacher = AdminUser(username="roster-teacher", password_hash="synthetic")
    db.add(teacher)
    await db.commit()
    app.dependency_overrides[require_admin] = lambda: AdminPrincipal(teacher.id, teacher.username)
    try:
        with patch.object(admin, "_import_service", return_value=ImportService(Settings(), Repository(), Archive())):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                payload = {"contest_id": 7, "roster_text": "231250002,231250003"}
                preview = await client.post("/api/admin/quizzes/preview-contest", json=payload)
                assert preview.status_code == 200
                assert preview.json()["roster"]["matched_students"] == ["231250002"]
                created = await client.post("/api/admin/quizzes", json=payload)
                assert created.status_code == 200, created.text
                quiz = created.json()
                response = await client.post(f"/api/quiz/{quiz['id']}/login", json={
                    "student_number": "231250001", "quiz_code": quiz["quiz_code"]})
                assert response.status_code == 403
                empty = await client.post("/api/admin/quizzes", json={"contest_id":7,"roster_text":""})
                assert empty.status_code == 400
                assert await db.scalar(select(func.count()).select_from(Quiz)) == 1
    finally:
        app.dependency_overrides.clear()
