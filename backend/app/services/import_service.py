from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings
from app.integrations.uoj.archive_client import UOJSubmissionArchiveClient
from app.integrations.uoj.repository import UOJRepository
from app.integrations.uoj.schemas import UOJContest, UOJProblem, UOJSubmission
from app.schemas.api import ImportIssue


@dataclass
class ImportedSubmission:
    submission: UOJSubmission
    source_code: str


@dataclass
class ImportBundle:
    contest: UOJContest
    cutoff: datetime
    problems: list[UOJProblem]
    students: list[str]
    selected: dict[tuple[str, int], ImportedSubmission]
    issues: list[ImportIssue]


def select_submission(submissions: list[UOJSubmission]) -> UOJSubmission:
    eligible = [item for item in submissions if item.score is not None and item.score > 0]
    if not eligible:
        raise ValueError("at least one positive-score submission is required")
    full_score = [item for item in eligible if item.score == 100]
    if full_score:
        return max(full_score, key=lambda item: (item.submit_time, item.submission_id))
    return max(
        eligible,
        key=lambda item: (
            item.score if item.score is not None else -1,
            item.submit_time,
            item.submission_id,
        ),
    )


class ImportService:
    def __init__(
        self,
        settings: Settings,
        repository: UOJRepository,
        archive_client: UOJSubmissionArchiveClient,
    ):
        self.settings = settings
        self.repository = repository
        self.archive_client = archive_client
        self.student_pattern = re.compile(settings.student_username_regex)

    async def build_bundle(
        self,
        contest_id: int,
        cutoff: datetime | None = None,
        *,
        require_cutoff_reached: bool = True,
        snapshot_at_now_if_future: bool = False,
    ) -> ImportBundle:
        contest = await self.repository.get_contest(contest_id)
        effective_cutoff = cutoff or contest.end_time
        if effective_cutoff.tzinfo is None:
            raise ValueError("submission cutoff must include a timezone")
        now = datetime.now(timezone.utc)
        if effective_cutoff > now:
            if require_cutoff_reached:
                raise ValueError("正式导入只能在 submission cutoff 到达后执行")
            if snapshot_at_now_if_future:
                # An explicitly confirmed early Quiz is frozen at creation time. Keeping the
                # future Contest cutoff here would make the stored snapshot boundary misleading.
                effective_cutoff = now

        problem_ids, all_students, submissions = await asyncio.gather(
            self.repository.get_contest_problem_ids(contest_id),
            self.repository.get_contest_students(contest_id),
            self.repository.get_contest_submissions(contest_id, effective_cutoff),
        )
        problems = await asyncio.gather(
            *(self.repository.get_problem(problem_id) for problem_id in problem_ids)
        )
        problem_by_id = {problem.problem_id: problem for problem in problems}
        students = [name for name in all_students if self.student_pattern.fullmatch(name)]
        student_set = set(students)

        grouped: dict[tuple[str, int], list[UOJSubmission]] = defaultdict(list)
        for submission in submissions:
            if (
                submission.submitter in student_set
                and submission.problem_id in problem_by_id
                and not submission.is_hidden
                and submission.submit_time <= effective_cutoff
                and submission.score is not None
                and submission.score > 0
            ):
                grouped[(submission.submitter, submission.problem_id)].append(submission)

        chosen = {key: select_submission(items) for key, items in grouped.items()}
        selected: dict[tuple[str, int], ImportedSubmission] = {}
        issues: list[ImportIssue] = []
        semaphore = asyncio.Semaphore(8)

        async def load_one(key: tuple[str, int], submission: UOJSubmission) -> None:
            async with semaphore:
                try:
                    source = await self.archive_client.read_source(
                        submission, problem_by_id[submission.problem_id]
                    )
                except Exception as exc:  # converted to an explicit preview issue
                    issues.append(
                        ImportIssue(
                            student_number=key[0],
                            problem_id=key[1],
                            submission_id=submission.submission_id,
                            error=str(exc),
                        )
                    )
                else:
                    selected[key] = ImportedSubmission(submission=submission, source_code=source)

        await asyncio.gather(*(load_one(key, item) for key, item in chosen.items()))
        return ImportBundle(
            contest=contest,
            cutoff=effective_cutoff,
            problems=list(problems),
            students=students,
            selected=selected,
            issues=issues,
        )
