import asyncio

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.services.llm_provider import OpenAICompatibleLLMProvider


class Payload(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_four_providers_allow_generation_and_grading_without_exceeding_cap():
    """Offline transport test, not a PostgreSQL or real API load test."""
    settings = Settings(_env_file=None, llm_api_key="synthetic-key")
    active = peak = calls = 0
    reached = asyncio.Event()
    release = asyncio.Event()
    providers = []
    tasks = []

    async def respond(request):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        if active == 100:
            reached.set()
        try:
            await release.wait()
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"value":1}'}}]})
        finally:
            active -= 1

    try:
        for _ in range(4):
            provider = OpenAICompatibleLLMProvider(settings)
            await provider.client.aclose()
            provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://offline.test/")
            providers.append(provider)
            # 20 generation calls, 5 grading calls, and one queued call per process.
            for i in range(26):
                tasks.append(asyncio.create_task(provider._request_json(
                    "generation" if i < 20 else "grading", "synthetic", Payload)))
        await asyncio.wait_for(reached.wait(), timeout=5)
        await asyncio.sleep(0)
        assert active == calls == 100
        assert not any(t.done() for t in tasks)
        # Cancellation must release the acquired slot, allowing a queued call in.
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        assert peak == 100 and active == 0 and calls == 104
        assert sum(isinstance(r, asyncio.CancelledError) for r in results) == 1
        assert all(r[0].value == 1 for r in results if not isinstance(r, BaseException))
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for provider in providers:
            await provider.close()
