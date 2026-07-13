"""Human-friendly resource display title selection."""

from __future__ import annotations

import json
import re
from typing import Any


_GENERIC_STEMS = {
    "asset",
    "assets",
    "download",
    "file",
    "files",
    "image",
    "images",
    "main",
    "original",
    "package",
    "png",
    "preview",
    "primary",
    "resource",
    "resources",
    "source",
}
_METADATA_TITLE_KEYS = (
    "display_title",
    "display_name",
    "resource_name",
    "original_title",
    "title",
    "name",
    "label",
)
_ANIMATION_METADATA_TITLE_KEYS = (
    "action_name",
    "animation_name",
    "display_title",
    "group_name",
    "resource_path",
    "source_title",
)
_HASH_RE = re.compile(r"^[0-9a-f]{24,}$", re.IGNORECASE)
_RESOURCE_ID_RE = re.compile(r"^(?:res|asset|task|job)[-_]?(?:v\d+[-_]?)?[0-9a-f][0-9a-f_-]{8,}$", re.IGNORECASE)
_TRAILING_INDEX_RE = re.compile(r"[-_\s]*(?:copy|\d{1,4})$", re.IGNORECASE)
_WORD_SEPARATOR_RE = re.compile(r"[_\-]+")
_SENTENCE_SPLIT_RE = re.compile(r"[\r\n.!?;\u3002\uff01\uff1f\uff1b]+")
_CLAUSE_SPLIT_RE = re.compile(r"[,:\u3001\uff0c\uff1a]+")


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().strip("\"'")
    return " ".join(text.split())


def _basename(value: str) -> str:
    text = _normalize_text(value).split("?", 1)[0].split("#", 1)[0]
    text = text.replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1] if "/" in text else text


def _stem(value: str) -> str:
    base = _basename(value)
    if "." in base and not base.startswith("."):
        base = base.rsplit(".", 1)[0]
    return base


def _looks_like_id(value: str) -> bool:
    text = _normalize_text(value)
    return bool(_HASH_RE.fullmatch(text) or _RESOURCE_ID_RE.fullmatch(text))


def is_generic_resource_title(value: Any) -> bool:
    text = _normalize_text(value)
    if not text:
        return True
    base = _basename(text).lower()
    if _looks_like_id(base):
        return True
    stem = _TRAILING_INDEX_RE.sub("", _stem(base)).strip().lower()
    return stem in _GENERIC_STEMS


def _title_from_plain_text(value: Any) -> str:
    text = _normalize_text(value)
    if not text or is_generic_resource_title(text):
        return ""
    return text


def _title_from_path(value: Any) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    name = _stem(text).strip()
    if not name or is_generic_resource_title(name):
        return ""
    return _WORD_SEPARATOR_RE.sub(" ", name).strip()


def _title_from_summary(value: Any, *, max_chars: int = 64) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    first_sentence = _SENTENCE_SPLIT_RE.split(text, 1)[0].strip()
    if not first_sentence:
        first_sentence = text
    first_clause = _CLAUSE_SPLIT_RE.split(first_sentence, 1)[0].strip()
    if len(first_clause) >= 6:
        first_sentence = first_clause
    if len(first_sentence) > max_chars:
        first_clause = _CLAUSE_SPLIT_RE.split(first_sentence, 1)[0].strip()
        if len(first_clause) >= 6:
            first_sentence = first_clause
    if len(first_sentence) > max_chars:
        first_sentence = first_sentence[: max_chars - 3].rstrip() + "..."
    return "" if is_generic_resource_title(first_sentence) else first_sentence


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _metadata_title_candidates(raw: Any, *, preferred_keys: tuple[str, ...] = _METADATA_TITLE_KEYS) -> list[str]:
    data = _json_object(raw)
    if not data:
        return []
    containers = [data]
    for key in ("metadata", "resource", "asset", "source"):
        nested = data.get(key)
        if isinstance(nested, dict):
            containers.append(nested)

    candidates: list[str] = []
    for container in containers:
        for key in preferred_keys:
            value = container.get(key)
            if isinstance(value, str):
                candidates.append(value)
    return candidates


def _description_text(description: Any) -> str:
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        for key in ("main_content", "summary", "full_description", "detail_content", "detail", "full"):
            value = description.get(key)
            if value:
                return str(value)
        return ""
    for attr in ("main_content", "full_description", "detail_content"):
        value = getattr(description, attr, "")
        if value:
            return str(value)
    return ""


def display_title_for_task(task: Any, *, description: Any = None) -> str:
    """Pick a user-facing resource title without mutating the persisted title."""
    resource_type = _normalize_text(getattr(task, "resource_type", ""))
    metadata_raw = getattr(task, "client_metadata_json", "")
    if resource_type == "animation_sequence":
        for candidate in _metadata_title_candidates(
            metadata_raw,
            preferred_keys=_ANIMATION_METADATA_TITLE_KEYS,
        ):
            title = _title_from_path(candidate) or _title_from_plain_text(candidate)
            if title:
                return title

    for candidate in _metadata_title_candidates(metadata_raw):
        title = _title_from_plain_text(candidate)
        if title:
            return title

    title = _title_from_plain_text(getattr(task, "title", ""))
    if title:
        return title

    path_title = _title_from_path(getattr(task, "source_directory", ""))
    pack_title = _title_from_plain_text(getattr(task, "pack_name", ""))
    if pack_title and path_title and path_title.lower() not in pack_title.lower():
        return f"{pack_title} / {path_title}"
    if path_title:
        return path_title
    if pack_title:
        return pack_title

    source_description_title = _title_from_summary(getattr(task, "source_description", ""))
    if source_description_title:
        return source_description_title

    generated_description_title = _title_from_summary(_description_text(description))
    if generated_description_title:
        return generated_description_title

    object_file_title = _title_from_path(getattr(task, "source_object_file_name", ""))
    if object_file_title:
        return object_file_title

    for attr in ("source_resource_id", "resource_id", "content_md5"):
        value = _normalize_text(getattr(task, attr, ""))
        if value:
            return value
    return "Untitled Resource"
