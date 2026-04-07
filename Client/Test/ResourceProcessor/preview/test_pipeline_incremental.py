import asyncio
import shutil
from pathlib import Path

from PIL import Image

from ResourceProcessor.preview.pipeline_incremental import (
    ensure_previews,
    fingerprint,
    get_resource_entities,
    load_state,
    norm_source,
    resolve_copies,
    save_state,
)
from ResourceProcessor.preview.thumbnail_generator import find_blender_executable


def test_find_blender_executable_searches_common_windows_locations(tmp_path, monkeypatch):
    expected = (
        tmp_path / "Blender Foundation" / "Blender 4.5" / "blender.exe"
    )
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"")

    monkeypatch.delenv("BLENDER_EXE", raising=False)
    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.shutil.which",
        lambda _name: None,
    )
    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator._find_blender_from_registry",
        lambda: None,
    )
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LocalAppData", raising=False)

    result = find_blender_executable()

    assert result == str(expected)


def test_ensure_previews_retries_placeholder_fbx_preview(tmp_path, monkeypatch):
    src = tmp_path / "source.fbx"
    copied = tmp_path / "copied.fbx"
    preview = tmp_path / "preview.gif"
    src.write_bytes(b"source-data")
    copied.write_bytes(b"copied-data")
    preview.write_bytes(b"old-placeholder")

    key = norm_source(str(src))
    state = {
        "by_source": {
            key: {
                "fingerprint": fingerprint(str(src)),
                "copied_path": str(copied),
                "preview_path": str(preview),
                "preview_renderer": "placeholder",
            }
        }
    }

    calls = {"count": 0}

    async def fake_generate(self, model_path, output_name, frame_count=24, frame_size=512):
        calls["count"] += 1
        from PIL import Image as _Img
        _Img.new("RGB", (128, 128), (50, 100, 150)).save(preview, format="GIF")
        return {
            "path": str(preview),
            "renderer": "blender",
            "used_placeholder": False,
        }

    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.ThumbnailGenerator.generate_fbx_preview_gif_result",
        fake_generate,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))

    assert calls["count"] == 1
    assert state["by_source"][key]["preview_renderer"] == "blender"
    assert str(preview.resolve()) in state["by_source"][key].get("preview_paths", [])


def test_ensure_previews_skips_existing_blender_fbx_preview(tmp_path, monkeypatch):
    src = tmp_path / "source.fbx"
    copied = tmp_path / "copied.fbx"
    preview = tmp_path / "preview.gif"
    src.write_bytes(b"source-data")
    copied.write_bytes(b"copied-data")
    from PIL import Image as _Img
    _Img.new("RGB", (128, 128), (50, 100, 150)).save(preview, format="GIF")

    key = norm_source(str(src))
    state = {
        "by_source": {
            key: {
                "fingerprint": fingerprint(str(src)),
                "copied_path": str(copied),
                "preview_paths": [str(preview)],
                "preview_renderer": "blender",
            }
        }
    }

    async def should_not_run(self, model_path, output_name, frame_count=24, frame_size=512):
        raise AssertionError("generate_fbx_preview_gif_result should not be called")

    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.ThumbnailGenerator.generate_fbx_preview_gif_result",
        should_not_run,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))


# ---------------------------------------------------------------------------
# New tests for Spec 1 — pipeline integration
# ---------------------------------------------------------------------------

import hashlib
from PIL import Image


def test_ensure_previews_uses_content_md5_naming(tmp_path, monkeypatch):
    """Image preview filenames must use content_md5 (file bytes MD5)."""
    src = tmp_path / "photo.png"
    Image.new("RGB", (200, 100), "green").save(src)
    copied_dir = tmp_path / "Images"
    copied_dir.mkdir()
    copied = copied_dir / "photo.png"
    import shutil
    shutil.copy2(src, copied)

    content_md5 = hashlib.md5(src.read_bytes()).hexdigest()

    key = norm_source(str(src))
    state = {"by_source": {key: {"fingerprint": fingerprint(str(src)), "copied_path": str(copied), "preview_paths": []}}}

    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))

    entry = state["by_source"][key]
    preview_paths = entry.get("preview_paths", [])
    assert len(preview_paths) > 0
    preview_name = Path(preview_paths[0]).name
    assert preview_name.startswith(content_md5)
    assert "_preview." in preview_name


