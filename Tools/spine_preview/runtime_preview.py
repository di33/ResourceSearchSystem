from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from PIL import Image

from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy, ResourceProcessingEntity

from .runtime.helper.postprocess_frames import process_manifest


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_WINDOWS_EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
DEFAULT_WINDOWS_CHROME = Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SPINE_RUNTIME_RENDERER = "spine-webgl-3.8-playwright"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _browser_candidates(raw: str = "") -> list[Path]:
    candidates: list[Path] = []
    env_value = os.environ.get("SPINE_PREVIEW_BROWSER", "")
    for value in (raw, env_value):
        if value:
            candidates.append(Path(value))
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge", "msedge"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    candidates.extend([DEFAULT_WINDOWS_EDGE, DEFAULT_WINDOWS_CHROME])

    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _is_spine_atlas_path(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix == ".atlas" or (suffix == ".txt" and "atlas" in path.name.lower())


def _spine_source_paths(entity: ResourceProcessingEntity) -> tuple[Path, Path, list[Path]]:
    paths = [Path(file.file_path) for file in entity.files if file.file_path]
    existing = [path for path in paths if path.is_file()]
    json_paths = [path for path in existing if path.suffix.lower() == ".json"]
    atlas_paths = [path for path in existing if _is_spine_atlas_path(path)]
    image_paths = [path for path in existing if path.suffix.lower() in RASTER_EXTS]

    if not json_paths:
        raise RuntimeError("missing spine skeleton json")
    if not atlas_paths:
        raise RuntimeError("missing spine atlas")
    if not image_paths:
        raise RuntimeError("missing spine texture image")
    return json_paths[0], atlas_paths[0], image_paths


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _atlas_pages(atlas_path: Path) -> list[tuple[str, tuple[int, int]]]:
    lines = atlas_path.read_text(encoding="utf-8", errors="replace").splitlines()
    pages: list[tuple[str, tuple[int, int]]] = []
    for index, raw in enumerate(lines):
        name = raw.strip()
        if not name or raw[:1].isspace() or ":" in name:
            continue
        next_line = ""
        for candidate in lines[index + 1 : index + 5]:
            stripped = candidate.strip()
            if stripped:
                next_line = stripped
                break
        if not next_line.lower().startswith("size:"):
            continue
        size_text = next_line.split(":", 1)[1]
        width, height = 1, 1
        parts = [part.strip() for part in size_text.split(",", 1)]
        if len(parts) == 2:
            try:
                width = max(1, int(parts[0]))
                height = max(1, int(parts[1]))
            except ValueError:
                width, height = 1, 1
        pages.append((name, (width, height)))
    return pages


def _complete_atlas_images(
    atlas_path: Path,
    image_paths: list[Path],
    placeholder_dir: Path,
) -> tuple[list[Path], list[str]]:
    by_name = {path.name.lower(): path for path in image_paths}
    completed = list(image_paths)
    missing_pages: list[str] = []
    for page_name, size in _atlas_pages(atlas_path):
        key = Path(page_name).name.lower()
        if key in by_name:
            continue
        adjacent = atlas_path.parent / page_name
        if adjacent.is_file():
            by_name[key] = adjacent
            completed.append(adjacent)
            continue
        placeholder = placeholder_dir / Path(page_name).name
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", size, (0, 0, 0, 0)).save(placeholder)
        by_name[key] = placeholder
        completed.append(placeholder)
        missing_pages.append(page_name)
    return completed, missing_pages


def _render_actions(
    *,
    json_path: Path,
    atlas_path: Path,
    image_paths: list[Path],
    out_dir: Path,
    browser: Path,
    frames: int,
    width: int,
    height: int,
    timeout_ms: int,
) -> Path:
    command = [
        "node",
        str(SCRIPT_DIR / "runtime" / "helper" / "render_actions_cli.mjs"),
        "--json",
        str(json_path),
        "--atlas",
        str(atlas_path),
        "--images",
        ";".join(str(path) for path in image_paths),
        "--out",
        str(out_dir),
        "--frames",
        str(frames),
        "--width",
        str(width),
        "--height",
        str(height),
        "--timeout",
        str(timeout_ms),
        "--chrome",
        str(browser),
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "spine runtime renderer failed with "
            f"{browser}\nSTDOUT:\n{completed.stdout[-2000:]}\nSTDERR:\n{completed.stderr[-2000:]}"
        )
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"spine runtime renderer did not write manifest: {manifest_path}")
    return manifest_path


