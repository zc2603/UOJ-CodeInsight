"""Gap tests for docs/acceptance-matrix.md (2026-10-02).

These cover branches that had an implementation but no assertion: A03 (>3 problems),
A04 (lightweight single-problem regeneration), A06 (illegal option edit -> 422),
A08 (clear a choice / resume from the server), A12 (regrade never moves the
completion time), A13 (result-purpose token cannot write), A14 (lightweight grader
protocol mismatch), A15 (blank/choice disputes and issue de-duplication),
A17 (per-question score requires explicit override clearance) and A27 (0010
migration is additive and re-runnable).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from test_attempt_flow import db
from test_import_integration import NOW, make_submission
from test_lightweight_v1 import prepared_quiz
from app.api.dependencies import require_student, require_student_result
from app.config import Settings, get_settings
from app.integrations.uoj.schemas import UOJContest, UOJProblem, UOJSubmissionRequirement
from app.models import (Attempt, AttemptStatus, GenerationJob, Question, QuizParticipant,
    QuizProblemSnapshot, ReviewIssue, SubmissionSnapshot)
from app.schemas.api import LightweightDraftRequest, LightweightSubmitRequest, QuizCreateRequest
from app.schemas.llm import ChoiceOption, LightweightGradingResult
from app.security import create_token
from app.services import generation_service, prepared_edit
from app.services.grading_service import grade_lightweight_attempt
from app.services.import_service import ImportBundle, ImportedSubmission
from app.services.lightweight_submission import save_draft, submit
from app.services.llm_provider import MockLLMProvider
from app.services.publication import score_question
from app.services.quiz_service import persist_quiz, start_attempt


@pytest.fixture(autouse=True)
def enable_v1_for_synthetic_tests(monkeypatch):
    monkeypatch.setenv("LIGHTWEIGHT_CREATION_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def four_problem_bundle():
    meta = UOJContest(contest_id=9, name="Synthetic4", start_time=NOW - timedelta(days=2),
        end_time=NOW - timedelta(days=1), status="finished")
    problems = [UOJProblem(problem_id=i, title=f"P{i}", statement="synthetic statement",
        submission_requirements=[UOJSubmissionRequirement(name="answer", type="source code",
            file_name="answer.code")]) for i in (21, 22, 23, 24)]
    selected = {("231250001", i): ImportedSubmission(
        submission=make_submission(i, "231250001", i, 100, i),
        source_code=f"int main() {{ return {i}; }}") for i in (21, 22, 23, 24)}
    return ImportBundle(contest=meta, cutoff=NOW - timedelta(days=1), problems=problems,
        students=["231250001"], selected=selected, issues=[])


async def start(db, quiz):
    return await start_attempt(db, Settings(), MockLLMProvider(), quiz_id=quiz.id,
        student_number="231250001", session_id="writer")


async def load_attempt(db, attempt_id):
    """Attempts need their full grading graph eagerly loaded: async lazy loads fail."""
    return await db.scalar(select(Attempt).where(Attempt.id == attempt_id)
        .execution_options(populate_existing=True)
        .options(selectinload(Attempt.questions).selectinload(Question.answer),
            selectinload(Attempt.questions).selectinload(Question.submission_snapshot)
                .selectinload(SubmissionSnapshot.problem)))


async def submit_all(db, quiz, *, dispute_ids=(), blank_ids=()):
    """Answer every question, optionally leaving one blank and/or marking a dispute."""
    view = await start(db, quiz)
    items = []
    for question in view.questions:
        question_id = uuid.UUID(question["id"])
        dispute = question_id in dispute_ids
        if question_id in blank_ids:
            items.append(LightweightDraftRequest(question_id=question_id, expected_revision=0,
                dispute=dispute, dispute_reason="我认为题目条件不对" if dispute else None))
        elif question["response_format"] == "short_answer":
            items.append(LightweightDraftRequest(question_id=question_id, expected_revision=0,
                answer_text="这段代码在更新当前状态", dispute=dispute))
        else:
            items.append(LightweightDraftRequest(question_id=question_id, expected_revision=0,
                choice_id="B", dispute=dispute))
    await submit(db, quiz_id=quiz.id, student_number="231250001", session_id="writer",
        payload=LightweightSubmitRequest(attempt_id=view.attempt_id,
            idempotency_key=f"synthetic-{uuid.uuid4()}", drafts=items))
    return view, await load_attempt(db, view.attempt_id)


# --- A03: more than three problems ------------------------------------------------

@pytest.mark.asyncio
async def test_more_than_three_problems_keep_id_order_and_last_is_explanation_only(db):
    quiz, _ = await persist_quiz(db, four_problem_bundle(), QuizCreateRequest(contest_id=9))
    snapshots = (await db.scalars(select(QuizProblemSnapshot).where(
        QuizProblemSnapshot.quiz_id == quiz.id).order_by(QuizProblemSnapshot.display_order))).all()
    assert [s.uoj_problem_id for s in snapshots] == [21, 22, 23, 24]
    assert [s.display_order for s in snapshots] == [1, 2, 3, 4]
    # Only the largest problem_id is explanation-only; no extra confirmation step exists.
    assert [s.include_choice for s in snapshots] == [True, True, True, False]


# --- A04: lightweight single-problem regeneration ---------------------------------

@pytest.mark.asyncio
async def test_single_problem_regeneration_queues_one_and_copies_the_rest(db):
    quiz = await prepared_quiz(db)
    participant = await db.scalar(select(QuizParticipant).where(QuizParticipant.quiz_id == quiz.id,
        QuizParticipant.student_number == "231250001"))
    result = await generation_service.regenerate_prepared(db, quiz.id, "231250001", 1, problem_id=11)
    assert result == {"round_no": 2, "queued": 1}
    jobs = (await db.scalars(select(GenerationJob).where(GenerationJob.quiz_id == quiz.id))).all()
    assert {j.round_no for j in jobs} == {1, 2}
    mine = [j for j in jobs if j.participant_id == participant.id]
    assert len([j for j in mine if j.round_no == 1]) == 3  # history preserved
    by_problem = {}
    for job in [j for j in mine if j.round_no == 2]:
        snapshot = await db.get(SubmissionSnapshot, job.submission_snapshot_id)
        by_problem[snapshot.uoj_problem_id] = job
    assert len(by_problem) == 3  # a complete next round, not just the regenerated problem
    assert by_problem[11].state == "queued" and by_problem[11].result_json is None
    assert by_problem[12].result_json and by_problem[13].result_json


# --- A06: an illegal option edit is a client error --------------------------------

@pytest.mark.asyncio
async def test_illegal_answer_key_is_rejected_as_422_not_500(db):
    quiz = await prepared_quiz(db)
    jobs = (await db.scalars(select(GenerationJob).where(GenerationJob.quiz_id == quiz.id,
        GenerationJob.state == "succeeded"))).all()
    job = next(j for j in jobs if len(j.result_json["questions"]) == 2)
    payload = prepared_edit.EditPreparedRequest(
        expected_revision=prepared_edit.revision(job.result_json),
        question="改后的题干", question_en="edited stem", reference_answer="参考解释",
        choices=[ChoiceOption(id=i, text=f"option {i}", text_en=f"option {i}") for i in "ABCD"],
        correct_choice_id="Z")
    with pytest.raises(HTTPException) as err:
        await prepared_edit.edit_prepared(db, quiz.id, "231250001", job.id, 2, payload)
    assert err.value.status_code == 422


# --- A08: clearing and resuming ---------------------------------------------------

@pytest.mark.asyncio
async def test_cleared_choice_and_short_answer_resume_from_the_server(db):
    quiz = await prepared_quiz(db)
    view = await start(db, quiz)
    args = dict(attempt_id=view.attempt_id, quiz_id=quiz.id, student_number="231250001",
        session_id="writer")
    choice = next(q for q in view.questions if q["response_format"] == "single_choice")
    question_id = uuid.UUID(choice["id"])
    await save_draft(db, **args, payload=LightweightDraftRequest(question_id=question_id,
        expected_revision=0, choice_id="B"))
    cleared = await save_draft(db, **args, payload=LightweightDraftRequest(question_id=question_id,
        expected_revision=1, choice_id=None))
    assert cleared["draft"]["choice_id"] is None and cleared["draft"]["revision"] == 2
    resumed = await start(db, quiz)
    stored = next(q for q in resumed.questions if q["id"] == choice["id"])["draft"]
    assert stored["choice_id"] is None and stored["revision"] == 2
    short = next(q for q in view.questions if q["response_format"] == "short_answer")
    short_id = uuid.UUID(short["id"])
    await save_draft(db, **args, payload=LightweightDraftRequest(question_id=short_id,
        expected_revision=0, answer_text="先写了一点"))
    emptied = await save_draft(db, **args, payload=LightweightDraftRequest(question_id=short_id,
        expected_revision=1, answer_text=""))
    assert emptied["draft"]["answer_text"] == ""


# --- A12: completion time is frozen at submission ---------------------------------

@pytest.mark.asyncio
async def test_regrade_and_manual_review_never_move_the_completion_time(db):
    quiz = await prepared_quiz(db)
    _, attempt = await submit_all(db, quiz)
    await grade_lightweight_attempt(db, MockLLMProvider(), attempt, None)
    attempt = await load_attempt(db, attempt.id)
    assert attempt.status == AttemptStatus.FINISHED and attempt.submitted_at is not None
    submitted_at = attempt.submitted_at
    question = attempt.questions[0]
    await score_question(db, attempt.id, question.id, score=2, reason="教师确认理解",
        actor="teacher", expected_version=attempt.score_version)
    attempt = await load_attempt(db, attempt.id)
    assert attempt.submitted_at == submitted_at
    await grade_lightweight_attempt(db, MockLLMProvider(), attempt, None)
    attempt = await load_attempt(db, attempt.id)
    assert attempt.submitted_at == submitted_at
    assert attempt.submission_source == "manual"


# --- A13: a read-only result token cannot write -----------------------------------

def test_result_purpose_token_is_read_only():
    quiz_id = str(uuid.uuid4())
    result_token = create_token("231250001", "student_result", quiz_id=quiz_id, sid="")
    assert require_student_result(result_token).student_number == "231250001"
    with pytest.raises(HTTPException) as err:
        require_student(result_token)
    assert err.value.status_code == 401
    writer_token = create_token("231250001", "student", quiz_id=quiz_id, sid="writer")
    assert require_student(writer_token).session_id == "writer"


# --- A14: lightweight grader protocol errors --------------------------------------

@pytest.mark.asyncio
async def test_grader_returning_mismatched_indexes_is_a_grading_error(db):
    quiz = await prepared_quiz(db)
    _, attempt = await submit_all(db, quiz)

    class Dropping(MockLLMProvider):
        async def grade_lightweight(self, **kwargs):
            result, raw = await super().grade_lightweight(**kwargs)
            return result.model_copy(update={"grades": result.grades[:-1]}), raw

    with pytest.raises(ValueError):
        await grade_lightweight_attempt(db, Dropping(), attempt, None)
    attempt = await load_attempt(db, attempt.id)
    assert attempt.status == AttemptStatus.GRADING_ERROR
    # A failed call must not fabricate a zero score for anything.
    assert all(question.answer.auto_score is None for question in attempt.questions)


def test_grading_schema_rejects_duplicate_indexes_and_bad_scores():
    item = dict(question_index=1, score=2, reason="ok", confidence=0.9)
    with pytest.raises(ValidationError):
        LightweightGradingResult.model_validate({"grades": [item, item]})
    with pytest.raises(ValidationError):
        LightweightGradingResult.model_validate({"grades": [{**item, "score": 3}]})


# --- A15: disputes on blank/choice questions and issue de-duplication -------------

@pytest.mark.asyncio
async def test_blank_choice_dispute_is_reviewed_and_not_reopened(db):
    quiz = await prepared_quiz(db)
    view = await start(db, quiz)
    choice = next(q for q in view.questions if q["response_format"] == "single_choice")
    choice_id = uuid.UUID(choice["id"])
    _, attempt = await submit_all(db, quiz, dispute_ids={choice_id}, blank_ids={choice_id})
    await grade_lightweight_attempt(db, MockLLMProvider(), attempt, None)
    attempt = await load_attempt(db, attempt.id)
    issues = (await db.scalars(select(ReviewIssue))).all()
    assert len(issues) == 1 and issues[0].source == "student_dispute"
    assert issues[0].resolved_at is None and attempt.review_required is True
    await score_question(db, attempt.id, choice_id, score=0, reason="已核查：学生空答，题目无误",
        actor="teacher", expected_version=attempt.score_version)
    attempt = await load_attempt(db, attempt.id)

    class Disputing(MockLLMProvider):
        async def grade_lightweight(self, **kwargs):
            result, raw = await super().grade_lightweight(**kwargs)
            grades = [g.model_copy(update={"student_dispute": True, "dispute_reason": "再次识别"})
                for g in result.grades]
            return result.model_copy(update={"grades": grades}), raw

    await grade_lightweight_attempt(db, Disputing(), attempt, None)
    count_after_first = await db.scalar(select(func.count()).select_from(ReviewIssue))
    attempt = await load_attempt(db, attempt.id)
    await grade_lightweight_attempt(db, Disputing(), attempt, None)
    # The resolved student item stays resolved; a model-sourced item is never duplicated.
    assert (await db.scalar(select(func.count()).select_from(ReviewIssue))) == count_after_first
    issues = (await db.scalars(select(ReviewIssue))).all()
    assert [i for i in issues if i.source == "student_dispute"][0].resolved_at is not None
    # One item per (answer, source): a repeated regrade never duplicates a concern.
    pairs = [(i.answer_id, i.source) for i in issues]
    assert len(pairs) == len(set(pairs))


# --- A17: per-question scoring needs explicit override clearance ------------------

@pytest.mark.asyncio
async def test_per_question_score_requires_clearing_a_whole_attempt_override(db):
    quiz = await prepared_quiz(db)
    _, attempt = await submit_all(db, quiz)
    await grade_lightweight_attempt(db, MockLLMProvider(), attempt, None)
    attempt = await load_attempt(db, attempt.id)
    attempt.manual_override_score = 8
    attempt.manual_override_reason = "历史整份覆盖"
    await db.commit()
    question = attempt.questions[0]
    with pytest.raises(HTTPException) as err:
        await score_question(db, attempt.id, question.id, score=2, reason="逐题改分",
            actor="teacher", expected_version=attempt.score_version)
    assert err.value.status_code == 409
    saved = await score_question(db, attempt.id, question.id, score=2, reason="逐题改分",
        actor="teacher", expected_version=attempt.score_version, clear_override=True)
    attempt = await load_attempt(db, attempt.id)
    assert attempt.manual_override_score is None and saved["score"] == 2


# --- A27: the newest migration is additive and re-runnable ------------------------

def test_model_switch_migration_is_additive_and_idempotent(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    path = Path(__file__).parents[1] / "alembic/versions/0010_model_switch_tests.py"
    spec = importlib.util.spec_from_file_location("model_switch_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite:///" + str(tmp_path / "model_switch.sqlite"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runtime_configuration_audits "
            "(id INTEGER PRIMARY KEY, revision INTEGER, values_json TEXT)"))
        conn.execute(text("INSERT INTO runtime_configuration_audits VALUES (1, 9, '{}')"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        assert conn.execute(text("SELECT revision, model_tests_json FROM "
            "runtime_configuration_audits")).one() == (9, None)
        # Fresh-install and re-run safety: the guard must tolerate the existing column.
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    engine.dispose()
