from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from pydantic import ValidationError, model_validator

from app.config import Settings
from app.schemas.llm import GradingResult, GradingAssessmentResult, QuestionGenerationResult


PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
GENERATOR_VERSION = "question_generator_v17"
GRADER_VERSION = "grader_v9"

KIND_RULES = {
    "trace": "type=trace。围绕具体实现给出可用少量步骤手工追踪的合法情境，明确所问位置和时刻，学生只交付结果。优先询问局部状态；询问最终输出时，也须依赖对具体实现的理解。",
    "boundary": "type=boundary。聚焦实际代码处理的一项合法边界，明确相关代码、触发条件和考查目标。问题可询问处理结果或原因，范围限于该边界的处理；泛泛罗列全部边界、证明整个程序正确或附加修改代码、完整执行追踪任务均不符合要求。",
    "modification": "type=modification。题干给出一个局部修改目标、允许修改的位置和必要限制，学生只交付一项可直接用于当前代码的小规模方案。目标须涉及代码理解，方案在范围内可实现、操作合法，并保持目标之外的行为。机械抄写和大范围重构不符合要求。效果分析、正确性证明和不变条件总结不作为作答要求。",
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


class MockLLMProvider(LLMProvider):
    model_name = "mock"

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
                user_prompt += "\n\n请重新生成完整的 JSON 对象：上一份响应未通过校验，请逐项核对规定的字段、类型和取值。"
        raise LLMProviderError(f"LLM response failed validation: {type(last_error).__name__}", raw_response=raw)

    async def generate_questions(
        self, *, title: str, statement: str, language: str, source_code: str, second_question_kind: str = "trace"
    ) -> tuple[QuestionGenerationResult, str]:
        schema = generation_schema(second_question_kind, source_code)
        system = (PROMPTS / f"{GENERATOR_VERSION}.txt").read_text(encoding="utf-8")
        system += f"\n\n## 本次第二问题型\nsecond_kind={second_question_kind}。" + KIND_RULES[second_question_kind]
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

输出格式：返回含 questions 数组的 JSON 对象，数组按 index=1、2 包含两题。
每题字段为 index、type、question（中文题干）、question_en（英文对照）、reference_answer（中文参考答案）、grading_points（1–3 个评分点）。
第一题 type=explanation；第二题 type={second_question_kind}，并额外包含 second_kind={second_question_kind}。
"""
        return await self._request_json(system, user, schema)

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

按系统规定的 JSON 协议评价以上全部题号；题号与这里提供的编号保持一致。
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
