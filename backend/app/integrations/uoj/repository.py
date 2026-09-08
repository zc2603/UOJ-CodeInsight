from __future__ import annotations

import json
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.integrations.uoj.schemas import (
    UOJContest,
    UOJProblem,
    UOJSubmission,
    UOJSubmissionRequirement,
)


class UOJNotFoundError(LookupError):
    pass


class UOJRepository:
    """The only component allowed to execute UOJ SQL.

    Every pooled connection is placed in MySQL's session-level read-only mode.
    The production DB account must additionally have SELECT-only grants.
    """

    def __init__(self, database_url: str, timezone_name: str = "Asia/Shanghai"):
        if not database_url:
            raise ValueError("UOJ_DATABASE_URL is required")
        self.timezone = ZoneInfo(timezone_name)
        self.engine: AsyncEngine = create_async_engine(
            database_url,
            # SQLAlchemy 2.0's aiomysql adapter and PyMySQL 1.2 disagree on the
            # ping(reconnect) signature. Recycling avoids stale connections
            # without invoking that incompatible pre-ping path.
            pool_pre_ping=False,
            pool_recycle=300,
            pool_size=5,
            max_overflow=5,
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def set_read_only(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            finally:
                cursor.close()

    async def close(self) -> None:
        await self.engine.dispose()

    def _as_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.timezone)
        return value.astimezone(timezone.utc)

    async def get_contest(self, contest_id: int) -> UOJContest:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT id, name, start_time, last_min, status "
                        "FROM contests WHERE id = :contest_id"
                    ),
                    {"contest_id": contest_id},
                )
            ).mappings().first()
        if row is None:
            raise UOJNotFoundError(f"contest {contest_id} does not exist")
        start = self._as_utc(row["start_time"])
        return UOJContest(
            contest_id=row["id"],
            name=row["name"],
            start_time=start,
            end_time=start + timedelta(minutes=row["last_min"]),
            status=row["status"],
        )

    async def verify_user_password(self, username: str, client_password_hash: str) -> bool:
        """Verify the client-side HMAC-MD5 used by this UOJ version."""
        if not re.fullmatch(r"[0-9a-fA-F]{32}", client_password_hash):
            return False
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT username, password, usergroup FROM user_info "
                        "WHERE username = :username"
                    ),
                    {"username": username},
                )
            ).mappings().first()
        if row is None or row["usergroup"] == "B":
            return False
        expected = hashlib.md5(
            (username + client_password_hash.lower()).encode("utf-8")
        ).hexdigest()
        return hmac.compare_digest(str(row["password"]), expected)

    async def get_contest_problem_ids(self, contest_id: int) -> list[int]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT problem_id FROM contests_problems "
                        "WHERE contest_id = :contest_id ORDER BY problem_id"
                    ),
                    {"contest_id": contest_id},
                )
            ).scalars().all()
        return list(rows)

    async def get_contest_students(self, contest_id: int) -> list[str]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT username FROM contests_registrants "
                        "WHERE contest_id = :contest_id ORDER BY username"
                    ),
                    {"contest_id": contest_id},
                )
            ).scalars().all()
        return list(rows)

    async def get_problem(self, problem_id: int) -> UOJProblem:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT p.id, p.title, p.submission_requirement, "
                        "pc.statement, pc.statement_md "
                        "FROM problems p LEFT JOIN problems_contents pc ON pc.id = p.id "
                        "WHERE p.id = :problem_id"
                    ),
                    {"problem_id": problem_id},
                )
            ).mappings().first()
        if row is None:
            raise UOJNotFoundError(f"problem {problem_id} does not exist")
        raw_requirements = json.loads(row["submission_requirement"] or "[]")
        return UOJProblem(
            problem_id=row["id"],
            title=row["title"],
            statement=row["statement_md"] or row["statement"] or "",
            submission_requirements=[
                UOJSubmissionRequirement.model_validate(item) for item in raw_requirements
            ],
        )

    async def get_contest_submissions(
        self, contest_id: int, cutoff: datetime | None = None
    ) -> list[UOJSubmission]:
        # cutoff is intentionally applied in Python too; one query reads the Contest once.
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, problem_id, contest_id, submit_time, submitter, "
                        "content, language, status, score, is_hidden "
                        "FROM submissions WHERE contest_id = :contest_id AND is_hidden = 0"
                    ),
                    {"contest_id": contest_id},
                )
            ).mappings().all()
        submissions = [
            UOJSubmission(
                submission_id=row["id"],
                problem_id=row["problem_id"],
                contest_id=row["contest_id"],
                submit_time=self._as_utc(row["submit_time"]),
                submitter=row["submitter"],
                content=row["content"],
                language=row["language"],
                status=row["status"],
                score=row["score"],
                is_hidden=bool(row["is_hidden"]),
            )
            for row in rows
        ]
        if cutoff is not None:
            cutoff_utc = cutoff.astimezone(timezone.utc)
            submissions = [item for item in submissions if item.submit_time <= cutoff_utc]
        return submissions
