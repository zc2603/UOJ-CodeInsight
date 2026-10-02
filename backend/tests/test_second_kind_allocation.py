"""Synthetic allocation checks; no real model or production data access."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_attempt_flow import db
from app.models import (GenerationControl, GenerationJob, Quiz, QuizParticipant,
    QuizParticipantStatus, QuizProblemSnapshot, QuizStatus, SubmissionSnapshot)
from app.services import generation_service as queue
from app.services.llm_provider import LIGHTWEIGHT_GENERATOR_VERSION


async def synthetic_snapshots(db, counts):
    now = datetime.now(timezone.utc)
    quiz = Quiz(name="Allocation", uoj_contest_id=1, quiz_code_hash="unused",
        start_time=now, end_time=now + timedelta(hours=1), duration_minutes=20,
        submission_cutoff=now, status=QuizStatus.DRAFT, assessment_version="lightweight_v1",
        pre_generate=True)
    db.add_all([quiz, GenerationControl(id=1)])
    await db.flush()
    problems = []
    for index in range(max(counts) + 1):
        problem = QuizProblemSnapshot(quiz_id=quiz.id, uoj_problem_id=index + 1,
            title=f"P{index + 1}", statement="synthetic", display_order=index + 1,
            include_choice=index < max(counts))
        db.add(problem)
        problems.append(problem)
    await db.flush()
    snapshots = []
    for student_index, count in enumerate(counts):
        participant = QuizParticipant(id=uuid.UUID(int=30 - student_index), quiz_id=quiz.id,
            student_number=f"23125000{student_index + 1}", eligible_problem_count=count + 1,
            status=QuizParticipantStatus.READY)
        db.add(participant)
        await db.flush()
        for index in range(count + 1):
            # Interleave UUIDs across students, opposite to participant UUID order.
            problem = problems[index] if index < count else problems[-1]
            snapshot = SubmissionSnapshot(id=uuid.UUID(int=100 + index * 10 + student_index),
                quiz_id=quiz.id, participant_id=participant.id, problem_snapshot_id=problem.id,
                uoj_submission_id=1000 + index * 10 + student_index,
                uoj_problem_id=problem.uoj_problem_id, source_code="int main() { return 0; }",
                language="C++", uoj_score=100, uoj_submit_time=now)
            db.add(snapshot)
            snapshots.append(snapshot)
    await db.commit()
    return quiz, snapshots


async def round_kinds(db, quiz_id, round_no):
    jobs = (await db.scalars(select(GenerationJob).where(
        GenerationJob.quiz_id == quiz_id, GenerationJob.round_no == round_no))).all()
    return {job.submission_snapshot_id: job.second_kind for job in jobs}


@pytest.mark.asyncio
@pytest.mark.parametrize("counts", [(2, 2, 2), (1, 2, 3), (4, 5, 6)])
async def test_student_diversity_and_quiz_balance(db, counts):
    quiz, snapshots = await synthetic_snapshots(db, counts)
    await queue.enqueue(db, list(reversed(snapshots)))
    await db.commit()
    kinds = await round_kinds(db, quiz.id, 1)
    all_choices = []
    for participant_id in {s.participant_id for s in snapshots}:
        values = [kinds[s.id] for s in snapshots if s.participant_id == participant_id
            and kinds[s.id] is not None]
        all_choices.extend(values)
        assert len(set(values)) == min(3, len(values))
        frequencies = [values.count(kind) for kind in ("trace", "boundary", "modification")]
        assert max(frequencies) - min(frequencies) <= 1
    frequencies = Counter(all_choices)
    assert max(frequencies.values()) - min(frequencies.values()) <= 1
    assert sum(kind is None for kind in kinds.values()) == len(counts)
    if counts == (2, 2, 2):
        # Participant UUID 28 comes first, even though its student number and snapshot UUIDs come last.
        assert kinds[uuid.UUID(int=102)] == "trace"
        assert kinds[uuid.UUID(int=112)] == "boundary"
        assert kinds[uuid.UUID(int=101)] == "modification"
        assert kinds[uuid.UUID(int=111)] == "trace"


@pytest.mark.asyncio
async def test_zero_score_does_not_take_a_choice_slot(db):
    quiz, snapshots = await synthetic_snapshots(db, (2, 2, 2))
    zero = next(s for s in snapshots if s.id == uuid.UUID(int=102))
    zero.uoj_score = 0
    await db.commit()
    await queue.enqueue(db, snapshots)
    await db.commit()
    kinds = await round_kinds(db, quiz.id, 1)
    assert kinds[zero.id] is None
    assert kinds[uuid.UUID(int=112)] == "trace"
    assert kinds[uuid.UUID(int=101)] == "boundary"


@pytest.mark.asyncio
async def test_new_assignment_survives_session_restart_and_single_problem_regeneration(db):
    quiz, snapshots = await synthetic_snapshots(db, (2, 2, 2))
    await queue.enqueue(db, snapshots)
    await db.commit()
    before = await round_kinds(db, quiz.id, 1)
    jobs = (await db.scalars(select(GenerationJob))).all()
    for job in jobs:
        job.state = "succeeded"
    await db.commit()
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as reopened:
        outcome = await queue.regenerate_prepared(reopened, quiz.id, "231250001", 1, problem_id=1)
        assert outcome == {"round_no": 2, "queued": 1}
        after = await round_kinds(reopened, quiz.id, 2)
        assert after == {s.id: before[s.id] for s in snapshots if s.participant_id == uuid.UUID(int=30)}
        other_jobs = (await reopened.scalars(select(GenerationJob).where(
            GenerationJob.quiz_id == quiz.id, GenerationJob.participant_id != uuid.UUID(int=30)))).all()
        assert all(job.round_no == 1 for job in other_jobs)


@pytest.mark.asyncio
async def test_existing_snapshot_order_assignment_stays_frozen_on_regeneration(db):
    quiz, snapshots = await synthetic_snapshots(db, (2, 2, 2))
    choices = sorted([s for s in snapshots if s.uoj_problem_id != 3], key=lambda s: str(s.id))
    old = {s.id: ("trace", "boundary", "modification")[index % 3]
        for index, s in enumerate(choices)}
    for snapshot in snapshots:
        db.add(GenerationJob(quiz_id=quiz.id, participant_id=snapshot.participant_id,
            submission_snapshot_id=snapshot.id, round_no=1, state="succeeded",
            second_kind=old.get(snapshot.id), prompt_version=LIGHTWEIGHT_GENERATOR_VERSION))
    await db.commit()
    expected = {s.id: old.get(s.id) for s in snapshots}
    # Enqueuing one student must use the existing frozen mapping, not a subset or new policy.
    target = [s for s in snapshots if s.participant_id == uuid.UUID(int=30)]
    await queue.enqueue(db, list(reversed(target)), round_no=2)
    await db.commit()
    assert await round_kinds(db, quiz.id, 2) == {s.id: expected[s.id] for s in target}
    await queue.enqueue(db, list(reversed(snapshots)), round_no=3)
    await db.commit()
    assert await round_kinds(db, quiz.id, 3) == expected
    assert await round_kinds(db, quiz.id, 1) == expected
