from __future__ import annotations

import re
import secrets
import uuid
import logging
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.api.dependencies import StudentPrincipal, require_student
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import GenerationJob, Attempt, AttemptStatus, Question, Quiz, QuizParticipant, QuizParticipantStatus, QuizStatus
from app.schemas.api import (
    AnswerSubmitRequest,
    StudentLoginRequest,
    StudentQuestionResponse,
    StudentResultResponse,
)
from app.security import create_token, decode_token, verify_secret
from app.services.grading_service import grade_attempt
from app.services.question_service import get_current_attempt, submit_answer
from app.services.quiz_service import _attempt_view, start_attempt
from app.time_utils import ensure_utc


router = APIRouter(tags=["student"])
logger = logging.getLogger(__name__)


@router.get("/api/quiz/{quiz_id}/login-options")
async def login_options(quiz_id: uuid.UUID, request: Request) -> dict[str, bool | str | None]:
    settings = get_settings()
    password_enabled = bool(
        settings.uoj_password_auth_enabled
        and settings.uoj_password_client_salt
        and getattr(request.app.state, "uoj_repository", None) is not None
    )
    return {
        "uoj_password_enabled": password_enabled,
        # UOJ exposes this salt in its own login page so the raw password can
        # be HMAC-MD5 transformed before crossing an HTTP connection.
        "uoj_password_client_salt": (
            settings.uoj_password_client_salt if password_enabled else None
        ),
        "backup_code_enabled": True,
    }


async def _grade_in_background(provider, attempt_id: uuid.UUID) -> None:
    """Grade after the submission response, using a session independent of the request."""
    async with SessionLocal() as db:
        try:
            await grade_attempt(db, provider, attempt_id)
        except Exception:
            # grade_attempt records GRADING_ERROR before re-raising. Keep the
            # exception out of the response lifecycle and retain it in logs.
            logger.exception("Background grading failed for attempt %s", attempt_id)


