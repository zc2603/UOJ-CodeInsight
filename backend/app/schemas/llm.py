from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import QuestionType


class GeneratedQuestion(BaseModel):
    index: int = Field(ge=1, le=2)
    type: QuestionType
    question: str = Field(min_length=1, max_length=500)
    question_en: str = Field(min_length=1, max_length=800)
    reference_answer: str = Field(min_length=1, max_length=5000)
    grading_points: list[str] = Field(min_length=1, max_length=3)

    @field_validator("grading_points")
    @classmethod
    def non_empty_points(cls, value: list[str]) -> list[str]:
        if any(not point.strip() for point in value):
            raise ValueError("grading points cannot be empty")
        return value


class QuestionGenerationResult(BaseModel):
    questions: list[GeneratedQuestion] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_structure(self) -> "QuestionGenerationResult":
        ordered = sorted(self.questions, key=lambda item: item.index)
        if [item.index for item in ordered] != [1, 2]:
            raise ValueError("question indexes must be 1 and 2")
        if ordered[0].type != QuestionType.EXPLANATION:
            raise ValueError("the first question must be explanation")
        if ordered[1].type not in (
            QuestionType.TRACE,
            QuestionType.BOUNDARY_OR_MODIFICATION,
        ):
            raise ValueError("the second question must be trace or boundary_or_modification")
        self.questions = ordered
        return self


class GradeItem(BaseModel):
    question_index: int = Field(ge=1)
    score: int
    reason: str = Field(min_length=1, max_length=5000)
    confidence: float = Field(ge=0, le=1)

    @field_validator("score")
    @classmethod
    def score_is_discrete(cls, value: int) -> int:
        if value not in (0, 1, 2):
            raise ValueError("score must be 0, 1, or 2")
        return value


class GradingResult(BaseModel):
    grades: list[GradeItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_indexes(self) -> "GradingResult":
        ordered = sorted(self.grades, key=lambda item: item.question_index)
        indexes = [item.question_index for item in ordered]
        if len(indexes) != len(set(indexes)):
            raise ValueError("grade indexes must be unique")
        self.grades = ordered
        return self
