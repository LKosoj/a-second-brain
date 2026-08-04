"""Project-local qmd integration for semantic vault retrieval."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from d_brain.manifest import load_manifest_for_vault
from d_brain.services.epistemic_memory import (
    EpistemicMemoryError,
    parse_epistemic_note,
)
from d_brain.services.memory_entries import DailyEntryMemoryStore

DEFAULT_QMD_EMBED_MODEL = (
    "hf:Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf"
)
QMD_ENV_FORWARD_KEYS = (
    "EMB_MODEL",
    "RERANK_MODEL",
    "OPENAI_API_KEY",
    "BASE_URL",
    "OPENAI_BASE_URL",
)
DEFAULT_RECALL_LIMIT = 5
QMD_RECALL_TIMEOUT_SECONDS = 600
_EMBED_PROCESS_TIMEOUT = 3600
RECALL_CONFIDENCE_THRESHOLD = 0.5
RECALL_LOG_LIMIT = 20
RECALL_TIER_BONUS = {
    "core": 0.18,
    "active": 0.10,
    "warm": 0.04,
    "cold": -0.03,
    "archive": -0.08,
}
SUPERSEDED_RESULT_PENALTY = -0.35
logger = logging.getLogger(__name__)
DATE_IN_PATH_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")


def _normalize_subprocess_output(value: bytes | str | None) -> str:
    """Normalize partial subprocess output captured during a timeout."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


