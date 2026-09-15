"""Read-only generation timing metadata; never export prompts or raw responses."""
import json
import re

from sqlalchemy import text


def safe_error(value):
    if not value:
        return None
    match = re.fullmatch(r"出题未完成（([A-Za-z][A-Za-z0-9_]{0,80})）", value)
    if match:
        return match.group(1)
    if value in ("Worker lease expired", "教师终止出题", "Late result ignored after cancellation or lease reassignment"):
        return value
    return "Other error (details withheld)"


async def latest_generation(db):
    quiz_id = await db.scalar(text("""
        SELECT q.id FROM quizzes q WHERE EXISTS
        (SELECT 1 FROM generation_jobs j JOIN generation_runs r ON r.job_id=j.id WHERE j.quiz_id=q.id)
        ORDER BY q.created_at DESC, q.id DESC LIMIT 1
    """))
    if quiz_id is None:
        return {"quiz_id": None, "runs": []}
    rows = (await db.execute(text("""
        SELECT j.id AS job_id, j.round_no, r.started_at, r.finished_at, r.state, r.error,
               r.model, r.prompt_version,
               row_number() OVER (PARTITION BY j.id ORDER BY r.started_at,r.id) AS run_no
        FROM generation_jobs j JOIN generation_runs r ON r.job_id=j.id
        WHERE j.quiz_id=:quiz_id ORDER BY r.started_at,r.id
    """), {"quiz_id": quiz_id})).mappings().all()
    runs = []
    for row in rows:
        item = dict(row)
        item['job_id'] = str(item['job_id'])
        item['seconds'] = ((row['finished_at']-row['started_at']).total_seconds()
                           if row['finished_at'] else None)
        item['started_at'] = row['started_at'].isoformat()
        item['finished_at'] = row['finished_at'].isoformat() if row['finished_at'] else None
        item['error'] = safe_error(row['error'])
        runs.append(item)
    return {"quiz_id": str(quiz_id), "runs": runs}