def _copy_preview(
    source: Path,
    target_dir: Path,
    entity: ResourceProcessingEntity,
    *,
    suffix: str,
    strategy: PreviewStrategy,
    mode: str,
    file_format: str,
    used_placeholder: bool = False,
    fail_reason: str = "",
) -> PreviewInfo:
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = entity.content_md5 or uuid.uuid4().hex
    target = target_dir / f"{stem}_{suffix}.{file_format}"
    shutil.copy2(source, target)
    with Image.open(target) as image:
        width, height = image.size
    return PreviewInfo(
        strategy=strategy,
        role="primary",
        path=str(target.resolve()),
        mode=mode,
        confidence="high",
        format=file_format,
        width=width,
        height=height,
        size=target.stat().st_size,
        renderer=SPINE_RUNTIME_RENDERER,
        used_placeholder=used_placeholder,
        fail_reason=fail_reason,
    )


def _generate_spine_runtime_previews_sync(
    entity: ResourceProcessingEntity,
    output_dir: Path,
    *,
    max_size: int,
    browser: str = "",
) -> list[PreviewInfo]:
    json_path, atlas_path, image_paths = _spine_source_paths(entity)
    browsers = _browser_candidates(browser)
    if not browsers:
        raise RuntimeError("no Chromium/Chrome/Edge executable found for spine runtime preview")

    frames = _int_env("SPINE_PREVIEW_FRAMES", 5)
    width = _int_env("SPINE_PREVIEW_WIDTH", 768)
    height = _int_env("SPINE_PREVIEW_HEIGHT", 768)
    timeout_ms = _int_env("SPINE_PREVIEW_TIMEOUT_MS", 30000)
    thumb_size = min(max(160, max_size), 320)
    stage_dir = output_dir / f"_spine_runtime_{uuid.uuid4().hex[:12]}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    image_paths, missing_pages = _complete_atlas_images(atlas_path, image_paths, stage_dir / "missing_atlas_pages")
    placeholder_reason = (
        "missing atlas image pages rendered as transparent placeholders: " + ", ".join(missing_pages)
        if missing_pages
        else ""
    )

    last_error = ""
    try:
        for candidate in browsers:
            try:
                manifest_path = _render_actions(
                    json_path=json_path,
                    atlas_path=atlas_path,
                    image_paths=image_paths,
                    out_dir=stage_dir,
                    browser=candidate,
                    frames=frames,
                    width=width,
                    height=height,
                    timeout_ms=timeout_ms,
                )
                post = process_manifest(manifest_path, thumb_size=thumb_size, gif_size=320, duration_ms=130)
                actions_gif = Path(str(post.get("actions_gif") or ""))
                if actions_gif.is_file():
                    return [
                        _copy_preview(
                            actions_gif,
                            output_dir,
                            entity,
                            suffix="spine_runtime_actions",
                            strategy=PreviewStrategy.GIF,
                            mode="spine_runtime_actions_gif",
                            file_format="gif",
                            used_placeholder=bool(missing_pages),
                            fail_reason=placeholder_reason,
                        )
                    ]
                overview = Path(str(post.get("overview") or ""))
                if not overview.is_file():
                    raise RuntimeError("spine runtime postprocess did not write preview")
                return [
                    _copy_preview(
                        overview,
                        output_dir,
                        entity,
                        suffix="spine_runtime_overview",
                        strategy=PreviewStrategy.CONTACT_SHEET,
                        mode="spine_runtime_overview",
                        file_format="webp",
                        used_placeholder=bool(missing_pages),
                        fail_reason=placeholder_reason,
                    )
                ]
            except Exception as exc:
                last_error = str(exc)
        raise RuntimeError(last_error or "spine runtime preview failed")
    finally:
        if not _truthy(os.environ.get("SPINE_PREVIEW_KEEP_WORK_DIR")):
            shutil.rmtree(stage_dir, ignore_errors=True)


async def generate_spine_runtime_previews(
    entity: ResourceProcessingEntity,
    output_dir: str | Path,
    *,
    max_size: int = 512,
    browser: str = "",
) -> list[PreviewInfo]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _generate_spine_runtime_previews_sync(
            entity,
            Path(output_dir),
            max_size=max_size,
            browser=browser,
        ),
    )
