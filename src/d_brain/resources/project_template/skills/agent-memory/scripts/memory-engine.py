#!/usr/bin/env python3
"""
memory-engine.py — Universal memory management for AI agents.

Implements Ebbinghaus-inspired forgetting curve with tiered recall.
Works with any directory of markdown files.

Commands:
  scan    [dir]           Analyze files, report stats (no changes)
  init    [dir]           Add YAML frontmatter to files missing it
  decay   [dir]           Update relevance scores and tiers
  touch   <file>          Refresh file memory and daily entry memory on read/use
  supersede <old> <new> [--vault <dir>]
                          Mark an epistemic note superseded by its successor
  recover-supersession [dir]
                          Roll a pending epistemic supersession forward
  creative <N> [dir]      Random N cards from cold/archive tiers
  stats   [dir]           Show tier distribution and health metrics

Options:
  --config <path>         Config JSON (default: .memory-config.json in target dir)
  --dry-run               Preview changes without writing
  --verbose               Show per-file details

Config (.memory-config.json):
{
  "tiers": {
    "active": 7,          // days threshold
    "warm": 21,
    "cold": 60
    // beyond cold = archive
  },
  "decay_rate": 0.015,    // relevance loss per day (linear)
  "relevance_floor": 0.1, // minimum relevance
  "skip_patterns": ["_index.md", "MOC-*"],
  "type_inference": {
    "crm/": "crm",
    "leads/": "lead",
    "personal/": "personal"
  },
  "use_git_dates": true
}
"""

import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from d_brain.manifest import VaultManifest, load_manifest_for_vault  # noqa: E402
from d_brain.services.frontmatter import (  # noqa: E402
    patch_frontmatter_bytes,
    patch_validated_vault_frontmatter,
    route_profile,
)
from d_brain.services.vault_lock import VaultWriteLock, vault_write_lock  # noqa: E402

# ─── defaults ───────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "tiers": {"active": 7, "warm": 21, "cold": 60},
    "decay_rate": 0.015,
    "relevance_floor": 0.1,
    "skip_patterns": ["_index.md"],
    "type_inference": {},
    "use_git_dates": True,
}

TODAY = date.today()


def _resolve_vault_root(value: str | Path) -> Path:
    """Resolve an explicit vault root without creating project-root state."""
    candidate = Path(value).resolve()
    if candidate.name == "vault" and candidate.is_dir():
        return candidate
    nested_vault = candidate / "vault"
    if nested_vault.is_dir() and not nested_vault.is_symlink():
        return nested_vault.resolve()
    raise ValueError(
        f"{candidate} is not a vault root or project root containing vault/"
    )


