from pathlib import Path
import zipfile

import ObjectStorageUpload.resource_manifest as resource_manifest
from ObjectStorageUpload.storage_profiles import load_storage_profiles
from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.core.object_storage_upload import ObjectStorageUploader, StorageObjectRef, safe_object_path_part
from ResourceProcessor.delete_processed_resource import cleanup_local_pipeline_db
from ResourceProcessor.preview_metadata import FileInfo, PreviewInfo, PreviewStrategy, ResourceProcessingEntity
from ResourceProcessor.submit_processing_manifest import submit_processing_job, wait_processing_job
from ResourceProcessor.upload_objects_to_storage import build_manifests_from_cache, upload_entity_objects
from resource_contracts.resource_types import PACK_RESOURCE_TYPE


class FakeUploader:
    def __init__(self, bucket: str = "resources", *args, **kwargs):
        self.bucket = bucket
        self.profile = type("Profile", (), {"profile_id": "default"})()
        self.calls = []
        self.deleted_keys = []
        self.zip_entries_by_key = {}

    def upload_file(self, file_path, *, object_key, is_primary=False, content_type=""):
        path = Path(file_path)
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                self.zip_entries_by_key[object_key] = sorted(zf.namelist())
        self.calls.append(
            {
                "file_path": str(path),
                "object_key": object_key,
                "is_primary": is_primary,
            }
        )
        return StorageObjectRef(
            storage_profile_id="default",
            object_key=object_key,
            file_name=path.name,
            file_format=path.suffix.lower().lstrip("."),
            size=path.stat().st_size,
            checksum="md5",
            etag="etag",
            is_primary=is_primary,
        )

    def delete_objects(self, object_keys):
        keys = list(object_keys)
        self.deleted_keys.extend(keys)
        return len(keys)


class FakeS3Client:
    def __init__(self):
        self.put_args = None

    def put_object(self, **kwargs):
        self.put_args = kwargs

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": self.put_args["ContentLength"], "ETag": '"etag"'}


class FakeDeleteRequiresMd5Client:
    def __init__(self):
        self.deleted_keys = []

    def delete_objects(self, **kwargs):
        from botocore.exceptions import ClientError

        raise ClientError(
            {
                "Error": {
                    "Code": "InvalidRequest",
                    "Message": "Missing required header for this request: Content-MD5",
                }
            },
            "DeleteObjects",
        )

    def delete_object(self, *, Bucket, Key):
        self.deleted_keys.append((Bucket, Key))


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeProcessingSession:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.get_payloads = [
            {"job_id": "job-1", "state": "queued"},
            {"job_id": "job-1", "state": "completed", "search_resource_id": "res-1"},
        ]

    def post(self, url, *, json, headers, timeout):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse({"job_id": "job-1", "state": "queued"})

    def get(self, url, *, headers, timeout):
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(self.get_payloads.pop(0))


def test_processing_manifest_submit_sends_api_key_and_waits_for_completion():
    session = FakeProcessingSession()
    created = submit_processing_job(
        {"client_resource_id": "asset-1"},
        processing_server="http://processing",
        client_id="client-a",
        api_key="processing-key",
        session=session,
    )
    completed = wait_processing_job(
        created["job_id"],
        processing_server="http://processing",
        client_id="client-a",
        api_key="processing-key",
        poll_interval=0.1,
        timeout_seconds=1,
        session=session,
    )

    assert session.posts[0]["headers"]["X-Client-Id"] == "client-a"
    assert session.posts[0]["headers"]["X-API-Key"] == "processing-key"
    assert [item["headers"]["X-API-Key"] for item in session.gets] == ["processing-key", "processing-key"]
    assert completed["state"] == "completed"
    assert completed["search_resource_id"] == "res-1"


def test_object_storage_uploader_puts_object_with_content_length(tmp_path):
    path = tmp_path / "asset.bin"
    path.write_bytes(b"asset-bytes")
    client = FakeS3Client()
    uploader = ObjectStorageUploader.__new__(ObjectStorageUploader)
    uploader.bucket = "resources"
    uploader.client = client
    uploader.profile = type(
        "Profile",
        (),
        {
            "profile_id": "default",
            "validate_object_key": staticmethod(lambda key: key),
        },
    )()

    ref = uploader.upload_file(path, object_key="client/files/asset/asset.bin")

    assert client.put_args["Bucket"] == "resources"
    assert client.put_args["Key"] == "client/files/asset/asset.bin"
    assert client.put_args["ContentLength"] == path.stat().st_size
    assert ref.size == path.stat().st_size


