"""Tests for ResourceProcessor.preview_metadata data structures."""

from ResourceProcessor.preview_metadata import (
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
        source_path="/src/photo.png",
        source_name="photo.png",
        source_size=98765,
        source_format="png",
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
        source_path="/src/model.fbx",
        source_name="model.fbx",
        source_size=1_000_000,
        source_format="fbx",
        process_state=ProcessState.PREVIEW_READY,
        preview=preview,
    )
    d = entity.to_dict()
    restored = ResourceProcessingEntity.from_dict(d)
    assert restored == entity
    assert isinstance(restored.preview, PreviewInfo)
    assert restored.preview.strategy is PreviewStrategy.GIF
    assert restored.preview.renderer == "blender"


def test_resource_entity_default_state():
    entity = ResourceProcessingEntity(
        content_md5="aaa",
        resource_type="image",
        source_path="/src/img.png",
        source_name="img.png",
        source_size=100,
        source_format="png",
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
