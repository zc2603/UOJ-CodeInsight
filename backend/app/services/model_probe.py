"""One small, bounded Chat Completions request per changed service/model."""
import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
from httpx import AsyncClient

from app.services.llm_provider import chat_payload

PROBE_TIMEOUT_SECONDS = 20
PROBE_MAX_TOKENS = 2048


def changed_services(previous, proposed):
    services = {name for name in ("deepseek", "openai")
        if getattr(previous, name + "_model") != getattr(proposed, name + "_model")}
    for field in ("generation_service", "grading_service"):
        if getattr(previous, field) != getattr(proposed, field):
            services.add(getattr(proposed, field))
    return sorted(services)


def pending_tests(previous, proposed):
    now = datetime.now(timezone.utc).isoformat()
    return [dict(service=name, model=getattr(proposed, name + "_model"), status="pending",
        message="正在进行简短问答测试", started_at=now) for name in changed_services(previous, proposed)]


def present_tests(items):
    result = []
    for item in items or []:
        value = dict(item)
        if value["status"] == "pending":
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(value["started_at"])).total_seconds()
            if age > 60:
                value.update(status="incomplete", message="测试未完成或结果未保存；未自动重试")
        result.append(value)
    return result


async def probe(settings, pending):
    result = dict(pending)
    started = time.monotonic()
    def finish(status, message):
        result.update(status=status, message=message, elapsed_ms=round((time.monotonic() - started) * 1000),
            finished_at=datetime.now(timezone.utc).isoformat())
        return result
    if settings.llm_provider.lower() == "mock":
        return finish("skipped", "Mock 环境，未请求真实模型")
    if not settings.llm_api_key:
        return finish("failed", "该服务尚未配置 API Key")
    timeout = min(PROBE_TIMEOUT_SECONDS, settings.llm_timeout_seconds)
    try:
        async with asyncio.timeout(timeout):
            async with AsyncClient(base_url=settings.llm_base_url.rstrip("/") + "/",
                headers={"Authorization": "Bearer " + settings.llm_api_key}, timeout=timeout) as client:
                payload = chat_payload(settings, "请回答一个简单问题，只返回 JSON 对象，格式为 {\"answer\":数字}。",
                    "1 + 1 等于多少？", max_tokens=min(PROBE_MAX_TOKENS, settings.llm_max_tokens))
                # Stream only to bound the response size; the request is non-streaming.
                async with client.stream("POST", "chat/completions", json=payload) as response:
                    if response.status_code != 200:
                        messages = {401: "认证失败", 403: "服务拒绝访问", 404: "模型或接口不存在",
                            429: "服务限流或额度不足", 400: "服务拒绝请求参数"}
                        return finish("failed", f"HTTP {response.status_code}：{messages.get(response.status_code, '服务请求失败')}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        body.extend(chunk)
                        if len(body) > 131072:
                            return finish("failed", "测试响应超过大小上限")
        parsed = json.loads(body)
        choice = parsed["choices"][0]
        if choice.get("finish_reason") == "length":
            return finish("failed", "模型已响应，但测试输出额度不足，未确认问答结果")
        answer = json.loads(choice["message"]["content"])
        if not isinstance(answer, dict) or answer.get("answer") not in (2, "2"):
            return finish("failed", "模型已响应，但未通过简单问答校验")
        return finish("passed", "测试通过：1 + 1 = 2")
    except (TimeoutError, httpx.TimeoutException):
        return finish("failed", f"测试在 {timeout:g} 秒内未完成")
    except httpx.HTTPError:
        return finish("failed", "网络连接失败")
    except (ValueError, KeyError, IndexError, TypeError):
        return finish("failed", "模型返回的内容不符合问答 JSON 格式")
    except Exception:
        return finish("failed", "测试执行失败，未自动重试")
