from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps


PROCESS_ORDER = {
    "discovered": 0,
    "preview_ready": 1,
    "preview_failed": 1,
    "description_ready": 2,
    "description_failed": 2,
    "embedding_ready": 3,
    "embedding_failed": 3,
    "package_ready": 4,
    "registered": 5,
    "uploaded": 6,
    "committed": 7,
    "synced": 8,
}

PRIORITY = {
    "tiled_map": 5,
    "atlas": 10,
    "spine_skeleton": 15,
    "tileset": 20,
    "animation_sequence": 30,
    "font_file": 40,
    "audio_file": 50,
    "single_image": 70,
}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _add_scripts_path(root: Path) -> None:
    scripts = root / "client" / "Scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))


def _pack_rows(
    conn: sqlite3.Connection,
    marker: str,
    limit: int | None,
    *,
    worker_count: int,
    worker_index: int,
) -> list[sqlite3.Row]:
    sql = """
        SELECT *
        FROM resource_task rt
        WHERE rt.resource_type = 'pack'
          AND NOT EXISTS (
              SELECT 1 FROM resource_preview rp
              WHERE rp.task_id = rt.id AND rp.created_at >= ?
          )
    """
    params: list[object] = [marker]
    if worker_count > 1:
        sql += " AND (rt.id % ?) = ?"
        params.extend([worker_count, worker_index])
    sql += " ORDER BY rt.id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _child_preview_rows(
    conn: sqlite3.Connection,
    pack: sqlite3.Row,
    sample_limit: int,
) -> list[sqlite3.Row]:
    params: list[object]
    if pack["source_resource_id"]:
        where = "c.parent_resource_id = ?"
        params = [pack["source_resource_id"], int(pack["id"])]
    else:
        where = "c.source = ? AND c.pack_name = ?"
        params = [pack["source"], pack["pack_name"], int(pack["id"])]

    priority_sql = "CASE c.resource_type " + " ".join(
        f"WHEN '{resource_type}' THEN {priority}" for resource_type, priority in PRIORITY.items()
    ) + " ELSE 100 END"
    sql = f"""
        SELECT c.id AS task_id, c.resource_type, c.title, c.resource_path, rp.path
        FROM resource_task c
        JOIN latest_primary_preview rp ON rp.task_id = c.id
        WHERE {where}
          AND c.id <> ?
          AND c.resource_type <> 'pack'
        ORDER BY {priority_sql}, c.id
        LIMIT ?
    """
    params.append(sample_limit)
    return conn.execute(sql, params).fetchall()


def _create_latest_preview_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.latest_primary_preview")
    conn.execute(
        """CREATE TEMP TABLE latest_primary_preview AS
           SELECT rp.task_id, rp.path
           FROM resource_preview rp
           JOIN (
               SELECT task_id, MAX(id) AS id
               FROM resource_preview
               WHERE role = 'primary'
                 AND COALESCE(path, '') <> ''
               GROUP BY task_id
           ) latest ON latest.id = rp.id"""
    )
    conn.execute("CREATE INDEX latest_primary_preview_task_idx ON latest_primary_preview(task_id)")


def _open_preview(path: str, cell: int) -> Image.Image | None:
    preview_path = Path(path)
    if not preview_path.is_file():
        return None
    try:
        with Image.open(preview_path) as image:
            image.seek(0)
            rgba = image.convert("RGBA")
    except Exception:
        return None

    rgba.thumbnail((cell - 12, cell - 12), Image.Resampling.LANCZOS)
    tile = Image.new("RGBA", (cell, cell), (246, 247, 249, 255))
    x = (cell - rgba.width) // 2
    y = (cell - rgba.height) // 2
    tile.alpha_composite(rgba, (x, y))
    return tile


def _save_contact_sheet(paths: Iterable[str], output_path: Path, size: int) -> bool:
    unique_paths = []
    seen = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique_paths.append(path)

    cols = 4
    rows = 4
    cell = size // cols
    tiles = []
    for path in unique_paths[: cols * rows]:
        tile = _open_preview(path, cell)
        if tile is not None:
            tiles.append(tile)
    if not tiles:
        return False

    canvas = Image.new("RGBA", (size, size), (246, 247, 249, 255))
    for index, tile in enumerate(tiles):
        row = index // cols
        col = index % cols
        canvas.alpha_composite(tile, (col * cell, row * cell))

    rgb = ImageOps.contain(canvas.convert("RGB"), (size, size))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rgb.save(output_path, format="WEBP", quality=88, method=4)
    return True


