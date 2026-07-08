from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
TOOLS_ROOT = REPO_ROOT / "Tools"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from PIL import Image
from spine_preview.runtime.helper.postprocess_frames import process_manifest
DEFAULT_DB_PATH = REPO_ROOT / "data" / "databases" / "pipeline.db"
DEFAULT_WORK_DIR = REPO_ROOT / "data" / "workdirs" / "test_workdir_rebuilt_20260608_150207"
DEFAULT_STAGE_DIR = REPO_ROOT / "data" / "workdirs" / "test_workdir_spine_runtime_preview"
DEFAULT_EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
DEFAULT_CHROME = Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
PREVIEW_FILE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"}

from ResourceProcessor.cache.local_cache import (  # noqa: E402
    CONTENT_HASH_ALGORITHM,
    LocalCacheStore,
    file_content_hash,
    refresh_resource_fingerprint_for_connection,
)
from resource_contracts.resource_types import SPINE_SKELETON_RESOURCE_TYPE  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh formal Spine skeleton previews in a pipeline SQLite DB.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR))
    parser.add_argument("--stage-dir", default=str(DEFAULT_STAGE_DIR))
    parser.add_argument(
        "--resource-type",
        default=SPINE_SKELETON_RESOURCE_TYPE,
        help=f"Only {SPINE_SKELETON_RESOURCE_TYPE} is supported by this helper.",
    )
    parser.add_argument("--task-id", action="append", type=int, help="Refresh only this task id; can be repeated.")
    parser.add_argument("--force", action="store_true", help="Refresh even when a valid primary preview already exists.")
    parser.add_argument("--browser", default="", help="Chromium/Edge/Chrome executable path.")
    parser.add_argument("--frames", type=int, default=5)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=768)
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path: Path) -> sqlite3.Connection:
    cache = LocalCacheStore(str(path))
    cache.close()
    conn = sqlite3.connect(path, timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def browser_candidates(raw: str) -> list[Path]:
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw))
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge", "msedge"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    candidates.extend([DEFAULT_EDGE, DEFAULT_CHROME])
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
        result.append(candidate)
    return result


def spine_tasks(conn: sqlite3.Connection, resource_type: str, task_ids: list[int] | None) -> list[dict]:
    sql = """
        SELECT id, content_md5, title, resource_path, pack_name
        FROM resource_task
        WHERE resource_type = ?
    """
    params: list[object] = [resource_type]
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        sql += f" AND id IN ({placeholders})"
        params.extend(task_ids)
    sql += " ORDER BY id"
    return [dict(row) for row in conn.execute(sql, params)]


