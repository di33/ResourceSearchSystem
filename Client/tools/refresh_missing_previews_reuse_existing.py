from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE = Path(r"G:\ResourceCrawler\tmp\refresh_missing_previews.py")


def _load_source_module():
    spec = importlib.util.spec_from_file_location("refresh_missing_previews_source", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


source = _load_source_module()
_original_try_fast_single_image_preview = source._try_fast_single_image_preview


def _try_existing_preview(
    preview_path: Path,
    *,
    strategy,
    mode: str,
    confidence: str,
    validate_preview,
    preview_info_cls,
):
    if not preview_path.is_file():
        return None
    return source._preview_info_from_path(
        preview_path,
        strategy=strategy,
        mode=mode,
        confidence=confidence,
        validate_preview=validate_preview,
        preview_info_cls=preview_info_cls,
    )


def _try_rasterize_svg_with_timeouts(svg_path: str, output_path: str, size: int = 1024) -> bool:
    from ResourceProcessor.preview import crawler_thumbnail_policy as policy

    if policy._try_rasterize_svg_resvg(svg_path, output_path, size):
        return True

    edge_candidates: list[Path] = []
    if os.name == "nt":
        edge_candidates.extend(
            [
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
                / "Microsoft"
                / "Edge"
                / "Application"
                / "msedge.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Microsoft"
                / "Edge"
                / "Application"
                / "msedge.exe",
            ]
        )
    edge = next((path for path in edge_candidates if path.is_file()), None)
    if edge is None:
        return False

    try:
        with tempfile.TemporaryDirectory(prefix="svg_preview_") as tmp:
            tmp_dir = Path(tmp)
            html_path = tmp_dir / "preview.html"
            user_data_dir = tmp_dir / "profile"
            svg_uri = Path(svg_path).resolve().as_uri()
            html_path.write_text(
                f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html, body {{ margin: 0; width: {size}px; height: {size}px; overflow: hidden; background: #9aa4b2; }}
.frame {{ width: {size}px; height: {size}px; display: flex; align-items: center; justify-content: center; }}
img {{ max-width: {size}px; max-height: {size}px; width: {size}px; height: {size}px; object-fit: contain; }}
</style>
</head>
<body><div class="frame"><img src="{svg_uri}" /></div></body>
</html>""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(edge),
                    "--headless=new",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--no-first-run",
                    f"--user-data-dir={user_data_dir}",
                    f"--window-size={size},{size}",
                    f"--screenshot={output_path}",
                    html_path.resolve().as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0 and Path(output_path).is_file()
    except Exception:
        return False


def _svg_placeholder_preview(
    file_path: str,
    preview_path: Path,
    *,
    size: int,
    preview_strategy_cls,
    preview_info_cls,
    validate_preview,
):
    from ResourceProcessor.preview import crawler_thumbnail_policy as policy

    preview_path.parent.mkdir(parents=True, exist_ok=True)
    policy._save_metadata_card(
        preview_path,
        "SVG preview",
        Path(file_path).name,
        ["Renderer timed out or failed"],
        size=min(size, 512),
    )
    info = source._preview_info_from_path(
        preview_path,
        strategy=preview_strategy_cls.STATIC,
        mode="fallback",
        confidence="low",
        validate_preview=validate_preview,
        preview_info_cls=preview_info_cls,
    )
    if info is not None:
        info.used_placeholder = True
        info.fail_reason = "svg_render_timeout"
        info.renderer = "crawler-policy-placeholder"
    return info


def _try_fast_single_image_preview_reuse(cache, task, previews_dir, **kwargs):
    if task.get("resource_type") != "single_image":
        return None

    task_id = int(task["id"])
    primary = source._single_image_primary_file(cache, task_id)
    if primary is None:
        return None

    file_path = str(primary["file_path"] or "")
    if not file_path or not Path(file_path).is_file():
        return None

    content_md5 = str(task.get("content_md5") or primary["content_md5"] or "")
    if not content_md5:
        return None

    ext = Path(file_path).suffix.lower()
    output_dir = Path(previews_dir) / "single_image"
    preview_strategy_cls = kwargs["preview_strategy_cls"]
    preview_info_cls = kwargs["preview_info_cls"]
    validate_preview = kwargs["validate_preview"]

    if ext in kwargs["raster_exts"]:
        existing = _try_existing_preview(
            output_dir / f"{content_md5}_preview.webp",
            strategy=preview_strategy_cls.STATIC,
            mode="direct",
            confidence="high",
            validate_preview=validate_preview,
            preview_info_cls=preview_info_cls,
        )
        if existing is not None:
            return existing

    if ext in kwargs["svg_exts"]:
        preview_path = output_dir / f"{content_md5}_svg.webp"
        existing = _try_existing_preview(
            preview_path,
            strategy=preview_strategy_cls.STATIC,
            mode="direct",
            confidence="medium",
            validate_preview=validate_preview,
            preview_info_cls=preview_info_cls,
        )
        if existing is not None:
            return existing

        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            if _try_rasterize_svg_with_timeouts(
                file_path,
                str(temp_path),
                kwargs.get("max_size", 1024),
            ):
                kwargs["save_existing_raster_preview"](
                    str(temp_path),
                    preview_path,
                    kwargs["max_size"],
                )
                rendered = source._preview_info_from_path(
                    preview_path,
                    strategy=preview_strategy_cls.STATIC,
                    mode="direct",
                    confidence="medium",
                    validate_preview=validate_preview,
                    preview_info_cls=preview_info_cls,
                )
                if rendered is not None:
                    return rendered
            return _svg_placeholder_preview(
                file_path,
                output_dir / f"{content_md5}_svg_placeholder.webp",
                size=kwargs.get("max_size", 512),
                preview_strategy_cls=preview_strategy_cls,
                preview_info_cls=preview_info_cls,
                validate_preview=validate_preview,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    return _original_try_fast_single_image_preview(cache, task, previews_dir, **kwargs)


source._try_fast_single_image_preview = _try_fast_single_image_preview_reuse

if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(source.main())
