from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB = Path("data/databases/pipeline.db")
DEFAULT_OLD_PREVIEW_ROOT = Path("data/workdirs/test_workdir_rebuilt_20260608_150207/previews")
DEFAULT_NEW_PREVIEW_ROOT = Path("data/previews")
BACKUP_TABLE = "resource_preview_path_backup_20260703"


def _safe_part(value: str, fallback: str = "resource") -> str:
    text = str(value or "").strip().strip(" .")
    for char in '<>:"/\\|?*\x00':
        text = text.replace(char, "_")
    return text if text and text not in {".", ".."} else fallback


@dataclass(frozen=True)
class MigrationItem:
    preview_id: int
    old_path: Path
    new_path: Path


def _label(role: str, index: int) -> str:
    role = _safe_part(role or "primary", "primary")
    if role == "primary" and index == 1:
        return "primary"
    if role == "gallery":
        return f"gallery-{index:03d}"
    return f"{role}-{index:03d}"


def build_items(conn: sqlite3.Connection, old_root: Path, new_root: Path) -> list[MigrationItem]:
    old_root_text = str(old_root)
    rows = conn.execute(
        """
        SELECT
          p.id AS preview_id,
          p.path AS old_path,
          p.role AS role,
          p.format AS format,
          t.resource_type AS resource_type,
          t.source_resource_id AS source_resource_id
        FROM resource_preview p
        JOIN resource_task t ON t.id = p.task_id
        WHERE p.path LIKE ?
        ORDER BY t.resource_type, t.source_resource_id, p.id
        """,
        (f"%{old_root.name}%",),
    ).fetchall()

    counters: dict[str, int] = {}
    targets: dict[Path, int] = {}
    items: list[MigrationItem] = []
    for row in rows:
        old_path = Path(row["old_path"])
        try:
            old_path.relative_to(old_root)
        except ValueError:
            # The LIKE filter intentionally catches both absolute and relative
            # paths; only migrate paths under the exact old preview root.
            if old_root_text.lower() not in str(old_path).lower():
                continue

        resource_type = _safe_part(row["resource_type"], "other")
        resource_id = _safe_part(row["source_resource_id"], f"task_{row['preview_id']}")
        key = f"{resource_type}/{resource_id}/{row['role'] or 'primary'}"
        counters[key] = counters.get(key, 0) + 1

        suffix = old_path.suffix
        if not suffix and row["format"]:
            suffix = "." + str(row["format"]).lstrip(".")
        suffix = suffix or ".webp"

        target = new_root / resource_type / f"{resource_id}_{_label(row['role'], counters[key])}{suffix.lower()}"
        existing_id = targets.get(target)
        if existing_id is not None:
            raise RuntimeError(f"target path collision: preview {existing_id} and {row['preview_id']} -> {target}")
        targets[target] = int(row["preview_id"])
        items.append(MigrationItem(int(row["preview_id"]), old_path, target.resolve()))
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Move preview files to data/previews and update resource_preview.path")
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--old-preview-root", default=str(DEFAULT_OLD_PREVIEW_ROOT))
    parser.add_argument("--new-preview-root", default=str(DEFAULT_NEW_PREVIEW_ROOT))
    parser.add_argument("--apply", action="store_true", help="copy files and update the database")
    args = parser.parse_args()

    db_path = Path(args.db_path).resolve()
    old_root = Path(args.old_preview_root).resolve()
    new_root = Path(args.new_preview_root).resolve()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        items = build_items(conn, old_root, new_root)
        missing = [item for item in items if not item.old_path.is_file()]
        existing_targets = [item for item in items if item.new_path.exists()]
        print(f"db: {db_path}")
        print(f"old root: {old_root}")
        print(f"new root: {new_root}")
        print(f"preview rows: {len(items)}")
        print(f"missing source files: {len(missing)}")
        print(f"existing target files: {len(existing_targets)}")
        if items:
            print("sample mappings:")
            for item in items[:5]:
                print(f"  {item.old_path} -> {item.new_path}")

        if missing:
            for item in missing[:20]:
                print(f"MISSING: preview_id={item.preview_id} {item.old_path}")
            raise SystemExit(2)
        if existing_targets:
            for item in existing_targets[:20]:
                print(f"EXISTS: preview_id={item.preview_id} {item.new_path}")
            raise SystemExit(3)
        if not args.apply:
            print("dry-run only; pass --apply to migrate")
            return 0

        copied = 0
        for item in items:
            item.new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.old_path, item.new_path)
            copied += 1
            if copied % 10000 == 0:
                print(f"copied {copied}/{len(items)}")

        migrated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
                    preview_id INTEGER PRIMARY KEY,
                    old_path TEXT NOT NULL,
                    new_path TEXT NOT NULL,
                    migrated_at TEXT NOT NULL
                )
                """
            )
            conn.executemany(
                f"""
                INSERT OR REPLACE INTO {BACKUP_TABLE}
                (preview_id, old_path, new_path, migrated_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (item.preview_id, str(item.old_path), str(item.new_path), migrated_at)
                    for item in items
                ],
            )
            conn.executemany(
                "UPDATE resource_preview SET path = ? WHERE id = ?",
                [(str(item.new_path), item.preview_id) for item in items],
            )
        print(f"migrated {len(items)} previews")
        print(f"backup table: {BACKUP_TABLE}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
