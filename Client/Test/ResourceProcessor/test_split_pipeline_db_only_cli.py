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


def test_pack_overview_detection_ignores_regular_sheet_suffix():
    from ResourceProcessor.generate_previews import _is_overview_single_image

    assert not _is_overview_single_image(
        {
            "resource_type": "single_image",
            "resource_path": "icons-master/lorc/flaming-sheet.svg",
            "title": "flaming-sheet.svg",
        },
        [{"file_name": "flaming-sheet.svg"}],
        single_image_count=128,
    )
    assert _is_overview_single_image(
        {
            "resource_type": "single_image",
            "resource_path": "preview/character.png",
            "title": "character.png",
        },
        [{"file_name": "character.png"}],
        single_image_count=128,
    )
    assert _is_overview_single_image(
        {
            "resource_type": "single_image",
            "resource_path": "spritesheet.png",
            "title": "spritesheet.png",
        },
        [{"file_name": "spritesheet.png"}],
        single_image_count=128,
    )


def test_force_refresh_supports_task_id_filter_and_clears_old_rows(monkeypatch, tmp_path, capsys):
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
        assert store.get_task_by_id(task_2)["process_state"] == ProcessState.DESCRIPTION_READY.value
        assert old_preview_1.exists()
        assert not stale_preview_2.exists()
        assert reused_preview_2.exists()
        assert reused_preview_2.read_bytes() == b"new"
    finally:
        store.close()


def test_force_refresh_pack_clears_old_rows_and_preserves_later_state(monkeypatch, tmp_path, capsys):
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
        calls.append((entity.resource_type, list(entity.auxiliary_metadata.get("child_previews", []))))
        preview_path = self.output_dir / "pack_refresh.webp"
        preview_path.write_bytes(b"preview")
        return [
            PreviewInfo(
                strategy=PreviewStrategy.CONTACT_SHEET,
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
            "--resource-type",
            "pack",
            "--force",
        ],
    )

    assert generate_previews.main() == 0
    capsys.readouterr()

    assert len(calls) == 1
    assert calls[0][0] == "pack"
    assert len(calls[0][1]) == 1

    store = LocalCacheStore(str(db_path))
    try:
        assert store.get_task_by_id(pack_id)["process_state"] == ProcessState.DESCRIPTION_READY.value
        pack_previews = store.get_previews_by_task(pack_id)
        assert len(pack_previews) == 1
        assert pack_previews[0]["path"].endswith("pack_refresh.webp")
        assert len(store.get_previews_by_task(child_id)) == 1
        assert not old_pack_preview_path.exists()
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


def test_generate_descriptions_keeps_existing_description_on_refresh_failure(monkeypatch, tmp_path, capsys):
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
    assert not _is_retryable_error(
        RuntimeError("Ksyun chat.completions 调用失败: code=413, body=Payload Too Large")
    )
    assert not _is_retryable_error(
        RuntimeError("Ksyun chat.completions 调用失败: code=400, body={\"error\":\"Bad Request\"}")
    )


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
    import requests
    from ResourceProcessor import upload_resources

    class _FakeResponse:
        def json(self):
            return {"status": "ok"}

    class _FakeSession:
        def get(self, url, timeout):
            return _FakeResponse()

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

    monkeypatch.setattr(requests, "Session", lambda: _FakeSession())
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
    assert "可上传 2 个资源" in out
