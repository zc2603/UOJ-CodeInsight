from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from pydantic import ValidationError, model_validator

from app.config import Settings
from app.schemas.llm import GradingResult, GradingAssessmentResult, QuestionGenerationResult, LightweightGenerationResult, LightweightGradingResult


PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
GENERATOR_VERSION = "question_generator_v20"
GRADER_VERSION = "grader_v11"
LIGHTWEIGHT_GENERATOR_VERSION = "question_generator_lightweight_v1"
LIGHTWEIGHT_GRADER_VERSION = "grader_lightweight_v1"


def lightweight_generation_schema(kind: str | None):
    class Assigned(LightweightGenerationResult):
        @model_validator(mode="after")
        def check_assignment(self):
            expected = 2 if kind else 1
            if len(self.questions) != expected or (kind and self.questions[1].type != kind):
                raise ValueError("generated questions do not match frozen configuration")
            return self
    return Assigned

KIND_RULES = {
    "trace": "- 围绕学生代码中的一个具体机制，给出可用少量步骤手工追踪的合法小情境，优先询问明确执行位置的一个局部状态或结果。\n- 避免仅凭原题规则即可作答的最终输出题，以及大量算术或长序列模拟；\n- 只要求结果，不附加解释任务。\n- type=trace。",
    "boundary": "- 围绕学生代码处理的一项合法边界情境，明确相关代码、触发条件和考查目标，提出一个聚焦边界处理的具体问题。\n- 考查实际实现对该边界的处理，不泛泛要求罗列所有边界或证明整个程序正确，不附带修改代码或追踪完整执行过程的任务。\n- type=boundary。",
    "modification": "- 围绕学生代码中的一个具体机制，由题干明确局部修改目标、允许修改的位置和必要限制，要求给出一项可直接用于当前代码的小规模修改方案。\n- 目标应涉及代码理解，避免机械抄写或大范围重构；\n- 方案须在给定范围内可实现，只要求修改，不附加效果分析、正确性证明或总结不变条件。\n- type=modification。",
}


def number_source_lines(source_code: str) -> str:
    normalized = source_code.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(f"{index} | {line}" for index, line in enumerate(normalized.split("\n"), 1))


def generation_schema(kind, source_code=None):
    if kind not in KIND_RULES:
        raise ValueError("Unknown second question kind")

    class AssignedQuestions(QuestionGenerationResult):
        @model_validator(mode="after")
        def enforce_kind(self):
            second = self.questions[1]
            expected = kind
            if second.second_kind != kind or second.type.value != expected:
                raise ValueError("Second question must match the assigned kind")
            if re.search(r"=\s*\?\s*;|\bTODO\b|\bFIXME\b", second.reference_answer):
                raise ValueError("Reference answer contains unfinished code")
            if source_code is not None:
                headers = {re.sub(r"\s+", "", h) for h in re.findall(r"\bfor\s*\([^()]*\)", source_code)}
                for question in self.questions:
                    if question.type.value == "modification":
                        continue  # Deliberately modified code is allowed in this type.
                    for body in (question.question, question.question_en):
                        for header in re.findall(r"\bfor\s*\([^()]*\)", body):
                            if re.sub(r"\s+", "", header) not in headers:
                                raise ValueError("Quoted for-loop header differs from actual source")
            return self

    return AssignedQuestions


def score_assessments(assessment, raw, answers, confidence_threshold=0.85):
    grades = []
    if {grade.question_index for grade in assessment.grades} != set(answers):
        raise LLMProviderError("评分题号与提交不一致", raw_response=raw)
    for grade in assessment.grades:
        review_reasons = []
        if grade.validity != "valid":
            review_reasons.append("题目存在缺陷" if grade.validity == "invalid" else "题目有效性不确定")
            review_reasons.append(grade.validity_reason)
        if grade.scoring_uncertain:
            review_reasons.append("模型无法确定应给分数")
        if grade.confidence <= confidence_threshold:
            review_reasons.append(f"置信度 {grade.confidence:.2f} 不高于复核阈值 {confidence_threshold:.2f}")
        if grade.validity == "invalid" and grade.objection == "correct":
            score = 2
            reason = "准确指出题目实质性缺陷，按规则建议 2 分，待教师复核。" + grade.validity_reason
        else:
            verdicts = [unit.verdict for unit in grade.units]
            score = (2 if all(v == "correct" for v in verdicts) else int(any(v in ("correct", "partial") for v in verdicts))) if verdicts else grade.suggested_score
            reason = grade.reason
        if not answers[grade.question_index].strip():
            score, reason = 0, "未提供答案。"
        grades.append(dict(question_index=grade.question_index, score=score, reason=reason, confidence=grade.confidence,
            review_required=bool(review_reasons), review_reason="；".join(review_reasons) or None,
            question_validity=grade.validity))
    return GradingResult.model_validate({"grades": grades})