class QmdService:
    """Manage a project-local qmd index without polluting global user state."""

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = Path(vault_path).resolve()
        self._manifest = load_manifest_for_vault(self.vault_path)
        self._state_root = self.vault_path / ".qmd"
        self._config_dir = self._state_root / "config"
        self._cache_home = self._state_root / "cache"

    @property
    def _config_path(self) -> Path:
        return self._config_dir / f"{self._manifest.qmd_index}.yml"

    @property
    def _lock_path(self) -> Path:
        return self._state_root / "qmd.lock"

    @property
    def _db_path(self) -> Path:
        return self._cache_home / "qmd" / f"{self._manifest.qmd_index}.sqlite"

    @property
    def _embed_state_path(self) -> Path:
        return self._state_root / "embed-state.json"

    @property
    def _embed_script_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "scripts" / "qmd_embed_full.mjs"

    @property
    def _remote_query_script_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "scripts" / "qmd_query_vec.mjs"

    @property
    def _project_env_path(self) -> Path:
        return self.vault_path.parent / ".env"

    def _collections(self) -> list[tuple[str, Path, str]]:
        return [
            (
                "core",
                self.vault_path,
                "Корневой личный контекст: MEMORY, основные индексы "
                "и ключевые заметки верхнего уровня.",
            ),
            (
                "daily",
                self.vault_path / "daily",
                "Ежедневные записи, быстрые мысли, текстовые входящие "
                "и дневниковый контекст.",
            ),
            (
                "thoughts",
                self.vault_path / "thoughts",
                "Обработанные мысли, заметки и тематические карточки "
                "для долгосрочного поиска.",
            ),
            (
                "compiled",
                self.vault_path / "compiled",
                "Derived compiled briefings: operational summaries across "
                "projects, people, topics, decisions and meetings.",
            ),
            (
                "plaud",
                self.vault_path / "imports" / "plaud" / "notes",
                "Транскрипты и summary из PLAUD: встречи, звонки, "
                "голосовые заметки и подготовка ко встречам.",
            ),
            (
                "documents",
                self.vault_path / "imports" / "documents" / "notes",
                "Саммари и метаданные импортированных документов: pdf, docx, xlsx, "
                "pptx, html и других поддержанных форматов.",
            ),
            (
                "youtube",
                self.vault_path / "imports" / "youtube" / "notes",
                "Саммари и метаданные YouTube-роликов с сохранёнными "
                "транскриптами и ссылками на исходные видео.",
            ),
            (
                "goals",
                self.vault_path / "goals",
                "Цели, приоритеты, недельный и месячный фокус, направления развития.",
            ),
            (
                "business",
                self.vault_path / "business",
                "Бизнес-контекст: CRM, клиенты, партнёры, события, "
                "сделки и рабочие отношения.",
            ),
            (
                "projects",
                self.vault_path / "projects",
                "Клиентские и личные проекты, лиды, pipeline и "
                "активные направления работы.",
            ),
            (
                "summaries",
                self.vault_path / "summaries",
                "Недельные и другие агрегированные сводки по событиям и прогрессу.",
            ),
            (
                "moc",
                self.vault_path / "MOC",
                "Карты контента и навигационные узлы vault.",
            ),
        ]

    @staticmethod
    def _should_bootstrap_collection_dir(name: str, path: Path) -> bool:
        """Create generated note roots eagerly so qmd can index them from day one."""
        if name == "compiled":
            return True
        if name not in {"documents", "youtube"}:
            return False
        return path.as_posix().endswith("/notes")

    def ensure_local_config(self) -> Path:
        """Write a deterministic qmd config under the vault state directory."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._cache_home.mkdir(parents=True, exist_ok=True)

        lines = ["collections:"]
        for name, path, context in self._collections():
            if self._should_bootstrap_collection_dir(name, path):
                path.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                continue
            pattern = "*.md" if path == self.vault_path else "**/*.md"
            lines.extend(
                [
                    f"  {name}:",
                    f'    path: "{path}"',
                    f'    pattern: "{pattern}"',
                    "    context:",
                    f'      "/": "{context}"',
                ]
            )
        self._config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self._config_path

    def _load_project_env(self) -> dict[str, str]:
        """Best-effort `.env` loader for subprocesses launched outside Pydantic."""
        if not self._project_env_path.exists():
            return {}
        values: dict[str, str] = {}
        for raw_line in self._project_env_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            normalized_key = key.strip()
            normalized_value = value.strip()
            if (
                len(normalized_value) >= 2
                and normalized_value[0] == normalized_value[-1]
                and normalized_value[0] in {'"', "'"}
            ):
                normalized_value = normalized_value[1:-1]
            if normalized_key in QMD_ENV_FORWARD_KEYS:
                values[normalized_key] = normalized_value
        return values

    def _resolved_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        """Project-local environment with `.env` taking precedence over shell state."""
        scope = os.environ.copy()
        scope.update(self._load_project_env())
        if env is not None:
            scope.update(env)
        return scope

    @staticmethod
    def _remote_embed_model(env: dict[str, str]) -> str:
        """Return the project-selected external embedding model, if any."""
        return env.get("EMB_MODEL", "").strip()

    def _desired_embed_model(self, env: dict[str, str] | None = None) -> str:
        """Resolve the embedding model requested for project-local qmd flows."""
        scope = self._resolved_env(env)
        return self._remote_embed_model(scope) or DEFAULT_QMD_EMBED_MODEL

    @staticmethod
    def _desired_rerank_model(env: dict[str, str]) -> str:
        """Return the optional rerank model configured for remote retrieval."""
        return env.get("RERANK_MODEL", "").strip()

    def _uses_remote_embeddings(self, env: dict[str, str] | None = None) -> bool:
        """Detect OpenAI-compatible embedding backends versus local GGUF models."""
        return bool(self._remote_embed_model(self._resolved_env(env)))

    def build_env(self) -> dict[str, str]:
        """Environment for qmd, scoped to the current project."""
        self.ensure_local_config()
        env = self._resolved_env()
        env["QMD_CONFIG_DIR"] = str(self._config_dir)
        env["XDG_CACHE_HOME"] = str(self._cache_home)
        env["QMD_DB_PATH"] = str(self._db_path)
        env["QMD_EMBED_MODEL"] = self._desired_embed_model(env)
        env["QMD_RERANK_MODEL"] = self._desired_rerank_model(env)
        env.setdefault("NODE_LLAMA_CPP_GPU", "off")
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "dumb")
        return env

    def _collection_roots(self) -> dict[str, Path]:
        """Resolve qmd collection names to vault-relative roots."""
        roots: dict[str, Path] = {}
        for name, path, _context in self._collections():
            if not path.exists():
                continue
            try:
                roots[name] = path.relative_to(self.vault_path)
            except ValueError:
                roots[name] = Path(".")
        return roots

    def _uri_to_rel_path(self, uri: str) -> str | None:
        """Map qmd://collection/path.md back to a vault-relative markdown path."""
        if not uri.startswith("qmd://"):
            return None
        location = uri[len("qmd://") :]
        collection, sep, remainder = location.partition("/")
        if not sep:
            return None
        root = self._collection_roots().get(collection)
        if root is None:
            return None
        rel_path = (root / remainder) if str(root) != "." else Path(remainder)
        return rel_path.as_posix()

    @staticmethod
    def _read_frontmatter_signal(path: Path) -> dict[str, Any] | None:
        """Best-effort tier/relevance extraction from note frontmatter."""
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.startswith("---\n"):
            return None
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if match is None:
            return None
        fields: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        try:
            relevance = float(fields.get("relevance", "0") or 0)
        except ValueError:
            relevance = 0.0
        signal: dict[str, Any] = {
            "tier": fields.get("tier", ""),
            "relevance": relevance,
            "last_accessed": fields.get("last_accessed", ""),
            "date": fields.get("date", ""),
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
            "entries": 1,
        }
        try:
            epistemic = parse_epistemic_note(content.encode("utf-8"))
        except EpistemicMemoryError:
            epistemic = None
        if epistemic is not None:
            signal.update(
                {
                    "epistemic_confidence": epistemic.confidence,
                    "epistemic_scope": epistemic.scope,
                    "epistemic_state": epistemic.state,
                    "superseded_by": epistemic.superseded_by or "",
                }
            )
        return signal

    def _memory_signal_for_rel_path(self, rel_path: str) -> dict[str, Any] | None:
        """Load the strongest memory signal for one vault-relative markdown path."""
        if rel_path.startswith("daily/"):
            signal = DailyEntryMemoryStore(self.vault_path).best_signal_for_path(
                rel_path
            )
            if signal is None:
                return None
            return {
                "tier": signal.tier,
                "relevance": signal.relevance,
                "last_accessed": signal.last_accessed,
                "date": "",
                "created": "",
                "updated": "",
                "entries": signal.entries,
            }
        return self._read_frontmatter_signal(self.vault_path / rel_path)

    @staticmethod
    def _parse_iso_date(value: str) -> date | None:
        """Best-effort YYYY-MM-DD parsing."""
        raw = str(value or "").strip()
        if len(raw) < 10:
            return None
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None

    def _record_date_for_rel_path(
        self,
        rel_path: str,
        signal: dict[str, Any] | None,
    ) -> date | None:
        """Infer one note/event date to use as a recency signal."""
        if rel_path:
            match = DATE_IN_PATH_RE.search(rel_path)
            if match is not None:
                parsed = self._parse_iso_date(match.group("date"))
                if parsed is not None:
                    return parsed

        if signal:
            for key in ("date", "created", "updated"):
                parsed = self._parse_iso_date(str(signal.get(key, "")))
                if parsed is not None:
                    return parsed

        path = self.vault_path / rel_path if rel_path else None
        if path and path.exists():
            try:
                return date.fromtimestamp(path.stat().st_mtime)
            except OSError:
                return None
        return None

    @staticmethod
    def _is_durable_context_path(rel_path: str) -> bool:
        """Do not penalize evergreen context notes for being old."""
        return (
            rel_path == "MEMORY.md"
            or rel_path.startswith("goals/")
            or rel_path.startswith("MOC/")
            or rel_path in {"business/_index.md", "projects/_index.md"}
        )

    def _today(self) -> date:
        """Clock seam for reproducible ranking tests."""
        return date.today()

    def _age_adjustment(
        self,
        rel_path: str,
        record_date: date | None,
    ) -> tuple[int | None, float]:
        """Recency bonus/penalty for prompt-facing recall ranking."""
        if record_date is None or self._is_durable_context_path(rel_path):
            return None, 0.0

        age_days = max(0, (self._today() - record_date).days)
        if age_days <= 7:
            return age_days, 0.08
        if age_days <= 30:
            return age_days, 0.05
        if age_days <= 90:
            return age_days, 0.03
        if age_days <= 180:
            return age_days, 0.0
        if age_days <= 365:
            return age_days, -0.05
        if age_days <= 730:
            return age_days, -0.10
        if age_days <= 1095:
            return age_days, -0.15
        return age_days, -0.20

    @staticmethod
    def _clamp_confidence(score: float) -> float:
        """Normalize a raw score into a 0..1 confidence signal."""
        return round(max(0.0, min(score, 1.0)), 4)

    def _log_recall_payload(
        self,
        *,
        query: str,
        deep: bool,
        backend: str,
        ranked: list[dict[str, Any]],
    ) -> None:
        """Write structured recall diagnostics to the runtime log."""
        logger.info(
            "QMD recall query=%r deep=%s backend=%s results=%s",
            query,
            deep,
            backend,
            len(ranked),
        )
        for index, item in enumerate(ranked, start=1):
            rerank_score = item.get("rerank_score")
            logger.info(
                (
                    "QMD recall #%s path=%s score=%.2f confidence=%.2f "
                    "retrieval=%.2f rerank=%s age_days=%s "
                    "age_adjustment=%.2f tier=%s relevance=%.2f"
                ),
                index,
                item.get("rel_path", "") or item.get("file", ""),
                float(item.get("effective_score", 0.0) or 0.0),
                float(item.get("confidence", 0.0) or 0.0),
                float(item.get("retrieval_score", item.get("score", 0.0)) or 0.0),
                (f"{float(rerank_score):.2f}" if rerank_score is not None else "n/a"),
                item.get("age_days"),
                float(item.get("age_adjustment", 0.0) or 0.0),
                item.get("memory_tier", ""),
                float(item.get("memory_relevance", 0.0) or 0.0),
            )

    def _recall_candidates(
        self, query: str, limit: int
    ) -> tuple[str, list[dict[str, Any]]]:
        """Fetch qmd candidates with semantic query and deterministic fallback."""
        attempts: list[tuple[str, list[str], int]]
        if self._uses_remote_embeddings():
            attempts = [
                (
                    "query",
                    [
                        "node",
                        str(self._remote_query_script_path),
                        query,
                        "--limit",
                        str(max(limit * 4, 12)),
                    ],
                    QMD_RECALL_TIMEOUT_SECONDS,
                ),
                (
                    "search",
                    [
                        "qmd",
                        "--index",
                        self._manifest.qmd_index,
                        "search",
                        query,
                        "--json",
                        "-n",
                        str(max(limit * 4, 12)),
                    ],
                    QMD_RECALL_TIMEOUT_SECONDS,
                ),
            ]
        else:
            attempts = [
                (
                    "query",
                    [
                        "qmd",
                        "--index",
                        self._manifest.qmd_index,
                        "query",
                        query,
                        "--json",
                        "-n",
                        str(max(limit * 4, 12)),
                        "-C",
                        "16",
                    ],
                    QMD_RECALL_TIMEOUT_SECONDS,
                ),
                (
                    "search",
                    [
                        "qmd",
                        "--index",
                        self._manifest.qmd_index,
                        "search",
                        query,
                        "--json",
                        "-n",
                        str(max(limit * 4, 12)),
                    ],
                    QMD_RECALL_TIMEOUT_SECONDS,
                ),
            ]
        for backend, command, timeout in attempts:
            try:
                proc = subprocess.run(
                    command,
                    cwd=self.vault_path,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self.build_env(),
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                logger.warning("qmd %s timed out for query: %s", backend, query)
                continue
            if proc.returncode != 0 or not proc.stdout.strip():
                continue
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, list):
                return backend, payload
        return "none", []

    def recall(
        self, query: str, *, deep: bool = False, limit: int = DEFAULT_RECALL_LIMIT
    ) -> dict[str, Any]:
        """Run memory-aware archive recall on top of qmd candidates."""
        backend, candidates = self._recall_candidates(query, limit)
        ranked: list[dict[str, Any]] = []
        for candidate in candidates:
            uri = str(candidate.get("file", ""))
            rel_path = self._uri_to_rel_path(uri)
            signal = self._memory_signal_for_rel_path(rel_path) if rel_path else None
            tier = str(signal.get("tier", "")) if signal else ""
            relevance = float(signal.get("relevance", 0.0)) if signal else 0.0
            base_score = float(candidate.get("score", 0.0) or 0.0)
            retrieval_score = float(candidate.get("retrievalScore", base_score) or 0.0)
            rerank_raw = candidate.get("rerankScore")
            rerank_score = (
                float(rerank_raw)
                if rerank_raw is not None and rerank_raw != ""
                else None
            )
            record_date = self._record_date_for_rel_path(rel_path or "", signal)
            age_days, age_adjustment = self._age_adjustment(rel_path or "", record_date)
            allowed = (
                deep
                or not tier
                or tier in {"core", "active", "warm"}
                or base_score >= 0.9
            )
            if not allowed:
                continue
            effective_score = round(
                base_score
                + RECALL_TIER_BONUS.get(tier, 0.0)
                + min(relevance, 1.0) * 0.05,
                4,
            )
            effective_score = round(effective_score + age_adjustment, 4)
            epistemic_state = str(signal.get("epistemic_state", "")) if signal else ""
            supersession_adjustment = (
                SUPERSEDED_RESULT_PENALTY if epistemic_state == "superseded" else 0.0
            )
            effective_score = round(effective_score + supersession_adjustment, 4)
            confidence = self._clamp_confidence(effective_score)
            ranked.append(
                {
                    **candidate,
                    "rel_path": rel_path,
                    "memory_tier": tier,
                    "memory_relevance": relevance,
                    "memory_entries": int(signal.get("entries", 0)) if signal else 0,
                    "record_date": record_date.isoformat() if record_date else "",
                    "age_days": age_days,
                    "age_adjustment": round(age_adjustment, 4),
                    "supersession_adjustment": supersession_adjustment,
                    "epistemic_confidence": (
                        str(signal.get("epistemic_confidence", "")) if signal else ""
                    ),
                    "epistemic_scope": (
                        str(signal.get("epistemic_scope", "")) if signal else ""
                    ),
                    "epistemic_state": epistemic_state,
                    "superseded_by": (
                        str(signal.get("superseded_by", "")) if signal else ""
                    ),
                    "retrieval_score": retrieval_score,
                    "rerank_score": rerank_score,
                    "effective_score": effective_score,
                    "confidence": confidence,
                }
            )

        ranked.sort(
            key=lambda item: float(item.get("effective_score", 0.0)), reverse=True
        )
        self._log_recall_payload(
            query=query,
            deep=deep,
            backend=backend,
            ranked=ranked[: min(limit, RECALL_LOG_LIMIT)],
        )
        return {
            "query": query,
            "backend": backend,
            "mode": "deep-recall" if deep else "recall",
            "confidence": float(ranked[0].get("confidence", 0.0)) if ranked else 0.0,
            "results": ranked[:limit],
        }

    @staticmethod
    def format_recall(payload: dict[str, Any]) -> str:
        """Human-readable result list for memory-aware qmd recall."""
        mode = payload.get("mode", "recall")
        backend = payload.get("backend", "none")
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        results = payload.get("results", [])
        lines = [f"QMD {mode} ({backend}, confidence={confidence:.2f})"]
        if not results:
            lines.append("No results.")
            return "\n".join(lines)
        for index, item in enumerate(results, start=1):
            title = str(
                item.get("title", "")
                or item.get("rel_path", "")
                or item.get("file", "")
            )
            file_ref = str(item.get("file", ""))
            metadata: list[str] = []
            tier = str(item.get("memory_tier", ""))
            if tier:
                metadata.append(tier)
                metadata.append(f"r={float(item.get('memory_relevance', 0.0)):.2f}")
            record_date = str(item.get("record_date", "") or "")
            if record_date:
                metadata.append(f"date={record_date}")
            age_days = item.get("age_days")
            if isinstance(age_days, int):
                metadata.append(f"age={age_days}d")
            epistemic_state = str(item.get("epistemic_state", "") or "")
            if epistemic_state == "superseded":
                metadata.append("superseded")
            epistemic_scope = str(item.get("epistemic_scope", "") or "")
            if epistemic_scope:
                metadata.append(f"scope={epistemic_scope}")
            epistemic_confidence = str(item.get("epistemic_confidence", "") or "")
            if epistemic_confidence:
                metadata.append(f"epistemic={epistemic_confidence}")
            score = float(
                item.get("confidence", item.get("effective_score", 0.0)) or 0.0
            )
            metadata.append(f"score={score:.2f}")
            tier_suffix = f" [{', '.join(metadata)}]" if metadata else ""
            lines.append(
                f"{index}. {title}{tier_suffix}\n"
                f"   {file_ref}\n"
                f"   {str(item.get('snippet', '')).strip()}"
            )
        return "\n".join(lines)

    def _index_counts(self) -> dict[str, int] | None:
        """Read lightweight qmd index stats directly from SQLite."""
        if not self._db_path.exists():
            return None
        try:
            with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as conn:
                documents = conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE active = 1"
                ).fetchone()[0]
                embedded_hashes = conn.execute(
                    "SELECT COUNT(DISTINCT hash) FROM content_vectors"
                ).fetchone()[0]
                vector_rows = conn.execute(
                    "SELECT COUNT(*) FROM content_vectors"
                ).fetchone()[0]
        except sqlite3.Error:
            return None
        return {
            "documents": int(documents),
            "embedded_hashes": int(embedded_hashes),
            "pending_hashes": max(int(documents) - int(embedded_hashes), 0),
            "vector_rows": int(vector_rows),
        }

    def _load_embed_model_state(self) -> str | None:
        """Return the last model URI used for the local qmd index."""
        if not self._embed_state_path.exists():
            return None
        try:
            payload = json.loads(self._embed_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        model_uri = payload.get("embed_model_uri")
        return model_uri if isinstance(model_uri, str) else None

    def _write_embed_model_state(self, model_uri: str) -> None:
        """Persist the last successfully used embed model URI."""
        self._state_root.mkdir(parents=True, exist_ok=True)
        self._embed_state_path.write_text(
            json.dumps({"embed_model_uri": model_uri}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )

    def _embed_force_required(self, desired_model: str) -> bool:
        """Force one full re-embed when the desired model changes."""
        counts = self._index_counts()
        has_vectors = bool(counts and counts["vector_rows"] > 0)
        return has_vectors and self._load_embed_model_state() != desired_model

    def _run_embed_process(self, *, force: bool) -> subprocess.CompletedProcess[str]:
        """Run the project-local full embed helper without qmd's internal timeout."""
        args = ["node", str(self._embed_script_path)]
        if force:
            args.append("--force")
        try:
            return subprocess.run(
                args,
                cwd=self.vault_path,
                capture_output=True,
                text=True,
                check=False,
                env=self.build_env(),
                timeout=_EMBED_PROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                args=args,
                returncode=124,
                stdout=_normalize_subprocess_output(exc.stdout),
                stderr=(
                    _normalize_subprocess_output(exc.stderr)
                    + f"\nqmd embed timed out after {_EMBED_PROCESS_TIMEOUT}s"
                ),
            )

    def run_embed_cli(self, *, force: bool = False) -> subprocess.CompletedProcess[str]:
        """Expose the project-local full embed flow for the public QMD command."""
        desired_model = self.build_env()["QMD_EMBED_MODEL"]
        effective_force = force or self._embed_force_required(desired_model)
        try:
            with self._exclusive_lock():
                result = self._run_embed_process(force=effective_force)
        except FileNotFoundError:
            return subprocess.CompletedProcess(
                args=["node", str(self._embed_script_path)],
                returncode=1,
                stdout="",
                stderr="qmd full embed helper is not available",
            )
        if result.returncode == 0:
            self._write_embed_model_state(desired_model)
        return result

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """Serialize qmd maintenance so concurrent writers do not fight each other."""
        self._state_root.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            logger.debug("Waiting for qmd lock: %s", self._lock_path)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def run(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute qmd against the project-local index."""
        return subprocess.run(
            ["qmd", "--index", self._manifest.qmd_index, *args],
            cwd=self.vault_path,
            capture_output=True,
            text=True,
            check=False,
            env=self.build_env(),
            timeout=timeout,
        )

    def cleanup(self) -> subprocess.CompletedProcess[str]:
        """Remove stale qmd data while excluding concurrent index writers."""
        with self._exclusive_lock():
            return self.run("cleanup")

    def refresh(self, *, with_embeddings: bool) -> dict[str, Any]:
        """Low-level qmd refresh helper for update-only or update+embed flows."""
        result: dict[str, Any] = {
            "available": True,
            "updated": False,
            "embedded": False,
            "errors": [],
        }
        try:
            with self._exclusive_lock():
                update_proc = self.run("update")
                if update_proc.returncode != 0:
                    result["errors"].append(
                        update_proc.stderr.strip() or update_proc.stdout.strip()
                    )
                    return result
                result["updated"] = True

                if not with_embeddings:
                    return result

                desired_model = self.build_env()["QMD_EMBED_MODEL"]
                embed_proc = self._run_embed_process(
                    force=self._embed_force_required(desired_model)
                )
                if embed_proc.returncode != 0:
                    result["errors"].append(
                        embed_proc.stderr.strip() or embed_proc.stdout.strip()
                    )
                    return result
                result["embedded"] = True
                self._write_embed_model_state(desired_model)
        except FileNotFoundError:
            return {
                "available": False,
                "updated": False,
                "embedded": False,
                "errors": ["qmd CLI is not installed"],
            }
        return result

    def refresh_after_searchable_write(self) -> dict[str, Any]:
        """Canonical runtime contract after a new searchable vault write."""
        return self.refresh(with_embeddings=True)

    def touch_notes(self, targets: list[str]) -> None:
        """Best-effort memory touch for notes explicitly opened via qmd get."""
        normalized_paths: list[Path] = []
        seen: set[Path] = set()

        for target in targets:
            candidate = Path(target.strip())
            if not str(candidate):
                continue
            if candidate.is_absolute():
                path = candidate
            else:
                path = self.vault_path / candidate
            if path.suffix != ".md":
                path = path.with_suffix(".md")
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.exists():
                continue
            try:
                resolved.relative_to(self.vault_path)
            except ValueError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            normalized_paths.append(resolved)

        for path in normalized_paths:
            relative = path.relative_to(self.vault_path)
            memory_engine = (
                self.vault_path.parent / "skills/agent-memory/scripts/memory-engine.py"
            )
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    str(memory_engine),
                    "touch",
                    str(relative),
                ],
                cwd=self.vault_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "Failed to touch memory note %s: %s",
                    relative,
                    (result.stderr or result.stdout).strip(),
                )