def test_object_storage_uploader_delete_objects_falls_back_without_content_md5():
    client = FakeDeleteRequiresMd5Client()
    uploader = ObjectStorageUploader.__new__(ObjectStorageUploader)
    uploader.bucket = "resources"
    uploader.client = client
    uploader.profile = type(
        "Profile",
        (),
        {
            "validate_object_key": staticmethod(lambda key: key),
        },
    )()

    deleted = uploader.delete_objects(["old/a.png", "old/b.json"])

    assert deleted == 2
    assert client.deleted_keys == [
        ("resources", "old/a.png"),
        ("resources", "old/b.json"),
    ]


def test_storage_profiles_load_from_file_and_env_refs(tmp_path):
    path = tmp_path / "storage_profiles.jsonc"
    path.write_text(
        """
{
  // JSONC comments are allowed in profile files.
  "default_profile_id": "cos",
  "aliases": {
    "default": "cos",
  },
  "profiles": {
    "cos": {
      "bucket": "game-ai-studio-resource-1252100362",
      "endpoint_env": ["MISSING_ENDPOINT", "OBJECT_STORAGE_ENDPOINT"],
      "access_key_env": ["OBJECT_STORAGE_ACCESS_KEY"],
      "secret_key_env": ["OBJECT_STORAGE_SECRET_KEY"],
      "cdn_auth_key_env": ["OBJECT_STORAGE_CDN_AUTH_KEY_PRIMARY", "OBJECT_STORAGE_CDN_AUTH_KEY_SECONDARY"],
      "cdn_auth_algorithm": "md5",
      "cdn_auth_expires": 600,
      "cdn_auth_time_format": "unix_decimal",
      "cdn_auth_scope": "file_suffix",
      "cdn_auth_file_suffixes": ["*"],
      "allowed_prefixes": ["resource-crawler/"],
    },
  },
}
""".strip(),
        encoding="utf-8",
    )

    registry = load_storage_profiles({
        "STORAGE_PROFILES_FILE": str(path),
        "OBJECT_STORAGE_ENDPOINT": "https://cos.example.com",
        "OBJECT_STORAGE_ACCESS_KEY": "ak",
        "OBJECT_STORAGE_SECRET_KEY": "sk",
        "OBJECT_STORAGE_CDN_AUTH_KEY_PRIMARY": "primary",
        "OBJECT_STORAGE_CDN_AUTH_KEY_SECONDARY": "secondary",
    })

    profile = registry.default()
    assert registry.default_profile_id == "cos"
    assert registry.get("default").profile_id == "cos"
    assert profile.profile_id == "cos"
    assert profile.bucket == "game-ai-studio-resource-1252100362"
    assert profile.endpoint == "https://cos.example.com"
    assert profile.access_key == "ak"
    assert profile.cdn_auth_key == "primary"
    assert profile.cdn_auth_keys == ("primary", "secondary")
    assert profile.cdn_auth_algorithm == "md5"
    assert profile.cdn_auth_expires == 600
    assert profile.cdn_auth_time_format == "unix_decimal"
    assert profile.cdn_auth_scope == "file_suffix"
    assert profile.cdn_auth_file_suffixes == ("*",)
    assert profile.allowed_prefixes == ("resource-crawler/",)


def test_upload_entity_objects_builds_bucket_key_manifest(tmp_path):
    model = tmp_path / "hero model.glb"
    model.write_bytes(b"glb")
    preview = tmp_path / "hero preview.png"
    preview.write_bytes(b"png")

    entity = ResourceProcessingEntity(
        resource_type="model",
        source_directory=str(tmp_path),
        source_resource_id="asset:001",
        title="Hero Model",
        category="character",
        tags=["hero"],
        source_description="original text",
        auxiliary_metadata={"prompt": "make a hero"},
        files=[
            FileInfo(
                file_path=str(model),
                file_name=model.name,
                file_size=model.stat().st_size,
                file_format="glb",
                content_md5="abc",
                file_role="main",
                is_primary=True,
            )
        ],
        previews=[
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(preview),
                width=128,
                height=96,
                renderer="client-preview",
            )
        ],
    )

    manifest = upload_entity_objects(
        entity,
        uploader=FakeUploader(),
        client_id="crawler/A",
        include_previews=True,
    )

    assert manifest["request_id"] == "crawler/A:asset:001"
    assert manifest["client_resource_id"] == "asset:001"
    assert manifest["source_object"]["storage_profile_id"] == "default"
    assert manifest["source_object"]["object_key"] == "crawler_A/files/asset:001/hero model.glb"
    assert manifest["source_files"][0]["file_name"] == "hero model.glb"
    assert manifest["provided_previews"][0]["object_key"] == "crawler_A/previews/asset:001/primary.png"
    assert manifest["provided_previews"][0]["origin"] == "provided"
    assert manifest["provided_previews"][0]["width"] == 128
    assert manifest["client_metadata"]["category"] == "character"
    assert manifest["client_metadata"]["auxiliary_metadata"] == {"prompt": "make a hero"}


