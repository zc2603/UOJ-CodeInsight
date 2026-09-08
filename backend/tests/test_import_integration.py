from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.integrations.uoj.schemas import (
    UOJContest,
    UOJProblem,
    UOJSubmission,
    UOJSubmissionRequirement,
)
from app.services.import_service import ImportService
from app.services.import_service import ImportBundle
from app.services.quiz_service import persist_quiz
from app.schemas.api import QuizCreateRequest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from app.models.base import Base
from app.time_utils import ensure_utc


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


def make_submission(
    submission_id: int,
    student: str,
    problem_id: int,
    score: int,
    minute: int,
    *,
    hidden: bool = False,
) -> UOJSubmission:
    return UOJSubmission(
        submission_id=submission_id,
        problem_id=problem_id,
        contest_id=7,
        submit_time=NOW - timedelta(days=1) + timedelta(minutes=minute),
        submitter=student,
        content=f'{{"file_name":"/submission/{submission_id}/token","config":[]}}',
        language="C++",
        status="Judged",
        score=score,
        is_hidden=hidden,
    )


class FakeRepository:
    async def get_contest(self, contest_id: int) -> UOJContest:
        return UOJContest(
            contest_id=contest_id,
            name="Integration Contest",
            start_time=NOW - timedelta(days=2),
            end_time=NOW - timedelta(days=1),
            status="finished",
        )

    async def get_contest_problem_ids(self, contest_id: int) -> list[int]:
        return [1, 2]

    async def get_contest_students(self, contest_id: int) -> list[str]:
        return ["231250001", "231250002", "teacher"]

    async def get_problem(self, problem_id: int) -> UOJProblem:
        return UOJProblem(
            problem_id=problem_id,
            title=f"P{problem_id}",
            statement="statement",
            submission_requirements=[
                UOJSubmissionRequirement(
                    name="answer", type="source code", file_name="answer.code"
                )
            ],
        )

    async def get_contest_submissions(
        self, contest_id: int, cutoff: datetime
    ) -> list[UOJSubmission]:
        return [
            make_submission(1, "231250001", 1, 60, 1),
            make_submission(2, "231250001", 1, 100, 2),
            make_submission(3, "231250001", 1, 100, 3),
            make_submission(4, "231250001", 2, 80, 4),
            make_submission(5, "231250002", 1, 100, 5),
            make_submission(6, "teacher", 1, 100, 6),
            make_submission(7, "231250002", 2, 100, 7, hidden=True),
            make_submission(8, "231250002", 2, 0, 8),
        ]


class FakeArchiveClient:
    async def read_source(
        self, submission: UOJSubmission, problem: UOJProblem
    ) -> str:
        if submission.submission_id == 5:
            raise ValueError("broken archive")
        return f"// submission {submission.submission_id}\nint main(){{}}"


class FutureContestRepository(FakeRepository):
    observed_cutoff: datetime | None = None

    async def get_contest(self, contest_id: int) -> UOJContest:
        return UOJContest(
            contest_id=contest_id,
            name="Running Contest",
            start_time=datetime.now(timezone.utc) - timedelta(hours=1),
            end_time=datetime.now(timezone.utc) + timedelta(days=1),
            status="in progress",
        )

    async def get_contest_submissions(
        self, contest_id: int, cutoff: datetime
    ) -> list[UOJSubmission]:
        self.observed_cutoff = cutoff
        return []


@pytest.mark.asyncio
async def test_build_bundle_filters_selects_and_reports_archive_errors() -> None:
    service = ImportService(Settings(), FakeRepository(), FakeArchiveClient())  # type: ignore[arg-type]
    bundle = await service.build_bundle(7, NOW - timedelta(hours=12))

    assert bundle.students == ["231250001", "231250002"]
    assert bundle.selected[("231250001", 1)].submission.submission_id == 3
    assert bundle.selected[("231250001", 2)].submission.submission_id == 4
    assert ("231250002", 1) not in bundle.selected
    assert ("231250002", 2) not in bundle.selected
    assert len(bundle.issues) == 1
    assert bundle.issues[0].submission_id == 5


@pytest.mark.asyncio
async def test_running_contest_still_rejected_without_explicit_override() -> None:
    service = ImportService(
        Settings(), FutureContestRepository(), FakeArchiveClient()
    )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="submission cutoff"):
        await service.build_bundle(8)


@pytest.mark.asyncio
async def test_confirmed_early_import_freezes_snapshot_at_creation_time() -> None:
    repository = FutureContestRepository()
    service = ImportService(
        Settings(), repository, FakeArchiveClient()
    )  # type: ignore[arg-type]
    before = datetime.now(timezone.utc)

    bundle = await service.build_bundle(
        8,
        require_cutoff_reached=False,
        snapshot_at_now_if_future=True,
    )
    after = datetime.now(timezone.utc)

    assert before <= bundle.cutoff <= after
    assert repository.observed_cutoff == bundle.cutoff


@pytest.mark.asyncio
async def test_quiz_defaults_to_draft_with_provisional_times() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    contest = await FakeRepository().get_contest(7)
    bundle = ImportBundle(
        contest=contest,
        cutoff=contest.end_time,
        problems=[],
        students=[],
        selected={},
        issues=[],
    )
    before = datetime.now(timezone.utc)
    async with factory() as db:
        quiz, _code = await persist_quiz(db, bundle, QuizCreateRequest(contest_id=7))
    after = datetime.now(timezone.utc)

    assert quiz.status.value.lower() == "draft" and quiz.pre_generate
    assert quiz.name == "Integration Contest"
    assert before <= ensure_utc(quiz.start_time) <= after
    assert ensure_utc(quiz.end_time) - ensure_utc(quiz.start_time) == timedelta(minutes=30)
    assert quiz.minutes_per_question == 3
    assert quiz.question_mode == "all_positive_2"
    await engine.dispose()
