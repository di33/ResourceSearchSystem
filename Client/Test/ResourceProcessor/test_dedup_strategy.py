"""Tests for ResourceProcessor.dedup_strategy – dedup, reuse & incremental rerun."""

from ResourceProcessor.dedup_strategy import (
    DedupResult,
    ProcessingConfig,
    ReuseDecision,
    check_dedup,
    get_resumable_tasks,
    get_retry_candidates,
)
from ResourceProcessor.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import (
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


def _build_committed_task(store: LocalCacheStore, **entity_overrides) -> int:
    """Insert a task and advance it to COMMITTED with description & embedding."""
    entity = _make_entity(**entity_overrides)
    task_id = store.insert_task(entity)
    store.update_task_state(task_id, ProcessState.COMMITTED)
    store.insert_description(
        task_id,
        main_content="test",
        detail_content="detail",
        full_description="full",
        prompt_version="prompt_v1",
    )
    store.insert_embedding(
        task_id,
        dimension=768,
        checksum="sha256abc",
        generate_time=0.5,
        model_version="mock_embed_v1",
    )
    return task_id


# ---- 1. test_check_dedup_new_resource ----


def test_check_dedup_new_resource(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        config = ProcessingConfig()
        result = check_dedup(store, "never_seen_md5", config)
        assert result.decision == ReuseDecision.NEW
        assert result.existing_task_id is None
    finally:
        store.close()


# ---- 2. test_check_dedup_reuse_all ----


def test_check_dedup_reuse_all(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = _build_committed_task(store, content_md5="md5_reuse")
        config = ProcessingConfig()
        result = check_dedup(store, "md5_reuse", config)
        assert result.decision == ReuseDecision.REUSE_ALL
        assert result.existing_task_id == task_id
    finally:
        store.close()


# ---- 3. test_check_dedup_rerun_description_on_prompt_change ----


def test_check_dedup_rerun_description_on_prompt_change(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        _build_committed_task(store, content_md5="md5_prompt")
        config = ProcessingConfig(prompt_version="prompt_v2")
        result = check_dedup(store, "md5_prompt", config)
        assert result.decision == ReuseDecision.RERUN_DESCRIPTION
    finally:
        store.close()


# ---- 4. test_check_dedup_rerun_embedding_on_model_change ----


def test_check_dedup_rerun_embedding_on_model_change(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        _build_committed_task(store, content_md5="md5_embed")
        config = ProcessingConfig(embedding_model_version="new_embed_v2")
        result = check_dedup(store, "md5_embed", config)
        assert result.decision == ReuseDecision.RERUN_EMBEDDING
    finally:
        store.close()


# ---- 5. test_check_dedup_resume_from_preview_ready ----


def test_check_dedup_resume_from_preview_ready(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity(content_md5="md5_preview"))
        store.update_task_state(task_id, ProcessState.PREVIEW_READY)
        config = ProcessingConfig()
        result = check_dedup(store, "md5_preview", config)
        assert result.decision == ReuseDecision.RESUME
        assert result.existing_task_id == task_id
    finally:
        store.close()


# ---- 6. test_check_dedup_resume_from_failed_state ----


def test_check_dedup_resume_from_failed_state(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        task_id = store.insert_task(_make_entity(content_md5="md5_fail"))
        store.update_task_state(
            task_id, ProcessState.DESCRIPTION_FAILED, "E01", "timeout"
        )
        config = ProcessingConfig()
        result = check_dedup(store, "md5_fail", config)
        assert result.decision == ReuseDecision.RESUME
        assert result.existing_task_id == task_id
    finally:
        store.close()


# ---- 7. test_check_dedup_uses_latest_task ----


def test_check_dedup_uses_latest_task(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        old_id = store.insert_task(_make_entity(content_md5="md5_multi"))
        store.update_task_state(old_id, ProcessState.PREVIEW_FAILED, "E01", "err")

        new_id = store.insert_task(_make_entity(content_md5="md5_multi"))
        store.update_task_state(new_id, ProcessState.PREVIEW_READY)

        config = ProcessingConfig()
        result = check_dedup(store, "md5_multi", config)
        assert result.existing_task_id == new_id
        assert result.decision == ReuseDecision.RESUME
    finally:
        store.close()


# ---- 8. test_check_dedup_filename_change_not_new ----


def test_check_dedup_filename_change_not_new(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        _build_committed_task(
            store,
            content_md5="md5_rename",
            source_name="old_name.png",
            source_path="/tmp/old_name.png",
        )
        config = ProcessingConfig()
        result = check_dedup(store, "md5_rename", config)
        assert result.decision != ReuseDecision.NEW
        assert result.decision == ReuseDecision.REUSE_ALL
    finally:
        store.close()


# ---- 9. test_get_resumable_tasks ----


def test_get_resumable_tasks(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        id1 = store.insert_task(_make_entity(content_md5="a"))
        id2 = store.insert_task(_make_entity(content_md5="b"))
        id3 = store.insert_task(_make_entity(content_md5="c"))
        id4 = store.insert_task(_make_entity(content_md5="d"))

        store.update_task_state(id2, ProcessState.PREVIEW_READY)
        store.update_task_state(id3, ProcessState.DESCRIPTION_FAILED, "E01", "err")
        store.update_task_state(id4, ProcessState.COMMITTED)

        resumable = get_resumable_tasks(store)
        resumable_ids = {t["id"] for t in resumable}
        assert id1 in resumable_ids  # DISCOVERED
        assert id2 in resumable_ids  # PREVIEW_READY
        assert id3 in resumable_ids  # DESCRIPTION_FAILED
        assert id4 not in resumable_ids  # COMMITTED is not resumable
    finally:
        store.close()


# ---- 10. test_get_retry_candidates_respects_max ----


def test_get_retry_candidates_respects_max(tmp_path):
    store = LocalCacheStore(str(tmp_path / "test.db"))
    try:
        id1 = store.insert_task(_make_entity(content_md5="a"))
        id2 = store.insert_task(_make_entity(content_md5="b"))

        store.update_task_state(id1, ProcessState.PREVIEW_FAILED, "E01", "err")
        store.update_task_state(id2, ProcessState.PREVIEW_FAILED, "E01", "err")

        for _ in range(3):
            store.increment_retry(id1)

        candidates = get_retry_candidates(store, max_retries=3)
        candidate_ids = {t["id"] for t in candidates}
        assert id1 not in candidate_ids  # retry_count == 3, not < 3
        assert id2 in candidate_ids  # retry_count == 0
    finally:
        store.close()


# ---- 11. test_processing_config_defaults ----


def test_processing_config_defaults():
    config = ProcessingConfig()
    assert config.prompt_version == "prompt_v1"
    assert config.embedding_model_version == "mock_embed_v1"
    assert config.preview_max_size == 512
    assert config.preview_format_priority == "webp"


# ---- 12. test_reuse_decision_enum_values ----


def test_reuse_decision_enum_values():
    assert ReuseDecision.NEW == "new"
    assert ReuseDecision.REUSE_ALL == "reuse_all"
    assert ReuseDecision.RERUN_DESCRIPTION == "rerun_description"
    assert ReuseDecision.RERUN_EMBEDDING == "rerun_embedding"
    assert ReuseDecision.RESUME == "resume"
