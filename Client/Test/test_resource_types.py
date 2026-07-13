from CloudService.search_client import CANONICAL_RESOURCE_TYPES as SEARCH_RESOURCE_TYPES
from resource_contracts.resource_types import (
    CANONICAL_RESOURCE_TYPES,
    CURRENT_RESOURCE_TYPES,
    LEGACY_GENERIC_RESOURCE_TYPES,
    MODEL_3D_ALT_RESOURCE_TYPE,
    MODEL_RESOURCE_TYPE,
    PACK_RESOURCE_TYPE,
    SINGLE_IMAGE_RESOURCE_TYPE,
    is_search_indexable_resource_type,
    normalize_resource_type,
    resource_type_label,
)


def test_search_client_reuses_shared_resource_types():
    assert SEARCH_RESOURCE_TYPES == CANONICAL_RESOURCE_TYPES


def test_model_is_current_search_indexable_resource_type():
    assert MODEL_RESOURCE_TYPE in CURRENT_RESOURCE_TYPES
    assert MODEL_RESOURCE_TYPE not in LEGACY_GENERIC_RESOURCE_TYPES
    assert is_search_indexable_resource_type(MODEL_RESOURCE_TYPE)


def test_normalize_resource_type_accepts_current_and_legacy_values():
    assert normalize_resource_type(" Single_Image ") == SINGLE_IMAGE_RESOURCE_TYPE
    assert normalize_resource_type(" model ") == MODEL_RESOURCE_TYPE
    assert normalize_resource_type(MODEL_3D_ALT_RESOURCE_TYPE) == MODEL_3D_ALT_RESOURCE_TYPE
    assert normalize_resource_type("not-a-type") == ""


def test_resource_type_label_uses_shared_display_names():
    assert resource_type_label(PACK_RESOURCE_TYPE) == "资源包"
    assert resource_type_label("custom_type", locale="zh") == "custom_type"
