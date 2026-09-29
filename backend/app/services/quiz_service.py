from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.config import get_settings
from app.models import (
    GenerationJob,
    Attempt,
    AttemptStatus,
    LLMCallLog,
    Question,
    Quiz,
    QuizParticipant,
    QuizParticipantStatus,
    QuizProblemSnapshot,
    QuizStatus,
    SubmissionSnapshot,
    AnswerDraft,
)
from app.schemas.api import (
    ContestPreviewResponse,
    ImportIssue,
    PreviewProblem,
    QuizCreateRequest,
    StudentQuestionResponse,
)
from app.security import generate_quiz_code, hash_secret
from app.services.import_service import ImportBundle
from app.services.llm_provider import LLMProvider, GENERATOR_VERSION
from app.services.question_policy import allocated_kind
from app.time_utils import ensure_utc
from app.schemas.llm import QuestionGenerationResult, LightweightGenerationResult
from app.services.generation_service import enqueue
from app.services.roster_service import select_roster
from app.services.teacher_settings import TeacherSettings, choice_defaults


def preview_from_bundle(bundle: ImportBundle, roster_text: str | None = None) -> ContestPreviewResponse:
    _, roster = select_roster(bundle, roster_text)
    ready_students = {student for student, _ in bundle.selected}
    return ContestPreviewResponse(
        roster=roster,
        contest_id=bundle.contest.contest_id,
        contest_name=bundle.contest.name,
        contest_start_time=bundle.contest.start_time,
        submission_cutoff=bundle.cutoff,
        cutoff_reached=datetime.now(timezone.utc) >= ensure_utc(bundle.cutoff),
        problems=[
            PreviewProblem(problem_id=item.problem_id, title=item.title,
                display_order=index, include_choice=(len(bundle.problems) < 3 or item.problem_id != max(p.problem_id for p in bundle.problems)))
            for index, item in enumerate(sorted(bundle.problems, key=lambda p: p.problem_id), 1)
        ],
        numeric_student_accounts=len(bundle.students),
        students_with_eligible_problem=len(ready_students),
        students_without_submission=len(bundle.students) - len(ready_students),
        selected_submission_snapshots=len(bundle.selected),
        parser_errors=bundle.issues,
    )


