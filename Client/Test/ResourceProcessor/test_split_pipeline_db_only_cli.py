"""DB-only contract tests for split pipeline CLI entry points."""

import sys

from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import (
    FileInfo,
    PreviewInfo,
    PreviewStrategy,
    ProcessState,
    ResourceProcessingEntity,
)


def _make_entity(tmp_path, **overrides) -> ResourceProcessingEntity:
    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"asset")
    defaults = dict(
        content_md5="asset-md5",
        resource_type="single_image",
        source_directory=str(tmp_path),
        source="test-source",
        title="Asset",
        source_resource_id="src-1",
        files=[
            FileInfo(
                file_path=str(source_file),
                file_name=source_file.name,
                file_size=source_file.stat().st_size,
                file_format="png",
                content_md5="file-md5",
                is_primary=True,
            )
        ],
    )
    defaults.update(overrides)
    return ResourceProcessingEntity(**defaults)


def test_generate_previews_reads_only_pipeline_db(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_previews

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
    finally:
        store.close()

    async def fake_generate_previews(self, entity):
        preview_path = self.output_dir / "preview.webp"
        preview_path.write_bytes(b"preview")
        return [
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(preview_path),
                format="webp",
                width=1,
                height=1,
                size=preview_path.stat().st_size,
            )
        ]

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy.CrawlerThumbnailPolicy.generate_previews",
        fake_generate_previews,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_previews",
            "--db-path",
            str(db_path),
            "--work-dir",
            str(tmp_path / "work"),
            "--preview-mode",
            "local",
        ],
    )

    assert generate_previews.main() == 0
    out = capsys.readouterr().out
    assert "Crawler DB" not in out

    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(task_id)["process_state"] == ProcessState.PREVIEW_READY.value
        assert len(store.get_previews_by_task(task_id)) == 1
    finally:
        store.close()


def test_generate_previews_default_renderer_mode_writes_db_and_skips_ready(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_previews

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        pending_task_id = store.insert_task(
            _make_entity(tmp_path, source_resource_id="asset-pending", content_md5="asset-pending-md5")
        )
        ready_task_id = store.insert_task(
            _make_entity(tmp_path, source_resource_id="asset-ready", content_md5="asset-ready-md5")
        )
        store.upsert_object_manifest(
            pending_task_id,
            {
                "client_resource_id": "asset-pending",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "resource-crawler/files/asset-pending/source.png",
                    "file_name": "source.png",
                },
                "source_files": [{"file_name": "source.png", "file_format": "png", "is_primary": True}],
                "client_metadata": {},
            },
        )
        store.upsert_object_manifest(
            ready_task_id,
            {
                "client_resource_id": "asset-ready",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "resource-crawler/files/asset-ready/source.png",
                    "file_name": "source.png",
                },
                "source_files": [{"file_name": "source.png", "file_format": "png", "is_primary": True}],
                "client_metadata": {},
            },
        )
        store.update_task_state(ready_task_id, ProcessState.PREVIEW_READY)
    finally:
        store.close()

    calls = []

    def fake_remote_renderer(manifest, *, preview_renderer, client_id, previews_dir, api_key="", session=None):
        calls.append((manifest["client_resource_id"], preview_renderer, client_id, session is not None))
        preview_path = tmp_path / "work" / "previews" / "single_image" / f"{manifest['client_resource_id']}_primary.webp"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(b"remote-preview")
        return [
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(preview_path),
                format="webp",
                width=2,
                height=2,
                size=preview_path.stat().st_size,
                renderer="preview-renderer",
            )
        ]

    monkeypatch.setattr(generate_previews, "_render_previews_with_remote_renderer", fake_remote_renderer)
    monkeypatch.setenv("PREVIEW_RENDERER_URL", "http://renderer-from-env")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_previews",
            "--db-path",
            str(db_path),
            "--work-dir",
            str(tmp_path / "work"),
            "--client-id",
            "resource-crawler",
        ],
    )

    assert generate_previews.main() == 0
    capsys.readouterr()

    assert calls == [("asset-pending", "http://renderer-from-env", "resource-crawler", True)]

    store = LocalCacheStore(str(db_path))
    try:
        previews = store.get_previews_by_task(pending_task_id)
        assert len(previews) == 1
        assert previews[0]["renderer"] == "preview-renderer"
        assert previews[0]["path"].endswith("asset-pending_primary.webp")
        assert store.get_task_by_id(pending_task_id)["process_state"] == ProcessState.PREVIEW_READY.value
        assert store.get_previews_by_task(ready_task_id) == []
    finally:
        store.close()


