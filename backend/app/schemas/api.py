from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import AttemptStatus, QuestionType, QuizParticipantStatus, QuizStatus


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class QuizPreviewRequest(BaseModel):
    contest_id: int = Field(gt=0)
    submission_cutoff: datetime | None = None


class PreviewProblem(BaseModel):
    problem_id: int
    title: str


class ImportIssue(BaseModel):
    student_number: str
    problem_id: int
    submission_id: int | None = None
    error: str


class ContestPreviewResponse(BaseModel):
    contest_id: int
    contest_name: str
    contest_start_time: datetime
    submission_cutoff: datetime
    cutoff_reached: bool
    problems: list[PreviewProblem]
    numeric_student_accounts: int
    students_with_eligible_problem: int
    students_without_submission: int
    selected_submission_snapshots: int
    parser_errors: list[ImportIssue]


class QuizCreateRequest(BaseModel):
    contest_id: int = Field(gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=180)
    submission_cutoff: datetime | None = None
    show_score_after_finish: bool = True
    allow_before_cutoff: bool = False


class QuizCreatedResponse(BaseModel):
    id: uuid.UUID
    quiz_code: str
    status: QuizStatus


class PreparationProgress(BaseModel):
    total: int
    completed: int
    running: int
    queued: int
    failed: int
    students_total: int
    students_ready: int
    ready: bool


class QuizSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    uoj_contest_id: int
    start_time: datetime
    end_time: datetime
    duration_minutes: int
    status: QuizStatus
    pre_generated: bool = False
    preparation: PreparationProgress | None = None
    participant_count: int = 0
    finished_count: int = 0
    average_score: float | None = None


class StudentLoginRequest(BaseModel):
    student_number: str = Field(min_length=1, max_length=20)
    quiz_code: str | None = Field(default=None, min_length=8, max_length=32)
    uoj_password_hash: str | None = Field(
        default=None, min_length=32, max_length=32, pattern=r"^[0-9a-fA-F]{32}$"
    )

    @model_validator(mode="after")
    def one_credential(self) -> "StudentLoginRequest":
        if bool(self.quiz_code) == bool(self.uoj_password_hash):
            raise ValueError("provide exactly one login credential")
        return self


class StudentQuestionResponse(BaseModel):
    attempt_id: uuid.UUID
    status: AttemptStatus
    problem_id: int
    problem_title: str
    problem_statement: str
    source_code: str
    language: str
    deadline_at: datetime | None
    question_index: int | None
    question_count: int
    problem_question_index: int | None
    question_type: QuestionType | None
    question_text: str | None
    question_text_en: str | None


class AnswerSubmitRequest(BaseModel):
    question_index: int = Field(ge=1, le=200)
    answer: str = Field(min_length=1, max_length=5000)


class StudentResultResponse(BaseModel):
    status: AttemptStatus
    submitted: bool
    score_visible: bool
    total_score: int | None = None
    max_score: int


class ManualOverrideRequest(BaseModel):
    score: int = Field(ge=0, le=400)
    reason: str = Field(min_length=1, max_length=5000)


class ResultRow(BaseModel):
    review_required: bool = False
    prepared_problem_count: int = 0
    preparation_total: int = 0
    student_number: str
    participant_status: QuizParticipantStatus
    attempt_id: uuid.UUID | None
    attempt_status: AttemptStatus | None
    problem_count: int
    question_count: int
    auto_score: int | None
    manual_score: int | None
    final_score: int | None
    max_score: int
    final_percent: float | None
    confidence: float | None