def _command_positionals(args: list[str]) -> list[str]:
    """Return command arguments after removing options and their values."""
    positionals: list[str] = []
    skip_value = False
    for argument in args[1:]:
        if skip_value:
            skip_value = False
            continue
        if argument in {"--config", "--vault"}:
            skip_value = True
            continue
        if not argument.startswith("-"):
            positionals.append(argument)
    return positionals


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically via tempfile + os.replace to avoid partial files."""
    import tempfile
    tmp_dir = path.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(tmp_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _vault_root_for_path(path: Path, fallback: Path | None = None) -> Path:
    """Find the vault root for a writer without following a note symlink."""
    for candidate in (path.parent, *path.parents):
        if (
            candidate.name == "vault"
            and candidate.is_dir()
            and not candidate.is_symlink()
        ):
            return candidate.resolve()
    if fallback is not None:
        return _resolve_vault_root(fallback)
    return _resolve_vault_root(Path.cwd())


def _patched_frontmatter_content(content: str, updates: dict[str, object]) -> str:
    """Preview the shared lossless top-level patch without changing the body."""
    return patch_frontmatter_bytes(content.encode("utf-8"), updates).decode("utf-8")


def _write_frontmatter_updates(
    vault_root: Path,
    path: Path,
    updates: dict[str, object],
    *,
    manifest: VaultManifest,
    lock: VaultWriteLock,
    expected_fingerprint: bytes | None = None,
) -> bool:
    """Apply a memory-field patch if the exact scanned source is still current."""
    if expected_fingerprint is not None:
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            return False
        if hashlib.sha256(current).digest() != expected_fingerprint:
            return False
    patch_validated_vault_frontmatter(
        vault_root,
        path,
        updates,
        manifest=manifest,
        existing_lock=lock,
    )
    return True

# ─── YAML frontmatter parsing ──────────────────────────────────


_TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*)$")


def _parse_yaml_block(yaml_block: str) -> tuple[dict, dict[str, slice]]:
    """Parse YAML block into (scalar_fields, line_ranges_per_top_level_key).

    Skips multiline scalars (>-, |, |-, >), block lists (- ), and inline
    multiline lists ([\n  ...\n]) so complex values are preserved during update.
    """
    lines = yaml_block.split("\n")
    fields: dict[str, str] = {}
    ranges: dict[str, slice] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _TOP_LEVEL_KEY_RE.match(line)
        if not m:
            i += 1
            continue
        key = m.group(1)
        value = m.group(2).strip()
        start = i
        if value in (">-", ">", "|", "|-"):
            i += 1
            while i < len(lines):
                if _TOP_LEVEL_KEY_RE.match(lines[i]) and not lines[i].startswith(
                    (" ", "\t")
                ):
                    break
                i += 1
            ranges[key] = slice(start, i)
            continue
        if value.startswith("[") and not value.rstrip().endswith("]"):
            i += 1
            while i < len(lines):
                if "]" in lines[i]:
                    i += 1
                    break
                i += 1
            ranges[key] = slice(start, i)
            continue
        if value == "" and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("-"):
            i += 1
            while i < len(lines) and (
                lines[i].lstrip().startswith("-")
                or (lines[i].startswith((" ", "\t")) and lines[i].strip())
            ):
                i += 1
            ranges[key] = slice(start, i)
            continue
        if (
            len(value) >= 2
            and value[0] in {'"', "'"}
            and value[-1] == value[0]
        ):
            value = value[1:-1]
        fields[key] = value
        ranges[key] = slice(start, start + 1)
        i += 1
    return fields, ranges


def parse_frontmatter(content: str) -> tuple[dict, str, bool]:
    """Parse YAML frontmatter. Returns (fields, body, had_yaml).

    Stores the raw YAML block on the returned dict under reserved key
    ``_raw_yaml`` so build_frontmatter can update scalars in place
    without losing multiline structures or block lists.

    Tolerates a leading UTF-8 BOM (e.g. from a manual edit) so it does not
    hide existing frontmatter from decay/tier calculations.
    """
    if content.startswith("\ufeff"):
        content = content[1:]
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            yaml_block = content[4:end]
            body = content[end + 5 :]
            fields, _ = _parse_yaml_block(yaml_block)
            fields["_raw_yaml"] = yaml_block
            return fields, body, True
    return {}, content, False


DEFAULT_FIELD_ORDER = [
    "type",
    "title",
    "description",
    "tags",
    "industry",
    "source",
    "priority",
    "status",
    "region",
    "owner",
    "responsible",
    "domain",
    "related",
    "client",
    "deal_status",
    "deal_deadline",
    "deadline",
    "created",
    "updated",
    "last_accessed",
    "relevance",
    "tier",
]


def build_frontmatter(fields: dict, field_order: list[str] | None = None) -> str:
    """Build YAML frontmatter, updating scalars in the original block when present.

    If ``fields`` carries the raw YAML block under ``_raw_yaml`` (set by
    parse_frontmatter), update only known scalar lines and append new keys.
    Multiline values, block lists, and inline lists are kept verbatim.
    Falls back to a fresh build when no raw block is available.
    """
    if field_order is None:
        field_order = DEFAULT_FIELD_ORDER
    raw_yaml = fields.get("_raw_yaml")
    fields = {k: v for k, v in fields.items() if k != "_raw_yaml"}

    if raw_yaml is None:
        lines: list[str] = []
        used: set[str] = set()
        for key in field_order:
            if key in fields:
                lines.append(f"{key}: {fields[key]}")
                used.add(key)
        for key, val in fields.items():
            if key not in used:
                lines.append(f"{key}: {val}")
        return "---\n" + "\n".join(lines) + "\n---\n"

    raw_lines = raw_yaml.split("\n")
    _, ranges = _parse_yaml_block(raw_yaml)
    seen: set[str] = set()
    out: list[str] = []
    i = 0
    while i < len(raw_lines):
        replaced = False
        for key, span in ranges.items():
            if span.start == i and span.stop == i + 1 and key in fields:
                out.append(f"{key}: {fields[key]}")
                seen.add(key)
                replaced = True
                i += 1
                break
        if replaced:
            continue
        out.append(raw_lines[i])
        for key, span in ranges.items():
            if span.start == i:
                seen.add(key)
        i += 1
    for key in field_order:
        if key in fields and key not in seen:
            out.append(f"{key}: {fields[key]}")
            seen.add(key)
    for key, val in fields.items():
        if key not in seen:
            out.append(f"{key}: {val}")
            seen.add(key)
    while out and out[-1].strip() == "":
        out.pop()
    return "---\n" + "\n".join(out) + "\n---\n"


# ─── date resolution ───────────────────────────────────────────


def get_git_date(filepath: Path) -> date | None:
    """Last git commit date for file."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%aI", "--", str(filepath)],
            capture_output=True,
            text=True,
            cwd=filepath.parent,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return date.fromisoformat(result.stdout.strip()[:10])
    except Exception:
        pass
    return None


