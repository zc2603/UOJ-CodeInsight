"""Bound abandoned attempts without relying on incoming HTTP requests."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from app.models import Attempt, AttemptStatus, LLMCallLog

logger = logging.getLogger(__name__)


async def reconcile_attempts(db, settings, *, now=None):
    now = now or datetime.now(timezone.utc)
    expired = await db.execute(
        update(Attempt).where(
            Attempt.status == AttemptStatus.IN_PROGRESS,
            or_(Attempt.deadline_at <= now, Attempt.deadline_at.is_(None)),
        ).values(status=AttemptStatus.EXPIRED).execution_options(synchronize_session="fetch")
    )
    # Row locks and a status predicate make concurrent worker sweeps idempotent.
    stale = (await db.execute(
        select(Attempt).where(
            Attempt.status == AttemptStatus.PREPARING,
            Attempt.created_at <= now - timedelta(seconds=settings.attempt_preparing_timeout_seconds),
        ).with_for_update(skip_locked=True).execution_options(populate_existing=True)
    )).scalars().all()
    for attempt in stale:
        attempt.status = AttemptStatus.RESET
        db.add(LLMCallLog(
            attempt_id=attempt.id, call_type="question_generation",
            model=settings.llm_model, prompt_version="question_generator_v4",
            success=False, error="Preparation time limit exceeded; attempt reset by maintenance.",
        ))
    await db.commit()
    return expired.rowcount, len(stale)


async def maintain_attempts(session_factory, settings):
    while True:
        try:
            async with session_factory() as db:
                expired, reset = await reconcile_attempts(db, settings)
            if expired or reset:
                logger.info("Attempt maintenance: expired=%s reset=%s", expired, reset)
        except Exception as exc:
            # Do not log connection strings or other exception payloads.
            logger.error("Attempt maintenance failed (%s); retrying next interval", type(exc).__name__)
        await asyncio.sleep(settings.attempt_maintenance_interval_seconds)
