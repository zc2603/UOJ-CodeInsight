from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Answer, Attempt, AttemptStatus, LLMCallLog, Question, SubmissionSnapshot
from app.services.llm_provider import LLMProvider, GRADER_VERSION
from app.schemas.llm import GradeItem, GradingResult


async def grade_attempt(
    db: AsyncSession, provider: LLMProvider, attempt_id: uuid.UUID, *, lease_token: str | None = None
) -> Attempt:
    attempt = (
        await db.execute(
            select(Attempt)
            .where(Attempt.id == attempt_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Attempt.selected_submission).selectinload(
                    SubmissionSnapshot.problem
                ),
                selectinload(Attempt.questions).selectinload(Question.answer),
                selectinload(Attempt.questions)
                .selectinload(Question.submission_snapshot)
                .selectinload(SubmissionSnapshot.problem),
            )
        )
    ).scalar_one()
    if lease_token is not None and (attempt.grading_token != lease_token or attempt.status != AttemptStatus.GRADING):
        return attempt
    if lease_token is None and attempt.timed_out and attempt.status == AttemptStatus.GRADING and attempt.grading_token:
        raise ValueError("超时作答正在自动评分，请稍后查看")
    if not attempt.questions or any(item.answer is None for item in attempt.questions):
        raise ValueError("attempt does not have all submitted answers")

    grouped: dict[uuid.UUID, tuple[SubmissionSnapshot, list[Question]]] = {}
    for question in attempt.questions:
        snapshot = question.submission_snapshot
        grouped.setdefault(snapshot.id, (snapshot, []))[1].append(question)

    async def grade_problem(snapshot: SubmissionSnapshot, questions: list[Question]):
        blanks = [GradeItem(question_index=q.question_index, score=0, reason="未作答", confidence=1)
            for q in questions if not q.answer.student_answer.strip()]
        answered = [q for q in questions if q.answer.student_answer.strip()]
        if not answered:
            return GradingResult(grades=blanks), "Empty answers scored locally"
        payload = [
            {
                "question_index": item.question_index,
                "question": item.question_text,
                "question_en": item.question_text_en,
                "reference_answer": item.reference_answer,
                "grading_points": item.grading_points_json,
                "student_answer": item.answer.student_answer,
            }
            for item in answered
        ]
        result, raw = await provider.grade_answers(
            title=snapshot.problem.title,
            statement=snapshot.problem.statement,
            language=snapshot.language,
            source_code=snapshot.source_code,
            question_payload=payload,
        )
        expected = {item.question_index for item in answered}
        actual = {item.question_index for item in result.grades}
        if actual != expected:
            raise ValueError("grader returned mismatched question indexes")
        return GradingResult(grades=[*result.grades, *blanks]), raw

    # Do not occupy a database connection while an external model call is in flight.
    await db.commit()
    try:
        graded_sets = await asyncio.gather(
            *(grade_problem(snapshot, questions) for snapshot, questions in grouped.values())
        )
    except Exception as exc:
        locked = (
            await db.execute(select(Attempt).where(Attempt.id == attempt_id).with_for_update().execution_options(populate_existing=True))
        ).scalar_one()
        if lease_token is not None and (locked.grading_token != lease_token or locked.status != AttemptStatus.GRADING):
            await db.commit()
            return locked
        locked.grading_token = None
        locked.grading_lease_until = None
        locked.status = AttemptStatus.GRADING_ERROR
        db.add(
            LLMCallLog(
                attempt_id=attempt_id,
                call_type="grading",
                model=provider.model_name,
                prompt_version=GRADER_VERSION,
                success=False,
                raw_response=getattr(exc, "raw_response", None),
                error=str(exc),
            )
        )
        await db.commit()
        raise

    locked = (
        await db.execute(
            select(Attempt)
            .where(Attempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
            .options(selectinload(Attempt.questions).selectinload(Question.answer))
        )
    ).scalar_one()
    if lease_token is not None and (locked.grading_token != lease_token or locked.status != AttemptStatus.GRADING):
        await db.commit()
        return locked
    locked.grading_token = None
    locked.grading_lease_until = None
    grade_by_index = {}
    raw_by_index: dict[int, str] = {}
    for (snapshot, _questions), (result, raw) in zip(
        grouped.values(), graded_sets, strict=True
    ):
        for grade in result.grades:
            grade_by_index[grade.question_index] = grade
            raw_by_index[grade.question_index] = raw
        db.add(
            LLMCallLog(
                attempt_id=attempt_id,
                call_type="grading",
                model=provider.model_name,
                prompt_version=GRADER_VERSION,
                success=True,
                raw_response=raw,
            )
        )
    for question in locked.questions:
        answer = question.answer
        assert answer is not None
        grade = grade_by_index[question.question_index]
        answer.auto_score = grade.score
        answer.grading_reason = grade.reason
        answer.confidence = grade.confidence
        answer.grader_model = provider.model_name
        answer.grader_prompt_version = GRADER_VERSION
        answer.grader_raw_response = raw_by_index[question.question_index]
        answer.review_required = grade.review_required
        answer.review_reason = grade.review_reason
        answer.question_validity = grade.question_validity
    locked.auto_score = sum(item.score for item in grade_by_index.values())
    # Regrading cannot silently dismiss an outstanding teacher review.
    locked.review_required = locked.review_required or any(item.review_required for item in grade_by_index.values())
    locked.status = AttemptStatus.FINISHED
    locked.finished_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(locked)
    return locked
