"""Database-backed scheduling; all writers serialize on the quiz row."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.models import GenerationControl, Quiz, QuizStatus
from app.services.generation_service import progress
from app.time_utils import ensure_utc

logger = logging.getLogger(__name__)


async def set_schedule(db, quiz_id, scheduled_at):
    quiz = await db.scalar(select(Quiz).where(Quiz.id == quiz_id).with_for_update()
        .execution_options(populate_existing=True))
    if quiz is None:
        raise HTTPException(404, "测评不存在")
    if quiz.status != QuizStatus.DRAFT or not quiz.pre_generate or quiz.published_at is not None:
        raise HTTPException(409, "仅未开放的测评可以设置定时开放")
    if scheduled_at is not None:
        if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
            raise HTTPException(422, "开放时间必须包含时区")
        scheduled_at = scheduled_at.astimezone(timezone.utc)
        if scheduled_at <= datetime.now(timezone.utc):
            raise HTTPException(422, "请选择未来的开放时间")
    quiz.scheduled_open_at = scheduled_at
    quiz.scheduled_open_error = None
    await db.commit()
    return quiz


async def open_due_quizzes(db, *, now=None):
    # Same lock order as immediate open, generation and deletion.
    await db.execute(select(GenerationControl).where(GenerationControl.id == 1).with_for_update())
    now = now or datetime.now(timezone.utc)
    quizzes = (await db.scalars(select(Quiz).where(
        Quiz.status == QuizStatus.DRAFT, Quiz.scheduled_open_at <= now,
        Quiz.scheduled_open_error.is_(None),
    ).with_for_update(skip_locked=True).execution_options(populate_existing=True))).all()
    for quiz in quizzes:
        start = ensure_utc(quiz.scheduled_open_at)
        end = start + timedelta(minutes=quiz.entry_minutes or 30)
        if now >= end:
            quiz.scheduled_open_error = "预约进入窗口已结束，请重新设置时间或立即开放。"
        elif quiz.published_at is not None or not quiz.pre_generate:
            quiz.scheduled_open_error = "测评当前状态不允许开放。"
        elif not (await progress(db, quiz.id))["ready"]:
            quiz.scheduled_open_error = "到点时题目尚未全部就绪，请处理出题后重新预约或手动开放。"
        else:
            quiz.status = QuizStatus.PUBLISHED
            quiz.start_time, quiz.end_time = start, end
            quiz.scheduled_open_at = None
    await db.commit()


async def schedule_worker(session_factory):
    while True:
        try:
            async with session_factory() as db:
                await open_due_quizzes(db)
        except Exception as exc:
            logger.error("Scheduled opening failed (%s); retrying", type(exc).__name__)
        await asyncio.sleep(2)
