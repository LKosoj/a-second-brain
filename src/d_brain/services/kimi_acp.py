"""Run Kimi Code through its ACP stdio interface."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from acp import PROTOCOL_VERSION, Client, RequestError, spawn_agent_process, text_block
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    ClientCapabilities,
    DeclineElicitationResponse,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
)


class _KimiAcpClient:
    def __init__(self) -> None:
        self.text_parts: list[str] = []

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        del session_id, tool_call, kwargs
        for option in options:
            if option.kind in {"allow_once", "allow_always"}:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(
                        option_id=option.option_id,
                        outcome="selected",
                    )
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,
    ) -> None:
        del session_id, kwargs
        if isinstance(update, AgentMessageChunk) and isinstance(
            update.content, TextContentBlock
        ):
            self.text_parts.append(update.content.text)

    async def write_text_file(self, **kwargs: Any) -> None:
        del kwargs
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(self, **kwargs: Any) -> None:
        del kwargs
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_elicitation(
        self,
        message: str,
        mode: Any,
        **kwargs: Any,
    ) -> DeclineElicitationResponse:
        del message, mode, kwargs
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(
        self,
        elicitation_id: str,
        **kwargs: Any,
    ) -> None:
        del elicitation_id, kwargs


async def _run_kimi_acp(
    prompt: str,
    workdir: Path,
    env: Mapping[str, str],
) -> str:
    client = _KimiAcpClient()
    async with spawn_agent_process(
        cast(Client, client),
        "kimi",
        "acp",
        env=env,
        cwd=workdir,
        transport_kwargs={"stderr": asyncio.subprocess.DEVNULL},
    ) as (connection, _process):
        await connection.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
        )
        session = await connection.new_session(cwd=str(workdir), mcp_servers=[])
        await connection.prompt(
            session_id=session.session_id,
            prompt=[text_block(prompt)],
        )
        await asyncio.sleep(0)

    result = "".join(client.text_parts).strip()
    if not result:
        raise RuntimeError("Failed to recover assistant text from Kimi ACP output")
    return result


def run_kimi_acp(
    prompt: str,
    workdir: Path,
    env: Mapping[str, str],
    timeout: int,
) -> str:
    """Run one isolated Kimi ACP session and return final assistant text."""

    return asyncio.run(
        asyncio.wait_for(
            _run_kimi_acp(prompt, workdir, env),
            timeout=timeout,
        )
    )
