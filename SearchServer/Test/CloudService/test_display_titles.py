from __future__ import annotations

from types import SimpleNamespace

from app.services.display_titles import display_title_for_task, is_generic_resource_title


def _task(**values):
    defaults = {
        "title": "",
        "resource_type": "",
        "client_metadata_json": "null",
        "source_directory": "",
        "pack_name": "",
        "source_description": "",
        "source_object_file_name": "",
        "source_resource_id": "src-1",
        "resource_id": "res-1",
        "content_md5": "abc",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_generic_resource_title_detection():
    assert is_generic_resource_title("source.zip")
    assert is_generic_resource_title("primary.webp")
    assert is_generic_resource_title("res-v1-f71a7315485cb515fc785fcf240adcf8")
    assert not is_generic_resource_title("Farm Animal Tilemap")


def test_display_title_keeps_specific_title():
    task = _task(title="Farm Animal Tilemap", source_description="should not win")

    assert display_title_for_task(task) == "Farm Animal Tilemap"


def test_display_title_uses_metadata_before_path():
    task = _task(
        title="source.zip",
        client_metadata_json='{"display_name": "Forest Dungeon Map"}',
        source_directory="packs/forest/source",
    )

    assert display_title_for_task(task) == "Forest Dungeon Map"


def test_display_title_uses_metadata_before_specific_file_title():
    task = _task(
        title="skeleton.json",
        resource_type="spine_skeleton",
        client_metadata_json='{"display_title": "Pink Monster | Free 2D Game Asset"}',
    )

    assert display_title_for_task(task) == "Pink Monster | Free 2D Game Asset"


def test_display_title_falls_back_to_description_for_source_zip():
    task = _task(title="source.zip")
    description = SimpleNamespace(
        main_content="这是一套免费的像素风格农场动物Tilemap资源包，包含绵羊、牛、鸡等角色。",
        detail_content="",
        full_description="",
    )

    assert display_title_for_task(task, description=description) == "这是一套免费的像素风格农场动物Tilemap资源包"


def test_display_title_combines_pack_and_path_when_useful():
    task = _task(
        title="source.zip",
        resource_type="tiled_map",
        pack_name="Dungeon Pack",
        source_directory="maps/ice_room.tmx",
    )

    assert display_title_for_task(task) == "Dungeon Pack / ice room"


def test_animation_sequence_prefers_action_name_metadata():
    task = _task(
        title="source.zip",
        resource_type="animation_sequence",
        client_metadata_json='{"action_name": "04 punch", "display_title": "Robot Boy Punch"}',
    )
    description = SimpleNamespace(main_content="这是一个机器人男孩挥拳攻击动作序列")

    assert display_title_for_task(task, description=description) == "04 punch"


def test_animation_sequence_skips_generic_png_for_group_name():
    task = _task(
        title="source.zip",
        resource_type="animation_sequence",
        client_metadata_json='{"action_name": "Png", "group_name": "snail"}',
    )

    assert display_title_for_task(task) == "snail"