def encode_untrusted(value: object) -> str:
    """JSON-encode data and neutralize the prompt's structural tag characters."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


class LLMProviderError(RuntimeError):
    def __init__(self, message: str, raw_response: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response


class WhitespaceGradingError(LLMProviderError):
    def __init__(self, raw_response: str):
        super().__init__("评分涉及空白差异，已阻止自动扣分，请教师复核")
        self.raw_response = raw_response


def normalize_output_whitespace(value: str) -> str:
    # Preserve leading spaces, internal spaces and internal empty lines.
    lines = value.replace("\r\n", "\n").split("\n")
    lines = [line.rstrip(" \t") for line in lines]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def validate_whitespace_grades(result: GradingResult, raw: str) -> None:
    # Conservative review gate, not a semantic classifier or a full-score override.
    # Even legitimate internal-space deductions require teacher review here.
    for grade in result.grades:
        if grade.score < 2 and re.search(
            r"空白|空格|换行|行尾|行末|末尾空行|whitespace|\bspaces?\b|new\s*lines?|line[ -]?(?:break|ending|feed)s?|\bCRLF\b|\bLF\b",
            grade.reason, re.IGNORECASE,
        ):
            grade.review_required = True
            grade.review_reason = (grade.review_reason + "；" if grade.review_reason else "") + "评分理由涉及空白差异，需教师核对建议分"



class LLMProvider(ABC):
    model_name: str

    @abstractmethod
    async def generate_questions(
        self, *, title: str, statement: str, language: str, source_code: str, second_question_kind: str = "trace"
    ) -> tuple[QuestionGenerationResult, str]:
        raise NotImplementedError

    @abstractmethod
    async def grade_answers(
        self,
        *,
        title: str,
        statement: str,
        language: str,
        source_code: str,
        question_payload: list[dict[str, object]],
    ) -> tuple[GradingResult, str]:
        raise NotImplementedError

    async def generate_lightweight(self, **kwargs) -> tuple[LightweightGenerationResult, str]:
        raise NotImplementedError

    async def grade_lightweight(self, **kwargs) -> tuple[LightweightGradingResult, str]:
        raise NotImplementedError


class MockLLMProvider(LLMProvider):
    model_name = "mock"

    async def generate_lightweight(self, **kwargs) -> tuple[LightweightGenerationResult, str]:
        kind = kwargs.get("second_question_kind")
        questions = [dict(index=1, type="explanation", response_format="short_answer",
            question="代码中这个状态更新起什么作用？", question_en="What does this state update do?",
            core_idea="解释状态更新的局部作用", reference_answer="它更新当前状态以供下一步使用。")]
        if kind:
            questions.append(dict(index=2, type=kind, response_format="single_choice",
                question="按这段代码执行一步后，哪个状态正确？",
                question_en="Which state follows one step of this code?",
                choices=[dict(id=key, text=f"状态 {key}", text_en=f"State {key}") for key in "ABCD"],
                correct_choice_id="B", reference_answer="B 符合局部更新。"))
        raw = json.dumps(dict(schema_version="lightweight_v1", questions=questions), ensure_ascii=False)
        return lightweight_generation_schema(kind).model_validate_json(raw), raw

    async def grade_lightweight(self, **kwargs) -> tuple[LightweightGradingResult, str]:
        grades = [dict(question_index=q["question_index"], score=2 if len(str(q["student_answer"]).strip()) >= 10 else 1,
            reason="Mock 开发评分。", confidence=0.5) for q in kwargs["question_payload"]]
        raw = json.dumps(dict(grades=grades), ensure_ascii=False)
        return LightweightGradingResult.model_validate_json(raw), raw

    async def generate_questions(self, **_: str) -> tuple[QuestionGenerationResult, str]:
        kind = _.get("second_question_kind", "trace")
        raw = json.dumps(
            {
                "questions": [
                    {
                        "index": 1,
                        "type": "explanation",
                        "question": "请解释这份代码中一个关键函数的主要作用。",
                        "question_en": "Explain the main purpose of one key function in this code.",
                        "reference_answer": "应结合代码准确说明一个关键函数的主要作用。",
                        "grading_points": ["准确说明关键函数的作用"],
                    },
                    {
                        "index": 2,
                        "type": kind,
                        "second_kind": kind,
                        "question": "给定一个很小的合法输入，这个关键变量最终是什么值？",
                        "question_en": "For a very small valid input, what is the final value of this key variable?",
                        "reference_answer": "最终值必须与当前代码的实际执行结果一致。",
                        "grading_points": ["结果与代码一致", "说明关键变化"],
                    },
                ]
            },
            ensure_ascii=False,
        )
        return generation_schema(kind).model_validate_json(raw), raw

    async def grade_answers(self, **kwargs: object) -> tuple[GradingResult, str]:
        payload = kwargs["question_payload"]
        assert isinstance(payload, list)
        grades = []
        for item in payload:
            answer = str(item.get("student_answer", "")).strip()
            score = 2 if len(answer) >= 20 else 1 if answer else 0
            grades.append(
                {
                    "question_index": item["question_index"],
                    "score": score,
                    "reason": "Mock Provider 根据非空答案长度返回固定开发评分。",
                    "confidence": 0.5,
                }
            )
        raw = json.dumps({"grades": grades}, ensure_ascii=False)
        return GradingResult.model_validate_json(raw), raw


class OpenAICompatibleLLMProvider(LLMProvider):
    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required for the configured provider")
        self.settings = settings
        self.model_name = settings.llm_model
        self.semaphore = asyncio.Semaphore(settings.llm_max_concurrency)
        self.client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=settings.llm_timeout_seconds,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request_json(self, system_prompt: str, user_prompt: str, schema):
        last_error: Exception | None = None
        raw = None
        network_failures = 0
        schema_failures = 0
        while network_failures < 3 and schema_failures < 3:
            try:
                async with self.semaphore:
                    response = await self.client.post(
                        "chat/completions",
                        json={
                            "model": self.model_name,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "thinking": {"type": "enabled"},
                            "reasoning_effort": self.settings.llm_reasoning_effort,
                            "max_tokens": self.settings.llm_max_tokens,
                            "response_format": {"type": "json_object"},
                        },
                    )
                response.raise_for_status()
                body = response.json()
                raw = body["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                network_failures += 1
                if network_failures >= 3:
                    break
                await asyncio.sleep(2 ** (network_failures - 1))
                continue
            try:
                return schema.model_validate_json(raw), raw
            except ValidationError as exc:
                last_error = exc
                schema_failures += 1
                if schema_failures >= 3:
                    break
                user_prompt += "\n\n上一次输出未通过结构或类型校验。\n请遵守指定题型、字段和取值，只返回完整合法的 JSON。"
        raise LLMProviderError(f"LLM response failed validation: {type(last_error).__name__}", raw_response=raw)

    async def generate_questions(
        self, *, title: str, statement: str, language: str, source_code: str, second_question_kind: str = "trace"
    ) -> tuple[QuestionGenerationResult, str]:
        schema = generation_schema(second_question_kind, source_code)
        system = (PROMPTS / f"{GENERATOR_VERSION}.txt").read_text(encoding="utf-8")
        system += f"\n\n## 本次第二问题型\n本次第二问 second_kind={second_question_kind}。\n" + KIND_RULES[second_question_kind]
        user = f"""<PROBLEM>
