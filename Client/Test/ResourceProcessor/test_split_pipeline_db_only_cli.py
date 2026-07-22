"""DB-only contract tests for split pipeline CLI entry points."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import (
    FileInfo,
    PreviewInfo,
    PreviewStrategy,
    ProcessState,
    ResourceProcessingEntity,
)
from resource_contracts.resource_types import PACK_RESOURCE_TYPE


class _ManifestRef:
    def __init__(self, payload: dict):
        self._payload = payload

    def to_manifest_dict(self) -> dict:
        return dict(self._payload)


class _FakeObjectUploader:
    def __init__(self):
        self.profile = type("Profile", (), {"profile_id": "default"})()
        self.calls = []
        self.deleted_keys = []

    def upload_file(self, file_path, *, object_key, is_primary=False, content_type=""):
        path = Path(file_path)
        self.calls.append({"file_path": str(path), "object_key": object_key, "is_primary": is_primary})
        return _ManifestRef(
            {
                "storage_profile_id": "default",
                "object_key": object_key,
                "file_name": path.name,
                "file_format": path.suffix.lower().lstrip("."),
                "size": path.stat().st_size,
                "checksum": "md5",
                "etag": "etag",
                "is_primary": is_primary,
            }
        )

    def delete_objects(self, object_keys):
        keys = list(object_keys)
        self.deleted_keys.extend(keys)
        return len(keys)


def _install_fake_object_uploader(monkeypatch):
    fake = _FakeObjectUploader()
    monkeypatch.setattr("ObjectStorageUpload.resource_manifest._thread_uploader", lambda storage_profile_id: fake)
    monkeypatch.setattr("ObjectStorageUpload.resource_manifest.ObjectStorageUploader", lambda *args, **kwargs: fake)
    return fake


def _preview_ref(client_resource_id: str = "src-1") -> dict:
    return {
        "storage_profile_id": "default",
        "object_key": f"client/previews/{client_resource_id}/primary.webp",
        "role": "primary",
        "file_name": "primary.webp",
        "file_format": "webp",
        "width": 32,
        "height": 16,
    }


def _with_preview(manifest: dict, client_resource_id: str | None = None) -> dict:
    result = dict(manifest)
    resource_id = client_resource_id or str(result.get("client_resource_id") or "src-1")
    result["previews"] = [_preview_ref(resource_id)]
    return result


def _mark_description_ready(store: LocalCacheStore, task_id: int, *, main: str = "main") -> None:
    store.insert_description(
        task_id,
        main_content=main,
        detail_content="detail",
        full_description="full",
        prompt_version="test",
    )
    store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)


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
        store.upsert_object_manifest(
            task_id,
            {
                "client_resource_id": "src-1",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "client/files/src-1/asset.png",
                    "file_name": "asset.png",
                    "file_format": "png",
                },
            },
        )
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
    fake_uploader = _install_fake_object_uploader(monkeypatch)
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
        manifest = store.get_object_manifest(task_id)["manifest"]
        assert manifest["previews"][0]["object_key"] == "resource-crawler/previews/src-1/primary.webp"
        assert [call["object_key"] for call in fake_uploader.calls] == ["resource-crawler/previews/src-1/primary.webp"]
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
    _install_fake_object_uploader(monkeypatch)
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
        assert store.get_object_manifest(pending_task_id)["manifest"]["previews"][0]["object_key"] == (
            "resource-crawler/previews/asset-pending/primary.webp"
        )
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
    _install_fake_object_uploader(monkeypatch)
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
        for task_id, source_id in ((task_1, "src-1"), (task_2, "src-2")):
            store.upsert_object_manifest(
                task_id,
                {
                    "client_resource_id": source_id,
                    "resource_type": "single_image",
                    "source_object": {
                        "storage_profile_id": "default",
                        "object_key": f"client/files/{source_id}/asset.png",
                        "file_name": "asset.png",
                        "file_format": "png",
                    },
                },
            )
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
    _install_fake_object_uploader(monkeypatch)
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
        store.upsert_object_manifest(
            task_id,
            {
                "client_resource_id": "src-1",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "client/files/src-1/asset.png",
                    "file_name": "asset.png",
                    "file_format": "png",
                },
            },
        )
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
    _install_fake_object_uploader(monkeypatch)
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


def test_generate_descriptions_does_not_keep_existing_description_below_description_ready(monkeypatch, tmp_path, capsys):
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
        raise RuntimeError("invalid generated description")

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

    assert generate_descriptions.main() == 1
    out = capsys.readouterr().out
    assert "描述保留 [Asset]" not in out

    store = LocalCacheStore(str(db_path))
    try:
        task = store.get_task_by_id(task_id)
        assert task["process_state"] == ProcessState.DESCRIPTION_FAILED.value
        assert task["last_error_code"] == "desc_error"
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
        store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
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
            "--resource-type",
            "single_image",
            "--force",
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


def test_description_refusal_retries_twice_and_uses_specific_error_code(monkeypatch):
    from types import SimpleNamespace

    from ResourceProcessor.description.ksyun_llm_provider import DescriptionRefusalResponse
    from ResourceProcessor.generate_descriptions import _generate_with_retry, _process_one

    calls = 0

    async def refuse(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise DescriptionRefusalResponse(
            "description response was refused by the model safety policy",
            raw_response="The request was rejected because it was considered high risk",
        )

    monkeypatch.setattr(
        "ResourceProcessor.description.description_generator.generate_resource_description_text",
        refuse,
    )
    monkeypatch.setattr(
        "ResourceProcessor.description.pack_description_builder.build_description_input_for_generation",
        lambda *args, **kwargs: asyncio.sleep(0, result=SimpleNamespace()),
    )
    monkeypatch.setattr("ResourceProcessor.generate_descriptions.random.uniform", lambda *_: 0.0)
    monkeypatch.setattr("ResourceProcessor.generate_descriptions._log_invalid_description_response", lambda *args: None)

    class FakeCache:
        state = None
        error_code = ""

        def get_task_by_id(self, task_id):
            return {"process_state": ProcessState.PREVIEW_READY.value}

        def get_description_by_task(self, task_id):
            return None

        def update_task_state(self, task_id, state, error_code="", error_message=""):
            self.state = state
            self.error_code = error_code

    class FakeReport:
        def ok(self, *args, **kwargs):
            pass

        def fail(self, *args, **kwargs):
            pass

    cache = FakeCache()
    entity = SimpleNamespace(
        title="safe.png",
        resource_path="safe.png",
        content_md5="abc",
        resource_type="single_image",
    )

    async def run_scenario():
        async def generate(*args, **kwargs):
            return await _generate_with_retry(
                cache,
                1,
                entity,
                "ksyun",
                FakeReport(),
                max_attempts=6,
                base_delay_seconds=0,
            )

        monkeypatch.setattr("ResourceProcessor.generate_descriptions._generate_with_retry", generate)
        await _process_one(
            1,
            entity,
            "ksyun",
            "",
            cache,
            FakeReport(),
            asyncio.Semaphore(1),
            {"processed": 0, "desc_ok": 0, "failed": 0},
        )

    asyncio.run(run_scenario())

    assert calls == 2
    assert cache.state == ProcessState.DESCRIPTION_FAILED
    assert cache.error_code == "description_refusal"


def test_description_retry_candidates_ignore_persisted_retry_count():
    from ResourceProcessor.generate_descriptions import _get_retry_candidates

    class FakeCache:
        def get_failed_tasks(self):
            return [
                {"id": 1, "process_state": ProcessState.DESCRIPTION_FAILED.value, "retry_count": 99},
                {"id": 2, "process_state": ProcessState.PREVIEW_FAILED.value, "retry_count": 0},
            ]

    candidates = _get_retry_candidates(FakeCache())

    assert [item["id"] for item in candidates] == [1]


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
    source = tmp_path / "asset.png"
    source.write_bytes(b"asset")
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.upsert_object_manifest(
            task_id,
            _with_preview({
                "client_resource_id": "src-1",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "client/files/src-1/asset.png",
                    "file_name": "asset.png",
                    "file_format": "png",
                },
            }),
            resource_fingerprint=fingerprint,
        )
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
        classified_fingerprint = store.get_task_by_id(classified_task_id)["resource_fingerprint"]
        store.upsert_object_manifest(
            classified_task_id,
            _with_preview({
                "client_resource_id": "src-classified",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "client/files/src-classified/asset.png",
                    "file_name": "asset.png",
                    "file_format": "png",
                },
            }, "src-classified"),
            resource_fingerprint=classified_fingerprint,
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
        store.upsert_object_manifest(
            task_id,
            store.get_object_manifest(task_id)["manifest"],
            resource_fingerprint=store.get_task_by_id(task_id)["resource_fingerprint"],
        )
        store.upsert_object_manifest(
            classified_task_id,
            store.get_object_manifest(classified_task_id)["manifest"],
            resource_fingerprint=store.get_task_by_id(classified_task_id)["resource_fingerprint"],
        )
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


def test_upload_resources_dry_run_uses_source_file_as_source_object(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.upsert_object_manifest(
            task_id,
            _with_preview({
                "client_resource_id": "src-1",
                "resource_type": "single_image",
                "source_object": {},
                "source_files": [
                    {
                        "storage_profile_id": "default",
                        "object_key": "client/files/src-1/asset.png",
                        "file_name": "asset.png",
                        "file_format": "png",
                        "size": 5,
                        "checksum": "md5",
                        "is_primary": True,
                    }
                ],
            }),
            resource_fingerprint=fingerprint,
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
    manifest = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert manifest["source_object"]["object_key"] == "client/files/src-1/asset.png"
    assert manifest["source_object"]["file_name"] == "asset.png"
    assert "生成 manifest 1" in out


def test_upload_resources_dry_run_uses_provided_previews(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.upsert_object_manifest(
            task_id,
            {
                "client_resource_id": "src-1",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "client/files/src-1/asset.png",
                    "file_name": "asset.png",
                    "file_format": "png",
                },
                "provided_previews": [
                    {
                        "storage_profile_id": "default",
                        "object_key": "client/previews/src-1/primary.webp",
                        "role": "primary",
                        "width": 32,
                        "height": 16,
                    }
                ],
            },
            resource_fingerprint=fingerprint,
        )
        _mark_description_ready(store, task_id)
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
    manifest = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert manifest["previews"][0]["object_key"] == "client/previews/src-1/primary.webp"
    assert manifest["previews"][0]["width"] == 32
    assert "生成 manifest 1" in out


def test_upload_resources_dry_run_skips_pack_manifests(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        pack_task_id = store.insert_task(
            _make_entity(
                tmp_path,
                content_md5="pack-md5",
                resource_type=PACK_RESOURCE_TYPE,
                source_resource_id="pack-src",
            )
        )
        image_task_id = store.insert_task(
            _make_entity(
                tmp_path,
                content_md5="image-md5",
                resource_type="single_image",
                source_resource_id="image-src",
            )
        )
        store.upsert_object_manifest(
            pack_task_id,
            {
                "client_resource_id": "pack-src",
                "resource_type": PACK_RESOURCE_TYPE,
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "client/files/pack-src/source.zip",
                    "file_name": "source.zip",
                    "file_format": "zip",
                },
                "source_files": [{"file_name": "source.zip"}],
            },
            resource_fingerprint=store.get_task_by_id(pack_task_id)["resource_fingerprint"],
        )
        store.upsert_object_manifest(
            image_task_id,
            _with_preview({
                "client_resource_id": "image-src",
                "resource_type": "single_image",
                "source_object": {
                    "storage_profile_id": "default",
                    "object_key": "client/files/image-src/asset.png",
                    "file_name": "asset.png",
                    "file_format": "png",
                },
                "source_files": [{"file_name": "asset.png"}],
            }, "image-src"),
            resource_fingerprint=store.get_task_by_id(image_task_id)["resource_fingerprint"],
        )
        _mark_description_ready(store, image_task_id)
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
    assert "image-src" in out
    assert "pack-src" not in out
    assert "生成 manifest 1" in out


def test_upload_resources_descriptions_are_enabled_by_default(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        store.insert_description(task_id, main_content="main", detail_content="detail", full_description="full", prompt_version="test")
        fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.upsert_object_manifest(
            task_id,
            _with_preview({
                "client_resource_id": "src-1",
                "source_object": {"object_key": "client/files/src-1/asset.png"},
            }),
            resource_fingerprint=fingerprint,
        )
        store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
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
    assert '"description"' in out
    assert '"description_context"' not in out


def test_upload_resources_no_descriptions_is_deprecated(monkeypatch, tmp_path):
    from ResourceProcessor import upload_resources

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upload_resources",
            "--db-path",
            str(tmp_path / "pipeline.db"),
            "--dry-run",
            "--no-descriptions",
        ],
    )

    with pytest.raises(SystemExit):
        upload_resources.main()


def test_upload_resources_submits_when_only_description_changed(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        old_fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.upsert_object_manifest(
            task_id,
            _with_preview({
                "client_resource_id": "src-1",
                "source_object": {"object_key": "client/files/src-1/asset.png"},
            }),
            resource_fingerprint=old_fingerprint,
        )
        store.insert_description(task_id, main_content="new", detail_content="", full_description="", prompt_version="test")
        store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
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
    assert "生成 manifest 1" in out
    assert '"description"' in out


def test_upload_resources_does_not_recompute_object_fingerprint(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.upsert_object_manifest(
            task_id,
            _with_preview({
                "client_resource_id": "src-1",
                "source_object": {"object_key": "client/files/src-1/asset.png"},
            }),
            resource_fingerprint=fingerprint,
            object_fingerprint="old-object-fingerprint",
        )
        _mark_description_ready(store, task_id)
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
    assert "生成 manifest 1" in out
    assert "等待对象上传" not in out


def test_upload_resources_completeness_gate_runs_before_fingerprint_skip(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        _mark_description_ready(store, task_id)
        fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.upsert_object_manifest(
            task_id,
            {
                "client_resource_id": "src-1",
                "source_object": {"object_key": "client/files/src-1/asset.png"},
            },
            resource_fingerprint=fingerprint,
        )
        current = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.mark_object_manifest_submitted(task_id, {"job_id": "job-old"}, resource_fingerprint=current)
        store._conn.execute(
            "UPDATE resource_object_manifest SET submit_state = 'pending' WHERE task_id = ?",
            (task_id,),
        )
        store._conn.commit()
        store.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
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
    assert "等待预览上传" in out
    assert "指纹未变化" not in out


def test_upload_resources_submits_after_uploaded_object_key_changes(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id = store.insert_task(_make_entity(tmp_path))
        store.upsert_object_manifest(
            task_id,
            _with_preview({
                "client_resource_id": "src-1",
                "source_object": {"storage_profile_id": "default", "object_key": "client/files/src-1/old.png"},
            }),
            object_fingerprint="old-object-fingerprint",
        )
        _mark_description_ready(store, task_id)
        old_fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
        store.mark_object_manifest_submitted(task_id, {"job_id": "job-old"}, resource_fingerprint=old_fingerprint)
        store.upsert_object_manifest(
            task_id,
            _with_preview({
                "client_resource_id": "src-1",
                "source_object": {"storage_profile_id": "default", "object_key": "client/files/src-1/new.png"},
            }),
            object_fingerprint="new-object-fingerprint",
        )
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
    assert "生成 manifest 1" in out
    assert "client/files/src-1/new.png" in out
    assert "指纹未变化" not in out


def test_upload_resources_defaults_to_async_enqueue_and_exit(monkeypatch, tmp_path, capsys):
    from ResourceProcessor import upload_resources

    db_path = tmp_path / "pipeline.db"
    store = LocalCacheStore(str(db_path))
    try:
        task_id_1 = store.insert_task(_make_entity(tmp_path, source_resource_id="src-1"))
        task_id_2 = store.insert_task(
            _make_entity(tmp_path, content_md5="asset-md5-2", source_resource_id="src-2")
        )
        for task_id, key in ((task_id_1, "k1"), (task_id_2, "k2")):
            fingerprint = store.get_task_by_id(task_id)["resource_fingerprint"]
            store.upsert_object_manifest(
                task_id,
                _with_preview({"client_resource_id": f"res-{task_id}", "source_object": {"object_key": key}}, f"res-{task_id}"),
                resource_fingerprint=fingerprint,
            )
            _mark_description_ready(store, task_id)
    finally:
        store.close()

    submit_calls = []

    def fake_submit_processing_job(manifest, **kwargs):
        submit_calls.append(manifest["client_resource_id"])
        return {"job_id": f"job-{len(submit_calls)}", "state": "queued"}

    monkeypatch.setattr(upload_resources, "submit_processing_job", fake_submit_processing_job)
    monkeypatch.setattr(
        upload_resources,
        "_wait_for_inflight_jobs",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("default upload must not wait for all jobs")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upload_resources",
            "--db-path",
            str(db_path),
            "--concurrency",
            "1",
        ],
    )

    assert upload_resources.main() == 0
    out = capsys.readouterr().out
    assert submit_calls == ["src-1", "src-2"]
    assert "生成 manifest 2, 提交 2, 失败 0" in out


def test_upload_resources_wait_all_is_explicit_opt_in(monkeypatch, tmp_path):
    from ResourceProcessor import upload_resources

    waited = []
    monkeypatch.setattr(
        upload_resources,
        "_reconcile_inflight_jobs",
        lambda **kwargs: {"checked": 0, "completed": 0, "active": 0, "failed": 0, "errors": 0},
    )
    monkeypatch.setattr(
        upload_resources,
        "_wait_for_inflight_jobs",
        lambda **kwargs: waited.append(True) or {"completed": 0, "failed": 0, "errors": 0},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upload_resources",
            "--db-path",
            str(tmp_path / "pipeline.db"),
            "--wait-all",
        ],
    )

    assert upload_resources.main() == 0
    assert waited == [True]
