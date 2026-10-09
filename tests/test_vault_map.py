import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from d_brain.bot.handlers import menu
from d_brain.services.vault_map import export_vault_map


def test_export_contains_metadata_and_resolved_links_without_note_bodies(tmp_path):
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects/a.md").write_text(
        '---\ntitle: "Проект Альфа"\ndescription: "Краткое описание"\n---\n'
        "SECRET BODY [[b]] [[missing]]",
        encoding="utf-8",
    )
    (tmp_path / "projects/b.md").write_text(
        '---\ndescription: "</script><script>alert(1)</script>"\n---\nOTHER SECRET',
        encoding="utf-8",
    )
    output = export_vault_map(tmp_path)
    html = output.read_text()
    payload = re.search(
        r'<script id="map-data" type="application/json">(.*?)</script>', html
    )
    data = json.loads(payload.group(1))
    assert output == tmp_path / "attachments/vault-map.html"
    assert data["links"] == {"projects/a": ["projects/b"]}
    assert data["notes"][0]["title"] == "Проект Альфа"
    assert data["notes"][0]["description"] == "Краткое описание"
    assert "SECRET" not in html
    assert "</script><script>alert" not in html
    assert data["notes"][1]["description"] == "</script><script>alert(1)</script>"
    assert "<script src=" not in html
    assert "__VAULT_MAP_DATA__" not in html


def test_export_empty_vault(tmp_path):
    output = export_vault_map(tmp_path)
    assert '"notes":[]' in output.read_text()


@pytest.mark.asyncio
async def test_menu_builds_and_sends_map(tmp_path, monkeypatch):
    (tmp_path / "example.md").write_text("# Example\nprivate text", encoding="utf-8")
    monkeypatch.setattr(
        menu, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )
    bot = SimpleNamespace(send_document=AsyncMock())
    query = SimpleNamespace(
        data="menu:vaultmap",
        answer=AsyncMock(),
        message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=42),
        from_user=SimpleNamespace(id=7),
    )
    state = SimpleNamespace(clear=AsyncMock())
    await menu.handle_menu_callback(query, bot, state)
    query.answer.assert_awaited_once()
    state.clear.assert_awaited_once()
    sent = bot.send_document.await_args.kwargs
    assert sent["chat_id"] == 7
    assert sent["parse_mode"] is None
    assert Path(sent["document"].path).is_file()
    assert Path(sent["document"].path).name == "vault-map.html"


@pytest.mark.asyncio
async def test_menu_reports_export_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        menu, "get_settings", lambda: SimpleNamespace(vault_path=tmp_path)
    )

    def fail(_path):
        raise OSError("write failed")

    monkeypatch.setattr(menu, "export_vault_map", fail)
    reply = AsyncMock()
    monkeypatch.setattr(menu, "answer_text", reply)
    bot = SimpleNamespace(send_document=AsyncMock())
    query = SimpleNamespace(
        data="menu:vaultmap",
        answer=AsyncMock(),
        message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=42),
        from_user=SimpleNamespace(id=7),
    )
    await menu.handle_menu_callback(query, bot, SimpleNamespace(clear=AsyncMock()))
    bot.send_document.assert_not_awaited()
    reply.assert_awaited_once()


def test_graph_refresh_also_exports_html(tmp_path):
    from d_brain.services.processor import CliProcessor

    calls = []
    host = SimpleNamespace(
        vault_path=tmp_path,
        _run_uv_script=calls.append,
        _load_graph_stats=lambda: {"notes": {}, "links_from": {}},
    )
    CliProcessor._rebuild_graph(host)
    assert calls == ["skills/graph-builder/scripts/analyze.py"]
    assert (tmp_path / "attachments/vault-map.html").is_file()