def test_ensure_previews_validates_preview(tmp_path, monkeypatch):
    """ensure_previews must validate generated previews; a bad preview sets preview_failed."""
    src = tmp_path / "black.png"
    Image.new("RGB", (200, 100), (0, 0, 0)).save(src)
    copied_dir = tmp_path / "Images"
    copied_dir.mkdir()
    copied = copied_dir / "black.png"
    import shutil
    shutil.copy2(src, copied)

    key = norm_source(str(src))
    state = {"by_source": {key: {"fingerprint": fingerprint(str(src)), "copied_path": str(copied), "preview_paths": []}}}

    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))

    entry = state["by_source"][key]
    assert entry.get("preview_failed") is True
    assert entry.get("preview_fail_reason")
    assert entry.get("preview_paths") == []


def test_ensure_previews_populates_preview_info(tmp_path, monkeypatch):
    """After ensure_previews, state entry contains preview_info with expected fields."""
    src = tmp_path / "color.png"
    Image.new("RGB", (300, 200), (100, 150, 200)).save(src)
    copied_dir = tmp_path / "Images"
    copied_dir.mkdir()
    copied = copied_dir / "color.png"
    import shutil
    shutil.copy2(src, copied)

    key = norm_source(str(src))
    state = {
        "by_source": {
            key: {
                "fingerprint": fingerprint(str(src)),
                "copied_path": str(copied),
                "preview_paths": [],
            }
        }
    }

    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))

    entry = state["by_source"][key]
    preview_paths = entry.get("preview_paths", [])
    assert len(preview_paths) > 0
    assert all(Path(p).is_file() for p in preview_paths)
    by_dir = state.get("by_directory", {})
    assert len(by_dir) > 0
    dir_entry = list(by_dir.values())[0]
    previews = dir_entry.get("previews", [])
    assert len(previews) > 0
    pi = previews[0]
    assert pi["strategy"] == "static"
    assert pi["format"] in ("webp", "png", "jpg")
    assert isinstance(pi["width"], int) and pi["width"] > 0
    assert isinstance(pi["height"], int) and pi["height"] > 0
    assert isinstance(pi["size"], int) and pi["size"] > 0


# ---------------------------------------------------------------------------
# Multi-file / multi-preview pipeline tests
# ---------------------------------------------------------------------------


def test_ensure_previews_multi_file_same_directory(tmp_path, monkeypatch):
    """Multiple files from the same directory produce entries grouped under by_directory."""
    src_dir = tmp_path / "resource"
    src_dir.mkdir()
    src_a = src_dir / "photo_a.png"
    src_b = src_dir / "photo_b.png"
    Image.new("RGB", (200, 100), (100, 150, 200)).save(src_a)
    Image.new("RGB", (300, 200), (50, 100, 150)).save(src_b)

    copied_dir = tmp_path / "work" / "images"
    copied_dir.mkdir(parents=True)
    copied_a = copied_dir / "photo_a.png"
    copied_b = copied_dir / "photo_b.png"
    shutil.copy2(src_a, copied_a)
    shutil.copy2(src_b, copied_b)

    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    state = {"by_source": {}, "by_directory": {}}
    mapping = {str(src_a): str(copied_a), str(src_b): str(copied_b)}
    asyncio.run(ensure_previews(mapping, str(tmp_path / "work"), state))

    by_dir = state.get("by_directory", {})
    assert len(by_dir) >= 1

    dir_entry = list(by_dir.values())[0]
    previews = dir_entry.get("previews", [])
    assert len(previews) >= 2

    assert dir_entry.get("composite_md5")
    assert len(dir_entry.get("composite_md5", "")) == 32


