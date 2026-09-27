"""Versioned draft and whole-attempt submission with one Attempt row lock."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models import Answer, AnswerDraft, Attempt, AttemptStatus, Question, ReviewIssue
from app.schemas.api import LightweightDraftRequest, LightweightSubmitRequest
from app.time_utils import ensure_utc


def draft_view(draft: AnswerDraft | None) -> dict:
    return dict(answer_text=draft.answer_text, choice_id=draft.choice_id, revisit=draft.revisit,
        dispute=draft.dispute, dispute_reason=draft.dispute_reason, revision=draft.revision) if draft else dict(
        answer_text="", choice_id=None, revisit=False, dispute=False, dispute_reason=None, revision=0)


def check_input(question: Question, payload: LightweightDraftRequest) -> None:
    if question.response_format == "single_choice":
        if payload.answer_text.strip():
            raise HTTPException(422, "单选题不能提交简答文字")
        if payload.choice_id is not None and payload.choice_id not in {c["id"] for c in question.choices_json or []}:
            raise HTTPException(422, "选项不存在")
    elif payload.choice_id is not None:
        raise HTTPException(422, "简答题不能提交选项")
    if payload.dispute_reason and not payload.dispute:
        raise HTTPException(422, "请先标记题目有疑问")


async def locked_attempt(db, attempt_id: uuid.UUID, quiz_id: uuid.UUID, student_number: str,
    session_id: str | None, *, require_writer: bool = True) -> Attempt:
    from app.models import QuizParticipant
    attempt = await db.scalar(select(Attempt).join(QuizParticipant, QuizParticipant.id == Attempt.participant_id)
        .where(Attempt.id == attempt_id, Attempt.quiz_id == quiz_id,
            QuizParticipant.student_number == student_number)
        .with_for_update().execution_options(populate_existing=True)
        .options(selectinload(Attempt.questions).selectinload(Question.answer)))
    if attempt is None:
        raise HTTPException(404, "作答不存在")
    if attempt.assessment_version != "lightweight_v1":
        raise HTTPException(409, "请使用原测评作答页面")
    latest_no = await db.scalar(select(func.max(Attempt.attempt_no))
        .where(Attempt.participant_id == attempt.participant_id))
    if attempt.attempt_no != latest_no or attempt.status == AttemptStatus.RESET:
        raise HTTPException(409, "当前作答已失效")
    if require_writer and attempt.session_id != session_id:
        raise HTTPException(403, "当前会话没有作答写入权限")
    return attempt


async def save_draft(db, *, attempt_id, quiz_id, student_number, session_id,
    payload: LightweightDraftRequest) -> dict:
    attempt = await locked_attempt(db, attempt_id, quiz_id, student_number, session_id)
    now = datetime.now(timezone.utc)
    if attempt.status != AttemptStatus.IN_PROGRESS or not attempt.deadline_at or now >= ensure_utc(attempt.deadline_at):
        raise HTTPException(410, "作答时间已结束，迟到编辑不能补交")
    question = next((q for q in attempt.questions if q.id == payload.question_id), None)
    if question is None:
        raise HTTPException(404, "题目不存在")
    check_input(question, payload)
    draft = await db.scalar(select(AnswerDraft).where(AnswerDraft.attempt_id == attempt.id,
        AnswerDraft.question_id == question.id))
    current_revision = draft.revision if draft else 0
    if payload.expected_revision != current_revision:
        raise HTTPException(409, detail={"message": "草稿已在其他页面更新", "server_draft": draft_view(draft)})
    if draft is None:
        draft = AnswerDraft(attempt_id=attempt.id, question_id=question.id)
        db.add(draft)
    draft.answer_text = payload.answer_text
    draft.choice_id = payload.choice_id
    draft.revisit = payload.revisit
    draft.dispute = payload.dispute
    draft.dispute_reason = payload.dispute_reason.strip() if payload.dispute_reason else None
    draft.revision = current_revision + 1
    draft.updated_at = now
    await db.commit()
    return {"saved": True, "draft": draft_view(draft)}


async def freeze(db, attempt: Attempt, *, source: str, now: datetime, drafts: dict[uuid.UUID, AnswerDraft]) -> None:
    for question in attempt.questions:
        draft = drafts.get(question.id)
        text = draft.answer_text.strip() if draft else ""
        choice = draft.choice_id if draft else None
        answer = question.answer
        if answer is None:
            answer = Answer(question_id=question.id, student_answer=text, choice_id=choice,
                student_dispute=bool(draft and draft.dispute),
                dispute_reason=draft.dispute_reason if draft and draft.dispute else None,
                submitted_at=now)
            question.answer = answer
            db.add(answer)
            await db.flush()
        if answer.student_dispute:
            db.add(ReviewIssue(answer_id=answer.id, source="student_dispute",
                reason=answer.dispute_reason or "学生标记题目有疑问"))
    attempt.status = AttemptStatus.GRADING
    attempt.submitted_at = now
    attempt.submission_source = source
    attempt.timed_out = source == "timeout"


async def submit(db, *, quiz_id, student_number, session_id,
    payload: LightweightSubmitRequest) -> dict:
    attempt = await locked_attempt(db, payload.attempt_id, quiz_id, student_number, session_id)
    if attempt.submitted_at is not None:
        if attempt.submit_key == payload.idempotency_key or attempt.submission_source == "timeout":
            await db.commit()
            return {"submitted": True, "status": attempt.status.value, "source": attempt.submission_source}
        raise HTTPException(409, "本次作答已经交卷")
    now = datetime.now(timezone.utc)
    if attempt.status not in {AttemptStatus.IN_PROGRESS, AttemptStatus.EXPIRED}:
        raise HTTPException(409, "当前作答不能交卷")
    questions = {q.id: q for q in attempt.questions}
    drafts = {d.question_id: d for d in (await db.scalars(select(AnswerDraft)
        .where(AnswerDraft.attempt_id == attempt.id))).all()}
    if attempt.status == AttemptStatus.EXPIRED or not attempt.deadline_at or now >= ensure_utc(attempt.deadline_at):
        await freeze(db, attempt, source="timeout", now=ensure_utc(attempt.deadline_at) if attempt.deadline_at else now,
            drafts=drafts)
        await db.commit()
        return {"submitted": True, "status": "GRADING", "source": "timeout"}
    if len(payload.drafts) != len(questions) or {d.question_id for d in payload.drafts} != set(questions):
        raise HTTPException(422, "交卷必须包含本次全部题目的最终输入")
    for item in payload.drafts:
        question = questions[item.question_id]
        check_input(question, item)
        draft = drafts.get(item.question_id)
        if item.expected_revision != (draft.revision if draft else 0):
            raise HTTPException(409, detail={"message": "交卷草稿版本冲突", "question_id": str(item.question_id),
                "server_draft": draft_view(draft)})
    for item in payload.drafts:
        draft = drafts.get(item.question_id)
        if draft is None:
            draft = AnswerDraft(attempt_id=attempt.id, question_id=item.question_id)
            db.add(draft)
            drafts[item.question_id] = draft
        draft.answer_text = item.answer_text
        draft.choice_id = item.choice_id
        draft.revisit = item.revisit
        draft.dispute = item.dispute
        draft.dispute_reason = item.dispute_reason.strip() if item.dispute_reason else None
        draft.revision = item.expected_revision + 1
        draft.updated_at = now
    await freeze(db, attempt, source="manual", now=now, drafts=drafts)
    attempt.submit_key = payload.idempotency_key
    await db.commit()
    return {"submitted": True, "status": "GRADING", "source": "manual"}
