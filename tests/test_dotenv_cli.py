"""CLI dotenv integration uses isolated files, synthetic credentials and mocked execution."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from bpt_process_agent.ingestion import cli

KEYS = [
    "OPENROUTER_API_KEY", "OPENROUTER_FLASH_MODEL", "OPENROUTER_PRO_MODEL",
    "OPENROUTER_BASE_URL", "OPENROUTER_EXTRACTION_MODEL", "OPENROUTER_CLASSIFICATION_MODEL",
    "OPENROUTER_EVIDENCE_AUDIT_MODEL", "OPENROUTER_COMPLEX_REASONING_MODEL",
]

@pytest.fixture
def isolated_cli(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(cli, "PROJECT_ROOT", root)
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    # Undo dotenv-created variables after each test, too.
    for key in KEYS:
        monkeypatch.setenv(key, "temporary")
        monkeypatch.delenv(key)
    received = []
    def client(config):
        received.append(config)
        return object()
    monkeypatch.setattr(cli, "OpenRouterClient", client)
    store = SimpleNamespace(load=lambda: SimpleNamespace(runs={
        "fixture-run": SimpleNamespace(status="audited", coverage="audited_parsed_content")}))
    monkeypatch.setattr(cli, "EvidenceStore", lambda path: store)
    pipeline = Mock()
    pipeline.return_value.run.return_value = "fixture-run"
    monkeypatch.setattr(cli, "ExtractionPipeline", pipeline)
    monkeypatch.setattr(cli, "write_report", lambda *args: (Path("evidence.json"), Path("evidence-report.md")))
    return root, received, pipeline

def dotenv(root):
    # UTF-8 BOM, quotes, comments and export syntax are common on Windows.
    (root / ".env").write_text(
        '# Local synthetic configuration\n'
        'export OPENROUTER_API_KEY="fixture-dotenv-credential"\n'
        "OPENROUTER_FLASH_MODEL='fixture/flash' # extraction\n"
        'OPENROUTER_PRO_MODEL=fixture/pro\n',
        encoding="utf-8-sig")

def test_cli_loads_project_root_independent_of_working_directory(isolated_cli, tmp_path, monkeypatch, capsys):
    root, configs, _ = isolated_cli
    dotenv(root)
    working = tmp_path / "elsewhere"
    working.mkdir()
    (working / ".env").write_text("OPENROUTER_FLASH_MODEL=wrong/cwd\n", encoding="utf-8")
    monkeypatch.chdir(working)
    assert cli.main(["source.pdf"]) == 0
    config = configs[0]
    assert config.api_key == "fixture-dotenv-credential"
    assert (config.flash_model, config.pro_model) == ("fixture/flash", "fixture/pro")
    output = capsys.readouterr()
    assert "fixture-dotenv-credential" not in output.out + output.err
    assert "fixture-dotenv-credential" not in repr(config)

@pytest.mark.parametrize("key", KEYS[:3])
def test_existing_environment_takes_precedence(isolated_cli, monkeypatch, key):
    root, configs, _ = isolated_cli
    dotenv(root)
    monkeypatch.setenv(key, "fixture/environment-value")
    assert cli.main(["source.pdf"]) == 0
    attribute = dict(zip(KEYS[:3], ["api_key", "flash_model", "pro_model"]))[key]
    assert getattr(configs[0], attribute) == "fixture/environment-value"

def test_explicit_empty_environment_value_is_not_overridden(isolated_cli, monkeypatch, capsys):
    root, configs, pipeline = isolated_cli
    dotenv(root)
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert cli.main(["source.pdf"]) == 1
    assert not configs
    pipeline.assert_not_called()
    output = capsys.readouterr()
    assert "OPENROUTER_API_KEY" in output.err
    assert "fixture-dotenv-credential" not in output.out + output.err

def test_missing_dotenv_uses_existing_environment(isolated_cli, monkeypatch):
    root, configs, _ = isolated_cli
    for key, value in zip(KEYS[:3], ["fixture-environment-key", "fixture/flash-env", "fixture/pro-env"]):
        monkeypatch.setenv(key, value)
    assert cli.main(["source.pdf"]) == 0
    assert configs[0].api_key == "fixture-environment-key"

def test_missing_dotenv_does_not_search_parent_or_print_credentials(isolated_cli, capsys):
    root, configs, _ = isolated_cli
    dotenv(root.parent)
    assert cli.main(["source.pdf"]) == 1
    assert not configs
    output = capsys.readouterr()
    assert "Missing required environment variable: OPENROUTER_API_KEY" in output.err
    assert "fixture-dotenv-credential" not in output.out + output.err

def test_parse_only_does_not_load_dotenv_or_create_client(isolated_cli, monkeypatch):
    root, configs, pipeline = isolated_cli
    dotenv(root)
    loader = Mock(side_effect=AssertionError("Offline parsing must not load credentials"))
    monkeypatch.setattr(cli, "load_dotenv", loader)
    assert cli.main(["source.pdf", "--parse-only"]) == 0
    loader.assert_not_called()
    assert not configs
    assert pipeline.call_args.args[1:3] == (None, None)
