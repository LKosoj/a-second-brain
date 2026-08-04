import fcntl
import inspect
import json
import threading
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from _paths import SKILLS_TEMPLATE_ROOT
from conftest import (
    _markdown_section,
    _setup_daily_processing_vault,
    _write_vault_manifest,
)

from d_brain.services import frontmatter as frontmatter_service
from d_brain.services.compiled_briefings import (
    CompiledBriefingService,
)
from d_brain.services.entry_status import (
    ENTRY_STATUS_ALREADY_PROCESSED,
)
from d_brain.services.frontmatter import parse_frontmatter_bytes
from d_brain.services.processor import (
    INTERACTIVE_MODE,
    SCHEDULED_MODE,
    CliProcessor,
)
from d_brain.services.vault_lock import vault_write_lock


def test_process_daily_facade_signature_and_timeout_mapping(tmp_path: Path) -> None:
    signature = inspect.signature(CliProcessor.process_daily)
    parameters = list(signature.parameters.values())

    assert [parameter.name for parameter in parameters] == ["self", "day", "mode"]
    assert parameters[1].default is None
    assert parameters[2].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters[2].default == INTERACTIVE_MODE

    processor = CliProcessor(tmp_path / "vault")
    processor._get_daily_file = lambda day: (_ for _ in ()).throw(TimeoutError)  # type: ignore[method-assign]

    assert processor.process_daily(mode=INTERACTIVE_MODE) == {
        "error": "Processing timed out",
        "processed_entries": 0,
    }