def get_best_date(fields: dict, filepath: Path, use_git: bool = True) -> date:
    """Most recent date from YAML fields, git history, or file mtime."""
    candidates = []
    for field in ["last_accessed", "updated", "created"]:
        val = fields.get(field, "")
        try:
            candidates.append(date.fromisoformat(val[:10]))
        except (ValueError, IndexError):
            continue
    if use_git:
        git_date = get_git_date(filepath)
        if git_date:
            candidates.append(git_date)
    if not candidates:
        mtime = os.path.getmtime(filepath)
        candidates.append(date.fromtimestamp(mtime))
    return max(candidates)


# ─── core logic ─────────────────────────────────────────────────


def calc_relevance(days: int, rate: float, floor: float) -> float:
    """Floor-adjusted exponential forgetting curve."""
    return round(floor + (1.0 - floor) * math.exp(-rate * days), 2)


def calc_tier(days: int, tiers: dict, current_tier: str = "") -> str:
    """Assign tier based on days since last access."""
    if current_tier == "core":
        return "core"  # never auto-demote core
    sorted_tiers = sorted(tiers.items(), key=lambda x: x[1])
    for tier_name, threshold in sorted_tiers:
        if days <= threshold:
            return tier_name
    return "archive"


def infer_type(filepath: Path, type_map: dict) -> str:
    """Infer card type from path using configurable mapping."""
    path_str = str(filepath)
    for pattern, card_type in type_map.items():
        if pattern in path_str:
            return card_type
    return "note"


def should_skip(filepath: Path, patterns: list[str]) -> bool:
    """Check if file matches skip patterns."""
    import fnmatch

    name = filepath.name
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def infer_title(body: str) -> str:
    """Extract title from first H1 heading."""
    for line in body.split("\n")[:10]:
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ─── config ─────────────────────────────────────────────────────


def load_config(target_dir: Path, config_path: str | None = None) -> dict:
    """Load config from file or return defaults."""
    if config_path:
        p = Path(config_path)
    else:
        p = target_dir / ".memory-config.json"
    if p.exists():
        with open(p) as f:
            user = json.load(f)
        # Merge with defaults
        config = {**DEFAULT_CONFIG, **user}
        config["tiers"] = {**DEFAULT_CONFIG["tiers"], **user.get("tiers", {})}
        return config
    return DEFAULT_CONFIG.copy()


