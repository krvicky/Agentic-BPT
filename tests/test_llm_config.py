import pytest
from bpt_process_agent.llm_config import ConfigurationError, LLMConfig

@pytest.fixture
def env():
    # Synthetic IDs and credentials; no selected real models or API calls.
    return {"OPENROUTER_API_KEY": "synthetic-test-credential",
            "OPENROUTER_FLASH_MODEL": "fixture/flash",
            "OPENROUTER_PRO_MODEL": "fixture/pro"}

@pytest.mark.parametrize("task,expected", [
    ("extraction", "fixture/flash"), ("classification", "fixture/flash"),
    ("evidence_audit", "fixture/pro"), ("complex_reasoning", "fixture/pro"),
])
def test_default_model_routing(env, task, expected):
    assert LLMConfig.from_env(env).model_for(task) == expected

@pytest.mark.parametrize("task,var", [
    ("extraction", "OPENROUTER_EXTRACTION_MODEL"),
    ("classification", "OPENROUTER_CLASSIFICATION_MODEL"),
    ("evidence_audit", "OPENROUTER_EVIDENCE_AUDIT_MODEL"),
    ("complex_reasoning", "OPENROUTER_COMPLEX_REASONING_MODEL"),
])
def test_task_environment_and_call_overrides(env, task, var):
    env[var] = "fixture/task-override"
    config = LLMConfig.from_env(env)
    assert config.model_for(task) == "fixture/task-override"
    assert config.model_for(task, override="fixture/call-override") == "fixture/call-override"
    other = "classification" if task != "classification" else "extraction"
    assert config.model_for(other) == "fixture/flash"

@pytest.mark.parametrize("name", [
    "OPENROUTER_API_KEY", "OPENROUTER_FLASH_MODEL", "OPENROUTER_PRO_MODEL",
])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_variables_fail_without_secret_values(env, name, value):
    if value is None:
        env.pop(name)
    else:
        env[name] = value
    with pytest.raises(ConfigurationError) as error:
        LLMConfig.from_env(env)
    assert name in str(error.value)
    assert "synthetic-test-credential" not in str(error.value)

def test_reads_process_environment_and_keeps_key_out_of_repr(env, monkeypatch):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for name in ["OPENROUTER_EXTRACTION_MODEL", "OPENROUTER_CLASSIFICATION_MODEL",
                 "OPENROUTER_EVIDENCE_AUDIT_MODEL", "OPENROUTER_COMPLEX_REASONING_MODEL",
                 "OPENROUTER_BASE_URL"]:
        monkeypatch.delenv(name, raising=False)
    config = LLMConfig.from_env()
    assert config.flash_model == env["OPENROUTER_FLASH_MODEL"]
    assert config.pro_model == env["OPENROUTER_PRO_MODEL"]
    assert config.api_key == env["OPENROUTER_API_KEY"]
    assert config.api_key not in repr(config)

def test_empty_overrides_use_defaults_and_url_is_configurable(env):
    env["OPENROUTER_EXTRACTION_MODEL"] = " "
    env["OPENROUTER_BASE_URL"] = "https://example.invalid/compatible/v1"
    config = LLMConfig.from_env(env)
    assert config.model_for("extraction") == "fixture/flash"
    assert config.base_url == env["OPENROUTER_BASE_URL"]
    env["OPENROUTER_BASE_URL"] = ""
    assert LLMConfig.from_env(env).base_url == "https://openrouter.ai/api/v1"

def test_legacy_single_model_and_direct_openai_key_are_not_fallbacks():
    with pytest.raises(ConfigurationError):
        LLMConfig.from_env({"OPENROUTER_MODEL": "fixture/legacy", "OPENAI_API_KEY": "synthetic-key"})

@pytest.mark.parametrize("override", ["", " ", 123])
def test_invalid_call_override(env, override):
    with pytest.raises(ConfigurationError):
        LLMConfig.from_env(env).model_for("extraction", override=override)

def test_unknown_task_rejected(env):
    with pytest.raises(ConfigurationError):
        LLMConfig.from_env(env).model_for("unknown")