def task_files(conn: sqlite3.Connection, task_id: int) -> tuple[Path, Path, list[Path]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT file_path, file_name, file_format
               FROM resource_file
               WHERE task_id = ?
               ORDER BY is_primary DESC, id""",
            (task_id,),
        )
    ]
    json_paths = [Path(row["file_path"]) for row in rows if Path(row["file_path"]).suffix.lower() == ".json"]
    atlas_paths = [
        Path(row["file_path"])
        for row in rows
        if Path(row["file_path"]).suffix.lower() in {".atlas", ".txt"} and "atlas" in Path(row["file_path"]).name.lower()
    ]
    image_paths = [
        Path(row["file_path"])
        for row in rows
        if Path(row["file_path"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    if not json_paths:
        raise RuntimeError(f"task {task_id}: missing skeleton json")
    if not atlas_paths:
        raise RuntimeError(f"task {task_id}: missing spine atlas")
    if not image_paths:
        raise RuntimeError(f"task {task_id}: missing spine texture image")
    missing = [path for path in [json_paths[0], atlas_paths[0], *image_paths] if not path.exists()]
    if missing:
        raise RuntimeError(f"task {task_id}: missing files: {', '.join(str(path) for path in missing)}")
    return json_paths[0], atlas_paths[0], image_paths


def render_actions(task_id: int, json_path: Path, atlas_path: Path, image_paths: list[Path], out_dir: Path, browser: Path, args: argparse.Namespace) -> Path:
    command = [
        "node",
        str(TOOLS_ROOT / "spine_preview" / "runtime" / "helper" / "render_actions_cli.mjs"),
        "--json",
        str(json_path),
        "--atlas",
        str(atlas_path),
        "--images",
        ";".join(str(path) for path in image_paths),
        "--out",
        str(out_dir),
        "--frames",
        str(args.frames),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--chrome",
        str(browser),
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"task {task_id}: renderer failed with {browser}\nSTDOUT:\n{completed.stdout[-2000:]}\nSTDERR:\n{completed.stderr[-2000:]}"
        )
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"task {task_id}: renderer did not write manifest: {manifest_path}")
    return manifest_path


def copy_formal_overview(task: dict, overview_path: Path, formal_dir: Path) -> tuple[Path, int, int, int]:
    formal_dir.mkdir(parents=True, exist_ok=True)
    target = formal_dir / f"{task['content_md5']}_all_actions_overview.webp"
    shutil.copy2(overview_path, target)
    with Image.open(target) as image:
        width, height = image.size
    return target, width, height, target.stat().st_size


def path_key(path: str | Path) -> str:
    return str(Path(path).resolve()).lower()


def preview_rows(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM resource_preview WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
    ]


def delete_old_preview_files(old_previews: list[dict], keep_paths: set[str]) -> tuple[int, int]:
    deleted = 0
    skipped = 0
    keep_keys = {path_key(path) for path in keep_paths if path}
    seen: set[str] = set()
    for preview in old_previews:
        raw_path = preview.get("path") if isinstance(preview, dict) else ""
        if not raw_path:
            skipped += 1
            continue
        path = Path(raw_path)
        key = path_key(path)
        if key in seen or key in keep_keys:
            skipped += 1
            continue
        seen.add(key)
        if path.suffix.lower() not in PREVIEW_FILE_EXTS or not path.is_file():
            skipped += 1
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError:
            skipped += 1
    return deleted, skipped


def latest_primary_preview(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute(
        """SELECT * FROM resource_preview
           WHERE task_id = ? AND role = 'primary'
           ORDER BY id DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    return dict(row) if row else None


def has_valid_primary_preview(conn: sqlite3.Connection, task_id: int) -> bool:
    preview = latest_primary_preview(conn, task_id)
    return bool(preview and preview.get("path") and Path(preview["path"]).is_file())


def replace_preview(conn: sqlite3.Connection, task_id: int, path: Path, width: int, height: int, size: int) -> tuple[int, int]:
    now = now_iso()
    content_hash = file_content_hash(path)
    delete_cur = conn.execute("DELETE FROM resource_preview WHERE task_id = ?", (task_id,))
    cur = conn.execute(
        """INSERT INTO resource_preview
           (task_id, strategy, role, path, format, width, height, size,
            renderer, used_placeholder, fail_reason, content_hash,
            content_hash_algorithm, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            "contact_sheet",
            "primary",
            str(path),
            "webp",
            width,
            height,
            size,
            "spine-webgl-3.8-playwright",
            0,
            None,
            content_hash,
            CONTENT_HASH_ALGORITHM,
            now,
        ),
    )
    refresh_resource_fingerprint_for_connection(conn, task_id, now=now)
    conn.commit()
    return int(cur.lastrowid), int(delete_cur.rowcount or 0)


def refresh_task(conn: sqlite3.Connection, task: dict, args: argparse.Namespace, browsers: list[Path]) -> dict:
    task_id = int(task["id"])
    if has_valid_primary_preview(conn, task_id) and not args.force:
        return {
            "task_id": task_id,
            "skipped": True,
            "reason": "primary preview already exists; pass --force to refresh",
        }
    old_previews = preview_rows(conn, task_id)
    json_path, atlas_path, image_paths = task_files(conn, task_id)
    stage_dir = Path(args.stage_dir).resolve() / str(task_id)
    last_error = ""
    manifest_path: Path | None = None
    used_browser: Path | None = None
    for browser in browsers:
        try:
            manifest_path = render_actions(task_id, json_path, atlas_path, image_paths, stage_dir, browser, args)
            used_browser = browser
            break
        except Exception as exc:
            last_error = str(exc)
    if manifest_path is None or used_browser is None:
        raise RuntimeError(last_error or f"task {task_id}: no browser could render preview")

    post = process_manifest(manifest_path, thumb_size=260, gif_size=320, duration_ms=130)
    overview = Path(str(post.get("overview") or ""))
    if not overview.exists():
        raise RuntimeError(f"task {task_id}: postprocess did not write overview")
    formal_path, width, height, size = copy_formal_overview(
        task,
        overview,
        Path(args.work_dir).resolve() / "previews" / SPINE_SKELETON_RESOURCE_TYPE,
    )
    preview_id, deleted_rows = replace_preview(conn, task_id, formal_path, width, height, size)
    deleted_files, skipped_files = delete_old_preview_files(old_previews, {str(formal_path)})
    return {
        "task_id": task_id,
        "skipped": False,
        "preview_id": preview_id,
        "deleted_preview_rows": deleted_rows,
        "deleted_preview_files": deleted_files,
        "skipped_preview_files": skipped_files,
        "path": str(formal_path),
        "width": width,
        "height": height,
        "size": size,
        "animation_count": post.get("animation_count"),
        "browser": str(used_browser),
    }


def main() -> int:
    args = parse_args()
    if args.resource_type != SPINE_SKELETON_RESOURCE_TYPE:
        print(f"This helper only supports --resource-type {SPINE_SKELETON_RESOURCE_TYPE}.", file=sys.stderr)
        return 2
    db_path = Path(args.db_path).resolve()
    browsers = browser_candidates(args.browser)
    if not browsers:
        print("No Chromium/Chrome/Edge executable found.", file=sys.stderr)
        return 2

    conn = open_db(db_path)
    try:
        tasks = spine_tasks(conn, args.resource_type, args.task_id)
        if not tasks:
            print(f"No {SPINE_SKELETON_RESOURCE_TYPE} tasks found.")
            return 0
        results = [refresh_task(conn, task, args, browsers) for task in tasks]
    finally:
        conn.close()

    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