def save_default_config(target_dir: Path):
    """Write default config file."""
    vault_root = _resolve_vault_root(target_dir)
    load_manifest_for_vault(vault_root)
    p = vault_root / ".memory-config.json"
    with vault_write_lock(vault_root):
        _atomic_write_text(p, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    print(f"  wrote default config: {p}")


# ─── commands ───────────────────────────────────────────────────


def find_cards(target_dir: Path, config: dict) -> list[Path]:
    """Find all markdown files, respecting skip patterns."""
    cards = sorted(target_dir.rglob("*.md"))
    return [
        c
        for c in cards
        if c.exists()
        and not c.is_symlink()
        and c.relative_to(target_dir).parts[:1] != ("skills",)
        and not should_skip(c, config["skip_patterns"])
    ]


def _initial_frontmatter_fields(
    vault_root: Path,
    card: Path,
    body: str,
    config: dict,
    manifest: VaultManifest,
) -> dict[str, object]:
    """Build the required fields for a newly initialized routed profile."""
    relative_path = card.relative_to(vault_root).as_posix()
    route = route_profile(relative_path, {}, manifest)
    ref_date = get_best_date({}, card, config["use_git_dates"])
    title = infer_title(body) or card.stem.replace("-", " ")
    days = max(0, (TODAY - ref_date).days)
    note_type = infer_type(card, config["type_inference"])
    if route.name == "daily":
        note_type = "daily"
    elif route.name == "reflection":
        note_type = "reflection"

    fields: dict[str, object] = {
        "type": note_type,
        "last_accessed": ref_date.isoformat(),
        "relevance": calc_relevance(
            days, config["decay_rate"], config["relevance_floor"]
        ),
        "tier": calc_tier(days, config["tiers"]),
    }
    if "description" in route.required_fields:
        fields["description"] = title
    if "tags" in route.required_fields:
        fields["tags"] = (
            ["system-reflection", "weekly-review"]
            if route.name == "reflection"
            else ["memory", "note"]
        )
    if "status" in route.required_fields:
        fields["status"] = "active"
    if "date" in route.required_fields:
        try:
            fields["date"] = date.fromisoformat(card.stem).isoformat()
        except ValueError:
            fields["date"] = ref_date.isoformat()
    for field in ("created", "updated"):
        if field in route.required_fields:
            fields[field] = ref_date.isoformat()
    return fields


def sync_daily_entry_memory(target_dir: Path) -> int:
    """Rebuild entry-level memory metadata for all daily files."""
    from d_brain.services.memory_entries import DailyEntryMemoryStore

    daily_dir = target_dir / "daily"
    if not daily_dir.exists():
        return 0
    store = DailyEntryMemoryStore(target_dir)
    daily_files = sorted(daily_dir.glob("*.md"))
    for daily_file in daily_files:
        store.sync_daily_file(daily_file, today=TODAY)
    return len(daily_files)


def cmd_scan(target_dir: Path, config: dict, verbose: bool = False):
    """Analyze files without changes."""
    cards = find_cards(target_dir, config)
    with_yaml = 0
    without_yaml = 0
    has_relevance = 0
    has_tier = 0
    total_bytes = 0

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        total_bytes += len(content.encode("utf-8"))
        fields, body, had_yaml = parse_frontmatter(content)
        if had_yaml:
            with_yaml += 1
        else:
            without_yaml += 1
        if "relevance" in fields:
            has_relevance += 1
        if "tier" in fields:
            has_tier += 1
        if verbose and not had_yaml:
            print(f"  no yaml: {card.relative_to(target_dir)}")

    print(f"\n  scan results for {target_dir}:")
    print(f"    total files:     {len(cards)}")
    print(f"    with yaml:       {with_yaml}")
    print(f"    without yaml:    {without_yaml}")
    print(f"    has relevance:   {has_relevance}")
    print(f"    has tier:        {has_tier}")
    print(f"    total size:      {total_bytes / 1024:.0f} KB")
    print(f"    avg file size:   {total_bytes / max(len(cards), 1) / 1024:.1f} KB")

    if without_yaml:
        print(f"\n  → run 'init' to add YAML frontmatter to {without_yaml} files")
    if not has_relevance:
        print("  → run 'decay' to add relevance scores and tiers")


def cmd_init(
    target_dir: Path, config: dict, dry_run: bool = False, verbose: bool = False
):
    """Add YAML frontmatter to files missing it."""
    vault_root = _resolve_vault_root(target_dir)
    manifest = load_manifest_for_vault(vault_root)
    cards = find_cards(vault_root, config)
    added = 0
    pending: list[tuple[Path, dict[str, object], bytes]] = []

    for card in cards:
        source = card.read_bytes()
        content = source.decode("utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)
        if had_yaml:
            continue

        new_fields = _initial_frontmatter_fields(
            vault_root,
            card,
            body,
            config,
            manifest,
        )

        pending.append((card, new_fields, hashlib.sha256(source).digest()))
        added += 1
        if verbose:
            print(
                "  "
                f"{'[dry] ' if dry_run else ''}init: "
                f"{card.relative_to(vault_root)} → {new_fields['tier']}"
            )

    conflicts = 0
    if not dry_run:
        with vault_write_lock(vault_root) as lock:
            for card, updates, fingerprint in pending:
                written = _write_frontmatter_updates(
                    vault_root,
                    card,
                    updates,
                    manifest=manifest,
                    lock=lock,
                    expected_fingerprint=fingerprint,
                )
                if not written:
                    conflicts += 1

    print(f"\n  {'DRY RUN — ' if dry_run else ''}init results:")
    print(f"    files processed: {len(cards)}")
    print(f"    frontmatter added: {added - conflicts}")
    if conflicts:
        print(f"    concurrent conflicts skipped: {conflicts}")


def cmd_decay(
    target_dir: Path, config: dict, dry_run: bool = False, verbose: bool = False
):
    """Update relevance and tiers based on time decay."""
    vault_root = _resolve_vault_root(target_dir)
    manifest = load_manifest_for_vault(vault_root)
    cards = find_cards(vault_root, config)
    results = []
    pending: list[tuple[Path, dict[str, object], bytes]] = []

    for card in cards:
        source = card.read_bytes()
        content = source.decode("utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)

        ref_date = get_best_date(fields, card, config["use_git_dates"])
        days = max(0, (TODAY - ref_date).days)

        old_tier = fields.get("tier", "")
        new_relevance = calc_relevance(
            days, config["decay_rate"], config["relevance_floor"]
        )
        new_tier = calc_tier(days, config["tiers"], old_tier)

        updates: dict[str, object] = {
            "relevance": new_relevance,
            "tier": new_tier,
        }
        if "last_accessed" not in fields:
            updates["last_accessed"] = ref_date.isoformat()
        if "type" not in fields:
            updates["type"] = infer_type(card, config["type_inference"])
        new_content = _patched_frontmatter_content(content, updates)
        changed = new_content != content

        if changed:
            pending.append((card, updates, hashlib.sha256(source).digest()))

        results.append(
            {
                "path": str(card.relative_to(target_dir)),
                "days": days,
                "relevance": new_relevance,
                "tier": new_tier,
                "changed": changed,
            }
        )

        if verbose and changed:
            print(
                "  "
                f"{'[dry] ' if dry_run else ''}"
                f"{card.relative_to(target_dir)}: "
                f"{old_tier or '?'}→{new_tier} r={new_relevance}"
            )

    # Stats
    tiers = {}
    changed_count = sum(1 for r in results if r["changed"])
    for r in results:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    avg_rel = sum(r["relevance"] for r in results) / max(len(results), 1)

    print(f"\n  {'DRY RUN — ' if dry_run else ''}decay results:")
    print(f"    total: {len(results)}, changed: {changed_count}")
    print(f"    avg relevance: {avg_rel:.2f}")
    for tier in ["core", "active", "warm", "cold", "archive"]:
        count = tiers.get(tier, 0)
        bar = "█" * (count // 3)
        if count:
            print(f"    {tier:8s}: {count:4d} {bar}")
    conflicts = 0
    if not dry_run:
        with vault_write_lock(vault_root) as lock:
            for card, updates, fingerprint in pending:
                written = _write_frontmatter_updates(
                    vault_root,
                    card,
                    updates,
                    manifest=manifest,
                    lock=lock,
                    expected_fingerprint=fingerprint,
                )
                if not written:
                    conflicts += 1
            synced = sync_daily_entry_memory(vault_root)
        if synced:
            print(f"    daily entries: synced {synced} files")
        if conflicts:
            print(f"    concurrent conflicts skipped: {conflicts}")


def cmd_touch(filepath: str, config: dict):
    """Promote a file one tier up (graduated recall).

    archive → cold → warm → active → active (refresh)
    Each touch promotes one level, not straight to top.
    Natural spaced repetition: multiple reads = stronger memory.
    """
    target_file = filepath.split("::", 1)[0]
    p = Path(target_file)
    if not p.exists():
        print(f"  error: {filepath} not found")
        sys.exit(1)
    if p.suffix != ".md":
        print(f"  error: refusing to touch non-markdown file: {filepath}")
        sys.exit(1)

    vault_root = _vault_root_for_path(p)
    manifest = load_manifest_for_vault(vault_root)

    with vault_write_lock(vault_root) as lock:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            print(f"  error: {filepath} not found")
            sys.exit(1)
        fields, body, had_yaml = parse_frontmatter(content)

        if fields.get("tier") == "core":
            updates = {"last_accessed": TODAY.isoformat(), "relevance": 1.0}
            _write_frontmatter_updates(
                vault_root,
                p,
                updates,
                manifest=manifest,
                lock=lock,
            )
            print(f"  touched: {filepath} → core (refreshed)")
            return

        tiers_cfg = config["tiers"]
        # Promotion targets: set last_accessed to midpoint of next-higher tier
        # archive → cold: midpoint of cold range
        # cold → warm: midpoint of warm range
        # warm → active: midpoint of active range
        # active → active: today (refresh)
        cold_threshold = tiers_cfg.get("cold", 60)
        warm_threshold = tiers_cfg.get("warm", 21)
        active_threshold = tiers_cfg.get("active", 7)

        current_tier = fields.get("tier", "archive")
        if current_tier == "archive":
            # Promote to cold: set last_accessed to midpoint of cold range
            target_days = (warm_threshold + cold_threshold) // 2
            new_tier = "cold"
        elif current_tier == "cold":
            # Promote to warm: midpoint of warm range
            target_days = (active_threshold + warm_threshold) // 2
            new_tier = "warm"
        elif current_tier == "warm":
            # Promote to active: midpoint of active range
            target_days = active_threshold // 2
            new_tier = "active"
        else:
            # Already active: refresh to today
            target_days = 0
            new_tier = "active"

        from datetime import timedelta

        new_date = TODAY - timedelta(days=target_days)
        new_relevance = calc_relevance(
            target_days, config["decay_rate"], config["relevance_floor"]
        )

        updates = {
            "last_accessed": new_date.isoformat(),
            "relevance": new_relevance,
            "tier": new_tier,
        }
        _write_frontmatter_updates(
            vault_root,
            p,
            updates,
            manifest=manifest,
            lock=lock,
        )
        if "daily" in p.parts:
            from d_brain.services.memory_entries import DailyEntryMemoryStore

            rel_path = p.resolve().relative_to(vault_root).as_posix()
            changed = DailyEntryMemoryStore(vault_root).touch_daily_file(
                rel_path,
                today=TODAY,
            )
        else:
            changed = 0
    print(
        f"  touched: {filepath} → {current_tier}→{new_tier}, relevance={new_relevance}"
    )
    if changed:
        print(f"  touched daily entries: {changed}")


def cmd_creative(n: int, target_dir: Path, config: dict):
    """Random sample from cold/archive tiers for divergent thinking."""
    cards = find_cards(target_dir, config)
    cold_cards = []

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)
        tier = fields.get("tier", "")
        if tier in ("cold", "archive", "warm"):
            title = fields.get("title", "") or infer_title(body)
            cold_cards.append(
                {
                    "path": str(card.relative_to(target_dir)),
                    "tier": tier,
                    "relevance": fields.get("relevance", "?"),
                    "title": title,
                    "last_accessed": fields.get("last_accessed", "?"),
                }
            )

    daily_dir = target_dir / "daily"
    if daily_dir.exists():
        from d_brain.services.memory_entries import DailyEntryMemoryStore

        for entry in DailyEntryMemoryStore(target_dir).all_entries():
            tier = str(entry.get("tier", ""))
            if tier in ("cold", "archive", "warm"):
                cold_cards.append(
                    {
                        "path": str(entry.get("path", "")),
                        "tier": tier,
                        "relevance": entry.get("relevance", "?"),
                        "title": entry.get("preview", "") or entry.get("heading", ""),
                        "last_accessed": entry.get("last_accessed", "?"),
                    }
                )

    if not cold_cards:
        print("  no cold/archive cards found — memory is too fresh")
        return

    sample = random.sample(cold_cards, min(n, len(cold_cards)))
    print(f"\n  🎲 creative recall — {len(sample)} random cards:")
    for card in sample:
        print(f"    [{card['tier']}] {card['title'] or card['path']}")
        print(
            "           "
            f"{card['path']} "
            f"(r={card['relevance']}, last={card['last_accessed']})"
        )
    print(
        "\n  read these cards and look for unexpected connections to your current task"
    )


def cmd_daily(
    target_dir: Path, config: dict, dry_run: bool = False, verbose: bool = False
):
    """Bootstrap and decay only ``daily/YYYY-MM-DD.md`` files."""
    vault_root = _resolve_vault_root(target_dir)
    manifest = load_manifest_for_vault(vault_root)
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
    daily_dir = vault_root / "daily"
    daily_files = sorted(
        f for f in daily_dir.glob("*.md") if date_pattern.match(f.name)
    )

    if not daily_files:
        print(f"  no daily files (YYYY-MM-DD.md) found in {target_dir}")
        return

    results = []
    pending: list[tuple[Path, dict[str, object], bytes]] = []
    for f in daily_files:
        source = f.read_bytes()
        content = source.decode("utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)

        # Extract date from filename
        file_date = date.fromisoformat(f.stem)
        days = max(0, (TODAY - file_date).days)

        # Use file_date as reference (not git/mtime — daily files are date-intrinsic)
        la = fields.get("last_accessed", "")
        try:
            last_acc = date.fromisoformat(la[:10])
            # Use most recent of: file_date, last_accessed
            ref_days = max(0, (TODAY - max(file_date, last_acc)).days)
        except (ValueError, IndexError):
            ref_days = days

        new_relevance = calc_relevance(
            ref_days, config["decay_rate"], config["relevance_floor"]
        )
        old_tier = fields.get("tier", "")
        new_tier = calc_tier(ref_days, config["tiers"], old_tier)

        updates: dict[str, object] = {
            "type": "daily",
            "date": file_date.isoformat(),
            "relevance": new_relevance,
            "tier": new_tier,
        }
        if "last_accessed" not in fields:
            updates["last_accessed"] = file_date.isoformat()
        new_content = _patched_frontmatter_content(content, updates)
        changed = new_content != content

        if changed:
            pending.append((f, updates, hashlib.sha256(source).digest()))

        results.append(
            {
                "file": f.name,
                "date": file_date.isoformat(),
                "days": ref_days,
                "relevance": new_relevance,
                "tier": new_tier,
                "changed": changed,
            }
        )

        if verbose:
            print(
                "  "
                f"{'[dry] ' if dry_run else ''}"
                f"{f.name}: {old_tier or '?'}→{new_tier} "
                f"r={new_relevance} ({ref_days}d)"
            )

    # Summary
    tiers = {}
    for r in results:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    changed_count = sum(1 for r in results if r["changed"])

    print(f"\n  {'DRY RUN — ' if dry_run else ''}daily results:")
    print(f"    files: {len(results)}, changed: {changed_count}")
    for tier in ["active", "warm", "cold", "archive"]:
        count = tiers.get(tier, 0)
        if count:
            dates = [r["date"] for r in results if r["tier"] == tier]
            print(f"    {tier:8s}: {count:3d}  ({dates[0]}..{dates[-1]})")
    conflicts = 0
    if not dry_run:
        with vault_write_lock(vault_root) as lock:
            for daily_file, updates, fingerprint in pending:
                written = _write_frontmatter_updates(
                    vault_root,
                    daily_file,
                    updates,
                    manifest=manifest,
                    lock=lock,
                    expected_fingerprint=fingerprint,
                )
                if not written:
                    conflicts += 1
            synced = sync_daily_entry_memory(vault_root)
        if synced:
            print(f"    entry memory synced: {synced} files")
        if conflicts:
            print(f"    concurrent conflicts skipped: {conflicts}")


def cmd_stats(target_dir: Path, config: dict):
    """Show comprehensive memory health stats."""
    cards = find_cards(target_dir, config)
    tiers = {}
    total_bytes = 0
    stale_count = 0
    no_yaml = 0

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        total_bytes += len(content.encode("utf-8"))
        fields, body, had_yaml = parse_frontmatter(content)
        if not had_yaml:
            no_yaml += 1
        tier = fields.get("tier", "unknown")
        tiers[tier] = tiers.get(tier, 0) + 1
        try:
            la = date.fromisoformat(fields.get("last_accessed", "")[:10])
            if (TODAY - la).days > 90:
                stale_count += 1
        except (ValueError, IndexError):
            pass

    print(f"\n  memory health — {target_dir}")
    print(f"  {'─' * 40}")
    print(f"  total cards:       {len(cards)}")
    print(f"  total size:        {total_bytes / 1024:.0f} KB")
    print(f"  without yaml:      {no_yaml}")
    print(f"  stale (>90 days):  {stale_count}")
    print(f"  {'─' * 40}")
    print("  tier distribution:")
    for tier in ["core", "active", "warm", "cold", "archive", "unknown"]:
        count = tiers.get(tier, 0)
        if count:
            pct = count / len(cards) * 100
            bar = "█" * int(pct / 2)
            print(f"    {tier:8s}: {count:4d} ({pct:4.1f}%) {bar}")

    # Context budget estimate (assuming ~4 chars per token)
    active_bytes = 0
    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        fields, _, _ = parse_frontmatter(content)
        if fields.get("tier") in ("core", "active"):
            active_bytes += len(content.encode("utf-8"))
    print(f"  {'─' * 40}")
    print(
        "  active context:    "
        f"{active_bytes / 1024:.0f} KB (~{active_bytes // 4:,} tokens)"
    )
    print(
        "  total context:     "
        f"{total_bytes / 1024:.0f} KB (~{total_bytes // 4:,} tokens)"
    )
    daily_dir = target_dir / "daily"
    if daily_dir.exists():
        from d_brain.services.memory_entries import DailyEntryMemoryStore

        entry_store = DailyEntryMemoryStore(target_dir)
        entry_tiers = {}
        entries = entry_store.all_entries()
        for entry in entries:
            tier = str(entry.get("tier", "unknown"))
            entry_tiers[tier] = entry_tiers.get(tier, 0) + 1
        print(f"  {'─' * 40}")
        print(f"  daily entries:     {len(entries)}")
        for tier in ["active", "warm", "cold", "archive"]:
            count = entry_tiers.get(tier, 0)
            if count:
                print(f"    entry {tier:7s}: {count:4d}")


# ─── main ───────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    dry_run = "--dry-run" in args
    verbose = "--verbose" in args

    # Find config path
    config_path = None
    if "--config" in args:
        idx = args.index("--config")
        config_path = args[idx + 1] if idx + 1 < len(args) else None

    vault_override = None
    if "--vault" in args:
        idx = args.index("--vault")
        vault_override = args[idx + 1] if idx + 1 < len(args) else None
        if vault_override is None:
            print("  error: --vault requires a vault or project directory")
            sys.exit(1)

    if cmd == "supersede":
        positional = _command_positionals(args)
        if len(positional) != 2:
            print(
                "  error: supersede requires old and new vault-relative Markdown paths"
            )
            sys.exit(1)
        from d_brain.services.epistemic_memory import supersede_notes

        try:
            result = supersede_notes(
                _resolve_vault_root(vault_override or Path(".")),
                positional[0],
                positional[1],
            )
        except Exception as exc:
            print(f"  error: {exc}")
            sys.exit(1)
        action = "updated" if result.changed else "already linked"
        print(f"  supersession {action}: {result.old_path} -> {result.new_path}")
        return

    if cmd == "recover-supersession":
        positional = _command_positionals(args)
        if len(positional) > 1:
            print("  error: recover-supersession accepts at most one vault directory")
            sys.exit(1)
        if vault_override is not None and positional:
            print("  error: use either positional vault directory or --vault, not both")
            sys.exit(1)
        from d_brain.services.epistemic_memory import recover_pending_supersession

        try:
            recovered = recover_pending_supersession(
                _resolve_vault_root(
                    vault_override or (positional[0] if positional else ".")
                )
            )
        except Exception as exc:
            print(f"  error: {exc}")
            sys.exit(1)
        print(
            "  supersession recovery: "
            + ("rolled forward" if recovered else "no pending journal")
        )
        return

    # Find target directory (first non-flag argument after command)
    target = None
    for a in args[1:]:
        if not a.startswith("-") and a != config_path:
            target = a
            break

    if cmd == "touch":
        if not target:
            print("  error: touch requires a file path")
            sys.exit(1)
        config = load_config(Path(target).parent, config_path)
        cmd_touch(target, config)
        return

    if cmd == "creative":
        n = 5
        creative_dir = None
        for a in args[1:]:
            if not a.startswith("-"):
                try:
                    n = int(a)
                except ValueError:
                    creative_dir = a
        target_dir = Path(creative_dir) if creative_dir else Path(".")
        config = load_config(target_dir, config_path)
        cmd_creative(n, target_dir, config)
        return

    target_dir = Path(target) if target else Path(".")
    if not target_dir.is_dir():
        print(f"  error: {target_dir} is not a directory")
        sys.exit(1)

    config = load_config(target_dir, config_path)

    if cmd == "scan":
        cmd_scan(target_dir, config, verbose)
    elif cmd == "init":
        cmd_init(target_dir, config, dry_run, verbose)
    elif cmd == "decay":
        cmd_decay(target_dir, config, dry_run, verbose)
    elif cmd == "daily":
        cmd_daily(target_dir, config, dry_run, verbose)
    elif cmd == "stats":
        cmd_stats(target_dir, config)
    elif cmd == "config":
        save_default_config(target_dir)
    else:
        print(f"  unknown command: {cmd}")
        print(
            "  commands: scan, init, decay, daily, touch, supersede, "
            "recover-supersession, creative, stats, config"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