def test_upload_entity_objects_can_skip_previews(tmp_path):
    model = tmp_path / "asset.obj"
    model.write_text("obj", encoding="utf-8")
    preview = tmp_path / "asset.png"
    preview.write_text("png", encoding="utf-8")
    entity = ResourceProcessingEntity(
        resource_type="model",
        source_directory=str(tmp_path),
        source_resource_id="asset-1",
        files=[
            FileInfo(
                file_path=str(model),
                file_name=model.name,
                file_size=model.stat().st_size,
                file_format="obj",
                content_md5="abc",
            )
        ],
        previews=[PreviewInfo(strategy=PreviewStrategy.STATIC, path=str(preview))],
    )

    manifest = upload_entity_objects(
        entity,
        uploader=FakeUploader(),
        client_id="client",
        include_previews=False,
    )

    assert manifest["source_object"]["object_key"] == "client/files/asset-1/asset.obj"
    assert len(manifest["source_files"]) == 1
    assert manifest["provided_previews"] == []


def test_upload_entity_objects_names_multiple_previews_by_role_and_order(tmp_path):
    source = tmp_path / "asset.png"
    source.write_bytes(b"png")
    primary = tmp_path / "primary from client.webp"
    gallery_a = tmp_path / "shot a.webp"
    gallery_b = tmp_path / "shot b.png"
    for path in (primary, gallery_a, gallery_b):
        path.write_bytes(b"preview")

    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        source_resource_id="asset-preview",
        files=[
            FileInfo(
                file_path=str(source),
                file_name=source.name,
                file_size=source.stat().st_size,
                file_format="png",
                content_md5="abc",
            )
        ],
        previews=[
            PreviewInfo(strategy=PreviewStrategy.STATIC, role="primary", path=str(primary)),
            PreviewInfo(strategy=PreviewStrategy.STATIC, role="gallery", path=str(gallery_a)),
            PreviewInfo(strategy=PreviewStrategy.STATIC, role="gallery", path=str(gallery_b)),
        ],
    )

    manifest = upload_entity_objects(
        entity,
        uploader=FakeUploader(),
        client_id="client",
        include_previews=True,
    )

    assert [item["object_key"] for item in manifest["provided_previews"]] == [
        "client/previews/asset-preview/primary.webp",
        "client/previews/asset-preview/gallery-001.webp",
        "client/previews/asset-preview/gallery-002.png",
    ]


def test_object_storage_manifest_is_saved_to_cache(tmp_path):
    model = tmp_path / "asset.glb"
    model.write_bytes(b"glb")
    entity = ResourceProcessingEntity(
        resource_type="model",
        source_directory=str(tmp_path),
        source_resource_id="asset-2",
        content_md5="asset-md5",
        files=[
            FileInfo(
                file_path=str(model),
                file_name=model.name,
                file_size=model.stat().st_size,
                file_format="glb",
                content_md5="abc",
                is_primary=True,
            )
        ],
    )
    manifest = upload_entity_objects(
        entity,
        uploader=FakeUploader(),
        client_id="client",
        include_previews=False,
    )

    cache = LocalCacheStore(str(tmp_path / "pipeline.db"))
    try:
        task_id = cache.insert_task(entity)
        cache.record_task_error(task_id, "object_storage_upload_error", "old upload error")
        cache.upsert_object_manifest(task_id, manifest)

        saved = cache.get_object_manifest(task_id)
        assert saved is not None
        assert saved["submit_state"] == "pending"
        assert saved["manifest"]["source_object"]["object_key"] == "client/files/asset-2/asset.glb"
        task = cache.get_task_by_id(task_id)
        assert task["last_error_code"] == ""
        assert task["last_error_message"] == ""

        rows = list(cache.iter_object_manifests(submit_state="pending"))
        assert len(rows) == 1
        assert rows[0]["task_id"] == task_id

        cache.mark_object_manifest_submitted(task_id, {"job_id": "job-1", "state": "queued"})
        submitted = cache.get_object_manifest(task_id)
        assert submitted["submit_state"] == "submitted"
        assert submitted["processing_job_id"] == "job-1"
    finally:
        cache.close()


