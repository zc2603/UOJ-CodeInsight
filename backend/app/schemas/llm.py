from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import QuestionType


class GeneratedQuestion(BaseModel):
    second_kind: Literal["trace", "boundary", "modification"] | None = None
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
            QuestionType.BOUNDARY,
            QuestionType.MODIFICATION,
            QuestionType.BOUNDARY_OR_MODIFICATION,
        ):
            raise ValueError("the second question must be trace or boundary_or_modification")
        self.questions = ordered
        return self


class ChoiceOption(BaseModel):
    id: Literal["A", "B", "C", "D"]
    text: str = Field(min_length=1, max_length=1500)
    text_en: str = Field(min_length=1, max_length=2000)


class LightweightQuestion(BaseModel):
    index: int = Field(ge=1, le=2)
    type: Literal["explanation", "trace", "boundary", "modification"]
    response_format: Literal["short_answer", "single_choice"]
    question: str = Field(min_length=1, max_length=1000)
    question_en: str = Field(min_length=1, max_length=1500)
    reference_answer: str = Field(min_length=1, max_length=5000)
    core_idea: str | None = Field(default=None, max_length=1000)
    choices: list[ChoiceOption] | None = None
    correct_choice_id: Literal["A", "B", "C", "D"] | None = None

    @model_validator(mode="after")
    def check_format(self):
        if self.response_format == "short_answer":
            if self.index != 1 or self.type != "explanation" or not self.core_idea or self.choices is not None or self.correct_choice_id is not None:
                raise ValueError("short answer must be the first explanation with one core idea")
        else:
            if self.index != 2 or self.type == "explanation" or self.core_idea is not None:
                raise ValueError("choice must be the second cognitive question")
            if self.choices is None or [c.id for c in self.choices] != ["A", "B", "C", "D"] or self.correct_choice_id is None:
                raise ValueError("choice must have A/B/C/D and one answer key")
            if len({c.text.strip() for c in self.choices}) != 4 or len({c.text_en.strip() for c in self.choices}) != 4:
                raise ValueError("choice options must be distinct")
        return self


class LightweightGenerationResult(BaseModel):
    schema_version: Literal["lightweight_v1"]
    questions: list[LightweightQuestion] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def check_indexes(self):
        if [q.index for q in self.questions] != list(range(1, len(self.questions) + 1)):
            raise ValueError("question indexes must be contiguous")
        return self


class LightweightGradeItem(BaseModel):
    question_index: int = Field(ge=1)
    score: Literal[0, 1, 2]
    reason: str = Field(min_length=1, max_length=1000)
    student_dispute: bool = False
    dispute_reason: str | None = Field(default=None, max_length=1000)
    needs_teacher_review: bool = False
    review_reason: str | None = Field(default=None, max_length=1000)
    confidence: float = Field(ge=0, le=1)


class LightweightGradingResult(BaseModel):
    grades: list[LightweightGradeItem] = Field(min_length=1)

    @model_validator(mode="after")
    def check_unique(self):
        indexes = [g.question_index for g in self.grades]
        if len(indexes) != len(set(indexes)):
            raise ValueError("grade indexes must be unique")
        return self


class GradeItem(BaseModel):
    review_required: bool = False
    review_reason: str | None = None
    question_validity: Literal["valid", "invalid", "uncertain"] | None = None
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


class GradingUnit(BaseModel):
    criterion: str = Field(min_length=1, max_length=1000)
    expected: str = Field(min_length=1, max_length=5000)
    verdict: Literal["correct", "partial", "incorrect", "missing"]


class GradeAssessment(BaseModel):
    suggested_score: int = Field(ge=0, le=2, strict=True)
    scoring_uncertain: bool = False
    question_index: int = Field(ge=1)
    validity: Literal["valid", "invalid", "uncertain"]
    validity_reason: str = Field(min_length=1, max_length=5000)
    objection: Literal["correct", "incorrect", "none"]
    units: list[GradingUnit] = Field(max_length=3)
    reason: str = Field(min_length=1, max_length=5000)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_assessment(self):
        if self.validity == "valid" and self.objection == "correct":
            raise ValueError("valid questions cannot have a correct defect objection")
        if self.validity == "valid" and not self.units and not self.scoring_uncertain:
            raise ValueError("valid questions without reliable units require scoring uncertainty")
        return self


class GradingAssessmentResult(BaseModel):
    grades: list[GradeAssessment] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_indexes(self):
        indexes = [grade.question_index for grade in self.grades]
        if len(indexes) != len(set(indexes)):
            raise ValueError("grade indexes must be unique")
        return self
