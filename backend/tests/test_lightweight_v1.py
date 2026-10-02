import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_attempt_flow import db
from test_import_integration import FakeRepository, NOW, make_submission
from app.config import Settings, get_settings
from app.integrations.uoj.schemas import UOJProblem, UOJSubmissionRequirement
from app.models import (Answer, AnswerDraft, Appeal, Attempt, AttemptStatus, GenerationControl, GenerationJob,
    Question, QuizProblemSnapshot, ReviewIssue, ScoreAudit)
from app.api.admin import delete_quiz, request_quality
from app.schemas.api import LightweightDraftRequest, LightweightSubmitRequest, QuizCreateRequest
from app.services import generation_service
from app.services.quality_audit import claim as claim_quality
from app.services.grading_queue import claim_grading
from app.services.grading_service import grade_attempt
from app.services.import_service import ImportBundle, ImportedSubmission
from app.services.lightweight_submission import save_draft, submit
from app.services.llm_provider import MockLLMProvider
from app.services.publication import (publish, publish_blockers, score_question, student_result,
    request_appeal, resolve_appeal)
from app.services.quiz_service import persist_quiz, start_attempt
from app.time_utils import ensure_utc


@pytest.fixture(autouse=True)
def enable_v1_for_synthetic_tests(monkeypatch):
    monkeypatch.setenv("LIGHTWEIGHT_CREATION_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def bundle():
    contest = FakeRepository()
    from app.integrations.uoj.schemas import UOJContest
    meta = UOJContest(contest_id=7, name="Synthetic", start_time=NOW - timedelta(days=2),
        end_time=NOW - timedelta(days=1), status="finished")
    problems = [UOJProblem(problem_id=i, title=f"P{i}", statement="synthetic statement",
        submission_requirements=[UOJSubmissionRequirement(name="answer", type="source code",
            file_name="answer.code")]) for i in (11, 12, 13)]
    selected = {}
    for i in (11, 12, 13):
        selected[("231250001", i)] = ImportedSubmission(
            submission=make_submission(i, "231250001", i, 100, i),
            source_code=f"int main() {{ return {i}; }}")
    for i in (11, 13):
        selected[("231250002", i)] = ImportedSubmission(
            submission=make_submission(100 + i, "231250002", i, 100, i),
            source_code=f"int main() {{ return {i}; }}")
    selected[("231250003", 13)] = ImportedSubmission(
        submission=make_submission(313, "231250003", 13, 100, 13),
        source_code="int main() { return 13; }")
    return ImportBundle(contest=meta, cutoff=NOW - timedelta(days=1),
        problems=problems, students=["231250001", "231250002", "231250003"], selected=selected, issues=[])


async def prepared_quiz(db, request=None):
    quiz, _ = await persist_quiz(db, bundle(), request or QuizCreateRequest(contest_id=7))
    db.add(GenerationControl(id=1))
    await db.commit()
    provider = MockLLMProvider()
    while True:
        identity = await generation_service.claim(db, Settings(), "mock")
        if identity is None:
            break
        job = await db.get(GenerationJob, identity[0])
        result, raw = await provider.generate_lightweight(second_question_kind=job.second_kind)
        assert await generation_service.finish(db, Settings(), identity, result=result, raw=raw)
    await generation_service.open_quiz(db, quiz.id)
    return quiz


@pytest.mark.asyncio
async def test_creation_freezes_largest_id_as_one_question_and_missing_problem_time(db):
    quiz = await prepared_quiz(db)
    snapshots = (await db.scalars(select(QuizProblemSnapshot).where(
        QuizProblemSnapshot.quiz_id == quiz.id).order_by(QuizProblemSnapshot.display_order))).all()
    assert [(p.uoj_problem_id, p.include_choice) for p in snapshots] == [
        (11, True), (12, True), (13, False)]
    first = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250001", session_id="first")
    assert first.assessment_version == "lightweight_v1"
    assert first.question_count == 5 and first.duration_minutes == 20
    assert len(first.questions) == 5
    assert all("correct_choice_id" not in q and "reference_answer" not in q and "core_idea" not in q
        for q in first.questions)
    second = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250002", session_id="second")
    assert second.question_count == 3 and second.duration_minutes == 12
    assert [q["problem_id"] for q in second.questions] == [11, 11, 13]
    third = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250003", session_id="third")
    assert third.question_count == 1 and third.duration_minutes == 4
    assert third.questions[0]["problem_id"] == 13


@pytest.mark.asyncio
async def test_fixed_duration_and_invalid_choice_configuration(db):
    with pytest.raises(ValueError):
        await persist_quiz(db, bundle(), QuizCreateRequest(contest_id=7,
            choice_problem_ids=[11, 11]))
    with pytest.raises(ValueError):
        await persist_quiz(db, bundle(), QuizCreateRequest(contest_id=7,
            choice_problem_ids=[99]))
    quiz = await prepared_quiz(db, QuizCreateRequest(contest_id=7,
        time_mode="fixed", duration_minutes=17))
    third = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250003", session_id="fixed")
    assert third.question_count == 1 and third.duration_minutes == 17


@pytest.mark.asyncio
async def test_quiz_average_uses_confirmed_raw_scores_not_percentages(db):
    from app.api.admin import list_quizzes

    quiz = await prepared_quiz(db)
    quiz_id = quiz.id
    attempts, answers = [], []
    for student, per_question in [("231250001", 2), ("231250002", 1), ("231250003", 0)]:
        current = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz_id,
            student_number=student, session_id=student)
        attempt = await db.get(Attempt, current.attempt_id)
        attempt.status = AttemptStatus.FINISHED
        attempt.auto_score = per_question * current.question_count
        attempts.append(attempt)
        student_answers = []
        for question in current.questions:
            answer = Answer(question_id=uuid.UUID(question["id"]), student_answer="synthetic",
                auto_score=per_question)
            db.add(answer)
            student_answers.append(answer)
        answers.append(student_answers)
    # Different actual maxima: 10/10, 4/6 after a per-question correction, 0/2.
    # The zero must count; averaging percentages would give a different result.
    answers[1][0].manual_score = 2

    async def average():
        await db.commit()
        db.expire_all()
        return next(row.average_score for row in await list_quizzes(db) if row.id == quiz_id)

    assert await average() == pytest.approx(14 / 3)
    attempts[1].review_required = True
    assert await average() == 5
    attempts[2].status = AttemptStatus.GRADING
    assert await average() == 10
    answers[0][0].auto_score = None
    assert await average() is None  # Missing grades must not turn into zero.
    attempts[0].manual_override_score = 7
    assert await average() == 7  # Existing whole-attempt overrides remain effective.
    db.add(Attempt(quiz_id=quiz_id, participant_id=attempts[0].participant_id,
        attempt_no=2, selected_submission_snapshot_id=attempts[0].selected_submission_snapshot_id,
        status=AttemptStatus.RESET, session_id="synthetic-new-round"))
    assert await average() is None  # Never average the superseded finished round.


