from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class UOJContest(BaseModel):
    contest_id: int
    name: str
    start_time: datetime
    end_time: datetime
    status: str


class UOJSubmissionRequirement(BaseModel):
    name: str
    type: str
    file_name: str


class UOJProblem(BaseModel):
    problem_id: int
    title: str
    statement: str
    submission_requirements: list[UOJSubmissionRequirement]


class UOJSubmission(BaseModel):
    submission_id: int
    problem_id: int
    contest_id: int
    submit_time: datetime
    submitter: str
    content: str
    language: str
    status: str
    score: int | None
    is_hidden: bool


class ParsedSubmissionContent(BaseModel):
    storage_path: str
    bucket: str
    random_id: str
    config: dict[str, str]