def test_object_manifest_queued_state_does_not_commit_fingerprint(tmp_path):
    model = tmp_path / "asset.png"
    model.write_bytes(b"png")
    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        source_resource_id="asset-queued",
        content_md5="asset-md5",
        files=[FileInfo(file_path=str(model), file_name=model.name, file_size=3, file_format="png", content_md5="md5")],
    )
    cache = LocalCacheStore(str(tmp_path / "pipeline.db"))
    try:
        task_id = cache.insert_task(entity)
        cache.upsert_object_manifest(
            task_id,
            {
                "client_resource_id": "asset-queued",
                "source_object": {"object_key": "client/files/asset/source.png"},
                "source_files": [{"file_name": "source.png"}],
            },
            resource_fingerprint="resource-fp",
            object_fingerprint="object-fp",
        )

        cache.mark_object_manifest_queued(task_id, {"job_id": "job-queued", "state": "queued"})
        queued = cache.get_object_manifest(task_id)

        assert queued["submit_state"] == "queued"
        assert queued["processing_job_id"] == "job-queued"
        assert queued["committed_fingerprint"] == ""
    finally:
        cache.close()


def test_source_object_keys_from_manifest_collects_source_and_preview_objects():
    keys = resource_manifest.source_object_keys_from_manifest(
        {
            "source_object": {"object_key": "client/files/asset/source.zip"},
            "source_files": [
                {"object_key": "client/files/asset/a.png"},
                {"object_key": "client/files/asset/a.png"},
                {"object_key": "client/files/asset/b.json"},
            ],
            "provided_previews": [
                {"object_key": "client/previews/asset/primary.webp"},
                {"object_key": "client/previews/asset/primary.webp"},
                {"object_key": "client/previews/asset/gallery-001.webp"},
            ],
        }
    )

    assert keys == [
        "client/files/asset/source.zip",
        "client/files/asset/a.png",
        "client/files/asset/b.json",
        "client/previews/asset/primary.webp",
        "client/previews/asset/gallery-001.webp",
    ]


def test_build_manifests_from_cache_uses_pack_as_package_object_only(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    pack_root = tmp_path / "pack"
    pack_root.mkdir()
    pack_a = pack_root / "a.png"
    pack_b = pack_root / "b.json"
    child = pack_root / "child.png"
    pack_a.write_bytes(b"pack-a")
    pack_b.write_bytes(b"pack-b")
    child.write_bytes(b"child")

    pack_entity = ResourceProcessingEntity(
        resource_type=PACK_RESOURCE_TYPE,
        source_directory=str(pack_root),
        source_resource_id="pack-1",
        source="kenney",
        pack_name="starter",
        files=[
            FileInfo(
                file_path=str(pack_a),
                file_name=pack_a.name,
                file_size=pack_a.stat().st_size,
                file_format="png",
                content_md5="pack-a-md5",
            ),
            FileInfo(
                file_path=str(pack_b),
                file_name=pack_b.name,
                file_size=pack_b.stat().st_size,
                file_format="json",
                content_md5="pack-b-md5",
            ),
        ],
    )
    child_entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(pack_root),
        source_resource_id="child-1",
        parent_resource_id="pack-1",
        source="kenney",
        pack_name="starter",
        files=[
            FileInfo(
                file_path=str(child),
                file_name=child.name,
                file_size=child.stat().st_size,
                file_format="png",
                content_md5="child-md5",
            ),
        ],
    )

    cache = LocalCacheStore(str(db_path))
    try:
        pack_task_id = cache.insert_task(pack_entity)
        child_task_id = cache.insert_task(child_entity)
    finally:
        cache.close()

    fake = FakeUploader()
    monkeypatch.setattr(resource_manifest, "ObjectStorageUploader", lambda *args, **kwargs: fake)

    records = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=True,
        include_descriptions=True,
        dry_run=False,
        workers=1,
    ))

    assert [manifest["client_resource_id"] for _, manifest in records] == ["child-1"]
    child_manifest = records[0][1]
    assert "child_resources" not in child_manifest
    assert child_manifest["package_object"] == {
        "storage_profile_id": "default",
        "object_key": "client/files/pack-1/source.zip",
    }
    assert child_manifest["provided_previews"] == []
    assert "client/files/pack-1/source.zip" in fake.zip_entries_by_key
    assert "client/files/child-1/child.png" in [call["object_key"] for call in fake.calls]

    cache = LocalCacheStore(str(db_path))
    try:
        pack_saved = cache.get_object_manifest(pack_task_id)
        child_saved = cache.get_object_manifest(child_task_id)
        assert pack_saved["submit_state"] == "package_only"
        assert child_saved["submit_state"] == "pending"
        assert child_saved["manifest"]["package_object"] == child_manifest["package_object"]
    finally:
        cache.close()


