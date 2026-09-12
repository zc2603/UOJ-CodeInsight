import httpx
import pytest

from app import maintenance_checks
from app.config import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("ids,expected", [(["deepseek-flash"], 0), (["deepseek-v4-pro"], 1)])
async def test_model_check_requires_configured_model_in_catalog(monkeypatch, ids, expected):
    monkeypatch.setattr(maintenance_checks, "get_settings",
                        lambda: Settings(llm_api_key="test-key", llm_model="deepseek-flash"))
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"data": [{"id": model} for model in ids]}))
    monkeypatch.setattr(maintenance_checks.httpx, "AsyncClient",
                        lambda **kwargs: client_type(**kwargs, transport=transport))
    assert await maintenance_checks.check_deepseek() == expected
