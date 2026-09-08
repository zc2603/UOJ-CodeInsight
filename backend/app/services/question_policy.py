"""Stable question allocation over a quiz's immutable submission snapshots."""
from sqlalchemy import select

from app.models import SubmissionSnapshot

SECOND_KINDS = ("trace", "boundary", "modification")


async def allocated_kind(db, snapshot):
    # UUID string ordering is explicit and identical on SQLite and PostgreSQL.
    # Reconstruct from frozen snapshots, not queue state or process-local counters.
    ids = (await db.execute(select(SubmissionSnapshot.id).where(
        SubmissionSnapshot.quiz_id == snapshot.quiz_id,
        SubmissionSnapshot.uoj_score > 0,
    ))).scalars().all()
    rank = sorted(str(value) for value in ids).index(str(snapshot.id))
    return SECOND_KINDS[rank % len(SECOND_KINDS)]
