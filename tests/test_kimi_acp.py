from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp import RequestError, text_block
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    ResourceContentBlock,
)

from d_brain.services import kimi_acp
from d_brain.services.kimi_acp import _KimiAcpClient, _run_kimi_acp, run_kimi_acp


def test_kimi_timeout_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pid"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "kimi"
    fake.write_text(
        f"#!/bin/sh\nsleep 300 &\necho $! > {pid_file}\nsleep 300\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    with pytest.raises(TimeoutError):
        run_kimi_acp("hello", tmp_path, {"PATH": f"{bin_dir}:/usr/bin:/bin"}, 2)

    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return
        if state in {"Z", "X"}:
            return
        time.sleep(0.05)
    raise AssertionError("grandchild of kimi survived the timeout")


def _option(option_id: str, kind: str) -> PermissionOption:
    return PermissionOption(option_id=option_id, name=option_id, kind=kind)


async def test_permission_picks_the_first_allowing_option() -> None:
    response = await _KimiAcpClient().request_permission(
        "s1",
        None,
        [
            _option("no", "reject_once"),
            _option("yes-always", "allow_always"),
            _option("yes-once", "allow_once"),
        ],
    )

    assert isinstance(response.outcome, AllowedOutcome)
    assert response.outcome.option_id == "yes-always"


async def test_permission_is_cancelled_when_nothing_allows() -> None:
    response = await _KimiAcpClient().request_permission(
        "s1", None, [_option("no", "reject_once"), _option("never", "reject_always")]
    )

    assert isinstance(response.outcome, DeniedOutcome)
    assert response.outcome.outcome == "cancelled"


async def test_permission_is_cancelled_without_options() -> None:
    response = await _KimiAcpClient().request_permission("s1", None, [])

    assert isinstance(response.outcome, DeniedOutcome)


async def test_session_update_collects_only_agent_text() -> None:
    client = _KimiAcpClient()

    await client.session_update(
        "s1",
        AgentMessageChunk(
            content=text_block("Hello "), session_update="agent_message_chunk"
        ),
    )
    # the model's reasoning, non-text content and other updates are not answer text
    await client.session_update(
        "s1",
        AgentThoughtChunk(
            content=text_block("thinking"), session_update="agent_thought_chunk"
        ),
    )
    await client.session_update(
        "s1",
        AgentMessageChunk(
            content=ResourceContentBlock(
                name="f", uri="file:///f", type="resource_link"
            ),
            session_update="agent_message_chunk",
        ),
    )
    await client.session_update("s1", SimpleNamespace(content=text_block("x")))
    await client.session_update(
        "s1",
        AgentMessageChunk(
            content=text_block("world"), session_update="agent_message_chunk"
        ),
    )

    assert client.text_parts == ["Hello ", "world"]


@pytest.mark.parametrize("method", ["write_text_file", "read_text_file"])
async def test_file_access_is_refused_as_method_not_found(method: str) -> None:
    with pytest.raises(RequestError) as excinfo:
        await getattr(_KimiAcpClient(), method)(path="/etc/passwd", session_id="s1")

    assert excinfo.value.code == -32601


async def test_elicitation_is_declined() -> None:
    client = _KimiAcpClient()

    response = await client.create_elicitation("Pick one", "form")

    assert response.action == "decline"
    assert await client.complete_elicitation("e1") is None


def _fake_spawn(monkeypatch: pytest.MonkeyPatch, *, reply: list[str]) -> dict:
    """Replace the real ``kimi`` process with a connection that "answers"."""
    seen: dict = {"args": None, "workdir": None}
    connection = SimpleNamespace(
        initialize=AsyncMock(),
        new_session=AsyncMock(return_value=SimpleNamespace(session_id="s1")),
    )

    @asynccontextmanager
    async def spawn(client, args, env, workdir):  # noqa: ANN001
        seen["args"], seen["workdir"], seen["env"] = args, workdir, env

        async def prompt(**kwargs):  # noqa: ANN003
            seen["prompt"] = kwargs
            for part in reply:
                await client.session_update(
                    "s1",
                    AgentMessageChunk(
                        content=text_block(part), session_update="agent_message_chunk"
                    ),
                )

        connection.prompt = prompt
        yield connection

    monkeypatch.setattr(kimi_acp, "_spawn_kimi", spawn)
    seen["connection"] = connection
    return seen


async def test_run_returns_the_joined_stripped_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_spawn(monkeypatch, reply=["  Answer ", "part two\n"])

    result = await _run_kimi_acp("Question?", tmp_path, {"K": "v"})

    assert result == "Answer part two"
    assert seen["args"] == ["acp"]
    assert seen["workdir"] == tmp_path
    assert seen["env"] == {"K": "v"}
    assert seen["prompt"]["session_id"] == "s1"
    assert seen["prompt"]["prompt"] == [text_block("Question?")]
    seen["connection"].initialize.assert_awaited_once()
    seen["connection"].new_session.assert_awaited_once_with(
        cwd=str(tmp_path), mcp_servers=[]
    )


async def test_run_passes_the_model_before_the_acp_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_spawn(monkeypatch, reply=["ok"])

    await _run_kimi_acp("q", tmp_path, {}, "example-mini")

    assert seen["args"] == ["--model", "example-mini", "acp"]


@pytest.mark.parametrize("reply", [[], ["   "], ["\n", " "]])
async def test_run_fails_when_the_agent_said_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: list[str]
) -> None:
    _fake_spawn(monkeypatch, reply=reply)

    with pytest.raises(RuntimeError, match="Failed to recover assistant text"):
        await _run_kimi_acp("q", tmp_path, {})