@pytest.mark.asyncio
async def test_custom_minutes_are_frozen_and_progress_counts_generated_questions(db):
    from app.schemas.api import PreparationProgress
    quiz = await prepared_quiz(db, QuizCreateRequest(contest_id=7, minutes_per_question=7))
    jobs = (await db.scalars(select(GenerationJob).where(GenerationJob.quiz_id == quiz.id))).all()
    from app.models import QuizParticipant
    participant = await db.scalar(select(QuizParticipant).where(
        QuizParticipant.quiz_id == quiz.id, QuizParticipant.student_number == "231250002"))
    summary = PreparationProgress.model_validate(generation_service.summarize(
        [job for job in jobs if job.participant_id == participant.id]))
    assert summary.completed == 2 and summary.completed_questions == 3
    view = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250002", session_id="custom-time")
    assert view.question_count == 3 and view.duration_minutes == 21


@pytest.mark.parametrize("minutes", [0, -1, 181, 2.5])
def test_custom_minutes_reject_invalid_values(minutes):
    with pytest.raises(ValueError):
        QuizCreateRequest(contest_id=7, minutes_per_question=minutes)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["question_generator_lightweight_v1", "question_generator_lightweight_v2"])
async def test_teacher_prepared_details_support_all_lightweight_prompt_versions(db, version):
    import httpx
    from sqlalchemy import func
    from app.database import get_db
    from app.main import app
    from app.security import create_token

    quiz = await prepared_quiz(db)
    jobs = (await db.scalars(select(GenerationJob).where(GenerationJob.quiz_id == quiz.id))).all()
    before = {job.id: job.result_json for job in jobs}
    for job in jobs:
        job.prompt_version = version
    await db.commit()

    async def test_db():
        yield db

    app.dependency_overrides[get_db] = test_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/admin/quizzes/{quiz.id}/students/231250002/prepared-questions"
            assert (await client.get(path)).status_code == 401
            client.cookies.set("admin_session", create_token(str(uuid.uuid4()), "admin", username="teacher"))
            listing = await client.get("/api/admin/quizzes")
            assert listing.status_code == 200
            preparation = next(row for row in listing.json() if row["id"] == str(quiz.id))["preparation"]
            assert preparation["completed"] == 6
            assert preparation["completed_questions"] == 9  # 5 + 3 + 1 across the three students.
            for student, count in [("231250001", 5), ("231250002", 3), ("231250003", 1)]:
                response = await client.get(path.replace("231250002", student))
                assert response.status_code == 200
                data = response.json()
                assert data["can_edit"] is True
                assert len(data["questions"]) == count
                assert [q["index"] for q in data["questions"]] == list(range(1, count + 1))
                for question in data["questions"]:
                    assert question["reference_answer"] and question["revision"]
                    if question["response_format"] == "single_choice":
                        assert len(question["choices"]) == 4
                        assert question["correct_choice_id"] in {c["id"] for c in question["choices"]}
                    else:
                        assert question["core_idea"]
            assert await db.scalar(select(func.count()).select_from(Attempt)) == 0
            for job in jobs:
                await db.refresh(job)
                assert job.result_json == before[job.id]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["question_generator_lightweight_v1", "question_generator_lightweight_v2"])
