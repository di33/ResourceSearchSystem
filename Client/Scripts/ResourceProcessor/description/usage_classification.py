"""Usage classification schema and parsers for game resources."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CLASSIFICATION_RULE_VERSION = "game_resource_usage_v1.1"

SPACE_FORMS = ["2D", "3D", "通用", "混合", "不确定"]

USAGE_TAXONOMY: dict[str, list[str]] = {
    "角色": ["人物", "动物", "怪物", "机器人", "机甲", "角色部件", "服装", "动作", "表情", "其他"],
    "环境": ["建筑与结构", "地形", "自然景观", "植被", "水体", "天空", "道路", "室内场景", "场景背景", "其他"],
    "界面": ["图标", "按钮", "面板", "菜单", "弹窗", "HUD", "状态条", "输入控件", "提示", "其他"],
    "物件": ["道具", "装备", "武器", "载具", "家具", "机关", "可交互物", "摆件", "其他"],
    "特效": ["环境特效", "技能与攻击特效", "弹道特效", "命中特效", "状态特效", "界面特效", "转场特效", "其他"],
    "文字与字体": ["字体", "字形集", "标题字", "数字字形", "其他"],
    "音频": ["音乐", "环境声", "界面音效", "战斗音效", "技能音效", "交互音效", "角色语音", "音效", "其他"],
    "其他": ["占位/辅助资源", "其他"],
}

USAGE_CATEGORIES = list(USAGE_TAXONOMY)


CLASSIFICATION_RULES_PATH = Path(__file__).with_name("usage_classification_rules.md")
CLASSIFICATION_RULE_PROMPT = CLASSIFICATION_RULES_PATH.read_text(encoding="utf-8").strip()
CLASSIFICATION_RUNTIME_PROMPT_PATH = Path(__file__).with_name("usage_classification_prompt.md")
CLASSIFICATION_RUNTIME_PROMPT = CLASSIFICATION_RUNTIME_PROMPT_PATH.read_text(encoding="utf-8").strip()


@dataclass
class UsageClassification:
    space: str = ""
    category: str = ""
    subcategories: list[str] = field(default_factory=list)
    reason: str = ""
    suggestion: dict[str, Any] | None = None
    version: str = CLASSIFICATION_RULE_VERSION

    def to_result_kwargs(self) -> dict[str, Any]:
        return {
            "usage_space": self.space,
            "usage_category": self.category,
            "usage_subcategories": list(self.subcategories),
            "usage_classification_reason": self.reason,
            "usage_classification_suggestion": self.suggestion,
            "usage_classification_version": self.version,
        }


def _strip_code_fence(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced JSON object found in a loose model response."""
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _loads_json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        candidate = _extract_json_object(text)
        if not candidate:
            return None
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in re.split(r"[,，/、]", value) if part.strip()]
    return []


def _remove_redundant_other(subcategories: list[str]) -> list[str]:
    if "其他" not in subcategories or len(subcategories) <= 1:
        return subcategories
    return [item for item in subcategories if item != "其他"]


def normalize_classification(value: Any) -> UsageClassification:
    if not isinstance(value, dict):
        return UsageClassification(
            space="不确定",
            reason="模型未返回结构化用途分类；仅保留描述结果。",
        )

    space = str(value.get("space") or value.get("spatial_form") or "").strip()
    if space not in SPACE_FORMS:
        space = "不确定"

    category = str(value.get("category") or value.get("usage_category") or "").strip()
    if category not in USAGE_TAXONOMY:
        category = ""

    allowed_subcategories = set(USAGE_TAXONOMY.get(category, []))
    raw_subcategories = _coerce_list(value.get("subcategories") or value.get("usage_subcategories"))
    subcategories = [item for item in raw_subcategories if item in allowed_subcategories][:3] if category else []
    subcategories = _remove_redundant_other(subcategories)

    suggestion = value.get("suggestion") or value.get("classification_suggestion")
    if not isinstance(suggestion, dict):
        suggestion = None

    reason = str(value.get("reason") or value.get("classification_reason") or "").strip()

    return UsageClassification(
        space=space,
        category=category,
        subcategories=subcategories,
        reason=reason,
        suggestion=suggestion,
    )