async def persist_quiz(
    db: AsyncSession, bundle: ImportBundle, request: QuizCreateRequest, defaults: TeacherSettings | None = None
) -> tuple[Quiz, str]:
    defaults = defaults or TeacherSettings()
    updates = {}
    for name, value in {"minutes_per_question": defaults.minutes_per_question,
        "time_mode": defaults.time_mode, "entry_minutes": defaults.entry_minutes,
        "reopen_minutes": defaults.reopen_minutes or defaults.entry_minutes}.items():
        if name not in request.model_fields_set:
            updates[name] = value
    if "duration_minutes" not in request.model_fields_set and updates.get("time_mode", request.time_mode) == "fixed":
        updates["duration_minutes"] = defaults.fixed_minutes
    request = request.model_copy(update=updates)
    if request.time_mode == "fixed" and request.duration_minutes is None:
        raise ValueError("固定总时长不能为空")
    bundle, roster = select_roster(bundle, request.roster_text)
    if roster is not None and not roster.matched_students:
        raise ValueError("名单中没有可参与的学生，请调整名单后再创建")
    now = datetime.now(timezone.utc)
    start_time = request.start_time or now
    end_time = request.end_time or (start_time + timedelta(minutes=request.entry_minutes))
    if start_time.tzinfo is None or end_time.tzinfo is None:
        raise ValueError("Quiz start and end times must include a timezone")
    if end_time <= start_time:
        raise ValueError("Quiz end time must be after start time")
    if bundle.issues:
        # Agreed policy: bad student/problem pairs are excluded, while explicit issues remain in Preview.
        pass
    settings = get_settings()
    version = request.assessment_version or ("lightweight_v1" if settings.lightweight_creation_enabled else "legacy")
    if version == "lightweight_v1" and not settings.lightweight_creation_enabled:
        raise ValueError("新测评协议尚未开放创建")
    problem_ids = {p.problem_id for p in bundle.problems}
    if version == "lightweight_v1" and not problem_ids:
        raise ValueError("本场 Contest 没有可测的原题")
    choice_ids = set(request.choice_problem_ids) if request.choice_problem_ids is not None else choice_defaults(problem_ids, defaults.question_template)
    if version == "lightweight_v1" and (not problem_ids or len(choice_ids) != len(request.choice_problem_ids or list(choice_ids)) or not choice_ids <= problem_ids):
        raise ValueError("问题安排必须使用本场原题且不能重复")
    max_count = len(problem_ids) + len(choice_ids)
    if version == "lightweight_v1" and request.time_mode == "per_question" and max_count * request.minutes_per_question > 180:
        raise ValueError("自动计算的个人时长超过 180 分钟，请调整")

    quiz_code = generate_quiz_code()
    quiz = Quiz(
        name=request.name or bundle.contest.name,
        uoj_contest_id=bundle.contest.contest_id,
        quiz_code_hash=hash_secret(quiz_code),
        start_time=start_time.astimezone(timezone.utc),
        end_time=end_time.astimezone(timezone.utc),
        duration_minutes=(request.duration_minutes or 6) if version == "legacy" else (request.duration_minutes or max_count * request.minutes_per_question),
        minutes_per_question=3 if version == "legacy" else request.minutes_per_question,
        question_mode="all_positive_2" if version == "legacy" else "lightweight_v1",
        assessment_version=version,
        entry_minutes=request.entry_minutes,
        reopen_minutes=request.reopen_minutes,
        grade_bands=[b.model_dump() for b in defaults.grade_bands],
        time_mode="per_question" if version == "legacy" else request.time_mode,
        submission_cutoff=bundle.cutoff.astimezone(timezone.utc),
        status=QuizStatus.DRAFT,
        pre_generate=True,
        show_score_after_finish=request.show_score_after_finish,
    )
    db.add(quiz)
    await db.flush()

    problem_map: dict[int, QuizProblemSnapshot] = {}
    for order, problem in enumerate(sorted(bundle.problems, key=lambda p: p.problem_id), 1):
        snapshot = QuizProblemSnapshot(
            quiz_id=quiz.id,
            uoj_problem_id=problem.problem_id,
            title=problem.title,
            statement=problem.statement,
            display_order=order,
            include_choice=True if version == "legacy" else problem.problem_id in choice_ids,
        )
        db.add(snapshot)
        problem_map[problem.problem_id] = snapshot
    await db.flush()

    selected_by_student: dict[str, list[tuple[int, object]]] = {}
    for (student, problem_id), imported in bundle.selected.items():
        selected_by_student.setdefault(student, []).append((problem_id, imported))

    for student in bundle.students:
        eligible = selected_by_student.get(student, [])
        participant = QuizParticipant(
            quiz_id=quiz.id,
            student_number=student,
            eligible_problem_count=len(eligible),
            status=(
                QuizParticipantStatus.READY
                if eligible
                else QuizParticipantStatus.NO_ELIGIBLE_SUBMISSION
            ),
        )
        db.add(participant)
        await db.flush()
        for problem_id, imported_obj in eligible:
            imported = imported_obj  # type narrowing for the dataclass value
            submission = imported.submission  # type: ignore[attr-defined]
            db.add(
                SubmissionSnapshot(
                    quiz_id=quiz.id,
                    participant_id=participant.id,
                    problem_snapshot_id=problem_map[problem_id].id,
                    uoj_submission_id=submission.submission_id,
                    uoj_problem_id=submission.problem_id,
                    source_code=imported.source_code,  # type: ignore[attr-defined]
                    language=submission.language,
                    uoj_score=submission.score,
                    uoj_submit_time=submission.submit_time,
                )
            )
    await db.flush()
    snapshots = (await db.execute(select(SubmissionSnapshot).where(SubmissionSnapshot.quiz_id == quiz.id))).scalars().all()
    await enqueue(db, snapshots)
    await db.commit()
    await db.refresh(quiz)
    return quiz, quiz_code