def test_build_manifests_limit_counts_emitted_manifests_after_skips(tmp_path):
    db_path = tmp_path / "pipeline.db"
    pack_root = tmp_path / "pack-limit"
    pack_root.mkdir()
    pack_file = pack_root / "pack.json"
    child_a = pack_root / "child-a.png"
    child_b = pack_root / "child-b.png"
    pack_file.write_bytes(b"pack")
    child_a.write_bytes(b"child-a")
    child_b.write_bytes(b"child-b")

    pack_entity = ResourceProcessingEntity(
        resource_type=PACK_RESOURCE_TYPE,
        source_directory=str(pack_root),
        source_resource_id="pack-limit",
        source="kenney",
        pack_name="limit pack",
        files=[
            FileInfo(
                file_path=str(pack_file),
                file_name=pack_file.name,
                file_size=pack_file.stat().st_size,
                file_format="json",
                content_md5="pack-limit-md5",
            ),
        ],
    )
    child_entities = [
        ResourceProcessingEntity(
            resource_type="single_image",
            source_directory=str(pack_root),
            source_resource_id=f"child-{suffix}",
            parent_resource_id="pack-limit",
            source="kenney",
            pack_name="limit pack",
            files=[
                FileInfo(
                    file_path=str(path),
                    file_name=path.name,
                    file_size=path.stat().st_size,
                    file_format="png",
                    content_md5=f"child-{suffix}-md5",
                )
            ],
        )
        for suffix, path in (("a", child_a), ("b", child_b))
    ]

    cache = LocalCacheStore(str(db_path))
    try:
        cache.insert_task(pack_entity)
        for entity in child_entities:
            cache.insert_task(entity)
    finally:
        cache.close()

    records = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        include_descriptions=False,
        dry_run=True,
        workers=1,
        limit=2,
    ))

    assert [manifest["client_resource_id"] for _, manifest in records] == ["child-a", "child-b"]
    assert [manifest["package_object"]["object_key"] for _, manifest in records] == [
        "client/files/pack-limit/pack.json",
        "client/files/pack-limit/pack.json",
    ]


def test_cleanup_local_pipeline_db_removes_task_and_child_rows(tmp_path):
    db_path = tmp_path / "pipeline.db"
    source = tmp_path / "asset.png"
    preview = tmp_path / "asset.webp"
    source.write_bytes(b"source")
    preview.write_bytes(b"preview")

    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        source_resource_id="asset-local-delete",
        files=[
            FileInfo(
                file_path=str(source),
                file_name=source.name,
                file_size=source.stat().st_size,
                file_format="png",
                content_md5="source-md5",
            )
        ],
    )
    cache = LocalCacheStore(str(db_path))
    try:
        task_id = cache.insert_task(entity)
        cache.insert_preview(
            task_id,
            PreviewInfo(strategy=PreviewStrategy.STATIC, path=str(preview), role="primary"),
        )
        cache.insert_description(task_id, "main", "detail", "full", "unit")
        cache.upsert_object_manifest(
            task_id,
            {
                "client_resource_id": "asset-local-delete",
                "source_object": {"object_key": "client/files/asset/source.png"},
            },
        )
    finally:
        cache.close()

    result = cleanup_local_pipeline_db(str(db_path), "asset-local-delete")

    assert result["tasks"] == 1
    assert result["child_rows"] >= 4
    cache = LocalCacheStore(str(db_path))
    try:
        assert cache.get_task_by_id(task_id) is None
        assert cache.get_object_manifest(task_id) is None
        assert cache.get_previews_by_task(task_id) == []
        assert cache.get_description_by_task(task_id) is None
    finally:
        cache.close()


