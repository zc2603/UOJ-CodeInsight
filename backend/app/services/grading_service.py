from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Answer, Attempt, AttemptStatus, LLMCallLog, Question, SubmissionSnapshot
from app.services.llm_provider import LLMProvider


async def grade_attempt(
    db: AsyncSession, provider: LLMProvider, attempt_id: uuid.UUID
) -> Attempt:
    attempt = (
        await db.execute(
            select(Attempt)
            .where(Attempt.id == attempt_id)
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
    if not attempt.questions or any(item.answer is None for item in attempt.questions):
        raise ValueError("attempt does not have all submitted answers")

    grouped: dict[uuid.UUID, tuple[SubmissionSnapshot, list[Question]]] = {}
    for question in attempt.questions:
        snapshot = question.submission_snapshot
        grouped.setdefault(snapshot.id, (snapshot, []))[1].append(question)

    async def grade_problem(snapshot: SubmissionSnapshot, questions: list[Question]):
        payload = [
            {
                "question_index": item.question_index,
                "question": item.question_text,
                "question_en": item.question_text_en,
                "reference_answer": item.reference_answer,
                "grading_points": item.grading_points_json,
                "student_answer": item.answer.student_answer,
            }
            for item in questions
        ]
        result, raw = await provider.grade_answers(
            title=snapshot.problem.title,
            statement=snapshot.problem.statement,
            language=snapshot.language,
            source_code=snapshot.source_code,
            question_payload=payload,
        )
        expected = {item.question_index for item in questions}
        actual = {item.question_index for item in result.grades}
        if actual != expected:
            raise ValueError("grader returned mismatched question indexes")
        return result, raw

    # Do not occupy a database connection while an external model call is in flight.
    await db.commit()
    try:
        graded_sets = await asyncio.gather(
            *(grade_problem(snapshot, questions) for snapshot, questions in grouped.values())
        )
    except Exception as exc:
        locked = (
            await db.execute(select(Attempt).where(Attempt.id == attempt_id).with_for_update())
        ).scalar_one()
        locked.status = AttemptStatus.GRADING_ERROR
        db.add(
            LLMCallLog(
                attempt_id=attempt_id,
                call_type="grading",
                model=provider.model_name,
                prompt_version="grader_v5",
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
            .options(selectinload(Attempt.questions).selectinload(Question.answer))
        )
    ).scalar_one()
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
                prompt_version="grader_v5",
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
        answer.grader_prompt_version = "grader_v5"
        answer.grader_raw_response = raw_by_index[question.question_index]
    locked.auto_score = sum(item.score for item in grade_by_index.values())
    locked.status = AttemptStatus.FINISHED
    locked.finished_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(locked)
    return locked
