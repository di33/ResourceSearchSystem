import json
from pathlib import Path

from PIL import Image

from ResourceProcessor.crawler.catalog_loader import load_crawler_catalog
from ResourceProcessor.crawler.resource_adapter import (
    build_description_input,
    build_processing_entity,
    compute_resource_fingerprint,
)


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _make_png(path: Path, color: str = "red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", (64, 64), color=color) as img:
        img.save(path)


def test_catalog_loader_and_adapter(tmp_path):
    output_root = tmp_path / "output"
    pack_name = "Demo Pack"
    image_rel = "sprites/hero.png"
    image_abs = output_root / "assets" / "kenney" / pack_name / image_rel
    _make_png(image_abs)

    _write_jsonl(
        output_root / "metadata" / "resource_index.jsonl",
        [
            {
                "id": "res-hero",
                "source": "kenney",
                "pack_id": "pack-hero",
                "pack_name": pack_name,
                "resource_type": "single_image",
                "title": "Hero",
                "resource_path": image_rel,
                "member_count": 1,
                "file_paths": [image_rel],
                "asset_ids": ["asset-hero"],
                "tags": ["character", "pixel"],
                "description": "Main hero sprite",
                "category": "sprites",
                "license": "CC0",
                "parent_resource_id": "res-pack",
            },
            {
                "id": "res-missing",
                "source": "kenney",
                "pack_id": "pack-hero",
                "pack_name": pack_name,
                "resource_type": "audio_file",
                "title": "Coin",
                "resource_path": "audio/coin.ogg",
                "member_count": 1,
                "file_paths": ["audio/coin.ogg"],
                "asset_ids": ["asset-coin"],
                "tags": ["ui"],
                "description": "Coin pickup",
                "category": "audio",
                "license": "CC0",
            },
            {
                "id": "res-pack",
                "source": "kenney",
                "pack_name": pack_name,
                "resource_type": "pack",
                "title": "Demo Pack",
                "resource_path": "",
                "member_count": 2,
                "file_paths": [image_rel],
                "asset_ids": ["asset-hero"],
                "tags": ["bundle"],
                "description": "Whole pack",
                "category": "sprites",
                "license": "CC0",
                "child_resource_ids": ["res-hero", "res-missing"],
                "child_resource_count": 2,
                "contains_resource_types": ["single_image", "audio_file"],
            },
        ],
    )
    _write_jsonl(
        output_root / "metadata" / "index.jsonl",
        [
            {
                "id": "asset-hero",
                "source": "kenney",
                "source_pack": pack_name,
                "file_path": image_rel,
                "metadata": {"format": "png", "style": "pixel-art", "theme": "fantasy"},
            },
            {
                "id": "asset-coin",
                "source": "kenney",
                "source_pack": pack_name,
                "file_path": "audio/coin.ogg",
                "metadata": {"format": "ogg"},
            },
        ],
    )
    pack_json = output_root / "metadata" / "kenney" / f"{pack_name}.json"
    pack_json.parent.mkdir(parents=True, exist_ok=True)
    pack_json.write_text(
        json.dumps(
            {
                "pack": {
                    "description": "Pack description",
                    "tags": ["pack-tag"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    catalog = load_crawler_catalog(str(output_root))
    records = list(catalog.iter_resources())
    assert len(records) == 3
    assert records[0].resolved_files == [str(image_abs.resolve())]
    assert records[1].missing_files == ["audio/coin.ogg"]
    assert records[2].child_resource_ids == ["res-hero", "res-missing"]

    entity = build_processing_entity(records[0])
    assert entity.pack_name == pack_name
    assert entity.title == "Hero"
    assert entity.tags == ["character", "pixel", "pack-tag"]
    assert entity.source_resource_id == "res-hero"
    assert entity.parent_resource_id == "res-pack"
    assert entity.files[0].is_primary is True
    assert entity.auxiliary_metadata["styles"] == {"pixel-art": 1}
    assert entity.auxiliary_metadata["themes"] == {"fantasy": 1}
    assert compute_resource_fingerprint(records[0]) == entity.content_md5

    desc_input = build_description_input(entity)
    context = desc_input.to_prompt_context()
    assert "资源包: Demo Pack" in context
    assert "资源路径: sprites/hero.png" in context
    assert "来源标签: character, pixel, pack-tag" in context

    missing_entity = build_processing_entity(records[1])
    assert missing_entity.files == []
    assert missing_entity.missing_files == ["audio/coin.ogg"]

    pack_entity = build_processing_entity(records[2])
    assert pack_entity.child_resource_ids == ["res-hero", "res-missing"]
    assert pack_entity.child_resource_count == 2
    assert pack_entity.contains_resource_types == ["single_image", "audio_file"]
