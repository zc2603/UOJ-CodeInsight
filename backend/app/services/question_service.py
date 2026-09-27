from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Answer, Attempt, AttemptStatus, Question, QuizParticipant
from app.schemas.api import StudentQuestionResponse
from app.services.quiz_service import _attempt_view
from app.time_utils import ensure_utc


async def get_current_attempt(
    db: AsyncSession, *, quiz_id: uuid.UUID, student_number: str, session_id: str
) -> Attempt:
    attempt = (
        await db.execute(
            select(Attempt)
            .join(QuizParticipant, QuizParticipant.id == Attempt.participant_id)
            .where(
                QuizParticipant.quiz_id == quiz_id,
                QuizParticipant.student_number == student_number,
                Attempt.status != AttemptStatus.RESET,
            )
            .order_by(Attempt.attempt_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未开始测评")
    if attempt.session_id != session_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Session 与 Attempt 不匹配")
    return attempt


async def submit_answer(
    db: AsyncSession,
    *,
    quiz_id: uuid.UUID,
    student_number: str,
    session_id: str,
    question_index: int,
    student_answer: str,
) -> StudentQuestionResponse | Attempt:
    attempt = await get_current_attempt(
        db, quiz_id=quiz_id, student_number=student_number, session_id=session_id
    )
    attempt = (
        await db.execute(
            select(Attempt)
            .where(Attempt.id == attempt.id)
            .with_for_update()
            .execution_options(populate_existing=True)
            .options(selectinload(Attempt.questions).selectinload(Question.answer))
        )
    ).scalar_one()
    now = datetime.now(timezone.utc)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise HTTPException(status.HTTP_409_CONFLICT, "当前 Attempt 不能提交答案")
    if attempt.assessment_version == "lightweight_v1":
        raise HTTPException(409, "请刷新页面并使用整份交卷入口")
    if attempt.deadline_at is None or now >= ensure_utc(attempt.deadline_at):
        attempt.status = AttemptStatus.EXPIRED
        await db.commit()
        raise HTTPException(status.HTTP_410_GONE, "测评已超时")

    unanswered = [item for item in attempt.questions if item.answer is None]
    if not unanswered or unanswered[0].question_index != question_index:
        raise HTTPException(status.HTTP_409_CONFLICT, "不能跳题、重复提交或修改旧答案")
    question = unanswered[0]
    db.add(Answer(question_id=question.id, student_answer=student_answer.strip()))
    attempt.draft_text = None
    attempt.draft_question_index = None
    is_last = len(unanswered) == 1
    attempt_id = attempt.id
    if is_last:
        attempt.status = AttemptStatus.GRADING
        attempt.submitted_at = now
        attempt.submission_source = "manual"
    await db.commit()

    if is_last:
        await db.refresh(attempt)
        return attempt

    db.expire_all()
    return await _attempt_view(db, attempt_id)


async def save_draft(db, *, quiz_id, student_number, session_id, question_index, student_answer, revision):
    current = await get_current_attempt(db, quiz_id=quiz_id, student_number=student_number, session_id=session_id)
    attempt = await db.scalar(select(Attempt).where(Attempt.id == current.id).with_for_update()
        .execution_options(populate_existing=True).options(selectinload(Attempt.questions).selectinload(Question.answer)))
    if attempt.assessment_version == "lightweight_v1":
        raise HTTPException(409, "请刷新页面并使用逐题草稿入口")
    if attempt.status != AttemptStatus.IN_PROGRESS or attempt.deadline_at is None or datetime.now(timezone.utc) >= ensure_utc(attempt.deadline_at):
        raise HTTPException(410, "测评时间已结束，已保存答案将自动提交")
    unanswered = [q for q in attempt.questions if q.answer is None]
    if not unanswered or unanswered[0].question_index != question_index:
        raise HTTPException(409, "当前问题已变更")
    if revision > attempt.draft_revision:
        attempt.draft_text = student_answer
        attempt.draft_question_index = question_index
        attempt.draft_revision = revision
    await db.commit()
    return {"saved": True}