def test_build_manifests_from_cache_resumes_by_default_and_force_reuploads(tmp_path):
    db_path = tmp_path / "pipeline.db"
    uploaded_file = tmp_path / "uploaded.png"
    pending_file = tmp_path / "pending.png"
    uploaded_file.write_bytes(b"uploaded")
    pending_file.write_bytes(b"pending")
    uploaded_entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        source_resource_id="uploaded",
        files=[
            FileInfo(
                file_path=str(uploaded_file),
                file_name=uploaded_file.name,
                file_size=uploaded_file.stat().st_size,
                file_format="png",
                content_md5="uploaded-md5",
            )
        ],
    )
    pending_entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        source_resource_id="pending",
        files=[
            FileInfo(
                file_path=str(pending_file),
                file_name=pending_file.name,
                file_size=pending_file.stat().st_size,
                file_format="png",
                content_md5="pending-md5",
            )
        ],
    )

    cache = LocalCacheStore(str(db_path))
    try:
        uploaded_task = cache.insert_task(uploaded_entity)
        cache.insert_task(pending_entity)
        uploaded_manifest = upload_entity_objects(
            uploaded_entity,
            uploader=FakeUploader(),
            client_id="client",
            include_previews=False,
        )
        profile_id = load_storage_profiles().get(None).profile_id
        uploaded_object_fp, _ = resource_manifest.object_fingerprint_for_entity(
            uploaded_entity,
            client_id="client",
            storage_profile_id=profile_id,
            key_prefix="",
            include_previews=False,
        )
        uploaded_resource_fp, _ = resource_manifest.manifest_resource_fingerprint_for_entity(
            uploaded_entity,
            client_id="client",
            storage_profile_id=profile_id,
            key_prefix="",
            include_previews=False,
            include_descriptions=False,
            object_fingerprint=uploaded_object_fp,
        )
        cache.upsert_object_manifest(
            uploaded_task,
            uploaded_manifest,
            resource_fingerprint=uploaded_resource_fp,
            object_fingerprint=uploaded_object_fp,
            upload_options=resource_manifest.upload_options_payload(
                client_id="client",
                storage_profile_id=profile_id,
                key_prefix="",
                include_previews=False,
                include_descriptions=False,
            ),
        )
        cache.mark_object_manifest_submitted(uploaded_task, {"job_id": "job-uploaded"})
    finally:
        cache.close()

    resumed = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        dry_run=True,
        resume=True,
    ))
    forced = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        dry_run=True,
        resume=True,
        force=True,
    ))

    assert [manifest["client_resource_id"] for _, manifest in resumed] == ["pending"]
    assert [manifest["client_resource_id"] for _, manifest in forced] == ["uploaded", "pending"]


def test_build_manifests_reuses_objects_when_only_description_changes(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    source = tmp_path / "asset.png"
    source.write_bytes(b"asset")
    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        source_resource_id="asset-desc-dirty",
        files=[
            FileInfo(
                file_path=str(source),
                file_name=source.name,
                file_size=source.stat().st_size,
                file_format="png",
                content_md5="asset-md5",
            )
        ],
    )
    first_fake = FakeUploader()
    monkeypatch.setattr(resource_manifest, "ObjectStorageUploader", lambda *args, **kwargs: first_fake)

    cache = LocalCacheStore(str(db_path))
    try:
        task_id = cache.insert_task(entity)
    finally:
        cache.close()

    first = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        include_descriptions=True,
        dry_run=False,
        workers=1,
    ))
    assert [manifest["client_resource_id"] for _, manifest in first] == ["asset-desc-dirty"]
    assert len(first_fake.calls) == 1

    cache = LocalCacheStore(str(db_path))
    try:
        cache.mark_object_manifest_submitted(task_id, {"job_id": "job-1"})
        cache.insert_description(task_id, "new main", "new detail", "new full", "v2")
    finally:
        cache.close()

    second_fake = FakeUploader()
    monkeypatch.setattr(resource_manifest, "ObjectStorageUploader", lambda *args, **kwargs: second_fake)
    second = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        include_descriptions=True,
        dry_run=False,
        workers=1,
    ))

    assert [manifest["client_resource_id"] for _, manifest in second] == ["asset-desc-dirty"]
    assert second[0][1]["provided_description"]["main_content"] == "new main"
    assert second_fake.calls == []
    assert second_fake.deleted_keys == []


