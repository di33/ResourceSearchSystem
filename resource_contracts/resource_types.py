"""Canonical resource type values shared by all ResourceUpload components."""

from __future__ import annotations

from enum import Enum


class ResourceType(str, Enum):
    PACK = "pack"
    ATLAS = "atlas"
    TILED_MAP = "tiled_map"
    TILED_TILESET = "tiled_tileset"
    SPINE_SKELETON = "spine_skeleton"
    SPRITER = "spriter"
    DRAGONBONES_SKELETON = "dragonbones_skeleton"
    FONT_FILE = "font_file"
    AUDIO_FILE = "audio_file"
    TILESET = "tileset"
    ANIMATION_SEQUENCE = "animation_sequence"
    SINGLE_IMAGE = "single_image"
    IMAGE = "image"
    MODEL = "model"
    MODEL_3D = "3d_model"
    MODEL_3D_ALT = "model_3d"
    DESIGN_FILE = "design_file"
    OTHER = "other"


PACK_RESOURCE_TYPE = ResourceType.PACK.value
ATLAS_RESOURCE_TYPE = ResourceType.ATLAS.value
TILED_MAP_RESOURCE_TYPE = ResourceType.TILED_MAP.value
TILED_TILESET_RESOURCE_TYPE = ResourceType.TILED_TILESET.value
SPINE_SKELETON_RESOURCE_TYPE = ResourceType.SPINE_SKELETON.value
SPRITER_RESOURCE_TYPE = ResourceType.SPRITER.value
DRAGONBONES_SKELETON_RESOURCE_TYPE = ResourceType.DRAGONBONES_SKELETON.value
FONT_FILE_RESOURCE_TYPE = ResourceType.FONT_FILE.value
AUDIO_FILE_RESOURCE_TYPE = ResourceType.AUDIO_FILE.value
TILESET_RESOURCE_TYPE = ResourceType.TILESET.value
ANIMATION_SEQUENCE_RESOURCE_TYPE = ResourceType.ANIMATION_SEQUENCE.value
SINGLE_IMAGE_RESOURCE_TYPE = ResourceType.SINGLE_IMAGE.value
IMAGE_RESOURCE_TYPE = ResourceType.IMAGE.value
MODEL_RESOURCE_TYPE = ResourceType.MODEL.value
MODEL_3D_RESOURCE_TYPE = ResourceType.MODEL_3D.value
MODEL_3D_ALT_RESOURCE_TYPE = ResourceType.MODEL_3D_ALT.value
DESIGN_FILE_RESOURCE_TYPE = ResourceType.DESIGN_FILE.value
OTHER_RESOURCE_TYPE = ResourceType.OTHER.value

CURRENT_RESOURCE_TYPES = (
    PACK_RESOURCE_TYPE,
    ATLAS_RESOURCE_TYPE,
    TILED_MAP_RESOURCE_TYPE,
    TILED_TILESET_RESOURCE_TYPE,
    SPINE_SKELETON_RESOURCE_TYPE,
    SPRITER_RESOURCE_TYPE,
    DRAGONBONES_SKELETON_RESOURCE_TYPE,
    FONT_FILE_RESOURCE_TYPE,
    AUDIO_FILE_RESOURCE_TYPE,
    TILESET_RESOURCE_TYPE,
    ANIMATION_SEQUENCE_RESOURCE_TYPE,
    SINGLE_IMAGE_RESOURCE_TYPE,
    OTHER_RESOURCE_TYPE,
)

LEGACY_GENERIC_RESOURCE_TYPES = (
    IMAGE_RESOURCE_TYPE,
    MODEL_RESOURCE_TYPE,
    MODEL_3D_RESOURCE_TYPE,
    MODEL_3D_ALT_RESOURCE_TYPE,
    DESIGN_FILE_RESOURCE_TYPE,
)

CANONICAL_RESOURCE_TYPES = CURRENT_RESOURCE_TYPES[:-1] + LEGACY_GENERIC_RESOURCE_TYPES + (OTHER_RESOURCE_TYPE,)
CANONICAL_RESOURCE_TYPE_SET = frozenset(CANONICAL_RESOURCE_TYPES)

RESOURCE_TYPE_ORDER = (
    PACK_RESOURCE_TYPE,
    TILED_MAP_RESOURCE_TYPE,
    ATLAS_RESOURCE_TYPE,
    SPINE_SKELETON_RESOURCE_TYPE,
    SPRITER_RESOURCE_TYPE,
    DRAGONBONES_SKELETON_RESOURCE_TYPE,
    TILESET_RESOURCE_TYPE,
    TILED_TILESET_RESOURCE_TYPE,
    ANIMATION_SEQUENCE_RESOURCE_TYPE,
    SINGLE_IMAGE_RESOURCE_TYPE,
    FONT_FILE_RESOURCE_TYPE,
    AUDIO_FILE_RESOURCE_TYPE,
    IMAGE_RESOURCE_TYPE,
    MODEL_RESOURCE_TYPE,
    MODEL_3D_RESOURCE_TYPE,
    MODEL_3D_ALT_RESOURCE_TYPE,
    DESIGN_FILE_RESOURCE_TYPE,
    OTHER_RESOURCE_TYPE,
)