async def test_new_protocol_never_enters_legacy_quality_audit(db, monkeypatch, version):
    monkeypatch.setenv("QUALITY_AUDIT_ENABLED", "true")
    get_settings.cache_clear()
    quiz = await prepared_quiz(db)
    jobs = (await db.scalars(select(GenerationJob).where(GenerationJob.quiz_id == quiz.id))).all()
    assert jobs and all(job.quality_state != "queued" for job in jobs)
    jobs[0].prompt_version = version
    jobs[0].quality_state = "queued"  # Even a stale/manual state must not be claimed.
    await db.commit()
    assert await claim_quality(db) is None
    with pytest.raises(HTTPException) as denied:
        await request_quality(jobs[0].id, db)
    assert denied.value.status_code == 409


@pytest.mark.asyncio
async def test_draft_submit_review_publish_and_appeal(db):
    quiz = await prepared_quiz(db)
    view = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250001", session_id="writer")
    first = view.questions[0]
    args = dict(attempt_id=view.attempt_id, quiz_id=quiz.id, student_number="231250001", session_id="writer")
    saved = await save_draft(db, **args, payload=LightweightDraftRequest(
        question_id=uuid.UUID(first["id"]), expected_revision=0, answer_text="我认为这里在更新当前状态",
        dispute=True))
    assert saved["draft"]["revision"] == 1
    with pytest.raises(HTTPException) as stale:
        await save_draft(db, **args, payload=LightweightDraftRequest(
            question_id=uuid.UUID(first["id"]), expected_revision=0, answer_text="旧标签"))
    assert stale.value.status_code == 409
    items = []
    for q in view.questions:
        items.append(LightweightDraftRequest(question_id=uuid.UUID(q["id"]),
            expected_revision=1 if q["id"] == first["id"] else 0,
            answer_text="说明状态更新的局部作用" if q["response_format"] == "short_answer" else "",
            choice_id="B" if q["response_format"] == "single_choice" else None,
            dispute=q["id"] == first["id"]))
    payload = LightweightSubmitRequest(attempt_id=view.attempt_id,
        idempotency_key="synthetic-submit-1", drafts=items)
    result = await submit(db, quiz_id=quiz.id, student_number="231250001",
        session_id="writer", payload=payload)
    assert result["source"] == "manual"
    assert (await submit(db, quiz_id=quiz.id, student_number="231250001",
        session_id="writer", payload=payload))["submitted"]
    attempt = await db.get(Attempt, view.attempt_id)
    assert attempt.submitted_at and attempt.status == AttemptStatus.GRADING

    class Counting(MockLLMProvider):
        calls = 0
        async def grade_lightweight(self, **kwargs):
            self.calls += 1
            assert all(q["student_answer"] for q in kwargs["question_payload"])
            return await super().grade_lightweight(**kwargs)
    provider = Counting()
    await grade_attempt(db, provider, attempt.id)
    assert provider.calls == 3
    attempt = await db.get(Attempt, attempt.id)
    assert attempt.status == AttemptStatus.FINISHED and attempt.review_required
    assert len((await db.scalars(select(ReviewIssue))).all()) == 1
    open_readiness = await publish_blockers(db, quiz.id)
    assert open_readiness["published"] is False
    assert "进入窗口仍开放" in open_readiness["blockers"]
    quiz.end_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    blocked_readiness = await publish_blockers(db, quiz.id)
    assert "进入窗口仍开放" not in blocked_readiness["blockers"]
    assert any("待处理复核" in item for item in blocked_readiness["blockers"])
    assert blocked_readiness["counts"]["review_pending"] == 1
    hidden = await student_result(db, quiz.id, "231250001")
    assert hidden == {"published": False, "message": "成绩尚未公布"}
    with pytest.raises(HTTPException) as blocked:
        await publish(db, quiz.id, "teacher")
    assert blocked.value.status_code == 409
    question = await db.get(Question, uuid.UUID(first["id"]))
    await score_question(db, attempt.id, question.id, score=2, reason="确认理解",
        actor="teacher", expected_version=attempt.score_version)
    assert (await publish_blockers(db, quiz.id))["blockers"] == []
    published = await publish(db, quiz.id, "teacher")
    assert published["published"] and published["include_answers"]
    assert (await publish_blockers(db, quiz.id))["published"] is True
    result = await student_result(db, quiz.id, "231250001")
    assert result["score"] == 10 and result["max_score"] == 10
    assert all("correct_choice_id" in q and "reference_answer" in q for q in result["questions"])
    # Older already-published records disclose answers under the current policy.
    quiz.publish_answers = False
    await db.commit()
    disclosed = await student_result(db, quiz.id, "231250001")
    assert all("reference_answer" in q and "correct_choice_id" in q for q in disclosed["questions"])
    # A stale client cannot accidentally suppress answers when republishing.
    assert (await publish(db, quiz.id, "teacher", include_answers=False))["include_answers"]
    appeal = await request_appeal(db, quiz.id, "231250001", question.id,
        reason="请再看一下", request_key="appeal-synthetic-1")
    same = await request_appeal(db, quiz.id, "231250001", question.id,
        reason="请再看一下", request_key="appeal-synthetic-1")
    assert same["id"] == appeal["id"]
    with pytest.raises(HTTPException):
        await request_appeal(db, quiz.id, "231250001", question.id,
            reason="重复", request_key="appeal-synthetic-2")
    await resolve_appeal(db, uuid.UUID(appeal["id"]), actor="teacher",
        resolution="已核查，维持原分", expected_version=attempt.score_version, score=None)
    assert (await student_result(db, quiz.id, "231250001"))["questions"][0]["appeals"][0]["state"] == "resolved"
    assert (await delete_quiz(quiz.id, db))["deleted"]
    for model in (AnswerDraft, Appeal, ReviewIssue, ScoreAudit):
        assert not (await db.scalars(select(model))).all()