def parse_classification_response(text: str) -> UsageClassification:
    """Parse a classification-only model response.

    Accepts either a top-level classification object or
    {"classification": {...}} for compatibility with older prompts.
    """
    cleaned = _strip_code_fence(text)
    data = _loads_json_object(cleaned)
    if isinstance(data, dict):
        return normalize_classification(data.get("classification") or data.get("usage_classification") or data)
    return normalize_classification(None)


def _extract_two_line_description(cleaned: str) -> tuple[str, str]:
    main, detail = "", ""
    for line in cleaned.splitlines():
        line = line.strip()
        if line.startswith("主体：") or line.startswith("主体:"):
            main = re.sub(r"^主体[：:]", "", line).strip()
        elif line.startswith("细节：") or line.startswith("细节:"):
            detail = re.sub(r"^细节[：:]", "", line).strip()
    if not main:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        main = lines[0] if lines else ""
        detail = lines[1] if len(lines) > 1 else main
    elif not detail:
        detail = main
    return main, detail


def parse_description_response(text: str) -> tuple[str, str, float | None, UsageClassification]:
    """Parse JSON-first model output, with a two-line description fallback."""
    cleaned = _strip_code_fence(text)
    data = _loads_json_object(cleaned)

    if isinstance(data, dict):
        main = str(data.get("main_content") or data.get("main") or "").strip()
        detail = str(data.get("detail_content") or data.get("detail") or "").strip()
        score = data.get("description_quality_score")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = None
        if main or detail:
            return (
                main,
                detail or main,
                score,
                normalize_classification(data.get("classification") or data.get("usage_classification")),
            )

    main, detail = _extract_two_line_description(cleaned)
    return main, detail, None, normalize_classification(None)


def ensure_usage_classification(
    classification: UsageClassification,
    *,
    resource_type: str = "",
    llm_input_type: str = "",
    title: str = "",
    resource_path: str = "",
    source_category: str = "",
    source_tags: list[str] | None = None,
    source_description: str = "",
    main_content: str = "",
    detail_content: str = "",
    auxiliary_metadata: dict[str, Any] | None = None,
) -> UsageClassification:
    """Keep valid model classifications without inferring missing categories."""
    if classification.category:
        classification.subcategories = _remove_redundant_other(
            [
                item
                for item in classification.subcategories
                if item in set(USAGE_TAXONOMY.get(classification.category, []))
            ][:3]
        )
        if not classification.reason:
            classification.reason = "模型返回了有效用途大类；系统补齐缺失的分类说明。"
        return classification

    if not classification.reason:
        classification.reason = "模型未返回有效结构化用途分类；未写入用途大类。"
    return classification


def _build_suggestion_schema() -> dict[str, Any]:
    return {
        "type": ["object", "null"],
        "additionalProperties": False,
        "properties": {
            "level": {"type": "string", "enum": ["大类", "小类", ""]},
            "parent_category": {"type": "string"},
            "name": {"type": "string"},
            "definition": {"type": "string"},
            "include_scope": {"type": "string"},
            "exclude_scope": {"type": "string"},
            "relation_to_existing": {"type": "string"},
            "examples": {"type": "array", "items": {"type": "string"}},
            "search_value": {"type": "string"},
            "affected_resources": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "level",
            "parent_category",
            "name",
            "definition",
            "include_scope",
            "exclude_scope",
            "relation_to_existing",
            "examples",
            "search_value",
            "affected_resources",
        ],
    }


def build_classification_response_schema() -> dict[str, Any]:
    suggestion_schema = _build_suggestion_schema()
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "space": {"type": "string", "enum": SPACE_FORMS},
            "category": {"type": "string", "enum": USAGE_CATEGORIES},
            "subcategories": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": sorted({item for items in USAGE_TAXONOMY.values() for item in items}),
                },
                "maxItems": 3,
            },
            "reason": {"type": "string"},
            "suggestion": suggestion_schema,
        },
        "required": ["space", "category", "subcategories", "reason", "suggestion"],
    }


def build_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "main_content": {"type": "string", "description": "主体描述，不包含'主体：'前缀。"},
            "detail_content": {"type": "string", "description": "细节描述，不包含'细节：'前缀。"},
            "description_quality_score": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 1,
            },
            "classification": build_classification_response_schema(),
        },
        "required": ["main_content", "detail_content", "description_quality_score", "classification"],
    }
