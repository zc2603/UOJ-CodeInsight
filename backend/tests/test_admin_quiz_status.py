from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.admin import _effective_quiz_status
from app.models import AttemptStatus, QuizStatus


def test_published_quiz_is_closed_after_entry_deadline() -> None:
    now = datetime.now(timezone.utc)
    quiz = SimpleNamespace(
        status=QuizStatus.PUBLISHED,
        start_time=now - timedelta(hours=1),
        end_time=now - timedelta(minutes=30),
    )

    assert _effective_quiz_status(quiz, now) == QuizStatus.CLOSED


def test_quiz_remains_active_while_students_or_grader_are_working() -> None:
    now = datetime.now(timezone.utc)
    quiz = SimpleNamespace(
        status=QuizStatus.PUBLISHED,
        start_time=now - timedelta(hours=1),
        end_time=now - timedelta(minutes=30),
    )

    for status in (
        AttemptStatus.PREPARING,
        AttemptStatus.IN_PROGRESS,
        AttemptStatus.GRADING,
    ):
        attempts = [SimpleNamespace(status=status, deadline_at=now + timedelta(minutes=1), created_at=now)]
        assert _effective_quiz_status(quiz, now, attempts) == QuizStatus.ACTIVE


def test_published_quiz_is_open_only_during_entry_window() -> None:
    now = datetime.now(timezone.utc)
    quiz = SimpleNamespace(
        status=QuizStatus.PUBLISHED,
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(minutes=25),
    )

    assert _effective_quiz_status(quiz, now) == QuizStatus.PUBLISHED


def test_published_quiz_is_not_open_before_start() -> None:
    now = datetime.now(timezone.utc)
    quiz = SimpleNamespace(
        status=QuizStatus.PUBLISHED,
        start_time=now + timedelta(minutes=5),
        end_time=now + timedelta(minutes=35),
    )

    assert _effective_quiz_status(quiz, now) == QuizStatus.DRAFT


def test_abandoned_attempts_do_not_keep_quiz_active():
    now = datetime.now(timezone.utc)
    quiz = SimpleNamespace(status=QuizStatus.PUBLISHED,
        start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1))
    attempts = [
        SimpleNamespace(status=AttemptStatus.IN_PROGRESS, deadline_at=now),
        SimpleNamespace(status=AttemptStatus.IN_PROGRESS, deadline_at=None),
        SimpleNamespace(status=AttemptStatus.PREPARING, created_at=now - timedelta(hours=2)),
    ]
    assert _effective_quiz_status(quiz, now, attempts) == QuizStatus.CLOSED
