from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, load_only

from app.api.dependencies import AdminPrincipal, require_admin
from app.services.quality_audit import presentation as quality_presentation
from app.config import get_settings
from app.database import get_db
from app.models import (
    GenerationJob,
    GenerationControl,
    GenerationRun,
    Answer,
    LLMCallLog,
    QuizProblemSnapshot,
    AdminUser,
    Attempt,
    AttemptStatus,
    Question,
    Quiz,
    QuizParticipant,
    QuizStatus,
    SubmissionSnapshot,
)
from app.schemas.api import (
    AdminLoginRequest,
    ContestPreviewResponse,
    ManualOverrideRequest,
    QuizCreateRequest,
    QuizOpenRequest,
    RegeneratePreparedRequest,
    QuizCreatedResponse,
    QuizPreviewRequest,
    QuizSummary,
    ResultRow,
)
from app.security import create_token, generate_quiz_code, hash_secret, verify_secret
from app.services.grading_queue import enqueue_regrade
from app.services.quiz_service import persist_quiz, preview_from_bundle, reset_attempt
from app.time_utils import ensure_utc
from app.services.generation_service import open_quiz, reopen_quiz, regenerate_prepared, retry_failed, summarize, stop_preparation


router = APIRouter(prefix="/api/admin", tags=["admin"])


def _effective_quiz_status(
    quiz: Quiz, now: datetime, attempts: list[Attempt] | None = None
) -> QuizStatus:
    """Return the student-entry status at the time the dashboard is requested."""
    if quiz.status != QuizStatus.PUBLISHED:
        return quiz.status
    if now < ensure_utc(quiz.start_time):
        return QuizStatus.DRAFT
    if now >= ensure_utc(quiz.end_time):
        if any(
            attempt.status == AttemptStatus.GRADING
            or (attempt.status == AttemptStatus.IN_PROGRESS
                and attempt.deadline_at is not None
                and ensure_utc(attempt.deadline_at) > now)
            or (attempt.status == AttemptStatus.PREPARING
                and ensure_utc(attempt.created_at) > now - timedelta(
                    seconds=get_settings().attempt_preparing_timeout_seconds))
            for attempt in (attempts or [])
        ):
            return QuizStatus.ACTIVE
        return QuizStatus.CLOSED
    return QuizStatus.PUBLISHED


