"""Tests for pack description aggregation from child descriptions."""

import asyncio

import pytest

from ResourceProcessor.generate_descriptions import _process_one
from ResourceProcessor.description.pack_description_builder import PackChildDescriptionsNotReadyError
from ResourceProcessor.description.pack_description_builder import (
    build_description_input_for_generation,
    build_pack_description_input,
    build_pack_prompt_context,
    summarize_child_descriptions,
)
from ResourceProcessor.preview_metadata import ProcessState, ResourceProcessingEntity


def _row(task_id, title, main, detail, resource_type="single_image"):
    return {
        "task_id": task_id,
        "source_resource_id": f"child-{task_id}",
        "resource_type": resource_type,
        "title": title,
        "resource_path": f"assets/{title}",
        "main_content": main,
        "detail_content": detail,
        "quality_score": 0.8,
    }


@pytest.mark.asyncio
async def test_summarize_child_descriptions_clusters_similar_rows(monkeypatch):
    monkeypatch.setenv("PACK_DESCRIPTION_SIMILARITY_THRESHOLD", "0.9")
    monkeypatch.setenv("PACK_DESCRIPTION_MAX_CHILD_EXAMPLES", "10")
    rows = [
        _row(1, "sword.png", "像素剑客攻击图标", "适合技能栏中的近战攻击。"),
        _row(2, "sword_alt.png", "像素剑客攻击图标变体", "适合技能栏中的近战攻击按钮。"),
        _row(3, "forest.png", "像素森林地形瓦片", "适合2D地图环境拼接。", "tileset"),
    ]

    async def fake_embedder(texts):
        assert len(texts) == 3
        return [
            [1.0, 0.0],
            [0.95, 0.05],
            [1.0, 0.0],
        ]

    summary = await summarize_child_descriptions(rows, embedder=fake_embedder)

    assert summary.child_description_count == 3
    assert summary.embedding_input_count == 3
    assert summary.semantic_cluster_count == 2
    assert [cluster.count for cluster in summary.selected_clusters] == [2, 1]
    assert [item.resource_type for item in summary.type_summaries] == ["single_image", "tileset"]
    assert summary.embedding_seconds >= 0
    assert summary.clustering_seconds >= 0


@pytest.mark.asyncio
async def test_pack_prompt_context_summarizes_selected_clusters(monkeypatch):
    monkeypatch.setenv("PACK_DESCRIPTION_SIMILARITY_THRESHOLD", "0.9")
    rows = [
        _row(1, "coin_01.png", "金币UI图标", "适合奖励、商店或背包货币显示。"),
        _row(2, "coin_02.png", "金币UI图标", "适合奖励、商店或背包货币显示。"),
        _row(3, "potion.png", "红色药水道具图标", "适合背包和消耗品栏。"),
    ]

    async def fake_embedder(texts):
        assert len(texts) == 3
        return [
            [1.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
        ]

    summary = await summarize_child_descriptions(rows, embedder=fake_embedder)
    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory="/packs/icons",
        content_md5="pack-md5",
        title="Game Icons",
        pack_name="Game Icons",
        source="itch",
        child_resource_count=3,
        contains_resource_types=["single_image"],
    )

    context = build_pack_prompt_context(entity, summary)

    assert "资源类型: pack" in context
    assert "有描述的子资源数: 3" in context
    assert "embedding输入描述数: 3" in context
    assert "embedding语义聚类数: 2" in context
    assert "类型: single_image" in context
    assert "中心样本" in context
    assert "差异样本" in context
    assert "覆盖 2 个相似子资源" in context
    assert "每组展示覆盖数量、标题示例、中心样本和差异样本" in context


@pytest.mark.asyncio
async def test_build_pack_description_input_uses_child_summary(monkeypatch):
    monkeypatch.setenv("PACK_DESCRIPTION_SIMILARITY_THRESHOLD", "0.9")
    monkeypatch.setenv("PACK_DESCRIPTION_PROMPT_VERSION", "test_pack_prompt_v1")

    class FakeCache:
        def get_pack_child_description_rows(self, task_id, *, pack_source_resource_id, child_source_ids):
            assert task_id == 10
            assert pack_source_resource_id == "pack-src"
            assert child_source_ids == ["child-1"]
            return [
                _row(1, "hero.png", "像素英雄角色帧", "适合RPG主角行走和攻击动画。")
            ]

    async def fake_embedder(texts):
        return [[1.0, 0.0]]

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory="/packs/hero",
        content_md5="pack-md5",
        title="Hero Pack",
        source_resource_id="pack-src",
        child_resource_ids=["child-1"],
        child_resource_count=1,
    )

    desc_input = await build_pack_description_input(
        FakeCache(),
        10,
        entity,
        embedder=fake_embedder,
    )

    assert desc_input is not None
    assert desc_input.description_prompt_env == "LLM_PACK_DESCRIPTION_PROMPT"
    assert desc_input.prompt_version_tag == "test_pack_prompt_v1"
    assert desc_input.attach_llm_media is False
    assert desc_input.resolved_llm_input_type == "text"
    assert desc_input.resolved_llm_input_paths == []
    prompt_context = desc_input.to_prompt_context()
    assert "子资源类型概览" in prompt_context
    assert "代表语义组" in prompt_context