def test_generate_previews_default_work_dir_uses_repo_data(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_previews

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(
            _make_entity(tmp_path, source_resource_id="asset-default-dir", content_md5="asset-default-dir-md5")
        )
        store.upsert_object_manifest(
            task_id,
            {
                "client_resource_id": "asset-default-dir",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "resource-crawler/files/asset-default-dir/source.png",
                    "file_name": "source.png",
                },
                "source_files": [{"file_name": "source.png", "file_format": "png", "is_primary": True}],
                "client_metadata": {},
            },
        )
    finally:
        store.close()

    seen_previews_dir = {}

    def fake_remote_renderer(manifest, *, preview_renderer, client_id, previews_dir, api_key="", session=None):
        seen_previews_dir["value"] = previews_dir
        preview_path = tmp_path / "preview.webp"
        preview_path.write_bytes(b"preview")
        return [
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(preview_path),
                format="webp",
                width=1,
                height=1,
                size=preview_path.stat().st_size,
                renderer="preview-renderer",
            )
        ]

    monkeypatch.setattr(generate_previews, "_render_previews_with_remote_renderer", fake_remote_renderer)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_previews",
            "--db-path",
            str(db_path),
        ],
    )

    assert generate_previews.main() == 0
    capsys.readouterr()

    expected = str((generate_previews.Path(__file__).resolve().parents[3] / "data" / "previews").resolve())
    assert seen_previews_dir["value"] == expected


def test_force_preview_supports_task_id_filter_and_clears_old_rows(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_previews

    db_path = tmp_path / "pipeline.db"
    old_preview_1 = tmp_path / "old_1.webp"
    stale_preview_2 = tmp_path / "old_2.webp"
    reused_preview_2 = tmp_path / "work" / "previews" / "asset-md5-2.webp"
    reused_preview_2.parent.mkdir(parents=True)
    old_preview_1.write_bytes(b"old-1")
    stale_preview_2.write_bytes(b"old-2")
    reused_preview_2.write_bytes(b"old-reused")

    store = LocalCacheStore(str(db_path))
    try:
        task_1 = store.insert_task(_make_entity(tmp_path, content_md5="asset-md5-1", source_resource_id="src-1"))
        task_2 = store.insert_task(_make_entity(tmp_path, content_md5="asset-md5-2", source_resource_id="src-2"))
        for task_id, preview_path in ((task_1, old_preview_1), (task_2, stale_preview_2), (task_2, reused_preview_2)):
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
            store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
    finally:
        store.close()

    calls = []

    async def fake_generate_previews(self, entity):
        calls.append(entity.content_md5)
        preview_path = self.output_dir / f"{entity.content_md5}.webp"
        preview_path.write_bytes(b"new")
        return [
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(preview_path),
                format="webp",
                width=1,
                height=1,
                size=preview_path.stat().st_size,
            )
        ]

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy.CrawlerThumbnailPolicy.generate_previews",
        fake_generate_previews,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_previews",
            "--db-path",
            str(db_path),
            "--work-dir",
            str(tmp_path / "work"),
            "--preview-mode",
            "local",
            "--task-id",
            str(task_2),
            "--force",
        ],
    )

    assert generate_previews.main() == 0
    capsys.readouterr()

    assert calls == ["asset-md5-2"]

    store = LocalCacheStore(str(db_path))
    try:
        previews_1 = store.get_previews_by_task(task_1)
        previews_2 = store.get_previews_by_task(task_2)
        assert [row["path"] for row in previews_1] == [str(old_preview_1)]
        assert len(previews_2) == 1
        assert previews_2[0]["path"].endswith("asset-md5-2.webp")
        assert store.get_task_by_id(task_2)["process_state"] == ProcessState.PREVIEW_READY.value
        assert old_preview_1.exists()
        assert not stale_preview_2.exists()
        assert reused_preview_2.exists()
        assert reused_preview_2.read_bytes() == b"new"
    finally:
        store.close()