def test_ensure_previews_multiple_directories(tmp_path, monkeypatch):
    """Files from different directories are grouped separately."""
    dir_a = tmp_path / "res_a"
    dir_b = tmp_path / "res_b"
    dir_a.mkdir()
    dir_b.mkdir()

    src_a = dir_a / "img_a.png"
    src_b = dir_b / "img_b.png"
    Image.new("RGB", (100, 100), "red").save(src_a)
    Image.new("RGB", (100, 100), "blue").save(src_b)

    work = tmp_path / "work"
    copied_dir = work / "images"
    copied_dir.mkdir(parents=True)
    copied_a = copied_dir / "img_a.png"
    copied_b = copied_dir / "img_b.png"
    shutil.copy2(src_a, copied_a)
    shutil.copy2(src_b, copied_b)

    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    state = {"by_source": {}, "by_directory": {}}
    mapping = {str(src_a): str(copied_a), str(src_b): str(copied_b)}
    asyncio.run(ensure_previews(mapping, str(work), state))

    by_dir = state.get("by_directory", {})
    assert len(by_dir) == 2


def test_get_resource_entities_from_state(tmp_path, monkeypatch):
    """get_resource_entities extracts structured resource dicts from pipeline state."""
    src_dir = tmp_path / "resource"
    src_dir.mkdir()
    src_a = src_dir / "texture.png"
    src_b = src_dir / "model.png"
    Image.new("RGB", (200, 100), (100, 150, 200)).save(src_a)
    Image.new("RGB", (300, 200), (50, 100, 150)).save(src_b)

    work = tmp_path / "work"
    copied_dir = work / "images"
    copied_dir.mkdir(parents=True)
    copied_a = copied_dir / "texture.png"
    copied_b = copied_dir / "model.png"
    shutil.copy2(src_a, copied_a)
    shutil.copy2(src_b, copied_b)

    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    state = {"by_source": {}, "by_directory": {}}
    mapping = {str(src_a): str(copied_a), str(src_b): str(copied_b)}
    asyncio.run(ensure_previews(mapping, str(work), state))

    resources = get_resource_entities(state)
    assert len(resources) >= 1
    r = resources[0]
    assert "source_directory" in r
    assert "content_md5" in r
    assert "files" in r
    assert "previews" in r
    assert len(r["files"]) > 0
    for f in r["files"]:
        assert "file_name" in f
        assert "file_role" in f
        assert "content_md5" in f


def test_load_state_v2_format(tmp_path):
    """load_state creates v2 format with by_directory key."""
    state = load_state(str(tmp_path))
    assert state["version"] == 2
    assert "by_source" in state
    assert "by_directory" in state


def test_resolve_copies_uses_preview_paths_list(tmp_path):
    """resolve_copies initializes preview_paths as list, not scalar."""
    src = tmp_path / "source.png"
    Image.new("RGB", (10, 10), "green").save(src)

    state = {"by_source": {}}
    mapping = resolve_copies([str(src)], str(tmp_path), state)
    assert len(mapping) == 1

    key = norm_source(str(src))
    entry = state["by_source"][key]
    assert isinstance(entry["preview_paths"], list)
    assert entry["preview_paths"] == []


def test_resolve_copies_resets_preview_paths_on_fingerprint_change(tmp_path):
    """When source changes, preview_paths is reset to empty."""
    src = tmp_path / "source.png"
    Image.new("RGB", (10, 10), "green").save(src)

    state = {"by_source": {}}
    mapping = resolve_copies([str(src)], str(tmp_path), state)
    key = norm_source(str(src))
    state["by_source"][key]["preview_paths"] = ["/old/preview.webp"]

    Image.new("RGB", (20, 20), "blue").save(src)
    mapping2 = resolve_copies([str(src)], str(tmp_path), state)
    assert state["by_source"][key]["preview_paths"] == []