@pytest.mark.asyncio
async def test_timeout_collects_every_saved_draft_and_rejects_late_edit(db):
    quiz = await prepared_quiz(db)
    view = await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250001", session_id="writer")
    for q in view.questions:
        await save_draft(db, attempt_id=view.attempt_id, quiz_id=quiz.id,
            student_number="231250001", session_id="writer", payload=LightweightDraftRequest(
                question_id=uuid.UUID(q["id"]), expected_revision=0,
                answer_text=f"已保存的第{q['index']}题" if q["response_format"] == "short_answer" else "",
                choice_id="A" if q["response_format"] == "single_choice" else None))
    attempt = await db.get(Attempt, view.attempt_id)
    attempt.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    with pytest.raises(HTTPException) as late:
        await save_draft(db, attempt_id=view.attempt_id, quiz_id=quiz.id,
            student_number="231250001", session_id="writer", payload=LightweightDraftRequest(
                question_id=uuid.UUID(view.questions[0]["id"]), expected_revision=1, answer_text="迟到"))
    assert late.value.status_code == 410
    identity = await claim_grading(db)
    assert identity and identity[0] == attempt.id
    answers = (await db.scalars(select(Answer).join(Question).where(Question.attempt_id == attempt.id))).all()
    assert len(answers) == 5
    assert all(a.student_answer or a.choice_id for a in answers)
    assert ensure_utc(attempt.submitted_at) == ensure_utc(attempt.deadline_at)
