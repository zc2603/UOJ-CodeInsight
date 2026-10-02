"""Account defaults and immutable quiz policies."""
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from sqlalchemy import select

from app.models import AdminUser


class GradeBand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=12)
    minimum: int = Field(ge=0, le=400, strict=True)


def default_bands():
    return [GradeBand(label=label, minimum=minimum) for label, minimum in
        [("A+", 9), ("A", 7), ("B+", 5), ("B", 3), ("C", 1), ("D", 0)]]


class TeacherSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_minutes: int = Field(default=30, ge=1, le=1440, strict=True)
    reopen_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    time_mode: Literal["per_question", "fixed"] = "per_question"
    minutes_per_question: int = Field(default=4, ge=1, le=180, strict=True)
    fixed_minutes: int = Field(default=25, ge=1, le=180, strict=True)
    question_template: Literal["standard", "all_choice", "all_short"] = "standard"
    grade_bands: list[GradeBand] = Field(default_factory=default_bands, min_length=2, max_length=10)
    result_filter: Literal["all", "finished", "active", "attention", "pending"] = "all"
    result_sort: Literal["student", "status", "completed", "score"] = "student"
    result_columns: list[Literal["score", "grade", "confidence", "completed", "timeout"]] = Field(
        default_factory=lambda: ["score", "grade", "confidence", "timeout"])
    code_font_size: int = Field(default=13, ge=11, le=22, strict=True)
    code_wrap: bool = False
    english_expanded: bool = True
    appeal_window_days: int | None = Field(default=None, ge=1, le=365, strict=True)
    appeal_prompt: str = Field(default="请说明你认为需要重新检查的地方", min_length=1, max_length=500)

    @field_validator("appeal_prompt")
    @classmethod
    def validate_appeal_prompt(cls, value):
        if not value.strip():
            raise ValueError("申诉提示语不能为空")
        return value.strip()

    @model_validator(mode="after")
    def validate_bands(self):
        bands = self.grade_bands
        if bands[-1].minimum != 0 or any(a.minimum <= b.minimum for a, b in zip(bands, bands[1:])):
            raise ValueError("等级最低分须严格递减，最后一级须为 0")
        if len({b.label for b in bands}) != len(bands):
            raise ValueError("等级名称不能重复")
        if len(set(self.result_columns)) != len(self.result_columns):
            raise ValueError("显示列不能重复")
        return self


class SettingsUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    settings: TeacherSettings


async def load_settings(db, user_id):
    user = await db.get(AdminUser, user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "教师账号不可用")
    return TeacherSettings.model_validate(user.settings_json or {}), user.settings_revision or 0


async def save_settings(db, user_id, payload):
    user = await db.scalar(select(AdminUser).where(AdminUser.id == user_id).with_for_update()
        .execution_options(populate_existing=True))
    if user is None or not user.is_active:
        raise HTTPException(401, "教师账号不可用")
    if (user.settings_revision or 0) != payload.expected_revision:
        raise HTTPException(409, "设置已在其他页面更新，请重新加载后再保存")
    merged = {**(user.settings_json or {}), **payload.settings.model_dump(mode="json", exclude_unset=True)}
    effective = TeacherSettings.model_validate(merged)
    user.settings_json = effective.model_dump(mode="json")
    user.settings_revision = payload.expected_revision + 1
    await db.commit()
    return {"settings": effective, "revision": user.settings_revision}


def grade_for(score, bands=None):
    if score is None or score < 0:
        return None
    rules = bands or [b.model_dump() for b in default_bands()]
    return next((b["label"] for b in rules if score >= b["minimum"]), None)


def choice_defaults(problem_ids, template):
    ids = set(problem_ids)
    if template == "all_short":
        return set()
    if template == "all_choice" or len(ids) < 3:
        return ids
    return ids - {max(ids)}
