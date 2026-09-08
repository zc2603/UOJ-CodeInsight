import re

import pytest

from app.config import Settings


@pytest.mark.parametrize("username", ["231250001", "20260001", "00123"])
def test_numeric_student_filter(username: str) -> None:
    assert re.fullmatch(Settings().student_username_regex, username)


@pytest.mark.parametrize("username", ["admin", "test1", "teacher"])
def test_non_students_are_excluded(username: str) -> None:
    assert not re.fullmatch(Settings().student_username_regex, username)
