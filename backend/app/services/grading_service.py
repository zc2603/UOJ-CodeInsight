from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Answer, Attempt, AttemptStatus, LLMCallLog, Question, SubmissionSnapshot, ReviewIssue
from app.services.llm_provider import LLMProvider, GRADER_VERSION, LIGHTWEIGHT_GRADER_VERSION
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
    if lease_token is None and attempt.status == AttemptStatus.GRADING and attempt.grading_token:
        raise ValueError("作答正在自动评分，请稍后查看")
    if not attempt.questions or any(item.answer is None for item in attempt.questions):
        raise ValueError("attempt does not have all submitted answers")
    if attempt.assessment_version == "lightweight_v1":
        return await grade_lightweight_attempt(db, provider, attempt, lease_token)

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
    tasks = [asyncio.create_task(grade_problem(snapshot, questions))
        for snapshot, questions in grouped.values()]
    try:
        graded_sets = await asyncio.gather(*tasks)
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
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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


async def grade_lightweight_attempt(db: AsyncSession, provider: LLMProvider,
    attempt: Attempt, lease_token: str | None) -> Attempt:
    grouped: dict[uuid.UUID, tuple[SubmissionSnapshot, list[Question]]] = {}
    for question in attempt.questions:
        snapshot = question.submission_snapshot
        grouped.setdefault(snapshot.id, (snapshot, []))[1].append(question)

    async def grade_problem(snapshot: SubmissionSnapshot, questions: list[Question]):
        local = {}
        needed = []
        for q in questions:
            a = q.answer
            if q.response_format == "single_choice":
                local[q.question_index] = dict(score=2 if a.choice_id == q.correct_choice_id else 0,
                    reason="选择正确。" if a.choice_id == q.correct_choice_id else "未选或选择不正确。",
                    confidence=None, student_dispute=False, dispute_reason=None,
                    needs_teacher_review=False, review_reason=None, raw=None, model=None)
            elif not a.student_answer.strip():
                local[q.question_index] = dict(score=0, reason="未作答。", confidence=None,
                    student_dispute=False, dispute_reason=None, needs_teacher_review=False,
                    review_reason=None, raw=None, model=None)
            else:
                needed.append(q)
        if needed:
            payload = [dict(question_index=q.question_index, question=q.question_text,
                question_en=q.question_text_en, student_answer=q.answer.student_answer) for q in needed]
            result, raw = await provider.grade_lightweight(title=snapshot.problem.title,
                statement=snapshot.problem.statement, language=snapshot.language,
                source_code=snapshot.source_code, question_payload=payload)
            expected = {q.question_index for q in needed}
            if len(result.grades) != len(expected) or {g.question_index for g in result.grades} != expected:
                raise ValueError("lightweight grader returned mismatched indexes")
            for grade in result.grades:
                local[grade.question_index] = dict(score=grade.score, reason=grade.reason,
                    confidence=grade.confidence, student_dispute=grade.student_dispute,
                    dispute_reason=grade.dispute_reason,
                    needs_teacher_review=grade.needs_teacher_review,
                    review_reason=grade.review_reason, raw=raw, model=provider.model_name)
        return local

    await db.commit()
    try:
        sets = await asyncio.gather(*(grade_problem(s, q) for s, q in grouped.values()))
        grades = {i: grade for group in sets for i, grade in group.items()}
        if len(grades) != len(attempt.questions):
            raise ValueError("lightweight grading omitted a question")
    except Exception as exc:
        locked = await db.scalar(select(Attempt).where(Attempt.id == attempt.id).with_for_update()
            .execution_options(populate_existing=True))
        if lease_token is None or (locked.grading_token == lease_token and locked.status == AttemptStatus.GRADING):
            locked.status = AttemptStatus.GRADING_ERROR
            locked.grading_token = None
            locked.grading_lease_until = None
            db.add(LLMCallLog(attempt_id=attempt.id, call_type="grading",
                model=provider.model_name, prompt_version=LIGHTWEIGHT_GRADER_VERSION,
                success=False, raw_response=getattr(exc, "raw_response", None), error=str(exc)))
        await db.commit()
        raise

    locked = await db.scalar(select(Attempt).where(Attempt.id == attempt.id).with_for_update()
        .execution_options(populate_existing=True)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    if lease_token is not None and (locked.grading_token != lease_token or locked.status != AttemptStatus.GRADING):
        await db.commit()
        return locked
    for q in locked.questions:
        a = q.answer
        grade = grades[q.question_index]
        if a.auto_score != grade["score"]:
            a.score_version += 1
        a.auto_score = grade["score"]
        a.grading_reason = grade["reason"]
        a.confidence = grade["confidence"]
        a.grader_model = grade["model"]
        a.grader_prompt_version = LIGHTWEIGHT_GRADER_VERSION if grade["model"] else None
        a.grader_raw_response = grade["raw"]
        for source, triggered, reason in (
            ("model_dispute", grade["student_dispute"], grade["dispute_reason"] or "模型识别到学生异议"),
            ("model_uncertain", grade["needs_teacher_review"], grade["review_reason"] or "模型要求教师复核"),
        ):
            if triggered:
                existing = await db.scalar(select(ReviewIssue).where(
                    ReviewIssue.answer_id == a.id, ReviewIssue.source == source))
                if existing is None:
                    db.add(ReviewIssue(answer_id=a.id, source=source, reason=reason))
    await db.flush()
    unresolved = (await db.scalars(select(ReviewIssue).join(Answer, Answer.id == ReviewIssue.answer_id)
        .join(Question, Question.id == Answer.question_id)
        .where(Question.attempt_id == locked.id, ReviewIssue.resolved_at.is_(None)))).all()
    unresolved_answers = {issue.answer_id for issue in unresolved}
    for q in locked.questions:
        q.answer.review_required = q.answer.id in unresolved_answers
        q.answer.review_reason = "；".join(i.reason for i in unresolved if i.answer_id == q.answer.id) or None
    locked.review_required = bool(unresolved)
    locked.auto_score = sum(grades[q.question_index]["score"] for q in locked.questions)
    locked.status = AttemptStatus.FINISHED
    locked.grading_token = None
    locked.grading_lease_until = None
    locked.finished_at = datetime.now(timezone.utc)
    locked.score_version += 1
    await db.commit()
    return locked
