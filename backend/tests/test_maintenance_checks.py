from app.maintenance_checks import check_config, smoke_check


def test_smoke_check_finds_current_prompts(capsys, monkeypatch) -> None:
    async def fake_schema_check():
        return None
    monkeypatch.setattr("app.maintenance_checks.check_preparation_schema", fake_schema_check)
    assert smoke_check() == 0
    output = capsys.readouterr().out
    assert "prompt_version=question_generator_v11" in output
    assert "grader_prompt_version=grader_v8" in output


def test_config_check_redacts_secret_values(monkeypatch, capsys) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-test-value")
    monkeypatch.setenv("UOJ_JUDGER_PASSWORD", "judger-secret-test-value")
    monkeypatch.setenv("UOJ_DATABASE_URL", "mysql+aiomysql://user:db-secret@test/db")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert check_config() == 0
        output = capsys.readouterr().out
        assert "sk-secret-test-value" not in output
        assert "judger-secret-test-value" not in output
        assert "db-secret" not in output
        assert "llm_api_key=configured" in output
    finally:
        get_settings.cache_clear()
