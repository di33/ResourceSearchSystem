"""Tests for ResourceProcessor.preview_metadata data structures."""

from ResourceProcessor.preview_metadata import (
    FileInfo,
    PreviewInfo,
    PreviewStrategy,
    ProcessState,
    ResourceProcessingEntity,
)


def test_preview_strategy_values():
    assert PreviewStrategy.STATIC == "static"
    assert PreviewStrategy.GIF == "gif"
    assert PreviewStrategy.CONTACT_SHEET == "contact_sheet"
    assert set(PreviewStrategy) == {
        PreviewStrategy.STATIC,
        PreviewStrategy.GIF,
        PreviewStrategy.CONTACT_SHEET,
    }


def test_process_state_values():
    expected = {
        "discovered",
        "preview_ready",
        "preview_failed",
        "description_ready",
        "description_failed",
        "embedding_ready",
        "embedding_failed",
        "package_ready",
        "registered",
        "uploaded",
        "committed",
        "synced",
    }
    actual = {s.value for s in ProcessState}
    assert actual == expected


def test_preview_info_to_dict_roundtrip():
    original = PreviewInfo(
        strategy=PreviewStrategy.STATIC,
        path="/tmp/preview.webp",
        format="webp",
        width=512,
        height=256,
        size=12345,
        renderer="pillow",
        used_placeholder=False,
        fail_reason=None,
    )
    d = original.to_dict()
    restored = PreviewInfo.from_dict(d)
    assert restored == original
    assert isinstance(restored.strategy, PreviewStrategy)


def test_preview_info_from_dict_handles_string_strategy():
    d = {
        "strategy": "static",
        "path": "/tmp/img.webp",
        "format": "webp",
        "width": 100,
        "height": 100,
        "size": 500,
        "renderer": "pillow",
        "used_placeholder": False,
        "fail_reason": None,
    }
    info = PreviewInfo.from_dict(d)
    assert info.strategy is PreviewStrategy.STATIC
    assert isinstance(info.strategy, PreviewStrategy)


def test_resource_entity_to_dict_roundtrip():
    entity = ResourceProcessingEntity(
        content_md5="abc123",
        resource_type="image",
        source_directory="/src",
        files=[FileInfo(
            file_path="/src/photo.png",
            file_name="photo.png",
            file_size=98765,
            file_format="png",
            content_md5="abc123",
            is_primary=True,
        )],
        process_state=ProcessState.PREVIEW_READY,
        retry_count=2,
        last_error_code="E001",
        last_error_message="timeout",
        updated_at="2025-01-01T00:00:00Z",
    )
    d = entity.to_dict()
    restored = ResourceProcessingEntity.from_dict(d)
    assert restored == entity
    assert isinstance(restored.process_state, ProcessState)


def test_resource_entity_with_preview():
    preview = PreviewInfo(
        strategy=PreviewStrategy.GIF,
        path="/tmp/model_preview.gif",
        format="gif",
        width=256,
        height=256,
        size=54321,
        renderer="blender",
        used_placeholder=False,
    )
    entity = ResourceProcessingEntity(
        content_md5="def456",
        resource_type="3d_model",
        source_directory="/src",
        files=[FileInfo(
            file_path="/src/model.fbx",
            file_name="model.fbx",
            file_size=1_000_000,
            file_format="fbx",
            content_md5="def456",
            file_role="model",
            is_primary=True,
        )],
        process_state=ProcessState.PREVIEW_READY,
        previews=[preview],
    )
    d = entity.to_dict()
    restored = ResourceProcessingEntity.from_dict(d)
    assert restored == entity
    assert len(restored.previews) == 1
    assert isinstance(restored.previews[0], PreviewInfo)
    assert restored.previews[0].strategy is PreviewStrategy.GIF
    assert restored.previews[0].renderer == "blender"


def test_resource_entity_default_state():
    entity = ResourceProcessingEntity(
        resource_type="image",
        source_directory="/src",
        content_md5="aaa",
    )
    assert entity.process_state is ProcessState.DISCOVERED


def test_preview_info_fail_state():
    info = PreviewInfo(
        strategy=PreviewStrategy.STATIC,
        fail_reason="all-black image detected",
    )
    assert info.fail_reason == "all-black image detected"
    assert info.path is None
    assert info.width is None

    d = info.to_dict()
    restored = PreviewInfo.from_dict(d)
    assert restored.fail_reason == "all-black image detected"
    assert restored.strategy is PreviewStrategy.STATIC


# ---------------------------------------------------------------------------
# Multi-file / multi-preview tests
# ---------------------------------------------------------------------------


def test_file_info_to_dict_roundtrip():
    fi = FileInfo(
        file_path="/tmp/texture.png",
        file_name="texture.png",
        file_size=4096,
        file_format="png",
        content_md5="aaabbb",
        file_role="texture",
        is_primary=False,
    )
    d = fi.to_dict()
    restored = FileInfo.from_dict(d)
    assert restored == fi