async def _attempt_view(db: AsyncSession, attempt_id: uuid.UUID) -> StudentQuestionResponse:
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
    current = next((item for item in attempt.questions if item.answer is None), None)
    lightweight = attempt.assessment_version == "lightweight_v1"
    drafts = {}
    if lightweight:
        drafts = {d.question_id: d for d in (await db.scalars(select(AnswerDraft)
            .where(AnswerDraft.attempt_id == attempt.id))).all()}
    snapshot = current.submission_snapshot if current is not None else attempt.selected_submission
    same_problem_index = None
    if current is not None:
        same_problem_index = sum(
            1
            for item in attempt.questions
            if item.question_index <= current.question_index
            and item.submission_snapshot_id == current.submission_snapshot_id
        )
    return StudentQuestionResponse(
        assessment_version=attempt.assessment_version,
        duration_minutes=int((ensure_utc(attempt.deadline_at) - ensure_utc(attempt.started_at)).total_seconds() // 60)
            if attempt.deadline_at and attempt.started_at else None,
        questions=[dict(id=str(item.id), index=item.question_index, type=item.question_type.value,
            response_format=item.response_format, question=item.question_text, question_en=item.question_text_en,
            choices=item.choices_json, problem_id=item.submission_snapshot.uoj_problem_id,
            problem_title=item.submission_snapshot.problem.title, problem_statement=item.submission_snapshot.problem.statement,
            source_code=item.submission_snapshot.source_code, language=item.submission_snapshot.language,
            draft=dict(answer_text=drafts[item.id].answer_text, choice_id=drafts[item.id].choice_id,
                revisit=drafts[item.id].revisit, dispute=drafts[item.id].dispute,
                dispute_reason=drafts[item.id].dispute_reason, revision=drafts[item.id].revision)
                if item.id in drafts else dict(answer_text="", choice_id=None, revisit=False, dispute=False,
                    dispute_reason=None, revision=0))
            for item in attempt.questions] if lightweight and attempt.status == AttemptStatus.IN_PROGRESS else [],
        attempt_id=attempt.id,
        server_time=datetime.now(timezone.utc),
        draft_revision=attempt.draft_revision,
        timed_out=attempt.timed_out or attempt.status == AttemptStatus.EXPIRED,
        draft_answer=(attempt.draft_text or "") if current and current.question_index == attempt.draft_question_index else "",
        status=attempt.status,
        problem_id=snapshot.uoj_problem_id,
        problem_title=snapshot.problem.title,
        problem_statement=snapshot.problem.statement,
        source_code=snapshot.source_code,
        language=snapshot.language,
        deadline_at=attempt.deadline_at,
        question_index=current.question_index if current else None,
        question_count=len(attempt.questions),
        problem_question_index=same_problem_index,
        question_type=current.question_type if current else None,
        question_text=current.question_text if current else None,
        question_text_en=current.question_text_en if current else None,
    )


async def start_attempt(
    db: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    *,
    quiz_id: uuid.UUID,
    student_number: str,
    session_id: str,
) -> StudentQuestionResponse:
    participant = (
        await db.execute(
            select(QuizParticipant)
            .where(
                QuizParticipant.quiz_id == quiz_id,
                QuizParticipant.student_number == student_number,
            )
            .with_for_update()
            .options(selectinload(QuizParticipant.quiz))
        )
    ).scalar_one_or_none()
    if participant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "你不属于本次测评")
    if participant.status != QuizParticipantStatus.READY:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "本次 Contest 中没有可用于代码抽查的提交，请联系教师。",
        )
    latest = (
        await db.execute(
            select(Attempt)
            .where(Attempt.participant_id == participant.id)
            .order_by(Attempt.attempt_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None and latest.status != AttemptStatus.RESET:
        if latest.session_id != session_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "该学号已在另一台设备开始测评")
        if latest.status == AttemptStatus.PREPARING:
            raise HTTPException(status.HTTP_409_CONFLICT, "题目正在生成，请稍后刷新")
        return await _attempt_view(db, latest.id)

    now = datetime.now(timezone.utc)
    quiz = participant.quiz
    if quiz.published_at is not None:
        raise HTTPException(409, "成绩已公布，不能开始新作答")
    quiz_start = ensure_utc(quiz.start_time)
    entry_deadline = ensure_utc(quiz.end_time)
    if quiz.status != QuizStatus.PUBLISHED or now < quiz_start or now >= entry_deadline:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "本次测评的进入时间已结束")

    # Recheck the score at selection time as well as import time. This protects
    # Quizzes snapshotted before zero-score submissions were made ineligible.
    snapshots = list(
        (
            await db.execute(
                select(SubmissionSnapshot).where(
                    SubmissionSnapshot.participant_id == participant.id,
                    SubmissionSnapshot.uoj_score.is_not(None),
                    SubmissionSnapshot.uoj_score > 0,
                ).order_by(SubmissionSnapshot.uoj_problem_id)
                .options(selectinload(SubmissionSnapshot.problem))
            )
        ).scalars().all()
    )
    participant.eligible_problem_count = len(snapshots)
    if not snapshots:
        participant.status = QuizParticipantStatus.NO_ELIGIBLE_SUBMISSION
        participant.assigned_submission_snapshot_id = None
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "本次 Contest 中没有得分大于 0 的可用提交，请联系教师。",
        )

    if quiz.pre_generate:
        round_no = await db.scalar(select(func.max(GenerationJob.round_no)).where(
            GenerationJob.participant_id == participant.id))
        if round_no is None or (latest is not None and round_no <= latest.attempt_no):
            raise HTTPException(409, "你的问题尚未准备完成，请联系教师")
        jobs = (await db.execute(select(GenerationJob).where(
            GenerationJob.participant_id == participant.id, GenerationJob.round_no == round_no)
            .order_by(GenerationJob.submission_snapshot_id))).scalars().all()
        by_snapshot = {job.submission_snapshot_id: job for job in jobs}
        if len(jobs) != len(snapshots) or any(
            snapshot.id not in by_snapshot or by_snapshot[snapshot.id].state != "succeeded" for snapshot in snapshots
        ):
            raise HTTPException(409, "你的问题尚未准备完成，请稍后再试或联系教师")
        prepared = []
        for snapshot in snapshots:
            job = by_snapshot[snapshot.id]
            if quiz.assessment_version == "lightweight_v1":
                generated = LightweightGenerationResult.model_validate(job.result_json)
                expected = 2 if snapshot.problem.include_choice else 1
                if len(generated.questions) != expected or (expected == 2 and generated.questions[1].type != job.second_kind):
                    raise HTTPException(409, "预生成题目结构与冻结配置不一致")
            else:
                generated = QuestionGenerationResult.model_validate(job.result_json)
            prepared.append((snapshot, job, generated))
        participant.assigned_submission_snapshot_id = snapshots[0].id
        attempt = Attempt(quiz_id=quiz.id, participant_id=participant.id, attempt_no=round_no,
            selected_submission_snapshot_id=snapshots[0].id, status=AttemptStatus.IN_PROGRESS,
            session_id=session_id, started_at=now, assessment_version=quiz.assessment_version,
            deadline_at=now + timedelta(minutes=(quiz.duration_minutes if quiz.assessment_version == "lightweight_v1" and quiz.time_mode == "fixed"
                else sum(len(generated.questions) for _, _, generated in prepared) * quiz.minutes_per_question)))
        db.add(attempt)
        await db.flush()
        index = 0
        for snapshot, job, generated in prepared:
            for item in generated.questions:
                index += 1
                db.add(Question(attempt_id=attempt.id, submission_snapshot_id=snapshot.id, question_index=index,
                    question_type=item.type, question_text=item.question, question_text_en=item.question_en,
                    reference_answer=item.reference_answer, grading_points_json=getattr(item, "grading_points", []),
                    response_format=getattr(item, "response_format", "short_answer"),
                    choices_json=[c.model_dump() for c in item.choices] if getattr(item, "choices", None) else None,
                    correct_choice_id=getattr(item, "correct_choice_id", None), core_idea=getattr(item, "core_idea", None),
                    generator_model=job.model, generator_prompt_version=job.prompt_version,
                    generator_raw_response=job.raw_response))
        await db.commit()
        return await _attempt_view(db, attempt.id)

    chosen = snapshots[0]
    participant.assigned_submission_snapshot_id = chosen.id

    attempt = Attempt(
        quiz_id=quiz.id,
        participant_id=participant.id,
        attempt_no=(latest.attempt_no + 1) if latest else 1,
        selected_submission_snapshot_id=chosen.id,
        status=AttemptStatus.PREPARING,
        assessment_version=quiz.assessment_version,
        session_id=session_id,
    )
    db.add(attempt)
    await db.commit()

    snapshots = list(
        (
            await db.execute(
                select(SubmissionSnapshot)
                .where(SubmissionSnapshot.id.in_([item.id for item in snapshots]))
                .order_by(SubmissionSnapshot.uoj_problem_id)
                .options(selectinload(SubmissionSnapshot.problem))
            )
        ).scalars().all()
    )
    kinds = {snapshot.id: await allocated_kind(db, snapshot) for snapshot in snapshots}
    # Release the read transaction while waiting for the external provider.
    await db.commit()
    remaining = settings.attempt_preparing_timeout_seconds - (
        datetime.now(timezone.utc) - ensure_utc(attempt.created_at)
    ).total_seconds()
    try:
        generated_sets = await asyncio.wait_for(asyncio.gather(
            *(
                provider.generate_questions(
                    title=snapshot.problem.title,
                    statement=snapshot.problem.statement,
                    language=snapshot.language,
                    source_code=snapshot.source_code,
                    second_question_kind=kinds[snapshot.id],
                )
                for snapshot in snapshots
            )
        ), timeout=max(0, remaining))
    except Exception as exc:
        failed_attempt = (
            await db.execute(select(Attempt).where(Attempt.id == attempt.id).with_for_update().execution_options(populate_existing=True))
        ).scalar_one()
        if failed_attempt.status == AttemptStatus.PREPARING:
            failed_attempt.status = AttemptStatus.RESET
        db.add(
            LLMCallLog(
                attempt_id=attempt.id,
                call_type="question_generation",
                model=provider.model_name,
                prompt_version=GENERATOR_VERSION,
                success=False,
                error=str(exc),
            )
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "题目生成服务暂时不可用，请联系教师。",
        ) from exc

    attempt = (
        await db.execute(select(Attempt).where(Attempt.id == attempt.id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one()
    if attempt.status != AttemptStatus.PREPARING:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "本次出题已取消，请重新进入测评")
    question_index = 0
    for snapshot, (generated, raw) in zip(snapshots, generated_sets, strict=True):
        for item in generated.questions:
            question_index += 1
            db.add(
                Question(
                    attempt_id=attempt.id,
                    submission_snapshot_id=snapshot.id,
                    question_index=question_index,
                    question_type=item.type,
                    question_text=item.question,
                    question_text_en=item.question_en,
                    reference_answer=item.reference_answer,
                    grading_points_json=item.grading_points,
                    generator_model=provider.model_name,
                    generator_prompt_version=GENERATOR_VERSION,
                    generator_raw_response=raw,
                )
            )
        db.add(
            LLMCallLog(
                attempt_id=attempt.id,
                call_type="question_generation",
                model=provider.model_name,
                prompt_version=GENERATOR_VERSION,
                success=True,
                raw_response=raw,
            )
        )
    started = datetime.now(timezone.utc)
    attempt.started_at = started
    attempt.deadline_at = started + timedelta(
        minutes=question_index * quiz.minutes_per_question
    )
    attempt.status = AttemptStatus.IN_PROGRESS
    await db.commit()
    return await _attempt_view(db, attempt.id)


async def reset_attempt(db: AsyncSession, attempt_id: uuid.UUID) -> None:
    existing = await db.get(Attempt, attempt_id)
    if existing is None:
        raise HTTPException(404, "Attempt 不存在")
    # Same lock order as start_attempt, preventing reset/start races.
    await db.execute(select(QuizParticipant).where(QuizParticipant.id == existing.participant_id).with_for_update())
    attempt = (await db.execute(select(Attempt).where(Attempt.id == attempt_id).with_for_update()
        .execution_options(populate_existing=True))).scalar_one()
    if attempt.status == AttemptStatus.RESET:
        await db.commit()
        return
    latest_no = await db.scalar(select(func.max(Attempt.attempt_no)).where(Attempt.participant_id == attempt.participant_id))
    if attempt.attempt_no != latest_no:
        raise HTTPException(409, "只能重置最新一次作答")
    quiz = await db.get(Quiz, attempt.quiz_id)
    if quiz.published_at is not None:
        raise HTTPException(409, "成绩已公布，不能重置作答")
    if quiz.pre_generate and datetime.now(timezone.utc) >= ensure_utc(quiz.end_time):
        raise HTTPException(409, "进入时间已结束，请另建测评安排重新作答")
    attempt.status = AttemptStatus.RESET
    if quiz.pre_generate:
        snapshots = (await db.execute(select(SubmissionSnapshot).where(
            SubmissionSnapshot.participant_id == attempt.participant_id, SubmissionSnapshot.uoj_score > 0))).scalars().all()
        await enqueue(db, snapshots, round_no=attempt.attempt_no + 1)
    await db.commit()
