import json
import subprocess
from types import SimpleNamespace

import pytest

from d_brain.services import cli_models


@pytest.mark.parametrize(
    ("cli", "model"),
    [("claude", "haiku"), ("claude-tmux", "haiku"), ("gemini", "flash-lite")],
)
def test_family_aliases_need_no_catalog(cli, model, tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("family alias should not launch catalog command")

    monkeypatch.setattr(cli_models.subprocess, "run", unexpected)
    assert cli_models.resolve_light_model(cli, tmp_path, {}) == model


@pytest.mark.parametrize(
    ("cli", "output", "expected", "command"),
    [
        (
            "codex",
            json.dumps(
                {
                    "models": [
                        {"slug": "gpt-5.6-luna", "visibility": "list"},
                        {"slug": "gpt-6-luna", "visibility": "list"},
                        {"slug": "gpt-7-luna", "visibility": "hide"},
                        {"slug": "gpt-7-sol", "visibility": "list"},
                    ]
                }
            ),
            "gpt-6-luna",
            ["codex", "debug", "models"],
        ),
        (
            "codex-tmux",
            '{"models": [{"slug": "gpt-6-luna", "visibility": "list"}]}',
            "gpt-6-luna",
            ["codex", "debug", "models"],
        ),
        (
            "grok",
            "Default model: grok-4.7\n  * grok-4.7\n  - grok-4.7-build-fast\n",
            "grok-4.7-build-fast",
            ["grok", "models"],
        ),
        (
            "kimi",
            json.dumps(
                {
                    "models": {
                        "example/kimi-mini": {"capabilities": ["tool_use"]},
                        "example/new-mini": {"capabilities": []},
                        "example/k3": {"capabilities": ["tool_use"]},
                    }
                }
            ),
            "example/kimi-mini",
            ["kimi", "provider", "list", "--json"],
        ),
        (
            "opencode",
            'example/model-tiny\n{"status":"active",'
            '"capabilities":{"toolcall":true}}\n'
            'example/model-9-tiny\n{"status":"deprecated",'
            '"capabilities":{"toolcall":true}}\n'
            'example/model-8-tiny\n{"status":"active",'
            '"capabilities":{"toolcall":false}}\n',
            "example/model-tiny",
            ["opencode", "models", "--refresh", "--verbose"],
        ),
    ],
)
def test_native_catalogs(cli, output, expected, command, tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(cli_models.subprocess, "run", run)
    assert (
        cli_models.resolve_light_model(cli, tmp_path, {"HOME": "/fictional"})
        == expected
    )
    assert calls[0][0] == command
    assert calls[0][1]["env"] == {"HOME": "/fictional"}
    assert calls[0][1]["cwd"] == tmp_path


def test_qwen_reads_configured_models_and_uses_numeric_versions(tmp_path):
    home = tmp_path / "home"
    settings = home / ".qwen" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "modelProviders": {
                    "openai": [
                        {"id": "qwen3.9-flash"},
                        {"id": "qwen3.10-flash"},
                        {"id": "qwen4-max"},
                    ]
                }
            }
        )
    )
    workdir = tmp_path / "project" / "vault"
    workdir.mkdir(parents=True)
    assert cli_models.resolve_light_model("qwen", workdir, {"HOME": str(home)}) == (
        "qwen3.10-flash"
    )


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired("catalog", 10),
        subprocess.CalledProcessError(1, "catalog"),
        ValueError("invalid catalog"),
    ],
)
def test_catalog_failure_keeps_default(tmp_path, monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(cli_models.subprocess, "run", run)
    assert cli_models.resolve_light_model("codex", tmp_path, {}) is None


def test_no_light_model_keeps_default(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_models.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "models": {
                        "kimi-code/kimi-for-coding-highspeed": {
                            "capabilities": ["tool_use"]
                        },
                    }
                }
            )
        ),
    )
    assert cli_models.resolve_light_model("kimi", tmp_path, {}) is None


@pytest.mark.parametrize("message", [
    "The 'example-mini' model is not supported "
    "when using Codex with a ChatGPT account.",
    '{"code":"model_not_found"}',
    "Unknown model: example-mini",
    "Model example-mini does not exist",
    "Model example-mini is not available",
    "Unknown option '--model'",
])
def test_model_rejection_is_recognized(message):
    assert cli_models.model_unavailable(message)