@router.post("/api/quiz/{quiz_id}/login")
async def login(
    quiz_id: uuid.UUID,
    payload: StudentLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    student_session: str | None = Cookie(default=None),
):
    settings = get_settings()
    if not re.fullmatch(settings.student_username_regex, payload.student_number):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "学号格式错误")
    participant = (
        await db.execute(
            select(QuizParticipant)
            .where(
                QuizParticipant.quiz_id == quiz_id,
                QuizParticipant.student_number == payload.student_number,
            )
            .options(selectinload(QuizParticipant.quiz))
        )
    ).scalar_one_or_none()
    valid_credential = False
    if participant is not None and payload.quiz_code:
        valid_credential = verify_secret(
            participant.quiz.quiz_code_hash, payload.quiz_code.upper()
        )
    elif participant is not None and payload.uoj_password_hash:
        repository = getattr(request.app.state, "uoj_repository", None)
        if (
            settings.uoj_password_auth_enabled
            and settings.uoj_password_client_salt
            and repository is not None
        ):
            try:
                valid_credential = await repository.verify_user_password(
                    payload.student_number,
                    payload.uoj_password_hash,
                )
            except SQLAlchemyError as exc:
                # Do not log SQL parameters, usernames, hashes or connection details.
                logger.warning("UOJ password verification unavailable: %s", type(exc).__name__)
                raise HTTPException(503, "UOJ 账号验证暂不可用，请改用备用测评码或联系教师") from None
    if participant is None or not valid_credential:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "用户名或凭据错误")

    if participant.quiz.pre_generate and participant.quiz.status == QuizStatus.DRAFT:
        raise HTTPException(403, "测评尚未开放，请等待教师通知")

    session_id: str | None = None
    if student_session:
        try:
            current = decode_token(student_session, "student")
            if current.get("quiz_id") == str(quiz_id) and current.get("sub") == payload.student_number:
                session_id = current.get("sid")
        except jwt.PyJWTError:
            pass

    now = datetime.now(timezone.utc)
    existing = None
    if session_id:
        existing = (
            await db.execute(
                select(Attempt).where(
                    Attempt.participant_id == participant.id,
                    Attempt.session_id == session_id,
                    Attempt.status != AttemptStatus.RESET,
                ).order_by(Attempt.attempt_no.desc()).limit(1)
            )
        ).scalar_one_or_none()
    if (
        existing is None
        and (now < ensure_utc(participant.quiz.start_time) or now >= ensure_utc(participant.quiz.end_time))
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "本次测评的进入时间已结束")
    if participant.status != QuizParticipantStatus.READY:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "本次 Contest 中没有可用于代码抽查的提交，请联系教师。",
        )

    if participant.quiz.pre_generate and existing is None:
        latest_round = await db.scalar(select(func.max(GenerationJob.round_no)).where(
            GenerationJob.participant_id == participant.id))
        states = (await db.execute(select(GenerationJob.state).where(
            GenerationJob.participant_id == participant.id,
            GenerationJob.round_no == latest_round))).scalars().all()
        if not states or len(states) != participant.eligible_problem_count or any(state != "succeeded" for state in states):
            raise HTTPException(409, "你的问题尚未全部准备完成，暂不能参加本场测评，请联系教师")

    session_id = session_id or secrets.token_urlsafe(24)
    token = create_token(
        payload.student_number,
        "student",
        quiz_id=str(quiz_id),
        sid=session_id,
    )
    response.set_cookie(
        "student_session",
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return {"ok": True, "quiz_name": participant.quiz.name, "pre_generated": participant.quiz.pre_generate}


@router.post("/api/quiz/{quiz_id}/start", response_model=StudentQuestionResponse)
async def start(
    quiz_id: uuid.UUID,
    request: Request,
    principal: StudentPrincipal = Depends(require_student),
    db: AsyncSession = Depends(get_db),
):
    if principal.quiz_id != quiz_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Session 不属于该 Quiz")
    return await start_attempt(
        db,
        get_settings(),
        request.app.state.llm_provider,
        quiz_id=quiz_id,
        student_number=principal.student_number,
        session_id=principal.session_id,
    )


@router.get("/api/attempt/current", response_model=StudentQuestionResponse)
async def current_attempt(
    principal: StudentPrincipal = Depends(require_student),
    db: AsyncSession = Depends(get_db),
):
    attempt = await get_current_attempt(
        db,
        quiz_id=principal.quiz_id,
        student_number=principal.student_number,
        session_id=principal.session_id,
    )
    if (
        attempt.status == AttemptStatus.IN_PROGRESS
        and attempt.deadline_at is not None
        and datetime.now(timezone.utc) >= ensure_utc(attempt.deadline_at)
    ):
        await db.execute(update(Attempt).where(
            Attempt.id == attempt.id,
            Attempt.status == AttemptStatus.IN_PROGRESS,
        ).values(status=AttemptStatus.EXPIRED))
        await db.commit()
        await db.refresh(attempt)
    return await _attempt_view(db, attempt.id)


@router.post("/api/attempt/current/answer")
async def answer(
    payload: AnswerSubmitRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    principal: StudentPrincipal = Depends(require_student),
    db: AsyncSession = Depends(get_db),
):
    result = await submit_answer(
        db,
        quiz_id=principal.quiz_id,
        student_number=principal.student_number,
        session_id=principal.session_id,
        question_index=payload.question_index,
        student_answer=payload.answer,
    )
    if isinstance(result, Attempt):
        background_tasks.add_task(
            _grade_in_background, request.app.state.llm_provider, result.id
        )
        return {"status": result.status, "submitted": True}
    return result


@router.get("/api/attempt/current/result", response_model=StudentResultResponse)
async def result(
    principal: StudentPrincipal = Depends(require_student),
    db: AsyncSession = Depends(get_db),
):
    attempt = await get_current_attempt(
        db,
        quiz_id=principal.quiz_id,
        student_number=principal.student_number,
        session_id=principal.session_id,
    )
    quiz = (
        await db.execute(select(Quiz).where(Quiz.id == principal.quiz_id))
    ).scalar_one()
    finished = attempt.status == AttemptStatus.FINISHED
    score_ready = finished and not attempt.review_required
    score = (
        attempt.manual_override_score
        if attempt.manual_override_score is not None
        else attempt.auto_score
    )
    question_count = await db.scalar(
        select(func.count(Question.id)).where(Question.attempt_id == attempt.id)
    )
    return StudentResultResponse(
        status=attempt.status,
        submitted=finished or attempt.status == AttemptStatus.GRADING_ERROR,
        score_visible=score_ready and quiz.show_score_after_finish,
        total_score=score if score_ready and quiz.show_score_after_finish else None,
        max_score=int(question_count or 0) * 2,
    )
