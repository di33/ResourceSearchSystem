import asyncio
from pathlib import Path

from ResourceProcessor.pipeline_incremental import ensure_previews, fingerprint, norm_source
from ResourceProcessor.thumbnail_generator import find_blender_executable


def test_find_blender_executable_searches_common_windows_locations(tmp_path, monkeypatch):
    expected = (
        tmp_path / "Blender Foundation" / "Blender 4.5" / "blender.exe"
    )
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"")

    monkeypatch.delenv("BLENDER_EXE", raising=False)
    monkeypatch.setattr(
        "ResourceProcessor.thumbnail_generator.shutil.which",
        lambda _name: None,
    )
    monkeypatch.setattr(
        "ResourceProcessor.thumbnail_generator._find_blender_from_registry",
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
        "ResourceProcessor.thumbnail_generator.ThumbnailGenerator.generate_fbx_preview_gif_result",
        fake_generate,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))

    assert calls["count"] == 1
    assert state["by_source"][key]["preview_renderer"] == "blender"
    assert state["by_source"][key]["preview_path"] == str(preview.resolve())


def test_ensure_previews_skips_existing_blender_fbx_preview(tmp_path, monkeypatch):
    src = tmp_path / "source.fbx"
    copied = tmp_path / "copied.fbx"
    preview = tmp_path / "preview.gif"
    src.write_bytes(b"source-data")
    copied.write_bytes(b"copied-data")
    preview.write_bytes(b"real-render")

    key = norm_source(str(src))
    state = {
        "by_source": {
            key: {
                "fingerprint": fingerprint(str(src)),
                "copied_path": str(copied),
                "preview_path": str(preview),
                "preview_renderer": "blender",
            }
        }
    }

    async def should_not_run(self, model_path, output_name, frame_count=24, frame_size=512):
        raise AssertionError("generate_fbx_preview_gif_result should not be called")

    monkeypatch.setattr(
        "ResourceProcessor.thumbnail_generator.ThumbnailGenerator.generate_fbx_preview_gif_result",
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
    state = {"by_source": {key: {"fingerprint": fingerprint(str(src)), "copied_path": str(copied), "preview_path": None}}}

    monkeypatch.setattr(
        "ResourceProcessor.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))

    entry = state["by_source"][key]
    preview_path = entry.get("preview_path")
    assert preview_path is not None
    preview_name = Path(preview_path).name
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
    state = {"by_source": {key: {"fingerprint": fingerprint(str(src)), "copied_path": str(copied), "preview_path": None}}}

    monkeypatch.setattr(
        "ResourceProcessor.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))

    entry = state["by_source"][key]
    assert entry.get("preview_failed") is True
    assert entry.get("preview_fail_reason")
    assert entry.get("preview_path") is None


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
                "preview_path": None,
            }
        }
    }

    monkeypatch.setattr(
        "ResourceProcessor.thumbnail_generator.find_blender_executable",
        lambda: None,
    )

    asyncio.run(ensure_previews({str(src): str(copied)}, str(tmp_path), state))

    entry = state["by_source"][key]
    pi = entry.get("preview_info")
    assert pi is not None
    assert isinstance(pi, dict)
    assert pi["strategy"] == "static"
    assert pi["format"] in ("webp", "png", "jpg")
    assert isinstance(pi["width"], int) and pi["width"] > 0
    assert isinstance(pi["height"], int) and pi["height"] > 0
    assert isinstance(pi["size"], int) and pi["size"] > 0