def test_force_preview_on_committed_resets_to_preview_ready(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_previews

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path, content_md5="committed-preview-md5"))
        store.update_task_state(task_id, ProcessState.COMMITTED)
    finally:
        store.close()

    async def fake_generate_previews(self, entity):
        preview_path = self.output_dir / "committed-preview.webp"
        preview_path.write_bytes(b"preview")
        return [
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(preview_path),
                format="webp",
                width=1,
                height=1,
                size=preview_path.stat().st_size,
            )
        ]

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy.CrawlerThumbnailPolicy.generate_previews",
        fake_generate_previews,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_previews",
            "--db-path",
            str(db_path),
            "--work-dir",
            str(tmp_path / "work"),
            "--preview-mode",
            "local",
            "--task-id",
            str(task_id),
            "--force",
        ],
    )

    assert generate_previews.main() == 0
    capsys.readouterr()

    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(task_id)["process_state"] == ProcessState.PREVIEW_READY.value
    finally:
        store.close()


def test_force_preview_pack_is_skipped(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_previews

    db_path = tmp_path / "pipeline.db"
    child_preview_path = tmp_path / "child_preview.webp"
    child_preview_path.write_bytes(b"preview")
    old_pack_preview_path = tmp_path / "old_pack_preview.webp"
    old_pack_preview_path.write_bytes(b"old preview")

    store = LocalCacheStore(str(db_path))
    try:
        pack_id = store.insert_task(
            _make_entity(
                tmp_path,
                content_md5="pack-md5",
                resource_type="pack",
                title="Pack",
                pack_name="Pack",
                resource_path="__pack__",
                source_resource_id="pack-src",
                child_resource_count=1,
                contains_resource_types=["single_image"],
                files=[],
            )
        )
        child_id = store.insert_task(
            _make_entity(
                tmp_path,
                content_md5="child-md5",
                resource_type="single_image",
                title="child.png",
                pack_name="Pack",
                resource_path="child.png",
                source_resource_id="child-src",
                parent_resource_id="pack-src",
            )
        )
        store.insert_preview(
            child_id,
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(child_preview_path),
                format="webp",
                width=1,
                height=1,
                size=child_preview_path.stat().st_size,
            ),
        )
        store.insert_preview(
            pack_id,
            PreviewInfo(
                strategy=PreviewStrategy.CONTACT_SHEET,
                path=str(old_pack_preview_path),
                format="webp",
                width=1,
                height=1,
                size=old_pack_preview_path.stat().st_size,
            ),
        )
        store.update_task_state(pack_id, ProcessState.DESCRIPTION_READY)
        store.update_task_state(child_id, ProcessState.DESCRIPTION_READY)
    finally:
        store.close()

    calls = []

    async def fake_generate_previews(self, entity):
        calls.append(entity.resource_type)
        raise AssertionError("pack previews should be skipped")

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy.CrawlerThumbnailPolicy.generate_previews",
        fake_generate_previews,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_previews",
            "--db-path",
            str(db_path),
            "--work-dir",
            str(tmp_path / "work"),
            "--preview-mode",
            "local",
            "--resource-type",
            "pack",
            "--force",
        ],
    )

    assert generate_previews.main() == 0
    capsys.readouterr()

    assert calls == []

    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(pack_id)["process_state"] == ProcessState.DESCRIPTION_READY.value
        pack_previews = store.get_previews_by_task(pack_id)
        assert len(pack_previews) == 1
        assert pack_previews[0]["path"] == str(old_pack_preview_path)
        assert len(store.get_previews_by_task(child_id)) == 1
        assert old_pack_preview_path.exists()
        assert child_preview_path.exists()
    finally:
        store.close()


def test_generate_descriptions_reads_only_pipeline_db(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_descriptions

    db_path = tmp_path / "pipeline.db"
    preview_path = tmp_path / "preview.webp"
    preview_path.write_bytes(b"preview")

    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
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
        store.update_task_state(task_id, ProcessState.PREVIEW_READY)
    finally:
        store.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_descriptions",
            "--db-path",
            str(db_path),
            "--llm-provider",
            "mock",
            "--concurrency",
            "1",
        ],
    )

    assert generate_descriptions.main() == 0
    out = capsys.readouterr().out
    assert "Crawler DB" not in out

    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(task_id)["process_state"] == ProcessState.DESCRIPTION_READY.value
        desc = store.get_description_by_task(task_id)
        assert desc is not None
        assert desc["full_description"]
        assert desc["usage_category"] == ""
    finally:
        store.close()