标题（JSON 字符串）：{encode_untrusted(title)}
题面（JSON 字符串）：
{encode_untrusted(statement)}
</PROBLEM>

语言（JSON 字符串）：{encode_untrusted(language)}

<STUDENT_CODE>
源码按原始行编号（从 1 开始，含空行）；每行左侧的“行号 | ”仅用于定位，不属于代码。
{encode_untrusted(number_source_lines(source_code))}
</STUDENT_CODE>

输出对象必须含 questions 数组，严格为两题；
每题含 index、type、question、question_en、reference_answer、grading_points。
第二题额外含 second_kind，值必须为 {second_question_kind}。
"""
        return await self._request_json(system, user, schema)

    async def generate_lightweight(self, *, title: str, statement: str, language: str,
        source_code: str, second_question_kind: str | None) -> tuple[LightweightGenerationResult, str]:
        system = (PROMPTS / f"{LIGHTWEIGHT_GENERATOR_VERSION}.txt").read_text(encoding="utf-8")
        if second_question_kind:
            system += f"\n本次第二问的认知类型必须是 {second_question_kind}；不能换题型。"
        else:
            system += "\n本次仅生成第一道简答题。"
        user = (f"标题：{encode_untrusted(title)}\n题面：{encode_untrusted(statement)}\n"
            f"语言：{encode_untrusted(language)}\n编号源码：{encode_untrusted(number_source_lines(source_code))}\n"
            f"schema_version=lightweight_v1，问题数={2 if second_question_kind else 1}。")
        return await self._request_json(system, user, lightweight_generation_schema(second_question_kind))

    async def grade_lightweight(self, *, title: str, statement: str, language: str,
        source_code: str, question_payload: list[dict[str, object]]) -> tuple[LightweightGradingResult, str]:
        system = (PROMPTS / f"{LIGHTWEIGHT_GRADER_VERSION}.txt").read_text(encoding="utf-8")
        user = (f"标题：{encode_untrusted(title)}\n题面：{encode_untrusted(statement)}\n"
            f"语言：{encode_untrusted(language)}\n编号源码：{encode_untrusted(number_source_lines(source_code))}\n"
            f"待评分简答题：{encode_untrusted(question_payload)}")
        result, raw = await self._request_json(system, user, LightweightGradingResult)
        expected = {int(q["question_index"]) for q in question_payload}
        if {g.question_index for g in result.grades} != expected:
            raise LLMProviderError("简答评分题号不匹配", raw_response=raw)
        return result, raw

    async def grade_answers(
        self,
        *,
        title: str,
        statement: str,
        language: str,
        source_code: str,
        question_payload: list[dict[str, object]],
    ) -> tuple[GradingResult, str]:
        system = (PROMPTS / f"{GRADER_VERSION}.txt").read_text(encoding="utf-8")
        blocks = []
        for item in question_payload:
            blocks.append(
                f"""题号：{item['question_index']}
