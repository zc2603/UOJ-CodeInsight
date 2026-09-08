from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.uoj.schemas import UOJSubmission
from app.services.import_service import select_submission


def submission(submission_id: int, score: int | None, minute: int) -> UOJSubmission:
    return UOJSubmission(
        submission_id=submission_id,
        problem_id=1,
        contest_id=1,
        submit_time=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minute),
        submitter="231250001",
        content='{"file_name":"/submission/1/abc","config":[]}',
        language="C++",
        status="Judged",
        score=score,
        is_hidden=False,
    )


def test_selects_last_full_score() -> None:
    items = [
        submission(1, 20, 1),
        submission(2, 70, 2),
        submission(3, 100, 3),
        submission(4, 100, 4),
    ]
    assert select_submission(items).submission_id == 4


def test_selects_last_highest_partial_score() -> None:
    items = [
        submission(1, 20, 1),
        submission(2, 70, 2),
        submission(3, 70, 3),
    ]
    assert select_submission(items).submission_id == 3


def test_zero_and_null_scores_are_not_eligible() -> None:
    with pytest.raises(ValueError, match="positive-score"):
        select_submission([submission(1, None, 2), submission(2, 0, 1)])


def test_zero_score_is_ignored_when_positive_submission_exists() -> None:
    assert select_submission([submission(1, 0, 2), submission(2, 20, 1)]).submission_id == 2