def _content_key(pack: sqlite3.Row) -> str:
    value = str(pack["content_md5"] or "")
    if value:
        return value
    text = f"{pack['id']}|{pack['source']}|{pack['pack_name']}"
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


def _insert_preview(
    conn: sqlite3.Connection,
    pack: sqlite3.Row,
    preview_path: Path,
    *,
    strategy: str,
    renderer: str,
    used_placeholder: bool,
    fail_reason: str,
) -> None:
    with Image.open(preview_path) as image:
        width, height = image.size
    created_at = _now()
    task_id = int(pack["id"])
    conn.execute("DELETE FROM resource_preview WHERE task_id = ?", (task_id,))
    conn.execute(
        """INSERT INTO resource_preview
           (task_id, strategy, role, path, format, width, height, size,
            renderer, used_placeholder, fail_reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            strategy,
            "primary",
            str(preview_path.resolve()),
            preview_path.suffix.lstrip("."),
            width,
            height,
            preview_path.stat().st_size,
            renderer,
            1 if used_placeholder else 0,
            fail_reason,
            created_at,
        ),
    )
    current_state = str(pack["process_state"] or "discovered")
    if PROCESS_ORDER.get(current_state, 0) < PROCESS_ORDER["preview_ready"]:
        conn.execute(
            """UPDATE resource_task
               SET process_state = 'preview_ready',
                   last_error_code = '',
                   last_error_message = '',
                   updated_at = ?
               WHERE id = ?""",
            (created_at, task_id),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast contact-sheet preview refresh for pack tasks.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--resource-upload-root", default=r"G:\ResourceUpload")
    parser.add_argument("--marker", required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sample-limit", type=int, default=24)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--worker-count", type=int, default=1)
    parser.add_argument("--worker-index", type=int, default=0)
    args = parser.parse_args()
    if args.worker_count < 1:
        parser.error("--worker-count must be >= 1")
    if not 0 <= args.worker_index < args.worker_count:
        parser.error("--worker-index must be in [0, worker-count)")

    root = Path(args.resource_upload_root)
    _add_scripts_path(root)
    from ResourceProcessor.preview.crawler_thumbnail_policy import _save_metadata_card
    from ResourceProcessor.preview.thumbnail_generator import validate_preview

    previews_dir = Path(args.work_dir) / "previews" / "pack"
    conn = sqlite3.connect(args.db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    packs = _pack_rows(
        conn,
        args.marker,
        args.limit,
        worker_count=args.worker_count,
        worker_index=args.worker_index,
    )

    print(
        f"fast_pack_refresh marker={args.marker} worker={args.worker_index}/{args.worker_count} "
        f"tasks={len(packs)}",
        flush=True,
    )
    _create_latest_preview_table(conn)
    print("latest_primary_preview ready", flush=True)
    processed = 0
    placeholders = 0
    t0 = time.time()
    try:
        for pack in packs:
            key = _content_key(pack)
            output_path = previews_dir / f"{key}_pack.webp"
            child_rows = _child_preview_rows(conn, pack, args.sample_limit)
            child_paths = [str(row["path"]) for row in child_rows if row["path"]]
            rendered = _save_contact_sheet(child_paths, output_path, 512)
            if rendered:
                strategy = "contact_sheet"
                renderer = "fast-pack-contact-sheet"
                used_placeholder = False
                fail_reason = ""
            else:
                output_path = previews_dir / f"{key}_pack_placeholder.webp"
                _save_metadata_card(
                    output_path,
                    "Pack preview",
                    str(pack["pack_name"] or pack["title"] or f"pack {pack['id']}"),
                    ["No child previews available"],
                    size=512,
                )
                placeholders += 1
                strategy = "static"
                renderer = "fast-pack-placeholder"
                used_placeholder = True
                fail_reason = "no_child_previews"

            passed, reason = validate_preview(str(output_path))
            if not passed:
                raise RuntimeError(f"invalid preview for pack {pack['id']}: {reason}")
            _insert_preview(
                conn,
                pack,
                output_path,
                strategy=strategy,
                renderer=renderer,
                used_placeholder=used_placeholder,
                fail_reason=fail_reason,
            )
            conn.commit()
            processed += 1
            if args.progress_every and processed % args.progress_every == 0:
                print(
                    f"progress processed={processed}/{len(packs)} placeholders={placeholders} "
                    f"elapsed={time.time() - t0:.1f}s",
                    flush=True,
                )
    finally:
        conn.close()

    print(
        f"summary processed={processed} placeholders={placeholders} elapsed={time.time() - t0:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
