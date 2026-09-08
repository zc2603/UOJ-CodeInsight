from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enum_column(enum_type: type[enum.Enum]) -> Enum:
    return Enum(enum_type, native_enum=False, values_callable=lambda e: [x.value for x in e])


class QuizStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ACTIVE = "active"
    CLOSED = "closed"


class QuizParticipantStatus(str, enum.Enum):
    READY = "READY"
    NO_ELIGIBLE_SUBMISSION = "NO_ELIGIBLE_SUBMISSION"


class AttemptStatus(str, enum.Enum):
    PREPARING = "PREPARING"
    IN_PROGRESS = "IN_PROGRESS"
    GRADING = "GRADING"
    FINISHED = "FINISHED"
    EXPIRED = "EXPIRED"
    RESET = "RESET"
    GRADING_ERROR = "GRADING_ERROR"


class QuestionType(str, enum.Enum):
    EXPLANATION = "explanation"
    TRACE = "trace"
    BOUNDARY_OR_MODIFICATION = "boundary_or_modification"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="teacher")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Quiz(Base):
    __tablename__ = "quizzes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    uoj_contest_id: Mapped[int] = mapped_column(Integer, index=True)
    quiz_code_hash: Mapped[str] = mapped_column(String(255))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int] = mapped_column(Integer, default=8)
    minutes_per_question: Mapped[int] = mapped_column(Integer, default=3)
    question_mode: Mapped[str] = mapped_column(String(30), default="all_positive_2")
    submission_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[QuizStatus] = mapped_column(enum_column(QuizStatus), default=QuizStatus.PUBLISHED)
    pre_generate: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    show_score_after_finish: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    problems: Mapped[list[QuizProblemSnapshot]] = relationship(
        back_populates="quiz", cascade="all, delete-orphan"
    )
    participants: Mapped[list[QuizParticipant]] = relationship(
        back_populates="quiz", cascade="all, delete-orphan"
    )


class QuizProblemSnapshot(Base):
    __tablename__ = "quiz_problem_snapshots"
    __table_args__ = (UniqueConstraint("quiz_id", "uoj_problem_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quiz_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    uoj_problem_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)

    quiz: Mapped[Quiz] = relationship(back_populates="problems")


class QuizParticipant(Base):
    __tablename__ = "quiz_participants"
    __table_args__ = (UniqueConstraint("quiz_id", "student_number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quiz_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    student_number: Mapped[str] = mapped_column(String(20), index=True)
    eligible_problem_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[QuizParticipantStatus] = mapped_column(enum_column(QuizParticipantStatus))
    assigned_submission_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("submission_snapshots.id", use_alter=True), nullable=True
    )

    quiz: Mapped[Quiz] = relationship(back_populates="participants")
    submissions: Mapped[list[SubmissionSnapshot]] = relationship(
        back_populates="participant",
        cascade="all, delete-orphan",
        foreign_keys="SubmissionSnapshot.participant_id",
    )
    attempts: Mapped[list[Attempt]] = relationship(back_populates="participant")


class SubmissionSnapshot(Base):
    __tablename__ = "submission_snapshots"
    __table_args__ = (
        UniqueConstraint("quiz_id", "participant_id", "problem_snapshot_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quiz_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quiz_participants.id", ondelete="CASCADE"), index=True
    )
    problem_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quiz_problem_snapshots.id", ondelete="CASCADE")
    )
    uoj_submission_id: Mapped[int] = mapped_column(Integer)
    uoj_problem_id: Mapped[int] = mapped_column(Integer)
    source_code: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(30))
    uoj_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uoj_submit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    participant: Mapped[QuizParticipant] = relationship(
        back_populates="submissions", foreign_keys=[participant_id]
    )
    problem: Mapped[QuizProblemSnapshot] = relationship()


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("participant_id", "attempt_no"),
        Index("ix_attempt_participant_status", "participant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quiz_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quiz_participants.id", ondelete="CASCADE"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    selected_submission_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission_snapshots.id")
    )
    status: Mapped[AttemptStatus] = mapped_column(enum_column(AttemptStatus))
    session_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_override_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    participant: Mapped[QuizParticipant] = relationship(back_populates="attempts")
    selected_submission: Mapped[SubmissionSnapshot] = relationship()
    questions: Mapped[list[Question]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", order_by="Question.question_index"
    )


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("attempt_id", "question_index"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"), index=True)
    submission_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission_snapshots.id"), nullable=False, index=True
    )
    question_index: Mapped[int] = mapped_column(Integer)
    question_type: Mapped[QuestionType] = mapped_column(enum_column(QuestionType))
    question_text: Mapped[str] = mapped_column(Text)
    question_text_en: Mapped[str] = mapped_column(Text, nullable=False)
    reference_answer: Mapped[str] = mapped_column(Text)
    grading_points_json: Mapped[list[str]] = mapped_column(JSON)
    generator_model: Mapped[str] = mapped_column(String(100))
    generator_prompt_version: Mapped[str] = mapped_column(String(50))
    generator_raw_response: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    attempt: Mapped[Attempt] = relationship(back_populates="questions")
    submission_snapshot: Mapped[SubmissionSnapshot] = relationship()
    answer: Mapped[Answer | None] = relationship(
        back_populates="question", cascade="all, delete-orphan", uselist=False
    )


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), unique=True, index=True
    )
    student_answer: Mapped[str] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    auto_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grading_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    grader_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    grader_prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    grader_raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    question: Mapped[Question] = relationship(back_populates="answer")


class LLMCallLog(Base):
    __tablename__ = "llm_call_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"), index=True)
    call_type: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50))
    success: Mapped[bool] = mapped_column(Boolean)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GenerationControl(Base):
    """Singleton row serializes claims for a global, cross-worker limit."""
    __tablename__ = "generation_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        UniqueConstraint("submission_snapshot_id", "round_no"),
        Index("ix_generation_claim", "state", "available_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quiz_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quizzes.id", ondelete="CASCADE"), index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quiz_participants.id", ondelete="CASCADE"), index=True)
    submission_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submission_snapshots.id", ondelete="CASCADE"))
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(50), default="question_generator_v4")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class GenerationRun(Base):
    __tablename__ = "generation_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="running")
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50), default="question_generator_v4")
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
