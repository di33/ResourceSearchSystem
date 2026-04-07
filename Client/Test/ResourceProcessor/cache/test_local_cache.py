"""Tests for ResourceProcessor.local_cache – SQLite local cache module."""

import sqlite3

from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import (
    FileInfo,
    PreviewInfo,
    PreviewStrategy,
    ProcessState,
    ResourceProcessingEntity,
)


def _make_entity(**overrides) -> ResourceProcessingEntity:
    defaults = dict(
        content_md5="abc123",
        resource_type="image",
        source_directory="/tmp",
        files=[FileInfo(
            file_path="/tmp/img.png",
            file_name="img.png",
            file_size=1024,
            file_format="png",
            content_md5="abc123",
            is_primary=True,
        )],
    )
    defaults.update(overrides)
    return ResourceProcessingEntity(**defaults)


# ---- 1. test_create_tables ----


def test_create_tables(tmp_path):
    db = tmp_path / "test.db"
    store = LocalCacheStore(str(db))
    try:
        conn = sqlite3.connect(str(db))
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = sorted(
            row[0] for row in cur.fetchall() if not row[0].startswith("sqlite_")
        )
        conn.close()
        expected = sorted([
            "resource_task",
            "resource_file",
            "resource_preview",
            "resource_description",
            "resource_embedding",
            "resource_upload_job",
            "process_log",
        ])
        assert tables == expected
    finally:
        store.close()


# ---- 2. test_insert_and_get_task ----


