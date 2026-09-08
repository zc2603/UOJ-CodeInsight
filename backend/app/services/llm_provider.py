from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas.llm import GradingResult, QuestionGenerationResult


PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


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
            raise WhitespaceGradingError(raw)



class LLMProvider(ABC):
    model_name: str

    @abstractmethod
    async def generate_questions(
        self, *, title: str, statement: str, language: str, source_code: str
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
                        "type": "trace",
                        "question": "给定一个很小的合法输入，这个关键变量最终是什么值？",
                        "question_en": "For a very small valid input, what is the final value of this key variable?",
                        "reference_answer": "最终值必须与当前代码的实际执行结果一致。",
                        "grading_points": ["结果与代码一致", "说明关键变化"],
                    },
                ]
            },
            ensure_ascii=False,
        )
        return QuestionGenerationResult.model_validate_json(raw), raw

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
                user_prompt += "\n\n上一次输出未通过 JSON schema 校验。请只返回完整、合法的 JSON。"
        raise LLMProviderError(f"LLM response failed validation: {type(last_error).__name__}", raw_response=raw)

    async def generate_questions(
        self, *, title: str, statement: str, language: str, source_code: str
    ) -> tuple[QuestionGenerationResult, str]:
        system = (PROMPTS / "question_generator_v4.txt").read_text(encoding="utf-8")
        user = f"""<PROBLEM>
标题（JSON 字符串）：{encode_untrusted(title)}
题面（JSON 字符串）：
{encode_untrusted(statement)}
</PROBLEM>

语言（JSON 字符串）：{encode_untrusted(language)}

<STUDENT_CODE>
{encode_untrusted(source_code)}
</STUDENT_CODE>

输出对象必须含 questions 数组，严格为两题；每题含 index、type、question、question_en、reference_answer、grading_points。
"""
        return await self._request_json(system, user, QuestionGenerationResult)

    async def grade_answers(
        self,
        *,
        title: str,
        statement: str,
        language: str,
        source_code: str,
        question_payload: list[dict[str, object]],
    ) -> tuple[GradingResult, str]:
        system = (PROMPTS / "grader_v5.txt").read_text(encoding="utf-8")
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
{encode_untrusted(source_code)}
</STUDENT_CODE>

{chr(10).join(blocks)}

输出对象必须含 grades 数组，每项只含 question_index、score、reason、confidence。
"""
        result, raw = await self._request_json(system, user, GradingResult)
        validate_whitespace_grades(result, raw)
        return result, raw


def create_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider.lower() == "mock":
        return MockLLMProvider()
    return OpenAICompatibleLLMProvider(settings)