def test_build_manifests_key_prefix_change_reuploads_and_deletes_old_key(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    source = tmp_path / "asset.png"
    source.write_bytes(b"asset")
    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        source_resource_id="asset-key-dirty",
        files=[
            FileInfo(
                file_path=str(source),
                file_name=source.name,
                file_size=source.stat().st_size,
                file_format="png",
                content_md5="asset-md5",
            )
        ],
    )
    cache = LocalCacheStore(str(db_path))
    try:
        task_id = cache.insert_task(entity)
    finally:
        cache.close()

    first_fake = FakeUploader()
    monkeypatch.setattr(resource_manifest, "ObjectStorageUploader", lambda *args, **kwargs: first_fake)
    list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        key_prefix="old",
        dry_run=False,
        workers=1,
    ))
    cache = LocalCacheStore(str(db_path))
    try:
        cache.mark_object_manifest_submitted(task_id, {"job_id": "job-old"})
    finally:
        cache.close()

    second_fake = FakeUploader()
    monkeypatch.setattr(resource_manifest, "ObjectStorageUploader", lambda *args, **kwargs: second_fake)
    second = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        key_prefix="new",
        dry_run=False,
        workers=1,
    ))

    assert [manifest["source_object"]["object_key"] for _, manifest in second] == [
        "new/client/files/asset-key-dirty/asset.png",
    ]
    assert [call["object_key"] for call in second_fake.calls] == [
        "new/client/files/asset-key-dirty/asset.png",
    ]
    assert second_fake.deleted_keys == ["old/client/files/asset-key-dirty/asset.png"]


def test_build_manifests_force_reuploads_existing_for_selected_resource_types(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    atlas_root = tmp_path / "atlas"
    atlas_root.mkdir()
    atlas_a = atlas_root / "a.png"
    atlas_b = atlas_root / "b.json"
    atlas_a.write_bytes(b"atlas-a")
    atlas_b.write_bytes(b"atlas-b")
    image = tmp_path / "single.png"
    image.write_bytes(b"single")

    atlas_entity = ResourceProcessingEntity(
        resource_type="atlas",
        source_directory=str(atlas_root),
        source_resource_id="atlas-1",
        files=[
            FileInfo(
                file_path=str(atlas_a),
                file_name=atlas_a.name,
                file_size=atlas_a.stat().st_size,
                file_format="png",
                content_md5="atlas-a-md5",
            ),
            FileInfo(
                file_path=str(atlas_b),
                file_name=atlas_b.name,
                file_size=atlas_b.stat().st_size,
                file_format="json",
                content_md5="atlas-b-md5",
            ),
        ],
    )
    image_entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        source_resource_id="single-1",
        files=[
            FileInfo(
                file_path=str(image),
                file_name=image.name,
                file_size=image.stat().st_size,
                file_format="png",
                content_md5="single-md5",
            )
        ],
    )

    cache = LocalCacheStore(str(db_path))
    try:
        atlas_task = cache.insert_task(atlas_entity)
        image_task = cache.insert_task(image_entity)
        cache.upsert_object_manifest(
            atlas_task,
            {
                "client_resource_id": "atlas-1",
                "source_files": [
                    {"object_key": "client/files/atlas-1/old-a.png"},
                    {"object_key": "client/files/atlas-1/old-b.json"},
                ],
                "provided_previews": [
                    {"object_key": "client/previews/atlas-1/old-primary.webp"},
                ],
            },
        )
        cache.upsert_object_manifest(
            image_task,
            {
                "client_resource_id": "single-1",
                "source_object": {"object_key": "client/files/single-1/single.png"},
                "source_files": [{"file_name": "single.png"}],
            },
        )
    finally:
        cache.close()

    fake = FakeUploader()
    monkeypatch.setattr(resource_manifest, "ObjectStorageUploader", lambda *args, **kwargs: fake)

    uploaded = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        dry_run=False,
        resume=False,
        force=True,
        resource_types=["atlas"],
        workers=1,
    ))

    assert [manifest["client_resource_id"] for _, manifest in uploaded] == ["atlas-1"]
    assert fake.deleted_keys == [
        "client/files/atlas-1/old-a.png",
        "client/files/atlas-1/old-b.json",
        "client/previews/atlas-1/old-primary.webp",
    ]
    assert fake.zip_entries_by_key["client/files/atlas-1/source.zip"] == ["a.png", "b.json"]

    cache = LocalCacheStore(str(db_path))
    try:
        atlas_saved = cache.get_object_manifest(atlas_task)
        image_saved = cache.get_object_manifest(image_task)
        assert atlas_saved["manifest"]["source_object"]["object_key"] == "client/files/atlas-1/source.zip"
        assert len(atlas_saved["manifest"]["source_files"]) == 2
        assert image_saved["manifest"]["source_object"]["object_key"] == "client/files/single-1/single.png"
    finally:
        cache.close()