@router.post("/login")
async def login(
    payload: AdminLoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user = (
        await db.execute(select(AdminUser).where(AdminUser.username == payload.username))
    ).scalar_one_or_none()
    if user is None or not user.is_active or not verify_secret(user.password_hash, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    token = create_token(str(user.id), "admin", username=user.username)
    settings = get_settings()
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return {"username": user.username}


@router.post("/logout", dependencies=[Depends(require_admin)])
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("admin_session", path="/")
    return {"ok": True}


def _import_service(request: Request):
    service = getattr(request.app.state, "import_service", None)
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法读取 UOJ 比赛信息，请联系管理员")
    return service


@router.post(
    "/quizzes/preview-contest",
    response_model=ContestPreviewResponse,
    dependencies=[Depends(require_admin)],
)
async def preview_contest(payload: QuizPreviewRequest, request: Request):
    try:
        bundle = await _import_service(request).build_bundle(
            payload.contest_id,
            payload.submission_cutoff,
            require_cutoff_reached=False,
        )
        return preview_from_bundle(bundle, payload.roster_text)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"读取 UOJ 失败：{exc}") from exc


@router.post(
    "/quizzes",
    response_model=QuizCreatedResponse,
    dependencies=[Depends(require_admin)],
)
async def create_quiz(
    payload: QuizCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        bundle = await _import_service(request).build_bundle(
            payload.contest_id,
            payload.submission_cutoff,
            require_cutoff_reached=not payload.allow_before_cutoff,
            snapshot_at_now_if_future=payload.allow_before_cutoff,
        )
        quiz, code = await persist_quiz(db, bundle, payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return QuizCreatedResponse(id=quiz.id, quiz_code=code, status=quiz.status)


@router.get("/overview", dependencies=[Depends(require_admin)])
async def overview(db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    count = await db.scalar(select(func.count(func.distinct(QuizParticipant.student_number))))
    return {"student_count": count or 0}


@router.get(
    "/quizzes", response_model=list[QuizSummary], dependencies=[Depends(require_admin)]
)
async def list_quizzes(db: AsyncSession = Depends(get_db)) -> list[QuizSummary]:
    quizzes = (
        await db.execute(
            select(Quiz)
            .order_by(Quiz.created_at.desc())
            .options(
                selectinload(Quiz.participants)
                .selectinload(QuizParticipant.attempts)
                .selectinload(Attempt.questions)
                .selectinload(Question.answer)
            )
        )
    ).scalars().all()
    preparation_jobs = (await db.execute(select(GenerationJob.quiz_id, GenerationJob.participant_id, GenerationJob.round_no, GenerationJob.state))).all()
    jobs_by_quiz = {}
    for job in preparation_jobs:
        jobs_by_quiz.setdefault(job.quiz_id, []).append(job)
    result = []
    now = datetime.now(timezone.utc)
    for quiz in quizzes:
        attempts = [
            max(p.attempts, key=lambda a: a.attempt_no)
            for p in quiz.participants
            if p.attempts
        ]
        finished = [a for a in attempts if a.status == AttemptStatus.FINISHED and not a.review_required]
        scores = [
            a.manual_override_score if a.manual_override_score is not None else a.auto_score
            for a in finished
        ]
        percentages = [
            score / (2 * len(attempt.questions)) * 100
            for attempt, score in zip(finished, scores, strict=True)
            if score is not None and attempt.questions
        ]
        result.append(
            QuizSummary(
                id=quiz.id,
                name=quiz.name,
                uoj_contest_id=quiz.uoj_contest_id,
                start_time=quiz.start_time,
                end_time=quiz.end_time,
                duration_minutes=quiz.duration_minutes,
                status=_effective_quiz_status(quiz, now, attempts),
                pre_generated=quiz.pre_generate,
                preparation=summarize(jobs_by_quiz.get(quiz.id, [])) if quiz.pre_generate else None,
                participant_count=len(quiz.participants),
                finished_count=len(finished),
                average_score=(
                    sum(percentages) / len(percentages) if percentages else None
                ),
            )
        )
    return result


@router.delete("/quizzes/{quiz_id}", dependencies=[Depends(require_admin)])
async def delete_quiz(quiz_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    # Serialize with claims so no new generation job is claimed during deletion.
    await db.execute(select(GenerationControl).where(GenerationControl.id == 1).with_for_update())
    # Block student starts/resets before locking the parent (same participant-first order).
    await db.execute(select(QuizParticipant.id).where(QuizParticipant.quiz_id == quiz_id).with_for_update())
    await db.execute(select(Quiz.id).where(Quiz.id == quiz_id).with_for_update())
    await db.execute(select(Attempt.id).where(Attempt.quiz_id == quiz_id).with_for_update())
    attempts = select(Attempt.id).where(Attempt.quiz_id == quiz_id)
    questions = select(Question.id).where(Question.attempt_id.in_(attempts))
    jobs = select(GenerationJob.id).where(GenerationJob.quiz_id == quiz_id)
    # Explicit child-first deletion avoids PostgreSQL cascade-trigger ordering conflicts
    # between questions/attempts and their referenced submission snapshots.
    await db.execute(delete(Answer).where(Answer.question_id.in_(questions)))
    await db.execute(delete(LLMCallLog).where(LLMCallLog.attempt_id.in_(attempts)))
    await db.execute(delete(Question).where(Question.attempt_id.in_(attempts)))
    await db.execute(delete(Attempt).where(Attempt.quiz_id == quiz_id))
    await db.execute(delete(GenerationRun).where(GenerationRun.job_id.in_(jobs)))
    await db.execute(delete(GenerationJob).where(GenerationJob.quiz_id == quiz_id))
    await db.execute(update(QuizParticipant).where(QuizParticipant.quiz_id == quiz_id)
        .values(assigned_submission_snapshot_id=None))
    await db.execute(delete(SubmissionSnapshot).where(SubmissionSnapshot.quiz_id == quiz_id))
    await db.execute(delete(QuizParticipant).where(QuizParticipant.quiz_id == quiz_id))
    await db.execute(delete(QuizProblemSnapshot).where(QuizProblemSnapshot.quiz_id == quiz_id))
    result = await db.execute(delete(Quiz).where(Quiz.id == quiz_id))
    await db.commit()
    return {"deleted": result.rowcount > 0}


@router.post("/quizzes/{quiz_id}/open", dependencies=[Depends(require_admin)])
async def publish_quiz(quiz_id: uuid.UUID, payload: QuizOpenRequest = Body(default=QuizOpenRequest()), db: AsyncSession = Depends(get_db)):
    quiz = await open_quiz(db, quiz_id, confirm_partial=payload.confirm_partial)
    return {"status": quiz.status, "start_time": quiz.start_time, "end_time": quiz.end_time}


@router.post("/quizzes/{quiz_id}/reopen", dependencies=[Depends(require_admin)])
async def reopen_published_quiz(quiz_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    quiz = await reopen_quiz(db, quiz_id)
    return {"status": quiz.status, "start_time": quiz.start_time, "end_time": quiz.end_time}


@router.post("/quizzes/{quiz_id}/stop-preparation", dependencies=[Depends(require_admin)])
async def stop_quiz_preparation(quiz_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return {"cancelled": await stop_preparation(db, quiz_id)}


@router.post("/quizzes/{quiz_id}/retry-preparation", dependencies=[Depends(require_admin)])
async def retry_preparation(quiz_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return {"retried": await retry_failed(db, quiz_id)}


@router.post(
    "/quizzes/{quiz_id}/regenerate-code",
    response_model=QuizCreatedResponse,
    dependencies=[Depends(require_admin)],
)
async def regenerate_quiz_code(
    quiz_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> QuizCreatedResponse:
    quiz = (
        await db.execute(select(Quiz).where(Quiz.id == quiz_id).with_for_update())
    ).scalar_one_or_none()
    if quiz is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quiz 不存在")
    code = generate_quiz_code()
    quiz.quiz_code_hash = hash_secret(code)
    await db.commit()
    return QuizCreatedResponse(id=quiz.id, quiz_code=code, status=quiz.status)



async def _result_rows(db: AsyncSession, quiz_id: uuid.UUID) -> list[ResultRow]:
    participants = (
        await db.execute(
            select(QuizParticipant)
            .where(QuizParticipant.quiz_id == quiz_id)
            .order_by(QuizParticipant.student_number)
            .options(
                selectinload(QuizParticipant.attempts)
                .selectinload(Attempt.questions)
                .selectinload(Question.answer),
                selectinload(QuizParticipant.attempts)
                .selectinload(Attempt.questions)
                .selectinload(Question.submission_snapshot),
            )
        )
    ).scalars().all()
    jobs = (await db.execute(select(GenerationJob).options(load_only(GenerationJob.id, GenerationJob.participant_id,
            GenerationJob.round_no, GenerationJob.state, GenerationJob.quality_state, GenerationJob.quality_result,
            GenerationJob.quality_acknowledged, GenerationJob.quality_error, GenerationJob.quality_model, GenerationJob.quality_version))
        .where(GenerationJob.quiz_id == quiz_id))).scalars().all()
    latest_round = {}
    for job in jobs:
        latest_round[job.participant_id] = max(latest_round.get(job.participant_id, 0), job.round_no)
    prepared_counts, preparation_totals = {}, {}
    for job in jobs:
        if job.round_no == latest_round[job.participant_id]:
            preparation_totals[job.participant_id] = preparation_totals.get(job.participant_id, 0) + 1
            if job.state == "succeeded":
                prepared_counts[job.participant_id] = prepared_counts.get(job.participant_id, 0) + 1
    rows: list[ResultRow] = []
    for participant in participants:
        attempt = max(participant.attempts, key=lambda item: item.attempt_no, default=None)
        questions = sorted(attempt.questions, key=lambda q: q.question_index) if attempt else []
        confidences = [q.answer.confidence for q in questions if q.answer and q.answer.confidence is not None]
        auto = attempt.auto_score if attempt else None
        manual = attempt.manual_override_score if attempt else None
        final = manual if manual is not None else auto
        if attempt and attempt.review_required:
            final = None
        max_score = len(questions) * 2
        problem_count = len({q.submission_snapshot_id for q in questions})
        rows.append(
            ResultRow(
                quality_attention=any(quality_presentation(j, i, get_settings().quality_audit_confidence_threshold)["attention"]
                    for j in jobs if j.participant_id == participant.id and j.round_no == (
                        attempt.attempt_no if attempt and attempt.status != AttemptStatus.RESET else latest_round.get(participant.id))
                    for i in (1, 2)),
                review_required=bool(attempt and attempt.review_required),
                student_number=participant.student_number,
                participant_status=participant.status,
                attempt_id=attempt.id if attempt else None,
                attempt_status=attempt.status if attempt else None,
                timed_out=bool(attempt and (attempt.timed_out or attempt.status == AttemptStatus.EXPIRED)),
                prepared_problem_count=prepared_counts.get(participant.id, 0),
                preparation_total=preparation_totals.get(participant.id, 0),
                problem_count=problem_count if attempt else preparation_totals.get(participant.id, 0),
                question_count=len(questions) if attempt else prepared_counts.get(participant.id, 0) * 2,
                auto_score=auto,
                manual_score=manual,
                final_score=final,
                max_score=max_score,
                final_percent=(final / max_score * 100 if final is not None and max_score else None),
                confidence=(min(confidences) if confidences else None),
            )
        )
    return rows


@router.get(
    "/quizzes/{quiz_id}/results",
    response_model=list[ResultRow],
    dependencies=[Depends(require_admin)],
)
async def quiz_results(quiz_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _result_rows(db, quiz_id)


@router.get("/quizzes/{quiz_id}/students/{student_number}/prepared-questions", dependencies=[Depends(require_admin)])
async def prepared_questions(quiz_id: uuid.UUID, student_number: str, db: AsyncSession = Depends(get_db)):
    participant = await db.scalar(select(QuizParticipant).where(
        QuizParticipant.quiz_id == quiz_id, QuizParticipant.student_number == student_number))
    if participant is None:
        raise HTTPException(404, "学生不属于本场测评")
    round_no = await db.scalar(select(func.max(GenerationJob.round_no)).where(GenerationJob.participant_id == participant.id))
    if round_no is None:
        raise HTTPException(404, "暂无预生成问题")
    jobs = (await db.execute(select(GenerationJob, SubmissionSnapshot)
        .join(SubmissionSnapshot, GenerationJob.submission_snapshot_id == SubmissionSnapshot.id)
        .where(GenerationJob.participant_id == participant.id, GenerationJob.round_no == round_no)
        .options(selectinload(SubmissionSnapshot.problem))
        .order_by(SubmissionSnapshot.uoj_problem_id))).all()
    from app.schemas.llm import QuestionGenerationResult
    questions = []
    for job, snapshot in jobs:
        if job.state != "succeeded":
            continue
        generated = QuestionGenerationResult.model_validate(job.result_json)
        for item in generated.questions:
            questions.append(dict(quality=quality_presentation(job, item.index, get_settings().quality_audit_confidence_threshold), index=len(questions) + 1, type=item.type, question=item.question,
                question_en=item.question_en, reference_answer=item.reference_answer, grading_points=item.grading_points,
                problem=dict(id=snapshot.uoj_problem_id, title=snapshot.problem.title, statement=snapshot.problem.statement),
                source_code=snapshot.source_code, language=snapshot.language,
                student_answer=None, score=None, reason=None, confidence=None))
    return dict(student_number=student_number, round_no=round_no, prepared_problem_count=len(questions) // 2,
        preparation_total=len(jobs), questions=questions)


@router.post("/quizzes/{quiz_id}/students/{student_number}/regenerate-questions", dependencies=[Depends(require_admin)])
async def regenerate_prepared_questions(quiz_id: uuid.UUID, student_number: str,
    payload: RegeneratePreparedRequest, db: AsyncSession = Depends(get_db)):
    return await regenerate_prepared(db, quiz_id, student_number, payload.expected_round)


@router.get("/attempts/{attempt_id}", dependencies=[Depends(require_admin)])
async def attempt_detail(attempt_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    attempt = (
        await db.execute(
            select(Attempt)
            .where(Attempt.id == attempt_id)
            .options(
                selectinload(Attempt.selected_submission).selectinload(SubmissionSnapshot.problem),
                selectinload(Attempt.participant),
                selectinload(Attempt.questions).selectinload(Question.answer),
                selectinload(Attempt.questions)
                .selectinload(Question.submission_snapshot)
                .selectinload(SubmissionSnapshot.problem),
            )
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attempt 不存在")
    questions = sorted(attempt.questions, key=lambda item: item.question_index)
    quality_jobs = (await db.execute(select(GenerationJob).where(
        GenerationJob.participant_id == attempt.participant_id, GenerationJob.round_no == attempt.attempt_no))).scalars().all()
    quality_by_snapshot = {job.submission_snapshot_id: job for job in quality_jobs}
    quality_by_question = {}
    local_indices = {}
    for question in questions:
        local_indices[question.submission_snapshot_id] = local_indices.get(question.submission_snapshot_id, 0) + 1
        quality_by_question[question.id] = quality_presentation(quality_by_snapshot.get(question.submission_snapshot_id),
            local_indices[question.submission_snapshot_id], get_settings().quality_audit_confidence_threshold)
    max_score = len(questions) * 2
    final_score = (
        attempt.manual_override_score
        if attempt.manual_override_score is not None
        else attempt.auto_score
    )
    return {
        "id": str(attempt.id),
        "status": attempt.status,
        "student_number": attempt.participant.student_number,
        "auto_score": attempt.auto_score,
        "review_required": attempt.review_required,
        "max_score": max_score,
        "final_percent": final_score / max_score * 100 if final_score is not None and max_score and not attempt.review_required else None,
        "manual_override_score": attempt.manual_override_score,
        "manual_override_reason": attempt.manual_override_reason,
        "questions": [
            {
                "quality": quality_by_question[item.id],
                "index": item.question_index,
                "type": item.question_type,
                "question": item.question_text,
                "question_en": item.question_text_en,
                "problem": {
                    "id": item.submission_snapshot.uoj_problem_id,
                    "title": item.submission_snapshot.problem.title,
                    "statement": item.submission_snapshot.problem.statement,
                },
                "source_code": item.submission_snapshot.source_code,
                "language": item.submission_snapshot.language,
                "reference_answer": item.reference_answer,
                "grading_points": item.grading_points_json,
                "student_answer": item.answer.student_answer if item.answer else None,
                "score": item.answer.auto_score if item.answer else None,
                "reason": item.answer.grading_reason if item.answer else None,
                "confidence": item.answer.confidence if item.answer else None,
                "review_required": item.answer.review_required if item.answer else False,
                "review_reason": item.answer.review_reason if item.answer else None,
                "question_validity": item.answer.question_validity if item.answer else None,
            }
            for item in questions
        ],
    }


@router.post("/attempts/{attempt_id}/reset", dependencies=[Depends(require_admin)])
async def reset(attempt_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await reset_attempt(db, attempt_id)
    return {"ok": True}


@router.post("/attempts/{attempt_id}/regrade", dependencies=[Depends(require_admin)])
async def regrade(
    attempt_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
):
    try:
        attempt = await enqueue_regrade(db, attempt_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"ok": True, "status": attempt.status}


@router.patch("/attempts/{attempt_id}/score", dependencies=[Depends(require_admin)])
async def override_score(
    attempt_id: uuid.UUID,
    payload: ManualOverrideRequest,
    db: AsyncSession = Depends(get_db),
):
    attempt = (
        await db.execute(select(Attempt).where(Attempt.id == attempt_id).with_for_update())
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attempt 不存在")
    question_count = (
        await db.execute(select(Question).where(Question.attempt_id == attempt_id))
    ).scalars().all()
    max_score = len(question_count) * 2
    if payload.score > max_score:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"人工分不能超过本次测评满分 {max_score}",
        )
    attempt.manual_override_score = payload.score
    attempt.manual_override_reason = payload.reason
    attempt.review_required = False
    await db.commit()
    return {"ok": True, "final_score": payload.score}


@router.get("/quizzes/{quiz_id}/export", dependencies=[Depends(require_admin)])
async def export_csv(quiz_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rows = await _result_rows(db, quiz_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "student_number",
            "problem_count",
            "question_count",
            "auto_score",
            "manual_score",
            "final_score",
            "max_score",
            "final_percent",
            "status",
            "review_required",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.student_number,
                row.problem_count,
                row.question_count,
                row.auto_score,
                row.manual_score,
                row.final_score,
                row.max_score,
                row.final_percent,
                (row.attempt_status or row.participant_status).value,
                row.review_required,
            ]
        )
    data = "\ufeff" + output.getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="quiz-{quiz_id}.csv"'},
    )


@router.post("/quality-audits/{job_id}/{question_index}/acknowledge", dependencies=[Depends(require_admin)])
async def acknowledge_quality(job_id: uuid.UUID, question_index: int, db: AsyncSession = Depends(get_db)):
    job = await db.scalar(select(GenerationJob).where(GenerationJob.id == job_id).with_for_update())
    if job is None:
        raise HTTPException(404, "审核不存在")
    if question_index not in (1, 2) or job.quality_state not in ("done", "failed"):
        raise HTTPException(409, "当前审核不能标记已查看")
    job.quality_acknowledged = {**(job.quality_acknowledged or {}), str(question_index): datetime.now(timezone.utc).isoformat()}
    await db.commit()
    return {"ok": True}


@router.post("/quality-audits/{job_id}/request", dependencies=[Depends(require_admin)])
async def request_quality(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = await db.scalar(select(GenerationJob).where(GenerationJob.id == job_id).with_for_update())
    if not get_settings().quality_audit_enabled:
        raise HTTPException(409, "质量审核未启用")
    if job is None:
        raise HTTPException(404, "题目不存在")
    if job.state != "succeeded" or job.quality_state not in ("not_requested", "failed", "done"):
        raise HTTPException(409, "当前题目不能申请审核")
    job.quality_state = "queued"
    job.quality_attempts = 0
    job.quality_token = None
    job.quality_error = None
    job.quality_acknowledged = None
    await db.commit()
    return {"ok": True}

@router.get("/quality-audits/{job_id}/{question_index}", dependencies=[Depends(require_admin)])
async def quality_detail(job_id: uuid.UUID, question_index: int, db: AsyncSession = Depends(get_db)):
    job = await db.get(GenerationJob, job_id)
    if job is None or question_index not in (1, 2):
        raise HTTPException(404, "审核不存在")
    return quality_presentation(job, question_index, get_settings().quality_audit_confidence_threshold)
