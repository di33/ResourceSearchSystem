"""Tests for ResourceProcessor.local_cache – SQLite local cache module."""

import sqlite3

from ResourceProcessor.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import (
    PreviewInfo,
    PreviewStrategy,
    ProcessState,
    ResourceProcessingEntity,
)


def _make_entity(**overrides) -> ResourceProcessingEntity:
    defaults = dict(
        content_md5="abc123",
        resource_type="image",
        source_path="/tmp/img.png",
        source_name="img.png",
        source_size=1024,
        source_format="png",
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
        assert row["source_path"] == "/tmp/img.png"
        assert row["source_name"] == "img.png"
        assert row["source_size"] == 1024
        assert row["source_format"] == "png"
        assert row["process_state"] == ProcessState.DISCOVERED.value
        assert row["resource_id"] == "rid-1"
        assert row["retry_count"] == 0
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