@pytest.mark.asyncio
async def test_build_pack_description_input_logs_timing(monkeypatch, capsys):
    monkeypatch.setenv("PACK_DESCRIPTION_TIMING_LOG", "1")

    class FakeCache:
        def get_pack_child_description_rows(self, task_id, *, pack_source_resource_id, child_source_ids):
            return [
                _row(1, "hero.png", "像素英雄角色帧", "适合RPG主角行走和攻击动画。")
            ]

    async def fake_embedder(texts):
        return [[1.0, 0.0]]

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory="/packs/hero",
        content_md5="pack-md5",
        title="Hero Pack",
        source_resource_id="pack-src",
        child_resource_ids=["child-1"],
        child_resource_count=1,
    )

    await build_pack_description_input(FakeCache(), 10, entity, embedder=fake_embedder)

    captured = capsys.readouterr()
    assert "[PACK_DESC]" in captured.err
    assert "status=ready" in captured.err
    assert "embedding=" in captured.err
    assert "cluster=" in captured.err
    assert "total=" in captured.err


@pytest.mark.asyncio
async def test_pack_without_child_descriptions_fails_instead_of_fallback():
    class EmptyCache:
        def get_pack_child_description_rows(self, task_id, *, pack_source_resource_id, child_source_ids):
            return []

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory="/packs/empty",
        content_md5="pack-md5",
        title="Empty Pack",
        source_resource_id="pack-src",
        child_resource_ids=["child-1"],
        child_resource_count=1,
    )

    with pytest.raises(ValueError, match="child descriptions are not ready"):
        await build_description_input_for_generation(EmptyCache(), 10, entity)


@pytest.mark.asyncio
async def test_pack_with_partial_child_descriptions_fails(monkeypatch):
    monkeypatch.setenv("PACK_DESCRIPTION_REQUIRE_ALL_CHILD_DESCRIPTIONS", "true")

    class PartialCache:
        def get_pack_child_description_rows(self, task_id, *, pack_source_resource_id, child_source_ids):
            return [
                _row(1, "hero.png", "像素英雄角色帧", "适合RPG主角动画。")
            ]

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory="/packs/partial",
        content_md5="pack-md5",
        title="Partial Pack",
        source_resource_id="pack-src",
        child_resource_ids=["child-1", "child-2"],
        child_resource_count=2,
    )

    with pytest.raises(ValueError, match="child descriptions are not ready"):
        await build_description_input_for_generation(PartialCache(), 10, entity)


@pytest.mark.asyncio
async def test_pack_child_description_failure_does_not_keep_existing_description(monkeypatch):
    class FakeCache:
        def __init__(self):
            self.state = None
            self.error_message = ""

        def get_description_by_task(self, task_id):
            return {"full_description": "old pack description"}

        def update_task_state(self, task_id, state, error_code="", error_message=""):
            self.state = state
            self.error_message = error_message

    class FakeReport:
        def __init__(self):
            self.ok_steps = []
            self.fail_steps = []

        def ok(self, step, detail=""):
            self.ok_steps.append((step, detail))

        def fail(self, step, detail=""):
            self.fail_steps.append((step, detail))

    async def fail_generation(*args, **kwargs):
        raise PackChildDescriptionsNotReadyError("child descriptions are not ready")

    monkeypatch.setattr(
        "ResourceProcessor.generate_descriptions._generate_with_retry",
        fail_generation,
    )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory="/packs/hero",
        content_md5="pack-md5",
        title="Hero Pack",
    )
    cache = FakeCache()
    report = FakeReport()
    counters = {"processed": 0, "desc_ok": 0, "failed": 0}

    await _process_one(
        10,
        entity,
        "mock",
        "",
        cache,
        report,
        semaphore=asyncio.Semaphore(1),
        counters=counters,
    )

    assert cache.state == ProcessState.DESCRIPTION_FAILED
    assert "child descriptions are not ready" in cache.error_message
    assert counters["failed"] == 1
    assert "fallback_existing" not in counters
    assert report.ok_steps == []
    assert report.fail_steps