def test_entity_multi_file_multi_preview_roundtrip():
    """Resource with multiple files and multiple previews survives serialization."""
    files = [
        FileInfo("/src/model.fbx", "model.fbx", 500_000, "fbx", "md5_a", "model", True),
        FileInfo("/src/diffuse.png", "diffuse.png", 200_000, "png", "md5_b", "texture", False),
        FileInfo("/src/normal.png", "normal.png", 150_000, "png", "md5_c", "texture", False),
    ]
    previews = [
        PreviewInfo(strategy=PreviewStrategy.GIF, role="primary", path="/p/model.gif",
                    format="gif", width=512, height=512, size=30000, renderer="blender"),
        PreviewInfo(strategy=PreviewStrategy.STATIC, role="gallery", path="/p/diffuse.webp",
                    format="webp", width=256, height=256, size=8000, renderer="pillow"),
    ]
    entity = ResourceProcessingEntity(
        resource_type="3d_model",
        source_directory="/src",
        files=files,
        content_md5="composite_md5",
        previews=previews,
    )
    d = entity.to_dict()
    restored = ResourceProcessingEntity.from_dict(d)
    assert len(restored.files) == 3
    assert len(restored.previews) == 2
    assert restored.primary_file.file_name == "model.fbx"
    assert restored.previews[0].role == "primary"
    assert restored.previews[1].role == "gallery"
    assert restored == entity


def test_entity_primary_file_property():
    """primary_file returns the file with is_primary=True, or first file as fallback."""
    f1 = FileInfo("/a.png", "a.png", 100, "png", "m1", "main", False)
    f2 = FileInfo("/b.fbx", "b.fbx", 200, "fbx", "m2", "model", True)
    entity = ResourceProcessingEntity(
        resource_type="3d_model", source_directory="/", files=[f1, f2])
    assert entity.primary_file == f2

    no_primary = ResourceProcessingEntity(
        resource_type="image", source_directory="/", files=[f1])
    assert no_primary.primary_file == f1

    empty = ResourceProcessingEntity(
        resource_type="other", source_directory="/")
    assert empty.primary_file is None


def test_preview_info_role_field():
    """PreviewInfo role field defaults to 'primary' and roundtrips."""
    p = PreviewInfo(strategy=PreviewStrategy.STATIC, role="gallery")
    assert p.role == "gallery"
    d = p.to_dict()
    assert d["role"] == "gallery"
    restored = PreviewInfo.from_dict(d)
    assert restored.role == "gallery"

    default = PreviewInfo(strategy=PreviewStrategy.STATIC)
    assert default.role == "primary"


def test_backward_compat_old_single_file_format():
    """from_dict migrates old source_path/source_name/source_size/source_format into files."""
    d = {
        "content_md5": "old_md5",
        "resource_type": "image",
        "source_directory": "/old",
        "source_path": "/old/pic.jpg",
        "source_name": "pic.jpg",
        "source_size": 9999,
        "source_format": "jpg",
        "process_state": "discovered",
    }
    entity = ResourceProcessingEntity.from_dict(d)
    assert len(entity.files) == 1
    assert entity.files[0].file_name == "pic.jpg"
    assert entity.files[0].file_size == 9999
    assert entity.files[0].is_primary is True


def test_backward_compat_old_single_preview():
    """from_dict migrates old single 'preview' dict into previews list."""
    d = {
        "content_md5": "md5",
        "resource_type": "image",
        "source_directory": "/",
        "files": [{"file_path": "/a.png", "file_name": "a.png", "file_size": 100,
                    "file_format": "png", "content_md5": "md5"}],
        "preview": {
            "strategy": "static",
            "path": "/p/a.webp",
            "format": "webp",
            "width": 128,
            "height": 128,
            "size": 500,
            "renderer": "pillow",
            "used_placeholder": False,
            "fail_reason": None,
        },
        "process_state": "preview_ready",
    }
    entity = ResourceProcessingEntity.from_dict(d)
    assert len(entity.previews) == 1
    assert entity.previews[0].strategy == PreviewStrategy.STATIC
    assert entity.previews[0].path == "/p/a.webp"


def test_process_state_transition_coverage():
    """Verify the state enum covers the full happy path and all failure branches."""
    happy_path = [
        ProcessState.DISCOVERED,
        ProcessState.PREVIEW_READY,
        ProcessState.DESCRIPTION_READY,
        ProcessState.EMBEDDING_READY,
        ProcessState.PACKAGE_READY,
        ProcessState.REGISTERED,
        ProcessState.UPLOADED,
        ProcessState.COMMITTED,
        ProcessState.SYNCED,
    ]
    for i in range(len(happy_path) - 1):
        assert happy_path[i] != happy_path[i + 1]
    assert happy_path[0] is ProcessState.DISCOVERED
    assert happy_path[-1] is ProcessState.SYNCED

    failure_states = {
        ProcessState.PREVIEW_FAILED,
        ProcessState.DESCRIPTION_FAILED,
        ProcessState.EMBEDDING_FAILED,
    }
    all_states = set(happy_path) | failure_states
    assert all_states == set(ProcessState)
