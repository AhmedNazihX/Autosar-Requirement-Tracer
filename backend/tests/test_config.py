from core.config import Settings


def test_settings_loads_without_openrouter_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Isolate from any real .env file on disk during this test.
    settings = Settings(_env_file=None)
    assert settings.openrouter_api_key is None


def test_max_report_cost_usd_defaults_to_2_0(monkeypatch) -> None:
    monkeypatch.delenv("MAX_REPORT_COST_USD", raising=False)
    settings = Settings(_env_file=None)
    assert settings.max_report_cost_usd == 2.0


def test_settings_env_var_names_are_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    settings = Settings(_env_file=None)
    assert settings.openrouter_api_key == "sk-test"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