def test_generate_descriptions_resume_skips_existing_description(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_descriptions

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        store.insert_description(
            task_id,
            main_content="old main",
            detail_content="old detail",
            full_description="主体：old main\n细节：old detail",
            prompt_version="old",
        )
        store.update_task_state(task_id, ProcessState.PREVIEW_READY)
    finally:
        store.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_descriptions",
            "--db-path",
            str(db_path),
            "--llm-provider",
            "missing_provider",
            "--concurrency",
            "1",
            "--resume",
        ],
    )

    assert generate_descriptions.main() == 0
    out = capsys.readouterr().out
    assert "跳过(已有描述) 1" in out

    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(task_id)["process_state"] == ProcessState.DESCRIPTION_READY.value
        desc_rows = store._conn.execute(
            "SELECT COUNT(*) AS cnt FROM resource_description WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert desc_rows["cnt"] == 1
        assert store.get_description_by_task(task_id)["prompt_version"] == "old"
    finally:
        store.close()


def test_generate_descriptions_keeps_existing_description_on_regeneration_failure(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_descriptions

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        store.insert_description(
            task_id,
            main_content="old main",
            detail_content="old detail",
            full_description="主体：old main\n细节：old detail",
            prompt_version="old",
        )
        store.update_task_state(task_id, ProcessState.PREVIEW_READY)
    finally:
        store.close()

    async def fail_generate(*args, **kwargs):
        raise RuntimeError("upstream timeout")

    monkeypatch.setattr(generate_descriptions, "_generate_with_retry", fail_generate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_descriptions",
            "--db-path",
            str(db_path),
            "--llm-provider",
            "mock",
            "--concurrency",
            "1",
        ],
    )

    assert generate_descriptions.main() == 0
    out = capsys.readouterr().out
    assert "沿用已有描述 1" in out
    assert "描述保留 [Asset]" in out

    store = LocalCacheStore(str(db_path))
    try:
        task = store.get_task_by_id(task_id)
        assert task["process_state"] == ProcessState.DESCRIPTION_READY.value
        assert task["last_error_code"] == ""
        assert store.get_description_by_task(task_id)["prompt_version"] == "old"
    finally:
        store.close()


def test_generate_descriptions_force_regenerates_only_requested_resource_type(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_descriptions

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        pack_id = store.insert_task(
            _make_entity(
                tmp_path,
                content_md5="pack-md5",
                resource_type="pack",
                title="Pack",
                source_resource_id="pack-src",
                child_resource_count=0,
                files=[],
            )
        )
        image_id = store.insert_task(
            _make_entity(
                tmp_path,
                content_md5="image-md5",
                resource_type="single_image",
                title="Image",
                source_resource_id="image-src",
            )
        )
        for task_id in (pack_id, image_id):
            store.insert_description(
                task_id,
                main_content="old main",
                detail_content="old detail",
                full_description="主体：old main\n细节：old detail",
                prompt_version="old",
            )
            store.update_task_state(task_id, ProcessState.COMMITTED)
    finally:
        store.close()

    calls = []

    async def fake_generate(cache, task_id, entity, *args, **kwargs):
        calls.append((task_id, entity.resource_type))
        cache.insert_description(
            task_id,
            main_content=f"new {entity.resource_type}",
            detail_content="new detail",
            full_description=f"主体：new {entity.resource_type}\n细节：new detail",
            prompt_version="force-test",
        )
        return True

    monkeypatch.setattr(generate_descriptions, "_generate_with_retry", fake_generate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_descriptions",
            "--db-path",
            str(db_path),
            "--llm-provider",
            "mock",
            "--concurrency",
            "1",
            "--resource-type",
            "pack",
            "--force",
        ],
    )

    assert generate_descriptions.main() == 0
    out = capsys.readouterr().out
    assert "强制刷新:       pack" in out
    assert calls == []

    store = LocalCacheStore(str(db_path))
    try:
        pack_desc_count = store._conn.execute(
            "SELECT COUNT(*) AS cnt FROM resource_description WHERE task_id = ?",
            (pack_id,),
        ).fetchone()["cnt"]
        image_desc_count = store._conn.execute(
            "SELECT COUNT(*) AS cnt FROM resource_description WHERE task_id = ?",
            (image_id,),
        ).fetchone()["cnt"]
        assert pack_desc_count == 1
        assert image_desc_count == 1
        assert store.get_description_by_task(pack_id)["prompt_version"] == "old"
        assert store.get_description_by_task(image_id)["prompt_version"] == "old"
        assert store.get_task_by_id(pack_id)["process_state"] == ProcessState.COMMITTED.value
        assert store.get_task_by_id(image_id)["process_state"] == ProcessState.COMMITTED.value
    finally:
        store.close()


def test_generate_descriptions_ctrl_c_returns_clean_interrupt(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import generate_descriptions

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    store.close()

    def raise_keyboard_interrupt(_coro):
        raise KeyboardInterrupt

    monkeypatch.setattr(generate_descriptions.asyncio, "run", raise_keyboard_interrupt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_descriptions",
            "--db-path",
            str(db_path),
            "--llm-provider",
            "mock",
            "--concurrency",
            "1",
        ],
    )

    assert generate_descriptions.main() == 130
    out = capsys.readouterr().out
    assert "用户中断" in out
    assert "中断时状态统计" in out
    assert "Traceback" not in out


def test_description_retry_classifies_transient_errors():
    import requests

    from ResourceProcessor.generate_descriptions import _is_retryable_error

    assert _is_retryable_error(requests.exceptions.ReadTimeout("read timed out"))
    assert _is_retryable_error(
        RuntimeError("HTTPSConnectionPool(host='kspmas.ksyun.com', port=443): Read timed out. (read timeout=60)")
    )
    assert _is_retryable_error(
        RuntimeError("HTTPSConnectionPool(host='kspmas.ksyun.com', port=443): Max retries exceeded with url")
    )
    assert _is_retryable_error(
        RuntimeError(
            'Ksyun chat.completions 调用失败: code=502, body=<!DOCTYPE HTML>'
            '<title>502 Bad Gateway</title>Powered by CLOUD ELB 1.0.0'
        )
    )
    assert _is_retryable_error(
        RuntimeError("Ksyun chat.completions 调用失败: code=503, body=Service Unavailable")
    )
    assert _is_retryable_error(
        RuntimeError("Ksyun chat.completions 调用失败: code=504, body=Gateway Timeout")
    )
    assert not _is_retryable_error(
        RuntimeError("Ksyun chat.completions 调用失败: code=413, body=Payload Too Large")
    )
    assert not _is_retryable_error(
        RuntimeError("Ksyun chat.completions 调用失败: code=400, body={\"error\":\"Bad Request\"}")
    )


def test_description_progress_label_uses_streaming_counts():
    from ResourceProcessor.generate_descriptions import _description_progress_label

    label = _description_progress_label(
        {
            "processed": 1500,
            "enqueued": 1532,
            "desc_ok": 1179,
            "failed": 321,
            "feeder_done": False,
            "skipped_existing": 2,
            "skipped_audio": 3,
            "skipped_rebuild": 4,
        }
    )

    assert "已处理 1500" in label
    assert "已入队 1532" in label
    assert "扫描中" in label
    assert "1500/1500" not in label


def test_classify_resources_reads_only_pipeline_db(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import classify_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        store.insert_description(
            task_id,
            main_content="像素风钥匙道具",
            detail_content="适合作为游戏中的可拾取钥匙。",
            full_description="主体：像素风钥匙道具\n细节：适合作为游戏中的可拾取钥匙。",
            prompt_version="test_desc",
            quality_score=0.9,
        )
        store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
    finally:
        store.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "classify_resources",
            "--db-path",
            str(db_path),
            "--llm-provider",
            "mock",
            "--concurrency",
            "1",
        ],
    )

    assert classify_resources.main() == 0
    out = capsys.readouterr().out
    assert "Crawler DB" not in out

    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(task_id)["process_state"] == ProcessState.CLASSIFY_READY.value
        desc = store.get_description_by_task(task_id)
        assert desc is not None
        assert desc["main_content"] == "像素风钥匙道具"
        assert desc["usage_category"] == "物件"
        assert desc["usage_subcategories"] == '["道具"]'
    finally:
        store.close()


def test_upload_resources_dry_run_reads_only_pipeline_db(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        store.insert_description(
            task_id,
            main_content="main",
            detail_content="detail",
            full_description="full",
            prompt_version="test",
        )
        store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
        classified_task_id = store.insert_task(
            _make_entity(tmp_path, content_md5="classified-md5", source_resource_id="src-classified")
        )
        store.insert_description(
            classified_task_id,
            main_content="classified main",
            detail_content="classified detail",
            full_description="classified full",
            prompt_version="test",
            usage_space="2D",
            usage_category="物件",
            usage_subcategories=["道具"],
        )
        store.update_task_state(classified_task_id, ProcessState.CLASSIFY_READY)
    finally:
        store.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upload_resources",
            "--db-path",
            str(db_path),
            "--dry-run",
        ],
    )

    assert upload_resources.main() == 0
    out = capsys.readouterr().out
    assert "Crawler DB" not in out
    assert "生成 manifest 2" in out