def test_build_manifests_from_cache_uploads_with_workers(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    cache = LocalCacheStore(str(db_path))
    try:
        for index in range(3):
            path = tmp_path / f"asset-{index}.png"
            path.write_bytes(f"asset-{index}".encode("utf-8"))
            entity = ResourceProcessingEntity(
                resource_type="single_image",
                source_directory=str(tmp_path),
                source_resource_id=f"asset-{index}",
                files=[
                    FileInfo(
                        file_path=str(path),
                        file_name=path.name,
                        file_size=path.stat().st_size,
                        file_format="png",
                        content_md5=f"md5-{index}",
                    )
                ],
            )
            cache.insert_task(entity)
    finally:
        cache.close()

    monkeypatch.setattr(
        resource_manifest,
        "_thread_uploader",
        lambda storage_profile_id: FakeUploader(),
    )

    uploaded = list(build_manifests_from_cache(
        db_path=str(db_path),
        client_id="client",
        include_previews=False,
        dry_run=False,
        workers=2,
    ))

    assert {manifest["client_resource_id"] for _, manifest in uploaded} == {"asset-0", "asset-1", "asset-2"}
    cache = LocalCacheStore(str(db_path))
    try:
        rows = list(cache.iter_object_manifests(submit_state="pending"))
        assert len(rows) == 3
    finally:
        cache.close()


def test_upload_entity_objects_can_include_local_description(tmp_path):
    model = tmp_path / "asset.glb"
    model.write_bytes(b"glb")
    entity = ResourceProcessingEntity(
        resource_type="model",
        source_directory=str(tmp_path),
        source_resource_id="asset-desc",
        description_main="main text",
        description_detail="detail text",
        description_full="main text detail text",
        prompt_version="local-v1",
        files=[
            FileInfo(
                file_path=str(model),
                file_name=model.name,
                file_size=model.stat().st_size,
                file_format="glb",
                content_md5="abc",
            )
        ],
    )

    manifest_without_description = upload_entity_objects(
        entity,
        uploader=FakeUploader(),
        client_id="client",
        include_previews=False,
    )
    manifest_with_description = upload_entity_objects(
        entity,
        uploader=FakeUploader(),
        client_id="client",
        include_previews=False,
        include_descriptions=True,
    )

    assert manifest_without_description["provided_description"] is None
    assert manifest_with_description["provided_description"]["main_content"] == "main text"
    assert "full_description" not in manifest_with_description["provided_description"]
    assert manifest_with_description["provided_description"]["prompt_version"] == "local-v1"


def test_upload_entity_objects_preserves_safe_relative_paths(tmp_path):
    root = tmp_path / "pack root"
    nested = root / "中文 文件"
    nested.mkdir(parents=True)
    image = nested / "orig.png"
    image.write_bytes(b"png")
    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(root),
        source_resource_id="asset-rel",
        files=[
            FileInfo(
                file_path=str(image),
                file_name=image.name,
                file_size=image.stat().st_size,
                file_format="png",
                content_md5="abc",
            )
        ],
    )

    manifest = upload_entity_objects(
        entity,
        uploader=FakeUploader(),
        client_id="client",
        include_previews=False,
    )

    assert manifest["source_object"]["object_key"] == "client/files/asset-rel/中文 文件/orig.png"


def test_safe_object_path_part_preserves_utf8_and_cleans_cos_forbidden_chars():
    assert safe_object_path_part("中文 文件，tag.png") == "中文 文件，tag.png"
    assert safe_object_path_part(f"bad%0a{chr(24)}name\\x.png") == "bad_0a_name_x.png"


def test_upload_entity_objects_uploads_pack_as_single_zip(tmp_path):
    root = tmp_path / "pack"
    bg1 = root / "background 1"
    bg2 = root / "background 2"
    bg1.mkdir(parents=True)
    bg2.mkdir(parents=True)
    file1 = bg1 / "orig.png"
    file2 = bg2 / "orig.png"
    file1.write_bytes(b"one")
    file2.write_bytes(b"two")
    uploader = FakeUploader()
    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(root),
        source_resource_id="pack:001",
        title="中文资源包，Long Pack Name",
        files=[
            FileInfo(
                file_path=str(file1),
                file_name=file1.name,
                file_size=file1.stat().st_size,
                file_format="png",
                content_md5="one",
            ),
            FileInfo(
                file_path=str(file2),
                file_name=file2.name,
                file_size=file2.stat().st_size,
                file_format="png",
                content_md5="two",
            ),
        ],
    )

    manifest = upload_entity_objects(
        entity,
        uploader=uploader,
        client_id="client",
        include_previews=False,
    )

    assert len(manifest["source_files"]) == 2
    package = manifest["source_object"]
    assert package["file_name"] == "source.zip"
    assert package["object_key"] == "client/files/pack:001/source.zip"
    assert uploader.zip_entries_by_key[package["object_key"]] == [
        "background 1/orig.png",
        "background 2/orig.png",
    ]