TILESET_RESOURCE_TYPES = (
    TILESET_RESOURCE_TYPE,
    TILED_TILESET_RESOURCE_TYPE,
)

EXCLUDED_REPORT_RESOURCE_TYPES = (TILED_TILESET_RESOURCE_TYPE,)

RESOURCE_TYPE_DISPLAY_NAMES_ZH = {
    PACK_RESOURCE_TYPE: "资源包",
    ATLAS_RESOURCE_TYPE: "图集",
    TILED_MAP_RESOURCE_TYPE: "Tiled 地图",
    TILED_TILESET_RESOURCE_TYPE: "Tiled 图块定义",
    SPINE_SKELETON_RESOURCE_TYPE: "Spine 骨骼",
    SPRITER_RESOURCE_TYPE: "Spriter 骨骼",
    DRAGONBONES_SKELETON_RESOURCE_TYPE: "DragonBones 骨骼",
    FONT_FILE_RESOURCE_TYPE: "字体文件",
    AUDIO_FILE_RESOURCE_TYPE: "音频文件",
    TILESET_RESOURCE_TYPE: "瓦片集",
    ANIMATION_SEQUENCE_RESOURCE_TYPE: "动画序列",
    SINGLE_IMAGE_RESOURCE_TYPE: "单图",
    IMAGE_RESOURCE_TYPE: "图片",
    MODEL_RESOURCE_TYPE: "模型",
    MODEL_3D_RESOURCE_TYPE: "3D 模型",
    MODEL_3D_ALT_RESOURCE_TYPE: "3D 模型",
    DESIGN_FILE_RESOURCE_TYPE: "设计文件",
    OTHER_RESOURCE_TYPE: "其他",
}

RESOURCE_TYPE_DISPLAY_NAMES_EN = {
    PACK_RESOURCE_TYPE: "Packs",
    ATLAS_RESOURCE_TYPE: "Atlases",
    TILED_MAP_RESOURCE_TYPE: "Tiled Maps",
    TILED_TILESET_RESOURCE_TYPE: "Tiled Tilesets",
    SPINE_SKELETON_RESOURCE_TYPE: "Spine Skeletons",
    SPRITER_RESOURCE_TYPE: "Spriter Animations",
    DRAGONBONES_SKELETON_RESOURCE_TYPE: "DragonBones Skeletons",
    FONT_FILE_RESOURCE_TYPE: "Fonts",
    AUDIO_FILE_RESOURCE_TYPE: "Audio",
    TILESET_RESOURCE_TYPE: "Tilesets",
    ANIMATION_SEQUENCE_RESOURCE_TYPE: "Animation Sequences",
    SINGLE_IMAGE_RESOURCE_TYPE: "Single Images",
    IMAGE_RESOURCE_TYPE: "Images",
    MODEL_RESOURCE_TYPE: "Models",
    MODEL_3D_RESOURCE_TYPE: "3D Models",
    MODEL_3D_ALT_RESOURCE_TYPE: "3D Models",
    DESIGN_FILE_RESOURCE_TYPE: "Design Files",
    OTHER_RESOURCE_TYPE: "Other",
}


def normalize_resource_type(value: str | ResourceType | None, *, allow_unknown: bool = False) -> str:
    """Return the normalized resource type value, or an empty string when unknown."""
    if value is None:
        return ""
    text = str(value.value if isinstance(value, ResourceType) else value).strip().lower()
    if text in CANONICAL_RESOURCE_TYPE_SET:
        return text
    return text if allow_unknown else ""


def is_resource_type(value: str | ResourceType | None) -> bool:
    return bool(normalize_resource_type(value))


def is_pack_resource_type(value: str | ResourceType | None) -> bool:
    return normalize_resource_type(value) == PACK_RESOURCE_TYPE


def is_search_indexable_resource_type(value: str | ResourceType | None) -> bool:
    """Return whether resources of this type should get previews/descriptions/search vectors."""
    normalized = normalize_resource_type(value, allow_unknown=True)
    return bool(normalized) and normalized != PACK_RESOURCE_TYPE


def resource_type_label(value: str | ResourceType | None, *, locale: str = "zh") -> str:
    normalized = normalize_resource_type(value, allow_unknown=True)
    if not normalized:
        return ""
    labels = RESOURCE_TYPE_DISPLAY_NAMES_EN if locale.lower().startswith("en") else RESOURCE_TYPE_DISPLAY_NAMES_ZH
    return labels.get(normalized, normalized)