问题（JSON 字符串）：{encode_untrusted(item['question'])}
英文对照（JSON 字符串）：{encode_untrusted(item.get('question_en'))}
<STUDENT_ANSWER>
{encode_untrusted(normalize_output_whitespace(str(item['student_answer'])))}
</STUDENT_ANSWER>"""
            )
        user = f"""<PROBLEM>
标题（JSON 字符串）：{encode_untrusted(title)}
题面：
{encode_untrusted(statement)}
</PROBLEM>

语言（JSON 字符串）：{encode_untrusted(language)}
<STUDENT_CODE>
源码按原始行编号（从 1 开始，含空行）；每行左侧的“行号 | ”仅用于定位，不属于代码。
{encode_untrusted(number_source_lines(source_code))}
</STUDENT_CODE>

{chr(10).join(blocks)}

逐一评价以上全部题号，按系统规定的 JSON 协议返回；
每题必须有整数 suggested_score，即使题目有缺陷或评分不确定也不得留空。
"""
        assessment, raw = await self._request_json(system, user, GradingAssessmentResult)
        result = score_assessments(assessment, raw, {item['question_index']: str(item['student_answer']) for item in question_payload},
            self.settings.grading_review_confidence_threshold)
        validate_whitespace_grades(result, raw)
        return result, raw


def create_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider.lower() == "mock":
        return MockLLMProvider()
    return OpenAICompatibleLLMProvider(settings)
