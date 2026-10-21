"""Select a light model from a CLI's own aliases or configured catalog."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)


def model_unavailable(detail: str) -> bool:
    """Recognize model rejection without retrying unrelated CLI failures."""
    return bool(re.search(
        r"model[_ -]?(?:not[_ -]?found|not[_ -]?supported|unavailable)"
        r"|(?:unknown|unsupported|invalid|no such) model"
        r"|model[^\n]{0,200}(?:not supported|not found|does not exist|"
        r"not available|unavailable|not configured|not allowed|no access|"
        r"not accessible|not enabled|overloaded|capacity)"
        r"|(?:unknown|unrecognized) (?:option|argument)[^\n]*--model",
        detail.lower(),
    ))


def _pick_light_model(models: list[str]) -> str | None:
    def rank(model: str) -> tuple[int, tuple[int, ...], str]:
        name = model.rsplit("/", 1)[-1].lower()
        tier = 2
        if re.search(r"(?:^|[-.])(flash-lite|nano|tiny)(?:$|[-.])", name):
            tier = 0
        elif re.search(r"(?:^|[-.])(haiku|luna|mini|flash|build-fast)(?:$|[-.])", name):
            tier = 1
        version = tuple(int(part) for part in re.findall(r"\d+", name))
        return -tier, version, model

    candidates = [model for model in models if rank(model)[0] > -2]
    return max(candidates, key=rank) if candidates else None


def resolve_light_model(
    cli: str,
    workdir: Path,
    env: Mapping[str, str],
    *,
    timeout: float = 10,
) -> str | None:
    """Return a light model, or None to keep the CLI's normal default.

    Catalog membership is not an access check. The runner handles rejection
    of the selected model when the actual question is sent.
    """
    cli = cli.removesuffix("-tmux")
    if cli == "claude":
        return "haiku"
    if cli == "gemini":
        return "flash-lite"

    def catalog(*args: str) -> str:
        result = subprocess.run(
            [cli, *args],
            cwd=workdir,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return result.stdout

    try:
        models: list[str] = []
        if cli == "codex":
            payload = json.loads(catalog("debug", "models"))
            models = [
                model["slug"]
                for model in payload["models"]
                if model.get("visibility") != "hide"
            ]
        elif cli == "grok":
            models = re.findall(
                r"(?m)^\s*[-*]\s+([\w.-]+)", catalog("models")
            )
        elif cli == "kimi":
            payload = json.loads(catalog("provider", "list", "--json"))
            models = [
                alias
                for alias, model in payload.get("models", {}).items()
                if "tool_use" in model.get("capabilities", [])
            ]
        elif cli == "qwen":
            home = Path(env.get("HOME", str(Path.home())))
            for root in dict.fromkeys((home, workdir.parent, workdir)):
                path = root / ".qwen" / "settings.json"
                if not path.exists():
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                for entries in payload.get("modelProviders", {}).values():
                    models.extend(model["id"] for model in entries)
        elif cli == "opencode":
            output = catalog("models", "--refresh", "--verbose")
            decoder = json.JSONDecoder()
            for match in re.finditer(r"(?m)^([^\s/]+/[^\s]+)\n(?=\{)", output):
                model, _end = decoder.raw_decode(output, match.end())
                if (
                    model.get("status", "active") == "active"
                    and model.get("capabilities", {}).get("toolcall", False)
                ):
                    models.append(match[1])
        return _pick_light_model(models)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Some CLI versions have no catalog command. Do not log raw config
        # or command output: provider catalogs can include credentials.
        logger.warning("Cannot read light-model catalog for %s; using default", cli)
        return None
