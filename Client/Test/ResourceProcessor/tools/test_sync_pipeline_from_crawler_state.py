import json
import sqlite3
from types import SimpleNamespace

from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy, ProcessState
from ResourceProcessor.tools import flush_object_delete_jobs as flush_jobs
from ResourceProcessor.tools import sync_pipeline_from_crawler_state as sync_tool


def _resource_record(resource_id: str, *, title: str, file_path: str, asset_id: str) -> dict:
    return {
        "id": resource_id,
        "pack_id": "pack-1",
        "source": "testsource",
        "pack_name": "Test Pack",
        "resource_type": "single_image",
        "title": title,
        "resource_path": file_path,
        "parent_resource_id": "",
        "child_resource_ids": [],
        "child_resource_count": 0,
        "contains_resource_types": [],
        "file_paths": [file_path],
        "asset_ids": [asset_id],
        "tags": ["tag-a"],
        "description": "source description",
        "category": "image",
        "license": "cc0",
        "source_url": "https://example.test/resource",
        "download_url": "https://example.test/download",
        "member_count": 1,
    }


def _write_crawler_db(tmp_path, records: list[dict]) -> tuple[str, str]:
    crawler_db = tmp_path / "crawler_state.db"
    output_root = tmp_path / "crawler_output"
    assets_root = output_root / "assets" / "testsource" / "Test Pack"
    assets_root.mkdir(parents=True, exist_ok=True)
    if crawler_db.exists():
        crawler_db.unlink()

    conn = sqlite3.connect(str(crawler_db))
    try:
        conn.execute(
            """CREATE TABLE assets (
                id TEXT PRIMARY KEY,
                pack_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                metadata_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                source TEXT,
                source_pack TEXT,
                source_url TEXT,
                asset_type TEXT,
                index_json TEXT,
                updated_at TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE resource_index (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                pack_id TEXT,
                source TEXT,
                pack_name TEXT,
                resource_type TEXT NOT NULL,
                title TEXT,
                resource_path TEXT,
                group_name TEXT,
                parent_resource_id TEXT,
                member_count INTEGER DEFAULT 0,
                record_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        for record in records:
            file_path = record["file_paths"][0]
            (assets_root / file_path).write_bytes(f"asset:{file_path}".encode("utf-8"))
            asset_id = record["asset_ids"][0]
            conn.execute(
                """INSERT INTO assets
                   (id, pack_id, file_path, metadata_json, source, source_pack, index_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    asset_id,
                    record["pack_id"],
                    file_path,
                    json.dumps({"format": "png"}, ensure_ascii=False),
                    record["source"],
                    record["pack_name"],
                    json.dumps({"style": "flat", "theme": "test"}, ensure_ascii=False),
                ),
            )
            conn.execute(
                """INSERT INTO resource_index
                   (id, pack_id, source, pack_name, resource_type, title, resource_path,
                    parent_resource_id, member_count, record_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"],
                    record["pack_id"],
                    record["source"],
                    record["pack_name"],
                    record["resource_type"],
                    record["title"],
                    record["resource_path"],
                    record["parent_resource_id"],
                    record["member_count"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return str(crawler_db), str(output_root)


def _args(tmp_path, crawler_db: str, output_root: str, db_path, preview_dir):
    return SimpleNamespace(
        crawler_state_db=crawler_db,
        crawler_output=output_root,
        db_path=str(db_path),
        dry_run=False,
        clear_first=False,
        replace_db_file=False,
        no_backup=True,
        keep_preview_files=False,
        preview_dir=[str(preview_dir)],
        commit_every=0,
        asset_batch_size=10,
    )


def _add_generated_outputs(db_path, preview_dir, *, task_id: int, name: str = "preview.webp"):
    preview_path = preview_dir / name
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"preview")
    store = LocalCacheStore(str(db_path))
    try:
        store.insert_preview(
            task_id,
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(preview_path),
                format="webp",
                width=1,
                height=1,
                size=preview_path.stat().st_size,
            ),
        )
        store.insert_description(
            task_id,
            main_content="main",
            detail_content="detail",
            full_description="full",
            prompt_version="test",
        )
        store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
    finally:
        store.close()
    return preview_path


def _add_object_manifest(db_path, *, task_id: int, client_resource_id: str):
    store = LocalCacheStore(str(db_path))
    try:
        store.upsert_object_manifest(
            task_id,
            {
                "client_resource_id": client_resource_id,
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": f"resource-crawler/client/files/{client_resource_id}/source.png",
                },
                "source_files": [
                    {
                        "object_key": f"resource-crawler/client/files/{client_resource_id}/legacy-extra.png",
                    }
                ],
                "provided_previews": [
                    {
                        "storage_profile_id": "default",
                        "object_key": f"resource-crawler/client/previews/{client_resource_id}/primary.webp",
                    }
                ],
                "package_object": {
                    "storage_profile_id": "default",
                    "object_key": "resource-crawler/client/files/shared-pack/source.zip",
                },
            },
        )
    finally:
        store.close()


def _task_id(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT id FROM resource_task").fetchone()[0]
    finally:
        conn.close()


def test_sync_preserves_invalidates_and_deletes_downstream_outputs(tmp_path):
    db_path = tmp_path / "pipeline.db"
    preview_dir = tmp_path / "previews"
    record = _resource_record("src-1", title="Original", file_path="image.png", asset_id="asset-1")
    crawler_db, output_root = _write_crawler_db(tmp_path, [record])

    assert sync_tool.sync(_args(tmp_path, crawler_db, output_root, db_path, preview_dir)) == 0
    task_id = _task_id(db_path)
    preview_path = _add_generated_outputs(db_path, preview_dir, task_id=task_id)

    assert sync_tool.sync(_args(tmp_path, crawler_db, output_root, db_path, preview_dir)) == 0
    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(task_id)["process_state"] == ProcessState.DESCRIPTION_READY.value
        assert len(store.get_previews_by_task(task_id)) == 1
        assert store.get_description_by_task(task_id) is not None
        assert preview_path.exists()
    finally:
        store.close()

    record = dict(record)
    record["title"] = "Renamed"
    crawler_db, output_root = _write_crawler_db(tmp_path, [record])
    assert sync_tool.sync(_args(tmp_path, crawler_db, output_root, db_path, preview_dir)) == 0
    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(task_id)["process_state"] == ProcessState.PREVIEW_READY.value
        assert store.get_task_by_id(task_id)["title"] == "Renamed"
        assert len(store.get_previews_by_task(task_id)) == 1
        assert store.get_description_by_task(task_id) is None
        assert preview_path.exists()
    finally:
        store.close()

    preview_path = _add_generated_outputs(db_path, preview_dir, task_id=task_id, name="preview2.webp")
    changed_record = _resource_record("src-1", title="Renamed", file_path="image2.png", asset_id="asset-2")
    crawler_db, output_root = _write_crawler_db(tmp_path, [changed_record])
    assert sync_tool.sync(_args(tmp_path, crawler_db, output_root, db_path, preview_dir)) == 0
    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(task_id)["process_state"] == ProcessState.DISCOVERED.value
        assert len(store.get_previews_by_task(task_id)) == 0
        assert store.get_description_by_task(task_id) is None
        assert store.get_files_by_task(task_id)[0]["file_name"] == "image2.png"
        assert not preview_path.exists()
    finally:
        store.close()

    preview_path = _add_generated_outputs(db_path, preview_dir, task_id=task_id, name="preview3.webp")
    _add_object_manifest(db_path, task_id=task_id, client_resource_id="src-1")
    crawler_db, output_root = _write_crawler_db(tmp_path, [])
    assert sync_tool.sync(_args(tmp_path, crawler_db, output_root, db_path, preview_dir)) == 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM resource_task").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM resource_preview").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM resource_description").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM resource_object_manifest").fetchone()[0] == 0
        job = conn.execute("SELECT * FROM resource_object_delete_job").fetchone()
        assert job["client_resource_id"] == "src-1"
        assert job["source_resource_id"] == "src-1"
        assert job["status"] == "pending"
        object_keys = json.loads(job["object_keys_json"])
        assert object_keys == [
            "resource-crawler/client/files/src-1/source.png",
            "resource-crawler/client/files/src-1/legacy-extra.png",
            "resource-crawler/client/previews/src-1/primary.webp",
        ]
        assert "shared-pack/source.zip" not in "\n".join(object_keys)
    finally:
        conn.close()
    assert not preview_path.exists()


def test_flush_object_delete_jobs_retries_failed_jobs(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    LocalCacheStore(str(db_path)).close()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """INSERT INTO resource_object_delete_job
               (client_resource_id, source_resource_id, task_id_snapshot,
                storage_profile_id, object_keys_json, object_refs_json,
                manifest_json_snapshot, status, attempt_count, last_error,
                reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, '{}', 'pending', 0, '', 'test', 'now', 'now')""",
            (
                "asset-1",
                "asset-1",
                1,
                "default",
                json.dumps(["resource-crawler/client/files/asset-1/source.png"]),
                json.dumps([
                    {
                        "storage_profile_id": "default",
                        "object_key": "resource-crawler/client/files/asset-1/source.png",
                    }
                ]),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    class FailingUploader:
        def __init__(self, *args, **kwargs):
            pass

        def delete_objects(self, object_keys):
            raise RuntimeError("offline")

    monkeypatch.setattr(flush_jobs, "ObjectStorageUploader", FailingUploader)
    args = SimpleNamespace(db_path=str(db_path), limit=None, max_attempts=10, dry_run=False)
    assert flush_jobs.flush(args) == 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        failed = conn.execute("SELECT * FROM resource_object_delete_job").fetchone()
        assert failed["status"] == "failed"
        assert failed["attempt_count"] == 1
    finally:
        conn.close()

    deleted_keys = []

    class SuccessfulUploader:
        def __init__(self, *args, **kwargs):
            pass

        def delete_objects(self, object_keys):
            deleted_keys.extend(object_keys)
            return len(object_keys)

    monkeypatch.setattr(flush_jobs, "ObjectStorageUploader", SuccessfulUploader)
    assert flush_jobs.flush(args) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        done = conn.execute("SELECT * FROM resource_object_delete_job").fetchone()
        assert done["status"] == "deleted"
        assert deleted_keys == ["resource-crawler/client/files/asset-1/source.png"]
    finally:
        conn.close()