def test_do_processor_uses_vault_scoped_runner(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (tmp_path / "mcp-config.json").write_text("{}", encoding="utf-8")

    processor = CliProcessor(vault_path)

    assert processor._assistant_runner.workdir == vault_path
    scope_rules = processor._assistant_scope_rules()
    assert "Current working directory is the vault root" in scope_rules
    assert "Never access ../" in scope_rules
    assert "Todoist" in scope_rules
    assert "start with MEMORY.md" not in scope_rules
    assert "qmd-local" not in scope_rules
    assert "memory touch" not in scope_rules
    assert processor._cli_extra_env()["MCP_CONFIG_PATH"] == str(
        tmp_path / "mcp-config.json"
    )


def test_processor_cli_extra_env_forwards_model_gateway_settings(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    processor = CliProcessor(
        vault_path,
        openai_api_key="test-openai",
        openai_base_url="https://gateway.example.com/v1",
        openai_model="gpt-5-mini",
    )

    assert processor._cli_extra_env()["OPENAI_API_KEY"] == "test-openai"
    assert processor._cli_extra_env()["BASE_URL"] == "https://gateway.example.com/v1"
    assert (
        processor._cli_extra_env()["OPENAI_BASE_URL"]
        == "https://gateway.example.com/v1"
    )
    assert processor._cli_extra_env()["MODEL"] == "gpt-5-mini"
    assert processor._cli_extra_env()["OPENAI_MODEL"] == "gpt-5-mini"


def test_process_prompts_include_shared_ownership_reference(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    ownership_path = (
        vault_path.parent / "skills/dbrain-processor/references/ownership.md"
    )
    ownership_path.parent.mkdir(parents=True, exist_ok=True)
    ownership_path.write_text(
        "owner={OWNER_FULL_NAME}\nshared ownership counts\n",
        encoding="utf-8",
    )

    processor = CliProcessor(
        vault_path,
        owner_full_name="Иван Иванов",
    )

    capture_prompt = processor._build_capture_prompt(day)
    execute_prompt = processor._build_execute_prompt(day)

    assert "OWNERSHIP REFERENCE" in capture_prompt
    assert "owner=Иван Иванов" in capture_prompt
    assert "shared ownership counts" in execute_prompt


def test_process_prompts_include_content_language_rule(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    reflect_source = SKILLS_TEMPLATE_ROOT / "dbrain-processor/phases/reflect.md"
    (vault_path.parent / "skills/dbrain-processor/phases/reflect.md").write_text(
        reflect_source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path, content_language="en")

    capture_prompt = processor._build_capture_prompt(day)
    reflect_prompt = processor._build_reflect_prompt(day)

    assert "LANGUAGE RULE" in capture_prompt
    assert "in English" in capture_prompt
    assert "in English" in reflect_prompt
    assert ".session/memory-audit.md" in reflect_prompt
    assert "Default to NO edit when unsure" in reflect_prompt
    assert "keep it in `daily` or `.session/handoff.md` instead" in reflect_prompt
    assert "Keep exactly one instance of each heading" in reflect_prompt
    assert "Keep at most 10 observation bullets in handoff" in reflect_prompt


def test_vault_retrieval_skill_is_injected_only_into_vault_write_and_answer_prompts(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    skill_path = vault_path.parent / "skills/vault-retrieval/SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("retrieval-contract", encoding="utf-8")
    processor = CliProcessor(vault_path)
    processor._build_injected_context = lambda **kwargs: "core-context"  # type: ignore[method-assign]

    execute_prompt = processor._build_execute_prompt(day)
    reflect_prompt = processor._build_reflect_prompt(day)
    question_prompt = processor._build_question_answer_prompt("Что важно?", user_id=0)
    capture_prompt = processor._build_capture_prompt(day)
    preview_prompt = processor._build_preview_prompt(day, {})

    for prompt in (execute_prompt, reflect_prompt, question_prompt):
        assert prompt.count("=== VAULT RETRIEVAL SKILL ===") == 1
        assert "retrieval-contract" in prompt
    assert "=== VAULT RETRIEVAL SKILL ===" not in capture_prompt
    assert "=== VAULT RETRIEVAL SKILL ===" not in preview_prompt


def test_context_pack_is_injected_once_for_vault_scoped_prompts_only(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    captured: dict[str, str] = {}
    processor._run_assistant_prompt = (  # type: ignore[method-assign]
        lambda prompt: captured.setdefault("do", prompt) and "готово"
    )

    prompts = [
        processor._build_capture_prompt(day),
        processor._build_preview_prompt(day, {}),
        processor._build_execute_prompt(day),
        processor._build_reflect_prompt(day),
        processor._build_question_answer_prompt("Что важно?", user_id=0),
    ]
    processor.execute_prompt("Проверь заметки")
    prompts.append(captured["do"])
    prompts.append(
        processor._build_weekly_digest_prompt(
            today=day,
            yearly_goals_name="1-yearly-2026.md",
        )
    )

    for prompt in prompts[:6]:
        assert prompt.count("=== INJECTED CORE CONTEXT ===") == 1
        assert "memory" in prompt
        assert "weekly" in prompt
    assert "=== INJECTED CORE CONTEXT ===" not in processor._build_text_intent_prompt(
        "Привет"
    )
    assert "=== INJECTED CORE CONTEXT ===" not in prompts[-1]


def test_vault_retrieval_skill_fails_fast_when_missing_or_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    processor._build_injected_context = lambda **kwargs: "core-context"  # type: ignore[method-assign]
    skill_path = vault_path.parent / "skills/vault-retrieval/SKILL.md"
    skill_path.unlink()

    with pytest.raises(
        RuntimeError,
        match="Vault retrieval skill is required but missing",
    ):
        processor._build_execute_prompt(day)

    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("retrieval-contract", encoding="utf-8")
    read_text = Path.read_text

    def raise_for_retrieval_skill(path: Path, *args: object, **kwargs: object) -> str:
        if path == skill_path:
            raise OSError("permission denied")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", raise_for_retrieval_skill)

    with pytest.raises(RuntimeError, match="Vault retrieval skill is unreadable"):
        processor._build_execute_prompt(day)


def test_vault_retrieval_skill_fails_fast_when_empty(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    skill_path = vault_path.parent / "skills/vault-retrieval/SKILL.md"
    skill_path.write_text("\n", encoding="utf-8")
    processor = CliProcessor(vault_path)

    with pytest.raises(RuntimeError, match="Vault retrieval skill is empty"):
        processor._load_vault_retrieval_skill()


def test_execute_prompt_injects_vault_retrieval_skill(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    write_vault_manifest(vault_path)
    skill_path = vault_path.parent / "skills/vault-retrieval/SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("retrieval-contract", encoding="utf-8")
    processor = CliProcessor(vault_path)
    processor._build_injected_context = lambda **kwargs: "core-context"  # type: ignore[method-assign]
    processor._build_auto_recall_block = lambda *args, **kwargs: (  # type: ignore[method-assign]
        "=== AUTO ARCHIVE RECALL ===\nQMD recall\n=== END AUTO ARCHIVE RECALL ==="
    )
    captured: dict[str, str] = {}
    processor._run_assistant_prompt = (
        lambda prompt: captured.setdefault(  # type: ignore[method-assign]
            "prompt", prompt
        )
        and "готово"
    )

    result = processor.execute_prompt("Проверь заметки")

    assert result["report"] == "готово"
    assert "retrieval-contract" in captured["prompt"]
    assert captured["prompt"].count("=== VAULT RETRIEVAL SKILL ===") == 1
    assert captured["prompt"].index("=== AUTO ARCHIVE RECALL ===") < captured[
        "prompt"
    ].index("USER REQUEST:")


def test_run_json_phase_retries_with_stricter_contract_and_persists_raw_output(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)

    prompts: list[str] = []
    responses = iter(
        [
            "not json at all",
            '```json\n{"status": "ok"}\n```',
        ]
    )

    def fake_run(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]

    payload = processor._run_json_phase("Return JSON.", phase_name="capture")
    raw_output_path = vault_path / ".session" / "capture-raw-output.txt"

    assert payload == {"status": "ok"}
    assert len(prompts) == 2
    assert "CRITICAL OUTPUT CONTRACT" in prompts[1]
    assert raw_output_path.exists()
    assert "not json at all" in raw_output_path.read_text(encoding="utf-8")


def test_normalize_saved_thoughts_repairs_broken_frontmatter(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _setup_daily_processing_vault(vault_path, day)
    note_path = vault_path / "thoughts" / "reflections" / "2026-04-05-memory.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        """---
type: note
description: >-
tags: [memory, policy]
source: daily/2026-04-05.md
status: active
created: 2026-04-05
updated: 2026-04-05
last_accessed: 2026-04-05
relevance: 1.0
tier: active
Ужесточена политика обновления MEMORY.md: по умолчанию не писать.
related:
  - "[[MOC/MOC-reflections]]"
---

# Политика обновления MEMORY.md

## Правило

Default — не писать в MEMORY.md.
""",
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)

    processor._normalize_saved_thoughts(
        {"thoughts_saved": [{"path": "thoughts/reflections/2026-04-05-memory.md"}]},
        day=day,
    )

    content = note_path.read_text(encoding="utf-8")
    document = parse_frontmatter_bytes(content.encode("utf-8"))

    assert document.fields["description"] == (
        "Ужесточена политика обновления MEMORY.md: по умолчанию не писать."
    )
    assert document.fields["type"] == "reflection"
    assert document.fields["tags"] == [
        "memory",
        "policy",
        "reflection",
        "system",
    ]
    assert "Ужесточена политика обновления MEMORY.md: по умолчанию не писать." in (
        content
    )
    assert '  - "[[MOC/MOC-reflections]]"' in content


def test_normalize_saved_thoughts_repairs_unclosed_frontmatter(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _setup_daily_processing_vault(vault_path, day)
    note_path = vault_path / "thoughts" / "reflections" / "2026-04-05-year-end.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        """---
type: note
description: >-
tags: [reflection, yearly]
source: daily/2026-04-05.md
status: active
created: 2026-04-05
updated: 2026-04-05
last_accessed: 2026-04-05
relevance: 1.0
tier: active

# Итоги года

Система должна уметь чинить такой frontmatter автоматически.
""",
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)

    processor._normalize_saved_thoughts(
        {"thoughts_saved": [{"path": "thoughts/reflections/2026-04-05-year-end.md"}]},
        day=day,
    )

    content = note_path.read_text(encoding="utf-8")

    assert content.startswith("---\n")
    assert (
        "description: >-\n"
        "  Система должна уметь чинить такой frontmatter автоматически."
    ) in content
    assert "type: reflection" in content
    assert "\n---\n\n# Итоги года\n" in content


def test_normalize_saved_thought_repairs_known_stray_description_line(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _write_vault_manifest(vault_path)
    note_path = vault_path / "thoughts" / "reflections" / "legacy-description.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        """---
type: note
description: >-
tags: [memory, policy]
status: active
created: 2026-04-05
updated: 2026-04-05
last_accessed: 2026-04-05
relevance: 1.0
tier: active
Generated description without a mapping colon.
related: []
---

# Legacy description

Body text.
""",
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)

    changed = processor._normalize_saved_thought_note(note_path, day=day)

    document = parse_frontmatter_bytes(note_path.read_bytes())
    assert changed is True
    assert document.fields["description"] == (
        "Generated description without a mapping colon."
    )
    assert document.fields["type"] == "reflection"


@pytest.mark.parametrize(
    "content",
    [
        (
            b"---\n"
            b"type: note\n"
            b"type: reflection\n"
            b"description: Duplicate keys must fail closed.\n"
            b"---\n# Duplicate\n"
        ),
        (b"---\ntype: note\ndescription: Invalid UTF-8 \xff\n---\n# Invalid UTF-8\n"),
        (
            b"---\n"
            b"type: note\n"
            b"description: >-\n"
            b"  Valid description.\n"
            b"custom: [unterminated\n"
            b"---\n# Invalid YAML\n"
        ),
    ],
)
def test_normalize_saved_thought_skips_unrecognized_parse_errors(
    tmp_path: Path,
    content: bytes,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _write_vault_manifest(vault_path)
    note_path = vault_path / "thoughts" / "reflections" / "invalid.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_bytes(content)
    processor = CliProcessor(vault_path)

    changed = processor._normalize_saved_thought_note(note_path, day=day)

    assert changed is False
    assert note_path.read_bytes() == content


def test_normalize_saved_thought_note_losslessly_patches_valid_yaml(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _write_vault_manifest(vault_path)
    note_path = vault_path / "thoughts" / "reflections" / "lossless.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        b"---\r\n"
        b"type: reflection\r\n"
        b"date: 2026-04-05\r\n"
        b"description: >- # keep description comment\r\n"
        b"  Durable retrieval description.\r\n"
        b"tags:\r\n"
        b"  - reflection\r\n"
        b"  - system\r\n"
        b"related:\r\n"
        b'  - "[[MOC/MOC-reflections]]"\r\n'
        b"source: daily/2026-04-05.md\r\n"
        b"status: active\r\n"
        b"created: 2026-04-05\r\n"
        b"updated: 2026-04-04 # keep update comment\r\n"
        b"# keep standalone comment\r\n"
        b"last_accessed: 2026-04-04\r\n"
        b"relevance: 0.75\r\n"
        b"tier: active\r\n"
        b"custom:\r\n"
        b"  nested:\r\n"
        b"    enabled: true\r\n"
        b"custom_notes: |-\r\n"
        b"  First custom line.\r\n"
        b"  Second custom line.\r\n"
        b"---\r\n"
        b"\r\n"
        b"# Lossless note\r\n"
        b"\r\n"
        b"Body line with trailing spaces.  \r\n"
    )
    note_path.write_bytes(original)
    processor = CliProcessor(vault_path)

    changed = processor._normalize_saved_thought_note(note_path, day=day)

    expected = original.replace(
        b"updated: 2026-04-04 # keep update comment\r\n",
        b'updated: "2026-04-05" # keep update comment\r\n',
    ).replace(
        b"last_accessed: 2026-04-04\r\n",
        b'last_accessed: "2026-04-05"\r\n',
    )
    first_result = note_path.read_bytes()
    assert changed is True
    assert first_result == expected
    assert (
        parse_frontmatter_bytes(first_result).body
        == parse_frontmatter_bytes(original).body
    )
    assert b"tags:\r\n  - reflection\r\n  - system\r\n" in first_result
    assert b'related:\r\n  - "[[MOC/MOC-reflections]]"\r\n' in first_result
    assert b"custom:\r\n  nested:\r\n    enabled: true\r\n" in first_result
    assert (
        b"custom_notes: |-\r\n"
        b"  First custom line.\r\n"
        b"  Second custom line.\r\n" in first_result
    )
    assert b"# keep standalone comment\r\n" in first_result

    changed_again = processor._normalize_saved_thought_note(note_path, day=day)

    assert changed_again is False
    assert note_path.read_bytes() == first_result


def test_normalize_saved_thought_note_preserves_external_atomic_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _write_vault_manifest(vault_path)
    note_path = vault_path / "thoughts" / "reflections" / "race.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        b"---\n"
        b"type: reflection\n"
        b"date: 2026-04-05\n"
        b"description: Race-safe note.\n"
        b"tags: [reflection, system]\n"
        b"status: active\n"
        b"created: 2026-04-05\n"
        b"updated: 2026-04-04\n"
        b"last_accessed: 2026-04-04\n"
        b"relevance: 1.0\n"
        b"tier: active\n"
        b"related: []\n"
        b"---\n"
        b"# Race-safe note\n"
    )
    external = original.replace(
        b"description: Race-safe note.\n",
        b"description: External writer won.\n",
    )
    note_path.write_bytes(original)
    expected_hash = sha256(original).hexdigest()
    real_atomic_write = frontmatter_service._atomic_write_at
    raced = False

    def inject_external_write(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        nonlocal raced
        assert kwargs["expected_full_sha256"] == expected_hash
        if not raced:
            raced = True
            note_path.write_bytes(external)
        real_atomic_write(*args, **kwargs)

    monkeypatch.setattr(
        frontmatter_service,
        "_atomic_write_at",
        inject_external_write,
    )
    processor = CliProcessor(vault_path)

    changed = processor._normalize_saved_thought_note(note_path, day=day)

    assert raced is True
    assert changed is False
    assert note_path.read_bytes() == external
    assert "normalization conflict" in caplog.text


def test_normalize_saved_thought_note_does_not_hide_unrelated_writer_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _write_vault_manifest(vault_path)
    note_path = vault_path / "thoughts" / "reflections" / "writer-error.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        b"---\n"
        b"type: reflection\n"
        b"date: 2026-04-05\n"
        b"description: Writer error note.\n"
        b"tags: [reflection, system]\n"
        b"status: active\n"
        b"created: 2026-04-05\n"
        b"updated: 2026-04-04\n"
        b"last_accessed: 2026-04-04\n"
        b"relevance: 1.0\n"
        b"tier: active\n"
        b"related: []\n"
        b"---\n"
        b"# Writer error note\n"
    )
    note_path.write_bytes(original)
    processor = CliProcessor(vault_path)

    def fail_write(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise frontmatter_service.UnsafeVaultPathError("unrelated writer safety error")

    monkeypatch.setattr(processor, "_write_vault_markdown", fail_write)

    with pytest.raises(
        frontmatter_service.UnsafeVaultPathError,
        match="unrelated writer safety error",
    ):
        processor._normalize_saved_thought_note(note_path, day=day)

    assert note_path.read_bytes() == original


def test_compact_handoff_file_keeps_latest_status_and_recent_observations(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    handoff_path = vault_path / ".session" / "handoff.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        """---
type: note
last_accessed: 2026-04-05
relevance: 1.0
tier: active
---
# Передача сессии

## Last Session
старый блок

## Key Decisions
- старое решение

## Observations
- [pattern] 2026-04-01: keep-1
- [pattern] 2026-04-02: keep-2
- [pattern] 2026-04-03: keep-3

## Last Session
новый блок

## Key Decisions
- новое решение

## In Progress
- текущий хвост

## Next Steps
- следующий шаг

## Observations
- [pattern] 2026-04-04: keep-4
- [pattern] 2026-04-05: keep-5
- [pattern] 2026-04-06: keep-6
- [pattern] 2026-04-07: keep-7
- [pattern] 2026-04-08: keep-8
- [pattern] 2026-04-09: keep-9
- [pattern] 2026-04-10: keep-10
- [pattern] 2026-04-11: keep-11
- [pattern] 2026-04-12: keep-12
- [pattern] 2026-04-12: keep-12
""",
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)

    processor._compact_handoff_file()

    content = handoff_path.read_text(encoding="utf-8")

    assert content.count("## Last Session") == 1
    assert content.count("## Observations") == 1
    assert "старый блок" not in content
    assert "новый блок" in content
    assert "старое решение" not in content
    assert "новое решение" in content
    observations = _markdown_section(content, "Observations").splitlines()
    assert len(observations) == 10
    assert "- [pattern] 2026-04-01: keep-1" not in observations
    assert observations[-1] == "- [pattern] 2026-04-12: keep-12"
    assert observations.count("- [pattern] 2026-04-12: keep-12") == 1


def test_compact_handoff_file_does_not_lose_cooperative_concurrent_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    handoff_path = vault_path / ".session" / "handoff.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        """---
type: note
last_accessed: 2026-04-05
relevance: 1.0
tier: active
---
# Передача сессии

## Last Session
исходный блок

## Key Decisions
- исходное решение

## In Progress
- текущий хвост

## Next Steps
- следующий шаг

## Observations
- [pattern] 2026-04-05: исходное наблюдение
""",
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)
    compaction_started = threading.Event()
    writer_attempted = threading.Event()
    writer_errors: list[Exception] = []
    original_compact = processor._compact_handoff_text

    def pause_compaction(text: str) -> str:
        compaction_started.set()
        assert writer_attempted.wait(timeout=5)
        return original_compact(text)

    monkeypatch.setattr(processor, "_compact_handoff_text", pause_compaction)

    def cooperative_writer() -> None:
        try:
            assert compaction_started.wait(timeout=5)
            writer_attempted.set()
            with vault_write_lock(vault_path) as lock:
                frontmatter, sections = processor._normalized_handoff_sections(
                    handoff_path.read_text(encoding="utf-8")
                )
                sections["Observations"] = (
                    sections["Observations"].rstrip()
                    + "\n- [idea] 2026-04-05: конкурентное наблюдение"
                )
                processor._write_vault_markdown(
                    handoff_path,
                    processor._render_handoff_sections(frontmatter, sections),
                    lock=lock,
                )
        except Exception as exc:
            writer_errors.append(exc)

    writer = threading.Thread(target=cooperative_writer)
    writer.start()
    processor._compact_handoff_file()
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    observations = _markdown_section(
        handoff_path.read_text(encoding="utf-8"),
        "Observations",
    )
    assert "исходное наблюдение" in observations
    assert "конкурентное наблюдение" in observations


def test_write_handoff_observations_preserves_cooperative_concurrent_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    _write_vault_manifest(vault_path)
    handoff_path = vault_path / ".session" / "handoff.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        """---
type: note
last_accessed: 2026-04-05
relevance: 1.0
tier: active
---
# Передача сессии

## Last Session
исходный блок

## Key Decisions
- исходное решение

## In Progress
- текущий хвост

## Next Steps
- следующий шаг

## Observations
- [pattern] 2026-04-05: исходное наблюдение
""",
        encoding="utf-8",
    )
    processor = CliProcessor(vault_path)
    normalization_started = threading.Event()
    writer_attempted = threading.Event()
    writer_errors: list[Exception] = []
    original_normalize = processor._normalized_handoff_sections

    def pause_after_read(text: str) -> tuple[str, dict[str, str]]:
        result = original_normalize(text)
        normalization_started.set()
        assert writer_attempted.wait(timeout=5)
        return result

    monkeypatch.setattr(processor, "_normalized_handoff_sections", pause_after_read)

    def cooperative_writer() -> None:
        try:
            assert normalization_started.wait(timeout=5)
            writer_attempted.set()
            with vault_write_lock(vault_path) as lock:
                frontmatter, sections = original_normalize(
                    handoff_path.read_text(encoding="utf-8")
                )
                sections["Key Decisions"] += "\n- конкурентное решение"
                processor._write_vault_markdown(
                    handoff_path,
                    processor._render_handoff_sections(frontmatter, sections),
                    lock=lock,
                )
        except Exception as exc:
            writer_errors.append(exc)

    writer = threading.Thread(target=cooperative_writer)
    writer.start()
    processor._write_handoff_observations(
        ["- [pattern] 2026-04-05: обновлённое наблюдение"]
    )
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    content = handoff_path.read_text(encoding="utf-8")
    assert "конкурентное решение" in _markdown_section(content, "Key Decisions")
    observations = _markdown_section(content, "Observations")
    assert "обновлённое наблюдение" in observations
    assert "исходное наблюдение" not in observations


def test_question_prompt_reads_core_business_and_projects_context(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)

    prompt = processor._build_question_answer_prompt(
        "Какие приоритеты на эту неделю?",
        user_id=0,
    )

    assert prompt.count("=== INJECTED CORE CONTEXT ===") == 1
    assert "memory" in prompt
    assert "weekly" in prompt
    assert "monthly" in prompt
    assert "business" in prompt
    assert "projects" in prompt
    assert "ALWAYS READ:" not in prompt
    assert ".session/question-creative-recall.txt" in prompt
    assert "Start with the actual answer in the first sentence." in prompt
    assert "Do not put an emoji or heading before that sentence." in prompt
    assert "If that block includes FULLTEXT FOLLOW-UP" not in prompt
    assert "history scope or explicit start point" in prompt
    assert "history of the topic/question" in prompt
    assert "latest snapshot." in prompt
    assert "Do not collapse a long-running project into a very short answer" in prompt
    assert "SOURCE FOOTER POLICY: OPTIONAL" in prompt
    assert "Источники:" in prompt


def test_telegram_output_rules_use_adaptive_answer_depth() -> None:
    rules = CliProcessor._telegram_markdown_output_rules(
        opening_line="Start with the answer.",
    )

    assert "Default to a complete, well-structured answer" in rules
    assert "Be brief only when the request is a simple factual question" in rules
    assert "user explicitly asks for brevity" in rules
    assert "Do not shorten complex, analytical, status/history" in rules
    assert "Be concise when possible" not in rules


def test_question_prompt_requires_source_footer_for_fact_and_history_routes(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)

    fact_prompt = processor._build_question_answer_prompt(
        "Какой ID проекта Example Project?",
        user_id=0,
    )
    history_prompt = processor._build_question_answer_prompt(
        "Что мы решили раньше по Example Project?",
        user_id=0,
    )

    assert "SOURCE FOOTER POLICY: REQUIRED" in fact_prompt
    assert "SOURCE FOOTER POLICY: REQUIRED" in history_prompt
    assert "2-5 фактически использованных vault-relative" in fact_prompt
    assert "не выдумывай ссылку" in fact_prompt


def test_prompt_references_avoid_legacy_year_and_keyword_lists() -> None:
    refs_root = SKILLS_TEMPLATE_ROOT / "dbrain-processor/references"

    classification = (refs_root / "classification.md").read_text(encoding="utf-8")
    goals = (refs_root / "goals.md").read_text(encoding="utf-8")
    links = (refs_root / "links.md").read_text(encoding="utf-8")
    question_answer = (refs_root / "question-answer.md").read_text(encoding="utf-8")
    todoist = (refs_root / "todoist.md").read_text(encoding="utf-8")

    assert "Keywords:" not in classification
    assert "Known client/project names" not in classification
    assert "1-yearly-2025" not in goals
    assert "Prefer the closest real goal" in goals
    assert "1-yearly-2025" not in links
    assert "source note first" in links
    assert "first sentence" in question_answer
    assert "Источники:" in question_answer
    assert "2-5" in question_answer
    assert "do not collapse a long-running project into a very short answer" in (
        question_answer.casefold()
    )
    assert "Priority Keywords" not in todoist


def test_prompt_references_support_delegated_control_tasks() -> None:
    refs_root = SKILLS_TEMPLATE_ROOT / "dbrain-processor/references"

    ownership = (refs_root / "ownership.md").read_text(encoding="utf-8")
    todoist = (refs_root / "todoist.md").read_text(encoding="utf-8")
    plaud = (refs_root / "plaud.md").read_text(encoding="utf-8")

    assert "delegates the action to another person" in ownership
    assert "control task" in ownership
    assert "owner's controllable next step" in ownership
    assert "owner's control action" in todoist
    assert "delegates work to someone else" in plaud
    assert "owner-relevant control task" in plaud


def test_execute_prompt_includes_todoist_project_catalog(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    references_path = vault_path.parent / "skills/dbrain-processor/references"
    references_path.mkdir(parents=True, exist_ok=True)
    (references_path / "ownership.md").write_text(
        "owner={OWNER_FULL_NAME}",
        encoding="utf-8",
    )
    (references_path / "todoist-project-routing.md").write_text(
        "Inbox is fallback only",
        encoding="utf-8",
    )

    processor = CliProcessor(
        vault_path,
        todoist_api_key="todoist-token",
        owner_full_name="Иван Иванов",
    )
    processor._todoist_project_catalog_snapshot = lambda **kwargs: {  # type: ignore[method-assign]
        "fetched_at": "2026-04-04T10:00:00+00:00",
        "inbox_project_id": "inbox-id",
        "projects": [{"id": "work-id", "name": "Рабочие"}],
    }

    prompt = processor._build_execute_prompt(day)

    assert "TODOIST PROJECT ROUTING" in prompt
    assert "Inbox is fallback only" in prompt
    assert '"name": "Рабочие"' in prompt


def test_process_daily_interactive_uses_preview_without_side_effects(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    prompts: list[str] = []
    responses = iter(
        [
            json.dumps(
                {
                    "date": day.isoformat(),
                    "one_big_thing": "Finish follow-up",
                    "entries": [
                        {"classification": "task", "task_content": "Send follow-up"},
                        {"classification": "idea", "title": "Layered memory"},
                    ],
                },
                ensure_ascii=False,
            ),
            "⚡ **Быстрый разбор за 2026-04-04**",
        ]
    )
    maintenance = {"graph": False, "qmd": False, "memory": False}

    def fake_run(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    def mark_qmd() -> None:
        maintenance["qmd"] = True

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]
    processor._rebuild_graph = lambda: maintenance.__setitem__("graph", True)  # type: ignore[method-assign]
    processor._refresh_qmd_index = mark_qmd  # type: ignore[method-assign]
    processor._run_memory_decay = lambda: maintenance.__setitem__("memory", True)  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=INTERACTIVE_MODE)

    assert result["report"] == "⚡ **Быстрый разбор за 2026-04-04**"
    assert result["mode"] == INTERACTIVE_MODE
    assert result["processed_entries"] == 2
    assert len(prompts) == 2
    assert "capture.md" in prompts[0]
    assert "preview.md" in prompts[1]
    assert not (vault_path / ".session" / "capture.json").exists()
    assert maintenance == {"graph": False, "qmd": False, "memory": False}


def test_process_daily_interactive_preserves_existing_session_artifacts(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    session_path = vault_path / ".session"
    session_path.mkdir(parents=True)
    for file_name, content in {
        "handoff.md": "handoff\n",
        "capture.json": '{"old": "capture"}\n',
        "execute.json": '{"old": "execute"}\n',
        "capture-raw-output.txt": "old raw output\n",
        "memory-audit.md": "old audit\n",
    }.items():
        (session_path / file_name).write_text(content, encoding="utf-8")
    before = {
        path.relative_to(session_path): path.read_bytes()
        for path in session_path.rglob("*")
        if path.is_file()
    }

    processor = CliProcessor(vault_path)
    responses = iter(
        [
            json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": [{"classification": "idea", "title": "Preview"}],
                }
            ),
            "Preview report",
        ]
    )
    processor._run_vault_prompt = lambda prompt: next(responses)  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=INTERACTIVE_MODE)

    after = {
        path.relative_to(session_path): path.read_bytes()
        for path in session_path.rglob("*")
        if path.is_file()
    }
    assert result["report"] == "Preview report"
    assert after == before


def test_process_daily_interactive_malformed_capture_does_not_create_session_artifacts(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    prompts: list[str] = []
    responses = iter(
        [
            "not json",
            json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": [{"classification": "idea", "title": "Preview"}],
                }
            ),
            "Preview report",
        ]
    )
    processor = CliProcessor(vault_path)

    def fake_run(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=INTERACTIVE_MODE)

    assert result["report"] == "Preview report"
    assert len(prompts) == 3
    assert "CRITICAL OUTPUT CONTRACT" in prompts[1]
    assert not (vault_path / ".session").exists()


def test_process_daily_scheduled_persists_phase_files_and_runs_maintenance(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    stale_raw_output = vault_path / ".session" / "capture-raw-output.txt"
    stale_raw_output.parent.mkdir(parents=True)
    stale_raw_output.write_text("stale\n", encoding="utf-8")

    processor = CliProcessor(vault_path)
    prompts: list[str] = []
    responses = iter(
        [
            json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": [
                        {
                            "classification": "task",
                            "task_content": "Send follow-up",
                        }
                    ],
                    "stats": {"total_entries": 1},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "tasks_created": [{"id": "1", "content": "Send follow-up"}],
                    "thoughts_saved": [],
                },
                ensure_ascii=False,
            ),
            "📊 **Обработка за 2026-04-04**",
        ]
    )
    maintenance = {
        "creative": False,
        "health": False,
        "memory_audit": False,
        "qmd": False,
        "memory": False,
    }

    def fake_run(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    def mark_qmd() -> None:
        maintenance["qmd"] = True

    def mark_memory() -> None:
        maintenance["memory"] = True

    def mark_creative() -> None:
        maintenance["creative"] = True

    def mark_health() -> None:
        maintenance["health"] = True

    def mark_memory_audit() -> None:
        maintenance["memory_audit"] = True

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]
    processor._refresh_qmd_index = mark_qmd  # type: ignore[method-assign]
    processor._run_memory_decay = mark_memory  # type: ignore[method-assign]
    processor._capture_creative_recall = mark_creative  # type: ignore[method-assign]
    processor._run_vault_health_maintenance = mark_health  # type: ignore[method-assign]
    processor._capture_memory_audit = mark_memory_audit  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=SCHEDULED_MODE)
    capture_json = json.loads((vault_path / ".session" / "capture.json").read_text())
    execute_json = json.loads((vault_path / ".session" / "execute.json").read_text())

    assert result["report"] == "📊 **Обработка за 2026-04-04**"
    assert result["mode"] == SCHEDULED_MODE
    assert capture_json["date"] == day.isoformat()
    assert execute_json["tasks_created"][0]["id"] == "1"
    assert not stale_raw_output.exists()
    assert (vault_path / ".session" / "handoff.md").exists()
    assert len(prompts) == 3
    assert "capture.md" in prompts[0]
    assert "execute.md" in prompts[1]
    assert "reflect.md" in prompts[2]
    daily_content = (vault_path / "daily" / f"{day.isoformat()}.md").read_text(
        encoding="utf-8"
    )
    assert "<!-- d-brain:reflect:start -->" in daily_content
    assert "**Tasks created:** 1" in daily_content
    assert maintenance == {
        "creative": True,
        "health": True,
        "memory_audit": True,
        "qmd": True,
        "memory": True,
    }


def test_reflect_daily_block_repeat_does_not_duplicate_heading(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _write_vault_manifest(vault_path)
    processor = CliProcessor(vault_path)
    execute_data = {
        "tasks_created": [{"id": "1", "content": "Send follow-up"}],
        "thoughts_saved": [],
        "crm_updated": [],
    }

    processor._write_reflect_daily_block(day, execute_data)
    processor._write_reflect_daily_block(day, execute_data)

    content = (vault_path / "daily" / f"{day.isoformat()}.md").read_text(
        encoding="utf-8"
    )
    reflect_headings = [
        line
        for line in content.splitlines()
        if line.startswith("## ") and line.endswith(" [text]")
    ]
    assert len(reflect_headings) == 1
    assert content.count("<!-- d-brain:reflect:start -->") == 1
    assert content.count("<!-- d-brain:reflect:end -->") == 1
    assert content.count("d-brain processing") == 1


def test_process_daily_scheduled_preserves_side_effect_order(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    daily_file = vault_path / "daily" / f"{day.isoformat()}.md"
    processor = CliProcessor(vault_path)
    events: list[str] = []
    compact_calls = {"count": 0}

    processor._ensure_daily_file = lambda target_day: (  # type: ignore[method-assign]
        events.append("daily"),
        daily_file,
    )[1]
    processor._clear_session_phase_artifacts = (  # type: ignore[method-assign]
        lambda: events.append("clear-session")
    )
    processor._ensure_handoff_file = lambda: events.append("ensure-handoff")  # type: ignore[method-assign]

    def compact_handoff() -> None:
        compact_calls["count"] += 1
        events.append(f"compact-handoff-{compact_calls['count']}")

    processor._compact_handoff_file = compact_handoff  # type: ignore[method-assign]
    processor._log_graph_age_warning = lambda: events.append("graph-age")  # type: ignore[method-assign]

    def run_json_phase(
        prompt: str,
        *,
        phase_name: str,
        retry_on_parse_error: bool = True,
        persist_raw_output: bool = True,
    ) -> dict[str, object]:
        del prompt
        events.append(
            f"{phase_name}:retry={retry_on_parse_error}:raw={persist_raw_output}"
        )
        if phase_name == "capture":
            return {"entries": [{"classification": "task"}]}
        return {"tasks_created": [], "thoughts_saved": [], "crm_updated": []}

    processor._run_json_phase = run_json_phase  # type: ignore[method-assign]
    processor._build_capture_prompt = lambda target_day: "capture"  # type: ignore[method-assign]
    processor._build_execute_prompt = lambda target_day: "execute"  # type: ignore[method-assign]
    processor._build_reflect_prompt = lambda target_day: "reflect"  # type: ignore[method-assign]
    processor._apply_entry_status_guardrails = (  # type: ignore[method-assign]
        lambda **kwargs: events.append("guardrails")
    )
    processor._count_processed_entries = (  # type: ignore[method-assign]
        lambda capture_data: events.append("count") or 1
    )
    processor._touch_memory_paths = (  # type: ignore[method-assign]
        lambda *paths: events.append(f"touch:{','.join(paths)}")
    )
    processor._write_session_json = (  # type: ignore[method-assign]
        lambda file_name, payload: events.append(f"write:{file_name}")
    )
    processor._normalize_saved_thoughts = (  # type: ignore[method-assign]
        lambda execute_data, day: events.append("normalize-thoughts")
    )
    processor._run_memory_decay = lambda: events.append("memory-decay")  # type: ignore[method-assign]
    processor._capture_creative_recall = lambda: events.append("creative-recall")  # type: ignore[method-assign]
    processor._run_vault_health_maintenance = (  # type: ignore[method-assign]
        lambda: events.append("vault-health")
    )
    processor._capture_memory_audit = lambda: events.append("memory-audit")  # type: ignore[method-assign]
    processor._run_vault_prompt = lambda prompt: events.append(prompt) or "report"  # type: ignore[method-assign]
    processor._write_reflect_daily_block = (  # type: ignore[method-assign]
        lambda target_day, execute_data: events.append("write-daily")
    )
    processor._refresh_qmd_index = lambda: events.append("qmd")  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=SCHEDULED_MODE)

    assert result == {
        "report": "report",
        "processed_entries": 1,
        "mode": SCHEDULED_MODE,
    }
    assert events == [
        "clear-session",
        "daily",
        "ensure-handoff",
        "compact-handoff-1",
        "graph-age",
        "capture:retry=True:raw=True",
        "guardrails",
        "count",
        "write:capture.json",
        "execute:retry=False:raw=True",
        "write:execute.json",
        "normalize-thoughts",
        "memory-decay",
        "creative-recall",
        "vault-health",
        "memory-audit",
        "reflect",
        "compact-handoff-2",
        "write-daily",
        "qmd",
    ]


def test_process_daily_scheduled_does_not_retry_malformed_execute_output(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    processor = CliProcessor(vault_path)
    prompts: list[str] = []
    maintenance_calls: list[str] = []

    def fake_run(prompt: str) -> str:
        prompts.append(prompt)
        if "capture.md" in prompt:
            return json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": [{"classification": "task"}],
                }
            )
        if "execute.md" in prompt:
            return "not json"
        raise AssertionError("reflect must not run after malformed execute output")

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]
    processor._run_memory_decay = lambda: maintenance_calls.append("memory")  # type: ignore[method-assign]
    processor._capture_creative_recall = lambda: maintenance_calls.append("recall")  # type: ignore[method-assign]
    processor._run_vault_health_maintenance = lambda: maintenance_calls.append("health")  # type: ignore[method-assign]
    processor._capture_memory_audit = lambda: maintenance_calls.append("audit")  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=SCHEDULED_MODE)

    execute_prompts = [prompt for prompt in prompts if "execute.md" in prompt]
    assert "error" in result
    assert len(execute_prompts) == 1
    assert (vault_path / ".session" / "execute-raw-output.txt").exists()
    assert not (vault_path / ".session" / "execute-retry-raw-output.txt").exists()
    assert maintenance_calls == []


def test_process_daily_entry_status_already_processed_forces_skip_before_execute(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)
    (vault_path / "daily" / f"{day.isoformat()}.md").write_text(
        (
            f"# {day.isoformat()}\n\n"
            "## 10:00 [plaud]\n"
            "<!-- d-brain:entry-status: already_processed -->\n"
            "Нужно отправить follow-up сегодня.\n"
        ),
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)

    def fake_run(prompt: str) -> str:
        if "capture.md" in prompt:
            return json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": [
                        {
                            "classification": "task",
                            "task_content": "Send follow-up",
                            "task_priority": 4,
                        }
                    ],
                    "stats": {"total_entries": 1, "tasks": 1, "skipped": 0},
                },
                ensure_ascii=False,
            )
        if "execute.md" in prompt:
            capture_json = json.loads(
                (vault_path / ".session" / "capture.json").read_text(encoding="utf-8")
            )
            entry = capture_json["entries"][0]
            assert entry["classification"] == "skip"
            assert entry["entry_statuses"] == [ENTRY_STATUS_ALREADY_PROCESSED]
            assert entry["skip_reason"] == "entry_status:already_processed"
            assert "task_content" not in entry
            assert "task_priority" not in entry
            assert capture_json["stats"]["tasks"] == 0
            assert capture_json["stats"]["skipped"] == 1
            return json.dumps(
                {
                    "tasks_created": [],
                    "thoughts_saved": [],
                    "crm_updated": [],
                },
                ensure_ascii=False,
            )
        return "📊 **Обработка за 2026-04-04**"

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    processor._run_memory_decay = lambda: None  # type: ignore[method-assign]
    processor._capture_creative_recall = lambda *args, **kwargs: None  # type: ignore[method-assign]
    processor._run_vault_health_maintenance = lambda: None  # type: ignore[method-assign]
    processor._capture_memory_audit = lambda: None  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=SCHEDULED_MODE)

    assert result["processed_entries"] == 0


def test_process_daily_interactive_does_not_skip_short_valid_daily_entry(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 5)
    _setup_daily_processing_vault(vault_path, day)
    (vault_path / "daily" / f"{day.isoformat()}.md").write_text(
        f"# {day.isoformat()}\n\n## 09:00 [text]\nок\n",
        encoding="utf-8",
    )

    processor = CliProcessor(vault_path)
    responses = iter(
        [
            json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": [{"classification": "idea", "title": "ок"}],
                },
                ensure_ascii=False,
            ),
            "⚡ **Короткая запись обработана**",
        ]
    )
    processor._run_vault_prompt = lambda prompt: next(responses)  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=INTERACTIVE_MODE)

    assert result["mode"] == INTERACTIVE_MODE
    assert result["processed_entries"] == 1
    assert result["report"] == "⚡ **Короткая запись обработана**"
    assert result.get("empty_daily") is None


def test_process_daily_scheduled_refuses_duplicate_full_run(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    lock_path = vault_path / ".locks" / "full-process.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = processor.process_daily(day, mode=SCHEDULED_MODE)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    assert result["error"] == "Full processing is already running"
    assert result["processed_entries"] == 0
    assert not (vault_path / ".session" / "capture.json").exists()


def test_process_daily_scheduled_compacts_handoff_after_reflect(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    responses = iter(
        [
            json.dumps(
                {
                    "date": day.isoformat(),
                    "entries": [{"classification": "idea", "title": "ок"}],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "tasks_created": [],
                    "thoughts_saved": [],
                    "crm_updated": [],
                },
                ensure_ascii=False,
            ),
            "📊 **Обработка за 2026-04-04**",
        ]
    )

    def fake_run(prompt: str) -> str:
        output = next(responses)
        if "reflect.md" in prompt:
            (vault_path / ".session" / "handoff.md").write_text(
                """---
type: note
---
# Передача сессии

## Last Session
старый блок

## Observations
- [pattern] 2026-04-01: старый сигнал

## Last Session
новый блок

## In Progress
- незавершённый шаг

## Next Steps
- следующий шаг

## Observations
- [pattern] 2026-04-02: новый сигнал
""",
                encoding="utf-8",
            )
        return output

    processor._run_vault_prompt = fake_run  # type: ignore[method-assign]
    processor._refresh_qmd_index = lambda: None  # type: ignore[method-assign]
    processor._run_memory_decay = lambda: None  # type: ignore[method-assign]
    processor._capture_creative_recall = lambda: None  # type: ignore[method-assign]
    processor._run_vault_health_maintenance = lambda: None  # type: ignore[method-assign]
    processor._capture_memory_audit = lambda: None  # type: ignore[method-assign]
    processor._touch_memory_paths = lambda *paths: None  # type: ignore[method-assign]

    result = processor.process_daily(day, mode=SCHEDULED_MODE)
    content = (vault_path / ".session" / "handoff.md").read_text(encoding="utf-8")

    assert result["report"] == "📊 **Обработка за 2026-04-04**"
    assert content.count("## Last Session") == 1
    assert content.count("## Observations") == 1
    assert "старый блок" not in content
    assert "новый блок" in content
    assert "- [pattern] 2026-04-01: старый сигнал" in content
    assert "- [pattern] 2026-04-02: новый сигнал" in content


def test_answer_question_uses_creative_recall_and_touches_core_memory(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    state: dict[str, object] = {"creative": None, "touched": ()}
    processor._capture_creative_recall = (  # type: ignore[method-assign]
        lambda sample_size=2, file_name="creative-recall.txt": state.update(
            {"creative": (sample_size, file_name)}
        )
    )
    processor._touch_memory_paths = lambda *paths: state.__setitem__(  # type: ignore[method-assign]
        "touched",
        paths,
    )
    processor._run_assistant_prompt = lambda prompt: "🎯 **Ответ**"  # type: ignore[method-assign]

    result = processor.answer_question("Какие приоритеты на эту неделю?", user_id=42)

    assert result["report"] == "🎯 **Ответ**"
    assert state["creative"] == (2, "question-creative-recall.txt")
    assert state["touched"] == (
        "MEMORY.md",
        "goals/3-weekly.md",
        "goals/2-monthly.md",
        "goals/1-yearly-2026.md",
        "business/_index.md",
        "projects/_index.md",
    )


def test_answer_question_does_not_depend_on_uv_for_memory_steps(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    touched: list[str] = []

    class FakeMemoryEngine:
        @staticmethod
        def load_config(*args, **kwargs) -> dict[str, object]:  # noqa: ANN002, ANN003
            return {}

        @staticmethod
        def cmd_touch(filepath: str, config: dict[str, object]) -> None:
            touched.append(filepath)

        @staticmethod
        def cmd_creative(
            sample_size: int,
            target_dir: Path,
            config: dict[str, object],
        ) -> None:
            print(f"creative sample size={sample_size} dir={target_dir}")

    processor._load_memory_engine_module = lambda: FakeMemoryEngine()  # type: ignore[method-assign]
    processor._run_uv_script = lambda *args: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("uv helper should not be used in answer_question")
    )
    processor._run_uv_script_capture = lambda *args: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("uv helper should not be used in answer_question")
    )
    processor._run_assistant_prompt = lambda prompt: "🎯 **Ответ**"  # type: ignore[method-assign]

    result = processor.answer_question("Какие приоритеты на эту неделю?", user_id=42)

    assert result["report"] == "🎯 **Ответ**"
    assert touched == [
        str(vault_path / "MEMORY.md"),
        str(vault_path / "goals/3-weekly.md"),
        str(vault_path / "goals/2-monthly.md"),
        str(vault_path / "goals/1-yearly-2026.md"),
        str(vault_path / "business/_index.md"),
        str(vault_path / "projects/_index.md"),
    ]
    creative_recall = vault_path / ".session" / "question-creative-recall.txt"
    assert creative_recall.exists()
    assert f"creative sample size=2 dir={vault_path}" in creative_recall.read_text(
        encoding="utf-8"
    )


def test_answer_question_injects_auto_recall_block(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    processor._capture_creative_recall = lambda *args, **kwargs: None  # type: ignore[method-assign]
    processor._touch_memory_paths = lambda *paths: None  # type: ignore[method-assign]
    recall_block = (
        "=== AUTO ARCHIVE RECALL ===\nQMD recall\n=== END AUTO ARCHIVE RECALL ==="
    )
    processor._build_auto_recall_block = (  # type: ignore[method-assign]
        lambda task, *, purpose: recall_block
    )
    captured: dict[str, str] = {}

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return "🎯 **Ответ**"

    processor._run_assistant_prompt = fake_run  # type: ignore[method-assign]

    result = processor.answer_question("Что мы решили раньше?", user_id=7)

    assert result["report"] == "🎯 **Ответ**"
    assert "=== AUTO ARCHIVE RECALL ===" in captured["prompt"]
    assert captured["prompt"].index("=== AUTO ARCHIVE RECALL ===") < captured[
        "prompt"
    ].index("USER QUESTION:")
    assert "If that block includes FULLTEXT FOLLOW-UP" not in captured["prompt"]
    assert "history scope or explicit start point" in captured["prompt"]
    assert "history of the topic/question" in captured["prompt"]
    assert "latest snapshot." in captured["prompt"]
    assert (
        "Do not collapse a long-running project into a very short answer"
        in (captured["prompt"])
    )


def test_answer_question_injects_compiled_briefings_block(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    processor._capture_creative_recall = lambda *args, **kwargs: None  # type: ignore[method-assign]
    processor._touch_memory_paths = lambda *paths: None  # type: ignore[method-assign]
    processor._build_auto_recall_block = lambda *args, **kwargs: ""  # type: ignore[method-assign]
    processor._build_compiled_briefings_block = (  # type: ignore[method-assign]
        lambda question: (
            "=== COMPILED BRIEFINGS ===\n"
            "compiled/projects/example-project.md\n"
            "=== END COMPILED BRIEFINGS ==="
        )
    )
    captured: dict[str, str] = {}

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return "Ответ"

    processor._run_assistant_prompt = fake_run  # type: ignore[method-assign]

    result = processor.answer_question("Что по Example Project?", user_id=7)

    assert result["report"] == "Ответ"
    assert "=== COMPILED BRIEFINGS ===" in captured["prompt"]
    assert captured["prompt"].index("=== COMPILED BRIEFINGS ===") < captured[
        "prompt"
    ].index("USER QUESTION:")


def test_question_route_classifier_covers_planning_and_fact_lookup(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)

    assert (
        processor._classify_question_route("Какие приоритеты на эту неделю?")
        == "planning"
    )
    assert (
        processor._classify_question_route("Какой ID проекта Example Project?")
        == "fact_lookup"
    )
    assert (
        processor._classify_question_route("Что мы решили раньше по Example Project?")
        == "status_history"
    )


def test_classify_text_intent_returns_control_plane_workflow(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    processor._run_json_phase = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "intent": "question",
        "confidence": "high",
        "reason": "direct answer expected",
    }

    payload = processor.classify_text_intent("Какие приоритеты на эту неделю?")

    assert payload["intent"] == "question"
    assert payload["workflow"] == "question.planning"
    assert payload["workflow_kind"] == "question"


def test_answer_question_injects_planning_route_block(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    processor._capture_creative_recall = lambda *args, **kwargs: None  # type: ignore[method-assign]
    processor._touch_memory_paths = lambda *paths: None  # type: ignore[method-assign]
    processor._build_auto_recall_block = lambda *args, **kwargs: ""  # type: ignore[method-assign]
    processor._build_compiled_briefings_block = lambda *args, **kwargs: ""  # type: ignore[method-assign]
    captured: dict[str, str] = {}

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return "Ответ"

    processor._run_assistant_prompt = fake_run  # type: ignore[method-assign]

    result = processor.answer_question("Какие приоритеты на эту неделю?", user_id=7)

    assert result["report"] == "Ответ"
    assert "=== QUESTION ROUTE ===" in captured["prompt"]
    assert "Route: planning" in captured["prompt"]
    assert "Use curated core context first" in captured["prompt"]


def test_answer_question_places_fact_lookup_recall_before_compiled(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    processor._capture_creative_recall = lambda *args, **kwargs: None  # type: ignore[method-assign]
    processor._touch_memory_paths = lambda *paths: None  # type: ignore[method-assign]
    processor._build_auto_recall_block = (  # type: ignore[method-assign]
        lambda *args, **kwargs: (
            "=== AUTO ARCHIVE RECALL ===\nQMD recall\n=== END AUTO ARCHIVE RECALL ==="
        )
    )
    processor._build_compiled_briefings_block = (  # type: ignore[method-assign]
        lambda *args, **kwargs: (
            "=== COMPILED BRIEFINGS ===\n"
            "compiled/projects/example-project.md\n"
            "=== END COMPILED BRIEFINGS ==="
        )
    )
    captured: dict[str, str] = {}

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return "Ответ"

    processor._run_assistant_prompt = fake_run  # type: ignore[method-assign]

    result = processor.answer_question("Какой ID проекта Example Project?", user_id=7)

    assert result["report"] == "Ответ"
    assert "Route: fact_lookup" in captured["prompt"]
    assert captured["prompt"].index("=== AUTO ARCHIVE RECALL ===") < captured[
        "prompt"
    ].index("=== COMPILED BRIEFINGS ===")


def test_answer_question_files_useful_output_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault_path = tmp_path / "vault"
    day = date(2026, 4, 4)
    _setup_daily_processing_vault(vault_path, day)

    processor = CliProcessor(vault_path)
    processor._capture_creative_recall = lambda *args, **kwargs: None  # type: ignore[method-assign]
    processor._touch_memory_paths = lambda *paths: None  # type: ignore[method-assign]
    processor._build_auto_recall_block = lambda *args, **kwargs: ""  # type: ignore[method-assign]
    processor._build_compiled_briefings_block = lambda *args, **kwargs: ""  # type: ignore[method-assign]
    filed: list[tuple[str, str, str]] = []

    def fake_file(self, *, request: str, output_markdown: str, artifact_type: str):  # noqa: ANN001
        filed.append((request, output_markdown, artifact_type))
        return "summaries/answers/2026/04/demo.md"

    monkeypatch.setattr(CompiledBriefingService, "file_output_artifact", fake_file)
    processor._run_assistant_prompt = (  # type: ignore[method-assign]
        lambda prompt: (
            "Статус по теме.\n\n- Первый факт\n- Второй факт\n- Третий факт\n"
        )
    )

    result = processor.answer_question("Что мы решили по проекту?", user_id=7)

    assert "- Первый факт" in result["report"]
    assert filed == [
        (
            "Что мы решили по проекту?",
            result["report"],
            "question-answer",
        )
    ]


def test_execute_prompt_injects_auto_recall_block(
    tmp_path: Path,
    write_vault_manifest,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir(parents=True)
    write_vault_manifest(vault_path)
    skill_path = vault_path.parent / "skills/vault-retrieval/SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("retrieval-contract", encoding="utf-8")
    processor = CliProcessor(vault_path)
    processor._build_injected_context = lambda **kwargs: "core-context"  # type: ignore[method-assign]
    recall_block = (
        "=== AUTO ARCHIVE RECALL ===\nQMD recall\n=== END AUTO ARCHIVE RECALL ==="
    )
    seen: dict[str, str] = {}
    processor._build_auto_recall_block = (  # type: ignore[method-assign]
        lambda task, *, purpose: seen.update({"task": task, "purpose": purpose})
        or recall_block
    )
    captured: dict[str, str] = {}

    def fake_run(prompt: str) -> str:
        captured["prompt"] = prompt
        return "📌 **Готово**"

    processor._run_assistant_prompt = fake_run  # type: ignore[method-assign]

    result = processor.execute_prompt("Найди старое решение по этой теме", user_id=7)

    assert result["report"] == "📌 **Готово**"
    assert "=== AUTO ARCHIVE RECALL ===" in captured["prompt"]
    assert captured["prompt"].count("=== VAULT RETRIEVAL SKILL ===") == 1
    assert captured["prompt"].index("=== AUTO ARCHIVE RECALL ===") < captured[
        "prompt"
    ].index("USER REQUEST:")
    assert (
        "If an AUTO ARCHIVE RECALL block includes FULLTEXT FOLLOW-UP"
        not in (captured["prompt"])
    )
    assert "history scope or explicit start point" in captured["prompt"]
    assert "history of that topic/question" in captured["prompt"]
    assert "latest snapshot." in captured["prompt"]
    assert (
        "Do not collapse a long-running project into a very short answer"
        in (captured["prompt"])
    )
    assert seen == {
        "task": "Найди старое решение по этой теме",
        "purpose": "assistant_request",
    }


def test_normalize_owner_report_markdown_supports_english_output(
    tmp_path: Path,
) -> None:
    processor = CliProcessor(tmp_path / "vault", content_language="en")

    markdown_body = processor._normalize_owner_report_markdown(
        "Here is the final digest:\n\n# Weekly Digest\n\n- **One**\n- *Two*"
    )

    assert markdown_body.startswith("# Weekly Digest")
    assert "Here is the final digest" not in markdown_body
    assert "- **One**" in markdown_body
    assert "- *Two*" in markdown_body
