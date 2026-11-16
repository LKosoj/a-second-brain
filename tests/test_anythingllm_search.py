import runpy

import pytest
from _paths import SKILLS_TEMPLATE_ROOT


def test_config_reads_only_working_directory_env(tmp_path, monkeypatch):
    script = SKILLS_TEMPLATE_ROOT / "anythingllm-search/scripts/anythingllm_search.py"
    config = runpy.run_path(str(script))["config"]
    (tmp_path / ".env").write_text("ANYTHINGLLM_API_KEY=parent-secret\n")
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.chdir(vault)
    monkeypatch.setenv("ANYTHINGLLM_API_KEY", "environment-secret")
    with pytest.raises(SystemExit, match="missing config file"):
        config()
    (vault / ".env").write_text(
        "ANYTHINGLLM_BASE_URL=https://knowledge.example.com/\n"
        "ANYTHINGLLM_API_KEY=fictional-key\n"
        "ANYTHINGLLM_WORKSPACE=data-ai\n"
    )
    assert config() == ("https://knowledge.example.com", "fictional-key", "data-ai")
