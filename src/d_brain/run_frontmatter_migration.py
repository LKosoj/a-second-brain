"""CLI for guarded frontmatter inventory, backup, apply, and enrichment."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from d_brain.manifest import ManifestValidationError, load_manifest_for_vault
from d_brain.services.frontmatter_migration import (
    MigrationStateError,
    apply_migration_state,
    apply_semantic_result,
    build_migration_state,
    create_backup_gate,
    inventory_vault,
    load_migration_state,
    projected_migration_summary,
    reconcile_migration_state,
    save_migration_state,
    validate_vault,
    write_inventory_report,
)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--vault", type=Path, default=Path("vault"))
    inventory.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--state", type=Path, required=True)
    prepare.add_argument("--backup-dir", type=Path, required=True)
    prepare.add_argument("--proof", type=Path, required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--state", type=Path, required=True)
    migrate.add_argument("--backup-manifest", type=Path)
    migrate.add_argument("--proof", type=Path)
    mode = migrate.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    enrich = commands.add_parser("enrich")
    enrich.add_argument("--state", type=Path, required=True)
    enrich.add_argument("--input-json", type=Path, required=True)
    enrich.add_argument("--backup-manifest", type=Path, required=True)
    enrich.add_argument("--proof", type=Path, required=True)
    enrich.add_argument("--batch-size", type=_positive_int, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--vault", type=Path, default=Path("vault"))
    return parser


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _pending_exit(summary: Mapping[str, int]) -> int:
    return int(
        any(summary.get(key, 0) for key in ("pending", "errors", "stale", "malformed"))
    )


def _load_results(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationStateError(f"cannot load semantic input: {exc}") from exc
    results = value.get("results") if isinstance(value, dict) else value
    if not isinstance(results, list) or not all(
        isinstance(item, dict) for item in results
    ):
        raise MigrationStateError(
            "semantic input must be a list or {'results': [...]} object"
        )
    return results


def _bounded_pending_results(
    state: Mapping[str, Any],
    results: list[dict[str, Any]],
    batch_size: int,
) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for result in results:
        path = result.get("path")
        payload = result.get("payload")
        if not isinstance(path, str) or not isinstance(payload, dict):
            raise MigrationStateError("each semantic result needs path and payload")
        if path in by_path:
            raise MigrationStateError(f"duplicate semantic result path: {path}")
        by_path[path] = result
    entries = state.get("entries")
    if not isinstance(entries, list):
        raise MigrationStateError("migration state entries are invalid")
    known_paths = {entry.get("path") for entry in entries if isinstance(entry, dict)}
    unknown = sorted(set(by_path) - known_paths)
    if unknown:
        raise MigrationStateError(
            "semantic results contain unknown paths: " + ", ".join(unknown)
        )
    pending_paths = [
        entry["path"]
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("status") == "pending"
        and isinstance(entry.get("semantic"), dict)
        and entry["semantic"].get("status") == "pending"
        and entry.get("path") in by_path
    ]
    return [by_path[path] for path in pending_paths[:batch_size]]


def main() -> int:
    args = _parser().parse_args()
    if args.command == "inventory":
        manifest = load_manifest_for_vault(args.vault)
        inventory = inventory_vault(args.vault, manifest)
        if not inventory["coverage_complete"]:
            report_path = write_inventory_report(inventory, args.output)
            _emit(
                {
                    "inventory": str(report_path),
                    "count": inventory["source_count"],
                    "coverage_complete": False,
                    "symlink_directory_count": inventory["symlink_directory_count"],
                }
            )
            return 1
        state = build_migration_state(inventory)
        report_path = write_inventory_report(inventory, args.output, state=state)
        state_path = args.output / "migration-state.json"
        save_migration_state(state_path, state)
        _emit(
            {
                "inventory": str(report_path),
                "state": str(state_path),
                "count": inventory["source_count"],
                "projected": projected_migration_summary(state),
            }
        )
        return 0
    if args.command == "prepare":
        state = load_migration_state(args.state)
        manifest = load_manifest_for_vault(Path(state["vault_path"]))
        reconcile_migration_state(
            state,
            state_path=args.state,
            manifest=manifest,
            backup_manifest_path=args.backup_dir / "backup-manifest.json",
            proof_path=args.proof,
        )
        backup, proof = create_backup_gate(
            state, backup_dir=args.backup_dir, proof_path=args.proof
        )
        _emit({"backup_manifest": str(backup), "proof": str(proof)})
        return 0
    if args.command == "migrate":
        state = load_migration_state(args.state)
        manifest = load_manifest_for_vault(Path(state["vault_path"]))
        if args.apply and (args.backup_manifest is None or args.proof is None):
            raise MigrationStateError("--apply requires --backup-manifest and --proof")
        summary = apply_migration_state(
            state,
            manifest=manifest,
            apply=args.apply,
            state_path=args.state if args.apply else None,
            backup_manifest_path=args.backup_manifest,
            proof_path=args.proof,
        )
        _emit(summary)
        return _pending_exit(summary)
    if args.command == "enrich":
        state = load_migration_state(args.state)
        manifest = load_manifest_for_vault(Path(state["vault_path"]))
        selected = _bounded_pending_results(
            state, _load_results(args.input_json), args.batch_size
        )
        for result in selected:
            path = result["path"]
            payload = result["payload"]
            apply_semantic_result(
                state,
                manifest=manifest,
                relative_path=path,
                payload=payload,
                state_path=args.state,
                backup_manifest_path=args.backup_manifest,
                proof_path=args.proof,
            )
        summary = apply_migration_state(state, manifest=manifest, apply=False)
        _emit({**summary, "processed": len(selected)})
        return _pending_exit(summary)
    manifest = load_manifest_for_vault(args.vault)
    report = validate_vault(args.vault, manifest)
    _emit({key: value for key, value in report.items() if key != "entries"})
    return int(
        not report["coverage_complete"]
        or bool(report["blocking_symlink_directory_count"])
        or any(
            report[key]
            for key in (
                "malformed",
                "missing",
                "invalid",
                "unreadable",
                "symlink",
                "racing",
            )
        )
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ManifestValidationError, MigrationStateError) as exc:
        print(f"frontmatter-migration: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