def test_insert_and_get_task(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        entity = _make_entity(resource_id="rid-1")
        task_id = store.insert_task(entity)
        assert isinstance(task_id, int) and task_id >= 1

        row = store.get_task_by_id(task_id)
        assert row is not None
        assert row["content_md5"] == "abc123"
        assert row["resource_type"] == "image"
        assert row["source_directory"] == "/tmp"
        assert row["process_state"] == ProcessState.DISCOVERED.value
        assert row["resource_id"] == "rid-1"
        assert row["retry_count"] == 0

        files = store.get_files_by_task(task_id)
        assert len(files) == 1
        assert files[0]["file_name"] == "img.png"
        assert files[0]["file_size"] == 1024
        assert files[0]["file_format"] == "png"
        assert files[0]["is_primary"] == 1
    finally:
        store.close()


# ---- 3. test_get_tasks_by_md5 ----


def test_get_tasks_by_md5(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        store.insert_task(_make_entity(content_md5="md5_a"))
        store.insert_task(_make_entity(content_md5="md5_a"))
        store.insert_task(_make_entity(content_md5="md5_b"))

        results = store.get_tasks_by_md5("md5_a")
        assert len(results) == 2
        assert all(r["content_md5"] == "md5_a" for r in results)
    finally:
        store.close()


# ---- 4. test_get_tasks_by_md5_no_match ----


def test_get_tasks_by_md5_no_match(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        results = store.get_tasks_by_md5("nonexistent")
        assert results == []
    finally:
        store.close()


# ---- 5. test_update_task_state ----


def test_update_task_state(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity())
        store.update_task_state(
            task_id,
            ProcessState.PREVIEW_FAILED,
            error_code="E001",
            error_message="timeout",
        )
        row = store.get_task_by_id(task_id)
        assert row["process_state"] == ProcessState.PREVIEW_FAILED.value
        assert row["last_error_code"] == "E001"
        assert row["last_error_message"] == "timeout"
    finally:
        store.close()


# ---- 6. test_increment_retry ----


def test_increment_retry(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity())
        assert store.get_task_by_id(task_id)["retry_count"] == 0
        store.increment_retry(task_id)
        assert store.get_task_by_id(task_id)["retry_count"] == 1
        store.increment_retry(task_id)
        assert store.get_task_by_id(task_id)["retry_count"] == 2
    finally:
        store.close()


# ---- 7. test_insert_and_get_preview ----


def test_insert_and_get_preview(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity())
        preview = PreviewInfo(
            strategy=PreviewStrategy.STATIC,
            path="/tmp/preview.jpg",
            format="jpg",
            width=256,
            height=256,
            size=5000,
            renderer="pillow",
            used_placeholder=False,
            fail_reason=None,
        )
        pid = store.insert_preview(task_id, preview)
        assert isinstance(pid, int)

        row = store.get_preview_by_task(task_id)
        assert row is not None
        assert row["strategy"] == PreviewStrategy.STATIC.value
        assert row["path"] == "/tmp/preview.jpg"
        assert row["width"] == 256
        assert row["height"] == 256
        assert row["size"] == 5000
        assert row["renderer"] == "pillow"
        assert row["used_placeholder"] == 0
        assert row["fail_reason"] is None
    finally:
        store.close()


# ---- 8. test_insert_and_get_description ----


def test_insert_and_get_description(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity())
        did = store.insert_description(
            task_id,
            main_content="A landscape photo",
            detail_content="Shows mountains at sunset",
            full_description="A landscape photo showing mountains at sunset with clouds",
            prompt_version="v2",
            quality_score=0.95,
        )
        assert isinstance(did, int)

        row = store.get_description_by_task(task_id)
        assert row is not None
        assert row["main_content"] == "A landscape photo"
        assert row["detail_content"] == "Shows mountains at sunset"
        assert row["full_description"] == "A landscape photo showing mountains at sunset with clouds"
        assert row["prompt_version"] == "v2"
        assert row["quality_score"] == 0.95
    finally:
        store.close()


# ---- 9. test_insert_and_get_embedding ----


def test_insert_and_get_embedding(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity())
        eid = store.insert_embedding(
            task_id,
            dimension=768,
            checksum="sha256abc",
            generate_time=1.23,
            model_version="clip-v2",
        )
        assert isinstance(eid, int)

        row = store.get_embedding_by_task(task_id)
        assert row is not None
        assert row["dimension"] == 768
        assert row["checksum"] == "sha256abc"
        assert abs(row["generate_time"] - 1.23) < 1e-6
        assert row["model_version"] == "clip-v2"
    finally:
        store.close()


# ---- 10. test_add_and_get_logs ----


def test_add_and_get_logs(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity())
        store.add_log(task_id, "start", "processing began")
        store.add_log(task_id, "preview_done", "generated thumbnail")
        store.add_log(task_id, "finish", "")

        logs = store.get_logs(task_id)
        assert len(logs) == 3
        assert logs[0]["event"] == "start"
        assert logs[1]["event"] == "preview_done"
        assert logs[2]["event"] == "finish"
        assert logs[0]["detail"] == "processing began"
    finally:
        store.close()


# ---- 11. test_get_tasks_by_state ----


def test_get_tasks_by_state(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        id1 = store.insert_task(_make_entity(content_md5="a"))
        id2 = store.insert_task(_make_entity(content_md5="b"))
        id3 = store.insert_task(_make_entity(content_md5="c"))

        store.update_task_state(id2, ProcessState.PREVIEW_READY)

        discovered = store.get_tasks_by_state(ProcessState.DISCOVERED)
        assert len(discovered) == 2
        assert {r["id"] for r in discovered} == {id1, id3}

        ready = store.get_tasks_by_state(ProcessState.PREVIEW_READY)
        assert len(ready) == 1
        assert ready[0]["id"] == id2
    finally:
        store.close()


# ---- 12. test_get_failed_tasks ----


def test_get_failed_tasks(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        id1 = store.insert_task(_make_entity(content_md5="a"))
        id2 = store.insert_task(_make_entity(content_md5="b"))
        id3 = store.insert_task(_make_entity(content_md5="c"))
        id4 = store.insert_task(_make_entity(content_md5="d"))

        store.update_task_state(id1, ProcessState.PREVIEW_FAILED, "E01", "err")
        store.update_task_state(id2, ProcessState.DESCRIPTION_FAILED, "E02", "err")
        store.update_task_state(id3, ProcessState.EMBEDDING_FAILED, "E03", "err")
        # id4 stays DISCOVERED

        failed = store.get_failed_tasks()
        assert len(failed) == 3
        assert {r["id"] for r in failed} == {id1, id2, id3}
    finally:
        store.close()


# ---- 13. test_db_persistence ----


def test_db_persistence(tmp_path):
    db_path = str(tmp_path / "persist.db")
    store = LocalCacheStore(db_path)
    task_id = store.insert_task(_make_entity(content_md5="persist_md5"))
    store.close()

    store2 = LocalCacheStore(db_path)
    try:
        row = store2.get_task_by_id(task_id)
        assert row is not None
        assert row["content_md5"] == "persist_md5"
    finally:
        store2.close()


# ---- 14. test_index_exists ----


def test_index_exists(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_resource_task_md5'"
        )
        result = cur.fetchone()
        conn.close()
        assert result is not None
        assert result[0] == "idx_resource_task_md5"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Multi-file / multi-preview tests
# ---------------------------------------------------------------------------


def test_insert_task_with_multiple_files(tmp_path):
    """insert_task persists all associated files via resource_file table."""
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        entity = ResourceProcessingEntity(
            resource_type="3d_model",
            source_directory="/models/chair",
            content_md5="composite_md5",
            files=[
                FileInfo("/models/chair/chair.fbx", "chair.fbx", 500_000, "fbx", "md5_a", "model", True),
                FileInfo("/models/chair/diffuse.png", "diffuse.png", 200_000, "png", "md5_b", "texture", False),
                FileInfo("/models/chair/normal.png", "normal.png", 100_000, "png", "md5_c", "texture", False),
            ],
        )
        task_id = store.insert_task(entity)

        row = store.get_task_by_id(task_id)
        assert row["source_directory"] == "/models/chair"
        assert row["content_md5"] == "composite_md5"

        files = store.get_files_by_task(task_id)
        assert len(files) == 3
        assert files[0]["is_primary"] == 1
        assert files[0]["file_name"] == "chair.fbx"
        names = {f["file_name"] for f in files}
        assert names == {"chair.fbx", "diffuse.png", "normal.png"}
    finally:
        store.close()


def test_insert_file_standalone(tmp_path):
    """insert_file adds a file to an existing task outside of insert_task."""
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        entity = _make_entity()
        task_id = store.insert_task(entity)
        extra = FileInfo("/tmp/extra.png", "extra.png", 777, "png", "md5_extra", "attachment", False)
        fid = store.insert_file(task_id, extra)
        assert isinstance(fid, int)

        files = store.get_files_by_task(task_id)
        assert len(files) == 2
        assert any(f["file_name"] == "extra.png" for f in files)
    finally:
        store.close()


def test_update_file_ks3_key(tmp_path):
    """update_file_ks3_key sets the storage key on a specific file."""
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        entity = _make_entity()
        task_id = store.insert_task(entity)
        files = store.get_files_by_task(task_id)
        assert files[0]["ks3_key"] is None

        store.update_file_ks3_key(files[0]["id"], "ks3://bucket/key.png")
        updated = store.get_files_by_task(task_id)
        assert updated[0]["ks3_key"] == "ks3://bucket/key.png"
    finally:
        store.close()


def test_insert_multiple_previews(tmp_path):
    """Multiple previews can be inserted and queried per task."""
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity())
        p1 = PreviewInfo(strategy=PreviewStrategy.GIF, role="primary",
                         path="/p/model.gif", format="gif", width=512, height=512,
                         size=30000, renderer="blender")
        p2 = PreviewInfo(strategy=PreviewStrategy.STATIC, role="gallery",
                         path="/p/tex.webp", format="webp", width=256, height=256,
                         size=5000, renderer="pillow")
        store.insert_preview(task_id, p1)
        store.insert_preview(task_id, p2)

        all_previews = store.get_previews_by_task(task_id)
        assert len(all_previews) == 2
        roles = {p["role"] for p in all_previews}
        assert roles == {"primary", "gallery"}

        primary = store.get_preview_by_task(task_id)
        assert primary is not None
        assert primary["role"] == "primary"
    finally:
        store.close()


def test_get_preview_by_task_returns_primary_only(tmp_path):
    """get_preview_by_task returns only the primary preview, not gallery."""
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity())
        gallery = PreviewInfo(strategy=PreviewStrategy.STATIC, role="gallery",
                              path="/p/tex.webp", format="webp", width=100, height=100,
                              size=1000, renderer="pillow")
        store.insert_preview(task_id, gallery)

        result = store.get_preview_by_task(task_id)
        assert result is None

        primary = PreviewInfo(strategy=PreviewStrategy.GIF, role="primary",
                              path="/p/model.gif", format="gif", width=512, height=512,
                              size=30000, renderer="blender")
        store.insert_preview(task_id, primary)

        result = store.get_preview_by_task(task_id)
        assert result is not None
        assert result["role"] == "primary"
    finally:
        store.close()


def test_resource_file_index_exists(tmp_path):
    """The idx_resource_file_md5 index should be created."""
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_resource_file_md5'"
        )
        result = cur.fetchone()
        conn.close()
        assert result is not None
    finally:
        store.close()


def test_task_with_no_files(tmp_path):
    """A task with no files should still be insertable."""
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        entity = ResourceProcessingEntity(
            resource_type="other",
            source_directory="/empty",
            content_md5="empty_md5",
        )
        task_id = store.insert_task(entity)
        files = store.get_files_by_task(task_id)
        assert files == []
    finally:
        store.close()
