import io
import json
import zipfile

import pytest

from app.config import Settings
from app.integrations.uoj.archive_client import UOJArchiveError, UOJSubmissionArchiveClient
from app.integrations.uoj.content_parser import (
    UOJSubmissionContentError,
    UOJSubmissionContentParser,
)
from app.integrations.uoj.schemas import UOJProblem, UOJSubmissionRequirement


def problem() -> UOJProblem:
    return UOJProblem(
        problem_id=1,
        title="P",
        statement="S",
        submission_requirements=[
            UOJSubmissionRequirement(name="answer", type="source code", file_name="answer.code")
        ],
    )


def make_zip(name: str, content: bytes) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return target.getvalue()


def test_parses_observed_uoj_json() -> None:
    parsed = UOJSubmissionContentParser().parse(
        json.dumps(
            {
                "file_name": "/submission/1716/D5AUpkw9w9IgvTNEF4x0",
                "config": [["answer_language", "C++"], ["problem_id", "4"]],
            }
        )
    )
    assert parsed.bucket == "1716"
    assert parsed.random_id == "D5AUpkw9w9IgvTNEF4x0"
    assert parsed.config["answer_language"] == "C++"


@pytest.mark.parametrize(
    "path",
    ["../secret", "/submission/1/../secret", "https://example/submission/1/x", "/tmp/x"],
)
def test_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(UOJSubmissionContentError):
        UOJSubmissionContentParser().parse(json.dumps({"file_name": path, "config": []}))


def test_extracts_source() -> None:
    client = UOJSubmissionArchiveClient(Settings())
    assert client._extract_source(make_zip("answer.code", b"int main(){}\n"), problem()) == "int main(){}\n"


def test_rejects_zip_slip() -> None:
    client = UOJSubmissionArchiveClient(Settings())
    with pytest.raises(UOJArchiveError):
        client._extract_source(make_zip("../answer.code", b"bad"), problem())
