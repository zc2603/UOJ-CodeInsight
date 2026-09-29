"""Publication, per-question review and appeals for current attempts."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import (Answer, Appeal, Attempt, AttemptStatus, GenerationControl, GenerationJob, Question,
    Quiz, QuizParticipant, QuizStatus, ReviewIssue, ScoreAudit)
from app.time_utils import ensure_utc
from app.services.teacher_settings import grade_for


def effective_score(answer: Answer) -> int | None:
    return answer.manual_score if answer.manual_score is not None else answer.auto_score


def effective_attempt_score(attempt: Attempt) -> int | None:
    if attempt.manual_override_score is not None:
        return attempt.manual_override_score
    scores = [effective_score(q.answer) if q.answer else None for q in attempt.questions]
    return sum(scores) if scores and all(score is not None for score in scores) else None


async def current_attempt(db, participant_id) -> Attempt | None:
    attempt = await db.scalar(select(Attempt).where(Attempt.participant_id == participant_id)
        .order_by(Attempt.attempt_no.desc()).limit(1)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    return attempt if attempt and attempt.status != AttemptStatus.RESET else None


async def publish(db, quiz_id: uuid.UUID, actor: str, *, include_answers: bool = True) -> dict:
    # Accept the legacy argument, but publication now always includes answers.
    include_answers = True
    await db.execute(select(GenerationControl).where(GenerationControl.id == 1).with_for_update())
    participants = (await db.scalars(select(QuizParticipant).where(QuizParticipant.quiz_id == quiz_id)
        .order_by(QuizParticipant.id).with_for_update())).all()
    quiz = await db.scalar(select(Quiz).where(Quiz.id == quiz_id).with_for_update()
        .execution_options(populate_existing=True))
    if quiz is None:
        raise HTTPException(404, "测评不存在")
    now = datetime.now(timezone.utc)
    if quiz.published_at is not None:
        if include_answers and not quiz.publish_answers:
            quiz.publish_answers = True
            await db.commit()
        return {"published": True, "include_answers": quiz.publish_answers}
    if quiz.status == QuizStatus.DRAFT or (quiz.status != QuizStatus.CLOSED and now < ensure_utc(quiz.end_time)):
        raise HTTPException(409, "进入窗口仍开放")
    active_preparation = await db.scalar(select(GenerationJob.id).where(
        GenerationJob.quiz_id == quiz_id, GenerationJob.state.in_(["queued", "running"])).limit(1))
    if active_preparation is not None:
        raise HTTPException(409, "仍有题目正在准备，不能公布")
    attempts = []
    for participant in participants:
        attempt = await current_attempt(db, participant.id)
        if attempt:
            attempts.append(attempt)
    for attempt in attempts:
        if attempt.status != AttemptStatus.FINISHED or attempt.review_required:
            raise HTTPException(409, "仍有未交卷、评分错误或待复核作答")
        if not attempt.questions or any(q.answer is None or effective_score(q.answer) is None for q in attempt.questions):
            raise HTTPException(409, "仍有缺失的逐题成绩")
        issue = await db.scalar(select(ReviewIssue.id).join(Answer, Answer.id == ReviewIssue.answer_id)
            .join(Question, Question.id == Answer.question_id)
            .where(Question.attempt_id == attempt.id, ReviewIssue.resolved_at.is_(None)).limit(1))
        if issue:
            raise HTTPException(409, "仍有待处理复核事项")
    quiz.published_at = now
    quiz.published_by = actor
    quiz.publish_answers = include_answers
    await db.commit()
    return {"published": True, "include_answers": include_answers, "participant_count": len(participants),
        "graded_count": len(attempts)}


async def score_question(db, attempt_id: uuid.UUID, question_id: uuid.UUID, *, score: int,
    reason: str, actor: str, expected_version: int, clear_override: bool = False) -> dict:
    attempt = await db.scalar(select(Attempt).where(Attempt.id == attempt_id).with_for_update()
        .execution_options(populate_existing=True)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    if attempt is None:
        raise HTTPException(404, "作答不存在")
    if attempt.status != AttemptStatus.FINISHED or attempt.score_version != expected_version:
        raise HTTPException(409, "成绩已更新或尚未完成")
    q = next((q for q in attempt.questions if q.id == question_id), None)
    if q is None or q.answer is None:
        raise HTTPException(404, "题目不存在")
    if not 0 <= score <= 2:
        raise HTTPException(422, "逐题分数须为 0、1 或 2")
    if attempt.manual_override_score is not None:
        if not clear_override:
            raise HTTPException(409, "请先明确确认清除历史整份总分覆盖")
        attempt.manual_override_score = None
        attempt.manual_override_reason = None
    answer = q.answer
    old = effective_score(answer)
    answer.manual_score = score
    answer.manual_reason = reason
    answer.manual_by = actor
    answer.manual_at = datetime.now(timezone.utc)
    if old != score:
        answer.score_version += 1
    attempt.score_version += 1
    issues = (await db.scalars(select(ReviewIssue).where(ReviewIssue.answer_id == answer.id,
        ReviewIssue.resolved_at.is_(None)).with_for_update())).all()
    for issue in issues:
        issue.resolved_at = answer.manual_at
        issue.resolved_by = actor
        issue.resolution = reason
    answer.review_required = False
    answer.review_reason = None
    unresolved_other = await db.scalar(select(ReviewIssue.id).join(Answer, Answer.id == ReviewIssue.answer_id)
        .join(Question, Question.id == Answer.question_id)
        .where(Question.attempt_id == attempt.id, ReviewIssue.resolved_at.is_(None),
            Answer.id != answer.id).limit(1))
    attempt.review_required = unresolved_other is not None
    total = effective_attempt_score(attempt)
    db.add(ScoreAudit(attempt_id=attempt.id, question_id=q.id, old_score=old,
        new_score=score, reason=reason, actor=actor, score_version=attempt.score_version))
    await db.commit()
    return {"score": score, "total_score": total, "score_version": attempt.score_version}


async def student_result(db, quiz_id: uuid.UUID, student_number: str) -> dict:
    participant = await db.scalar(select(QuizParticipant).where(
        QuizParticipant.quiz_id == quiz_id, QuizParticipant.student_number == student_number))
    if participant is None:
        raise HTTPException(404, "成绩不存在")
    quiz = await db.get(Quiz, quiz_id)
    if quiz.published_at is None:
        return {"published": False, "message": "成绩尚未公布"}
    attempt = await current_attempt(db, participant.id)
    if attempt is None:
        return {"published": True, "participated": False}
    if attempt.status != AttemptStatus.FINISHED:
        return {"published": True, "participated": False}
    appeals = (await db.scalars(select(Appeal).where(Appeal.attempt_id == attempt.id))).all()
    by_question = {}
    for appeal in appeals:
        by_question.setdefault(appeal.question_id, []).append(appeal)
    score = effective_attempt_score(attempt)
    if score is None:
        raise HTTPException(409, "成绩尚未完整")
    questions = []
    for q in sorted(attempt.questions, key=lambda item: item.question_index):
        item = dict(id=str(q.id), index=q.question_index, type=q.question_type.value,
            response_format=q.response_format, question=q.question_text, question_en=q.question_text_en,
            choices=q.choices_json, answer_text=q.answer.student_answer, choice_id=q.answer.choice_id,
            score=effective_score(q.answer), reason=q.answer.manual_reason or q.answer.grading_reason,
            score_version=q.answer.score_version,
            appeals=[dict(id=str(a.id), state=a.state, reason=a.reason, resolution=a.resolution,
                question_score_version=a.question_score_version)
                for a in by_question.get(q.id, [])])
        # Applies to previously published quizzes too, without rewriting history.
        item["reference_answer"] = q.reference_answer
        item["correct_choice_id"] = q.correct_choice_id
        questions.append(item)
    return {"published": True, "participated": True, "assessment_version": attempt.assessment_version,
        "submitted_at": attempt.submitted_at or (ensure_utc(attempt.deadline_at) if attempt.timed_out else
            max((ensure_utc(q.answer.submitted_at) for q in attempt.questions if q.answer), default=None)),
        "submission_source": attempt.submission_source or ("timeout" if attempt.timed_out else "manual"),
        "score": score, "grade": grade_for(score, quiz.grade_bands), "max_score": len(attempt.questions) * 2,
        "percent": score / (len(attempt.questions) * 2) * 100 if attempt.questions else None,
        "questions": questions}


async def request_appeal(db, quiz_id: uuid.UUID, student_number: str, question_id: uuid.UUID,
    *, reason: str, request_key: str) -> dict:
    participant = await db.scalar(select(QuizParticipant).where(QuizParticipant.quiz_id == quiz_id,
        QuizParticipant.student_number == student_number))
    if participant is None:
        raise HTTPException(404, "题目不存在")
    attempt = await current_attempt(db, participant.id)
    if attempt is None:
        raise HTTPException(404, "题目不存在")
    attempt = await db.scalar(select(Attempt).where(Attempt.id == attempt.id).with_for_update())
    quiz = await db.get(Quiz, quiz_id)
    if quiz.published_at is None or attempt.status != AttemptStatus.FINISHED:
        raise HTTPException(403, "成绩尚未公布")
    question = await db.scalar(select(Question).where(Question.id == question_id,
        Question.attempt_id == attempt.id).options(selectinload(Question.answer)))
    if question is None or question.answer is None:
        raise HTTPException(404, "题目不存在")
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise HTTPException(422, "请填写 1–1000 字的申请理由")
    existing = await db.scalar(select(Appeal).where(Appeal.question_id == question.id,
        Appeal.question_score_version == question.answer.score_version))
    if existing is not None:
        if existing.request_key == request_key:
            return {"id": str(existing.id), "state": existing.state}
        raise HTTPException(409, "本题当前成绩版本已有申请")
    pending = await db.scalar(select(Appeal.id).where(Appeal.question_id == question.id,
        Appeal.state == "pending").limit(1))
    if pending is not None:
        raise HTTPException(409, "本题已有待处理申请")
    appeal = Appeal(attempt_id=attempt.id, question_id=question.id,
        question_score_version=question.answer.score_version, request_key=request_key,
        reason=reason, score_snapshot=effective_score(question.answer) or 0,
        feedback_snapshot=question.answer.manual_reason or question.answer.grading_reason or "",
        answer_snapshot=question.answer.student_answer if question.response_format == "short_answer"
            else (question.answer.choice_id or ""),
        question_snapshot=question.question_text, state="pending")
    db.add(appeal)
    await db.commit()
    return {"id": str(appeal.id), "state": "pending"}


async def resolve_appeal(db, appeal_id: uuid.UUID, *, actor: str, resolution: str,
    expected_version: int, score: int | None) -> dict:
    identity = await db.scalar(select(Appeal).where(Appeal.id == appeal_id))
    if identity is None:
        raise HTTPException(404, "申诉不存在")
    attempt = await db.scalar(select(Attempt).where(Attempt.id == identity.attempt_id).with_for_update()
        .execution_options(populate_existing=True)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    appeal = await db.scalar(select(Appeal).where(Appeal.id == appeal_id).with_for_update()
        .execution_options(populate_existing=True))
    if appeal.state != "pending" or attempt.score_version != expected_version:
        raise HTTPException(409, "申诉或成绩已更新")
    q = next(q for q in attempt.questions if q.id == appeal.question_id)
    if appeal.question_score_version != q.answer.score_version:
        raise HTTPException(409, "本题分数版本已更新")
    resolution = resolution.strip()
    if not resolution:
        raise HTTPException(422, "处理说明不能为空")
    if score is not None:
        if not 0 <= score <= 2:
            raise HTTPException(422, "逐题分数须为 0、1 或 2")
        old = effective_score(q.answer)
        q.answer.manual_score = score
        q.answer.manual_reason = resolution
        q.answer.manual_by = actor
        q.answer.manual_at = datetime.now(timezone.utc)
        if old != score:
            q.answer.score_version += 1
        attempt.score_version += 1
        db.add(ScoreAudit(attempt_id=attempt.id, question_id=q.id, old_score=old,
            new_score=score, reason=resolution, actor=actor, score_version=attempt.score_version))
    appeal.state = "resolved"
    appeal.resolution = resolution
    appeal.resolved_by = actor
    appeal.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": str(appeal.id), "state": appeal.state, "total_score": effective_attempt_score(attempt)}
