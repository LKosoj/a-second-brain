"""Standalone navigation snapshot of the vault, without note bodies."""

import json
import runpy
from datetime import datetime
from pathlib import Path
from typing import Any

from d_brain.services.frontmatter import (
    FrontmatterError,
    atomic_write_bytes,
    read_frontmatter,
)


def export_vault_map(
    vault_path: Path | str, graph: dict[str, Any] | None = None
) -> Path:
    """Build an offline HTML map using the existing wiki-link analyzer."""
    vault_path = Path(vault_path)
    resources = Path(__file__).resolve().parents[1] / "resources"
    if graph is None:
        analyzer = runpy.run_path(
            str(resources / "project_template/skills/graph-builder/scripts/analyze.py")
        )
        graph = analyzer["analyze_vault"](vault_path)
    notes = []
    for key, info in graph.get("notes", {}).items():
        try:
            fields = read_frontmatter(vault_path / info["path"]).fields
        except (FrontmatterError, OSError):
            fields = {}
        notes.append(
            {
                "id": key,
                "title": str(fields.get("title") or info["title"]),
                "path": info["path"],
                "description": " ".join(str(fields.get("description") or "").split()),
            }
        )
    data = {
        "built": datetime.now().astimezone().isoformat(timespec="minutes"),
        "notes": notes,
        "links": graph.get("links_from", {}),
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # A note's metadata must not terminate the embedded JSON script element.
    payload = payload.replace("<", "\\u003c").replace("&", "\\u0026")
    template = (resources / "vault_map.html").read_text(encoding="utf-8")
    output = vault_path / "attachments" / "vault-map.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        output, template.replace("__VAULT_MAP_DATA__", payload).encode("utf-8")
    )
    return output
