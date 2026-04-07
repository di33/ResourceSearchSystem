import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image

logging.basicConfig(level=logging.INFO)


def validate_preview(
    preview_path: str,
    max_static_size_kb: int = 100,
    max_dynamic_size_kb: int = 200,
    min_dimension: int = 64,
) -> tuple:
    """
    Validate a generated preview image.

    Returns ``(passed: bool, reason: str)``.  Size-limit violations are treated
    as warnings (still passes), but all-black / all-white images hard-fail.
    """
    p = Path(preview_path)

    if not p.is_file():
        return False, "File does not exist or is not a regular file"
    if p.stat().st_size == 0:
        return False, "File is empty (0 bytes)"

    try:
        img = Image.open(p)
        img.load()
    except Exception as exc:
        return False, f"Cannot open image: {exc}"

    w, h = img.size
    if w < min_dimension and h < min_dimension:
        return False, f"Dimensions {w}x{h} below minimum {min_dimension}"

    is_gif = p.suffix.lower() == ".gif"
    file_kb = p.stat().st_size / 1024
    if is_gif and file_kb > max_dynamic_size_kb:
        logging.warning("Preview %s exceeds dynamic size limit (%.1f KB)", p, file_kb)
    elif not is_gif and file_kb > max_static_size_kb:
        logging.warning("Preview %s exceeds static size limit (%.1f KB)", p, file_kb)

    rgb = img.convert("RGB")
    extrema = rgb.getextrema()
    max_val = max(ch[1] for ch in extrema)
    min_val = min(ch[0] for ch in extrema)

    if max_val == 0:
        return False, "Image is all black"
    if min_val == 255:
        return False, "Image is all white / blank"

    return True, ""


def _iter_windows_blender_candidates() -> List[Path]:
    candidates: List[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(env_name, "").strip()
        if not base:
            continue
        root = Path(base)
        patterns = [
            "Blender Foundation/Blender/blender.exe",
            "Blender Foundation/Blender */blender.exe",
            "Programs/Blender Foundation/Blender/blender.exe",
            "Programs/Blender Foundation/Blender */blender.exe",
        ]
        for pattern in patterns:
            candidates.extend(root.glob(pattern))
    return [p for p in candidates if p.is_file()]


def _find_blender_from_registry() -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None

    key_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\blender.exe",
    ]
    hives = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
    for hive in hives:
        for key_path in key_paths:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                if value and Path(value).is_file():
                    return value
            except OSError:
                continue
    return None


def find_blender_executable() -> Optional[str]:
    """返回 Blender 可执行文件路径；优先环境变量，其次 PATH、注册表与常见安装目录。"""
    env = os.environ.get("BLENDER_EXE", "").strip()
    if env and Path(env).is_file():
        return env
    for name in ("blender", "blender.exe"):
        p = shutil.which(name)
        if p:
            return p
    reg_path = _find_blender_from_registry()
    if reg_path:
        return reg_path
    candidates = sorted(_iter_windows_blender_candidates(), reverse=True)
    if candidates:
        return str(candidates[0])
    return None


def run_blender_fbx_to_frames(
    fbx_path: Path,
    frames_dir: Path,
    frame_count: int,
    size: int,
    blender_exe: str,
) -> bool:
    script = Path(__file__).resolve().parent / "blender_render_fbx_frames.py"
    if not script.is_file():
        return False
    frames_dir.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            [
                blender_exe,
                "-b",
                "--python",
                str(script),
                "--",
                str(fbx_path.resolve()),
                str(frames_dir.resolve()),
                str(frame_count),
                str(size),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if r.returncode != 0:
            logging.warning(
                "Blender FBX 渲染失败 (code=%s): %s",
                r.returncode,
                (r.stderr or r.stdout or "")[:2000],
            )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        logging.warning("Blender 调用异常: %s", e)
        return False


def create_gif_from_png_paths(
    png_paths: List[Path],
    output_path: Path,
    duration_ms: int = 80,
) -> None:
    paths = sorted(png_paths)
    if not paths:
        raise ValueError("no png frames")
    images: List[Image.Image] = []
    for p in paths:
        with Image.open(p) as im:
            images.append(im.convert("RGB").copy())
    if len(images) == 1:
        images.append(images[0].copy())
    first, *rest = images
    first.save(
        output_path,
        save_all=True,
        append_images=rest,
        duration=duration_ms,
        loop=0,
        optimize=True,
    )


def write_placeholder_model_gif(model_path: Path, output_path: Path) -> None:
    """无 Blender 或渲染失败时的占位 GIF（多帧纯色渐变）。"""
    frames: List[Image.Image] = []
    for i in range(8):
        frames.append(Image.new("RGB", (512, 512), (28 + i * 6, 40, 65 + i * 10)))
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=120,
        loop=0,
    )

class ThumbnailGenerator:
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate_thumbnail(
        self,
        input_path: str,
        max_size: int = 512,
        output_name: Optional[str] = None,
    ) -> str:
        """
        Generate a thumbnail for an image file.

        Args:
            input_path: Path to the input image file.
            max_size: Long-edge upper bound in pixels (short edge scales proportionally).
            output_name: 若指定，则预览文件使用该文件名（避免不同目录同名资源冲突）。

        Returns:
            str: Path to the generated thumbnail.
        """
        try:
            input_path = Path(input_path)
            if output_name:
                output_path = self.output_dir / output_name
            else:
                output_path = self.output_dir / f"{input_path.stem}_thumbnail.webp"

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._create_thumbnail, input_path, output_path, max_size)

            logging.info(f"Thumbnail generated: {output_path}")
            return str(output_path)
        except Exception as e:
            logging.error(f"Failed to generate thumbnail for {input_path}: {e}")
            raise

    async def generate_preview(
        self,
        input_path: str,
        content_md5: str,
        max_size: int = 512,
    ) -> str:
        """
        Generate a preview image named ``{content_md5}_preview.webp``.

        Falls back to PNG if the Pillow webp encoder is unavailable.
        """
        input_path = Path(input_path)
        ext = "webp"
        try:
            Image.new("RGB", (1, 1)).save(
                self.output_dir / "__webp_probe__.webp", format="WEBP"
            )
            (self.output_dir / "__webp_probe__.webp").unlink(missing_ok=True)
        except Exception:
            ext = "png"

        output_name = f"{content_md5}_preview.{ext}"
        return await self.generate_thumbnail(
            str(input_path), max_size=max_size, output_name=output_name
        )

    def _create_thumbnail(self, input_path: Path, output_path: Path, max_size: int):
        with Image.open(input_path) as img:
            if img.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            img.thumbnail((max_size, max_size))
            img.save(output_path)

    async def generate_gif(self, input_dir: str, output_name: str = "output.gif") -> str:
        """
        Generate a GIF from a sequence of images in a directory.

        Args:
            input_dir (str): Path to the directory containing images.
            output_name (str): Name of the output GIF file.

        Returns:
            str: Path to the generated GIF.
        """
        try:
            input_dir = Path(input_dir)
            output_path = self.output_dir / output_name

            images = []
            for image_file in sorted(input_dir.glob("*.png")):
                with Image.open(image_file) as img:
                    images.append(img.copy())

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._create_gif, images, output_path)

            logging.info(f"GIF generated: {output_path}")
            return str(output_path)
        except Exception as e:
            logging.error(f"Failed to generate GIF: {e}")
            raise

    def _create_gif(self, images: list, output_path: Path):
        images[0].save(output_path, save_all=True, append_images=images[1:], loop=0, duration=500)

    async def generate_fbx_preview_gif(
        self,
        model_path: str,
        output_name: str,
        frame_count: int = 24,
        frame_size: int = 512,
    ) -> str:
        """
        为 FBX 生成旋转预览 GIF：优先调用 Blender（PATH 或 BLENDER_EXE）渲染帧序列；
        不可用时写入 Pillow 占位多帧 GIF。
        """
        result = await self.generate_fbx_preview_gif_result(
            model_path,
            output_name,
            frame_count=frame_count,
            frame_size=frame_size,
        )
        return result["path"]

    async def generate_fbx_preview_gif_result(
        self,
        model_path: str,
        output_name: str,
        frame_count: int = 24,
        frame_size: int = 512,
    ) -> Dict[str, object]:
        """
        返回带元数据的 FBX 预览结果，供上层判断是否是占位图。
        """
        model_path = Path(model_path)
        if model_path.suffix.lower() != ".fbx":
            raise ValueError("generate_fbx_preview_gif 仅支持 .fbx 文件")
        output_path = self.output_dir / output_name

        def work() -> Dict[str, object]:
            blender = find_blender_executable()
            with tempfile.TemporaryDirectory(prefix="fbx_prev_") as tmp:
                fdir = Path(tmp) / "frames"
                if blender and run_blender_fbx_to_frames(
                    model_path, fdir, frame_count, frame_size, blender
                ):
                    pngs = sorted(fdir.glob("frame_*.png"))
                    if pngs:
                        try:
                            create_gif_from_png_paths(pngs, output_path)
                            logging.info("FBX preview GIF (Blender): %s", output_path)
                            return {
                                "path": str(output_path.resolve()),
                                "renderer": "blender",
                                "used_placeholder": False,
                            }
                        except OSError as e:
                            logging.warning("GIF 合成失败，使用占位: %s", e)
                elif not blender:
                    logging.warning(
                        "未找到 Blender，FBX 预览将写入占位 GIF。"
                        " 请安装 Blender 并加入 PATH，或设置 BLENDER_EXE。"
                    )
                write_placeholder_model_gif(model_path, output_path)
                logging.info("FBX preview GIF (placeholder): %s", output_path)
                return {
                    "path": str(output_path.resolve()),
                    "renderer": "placeholder",
                    "used_placeholder": True,
                }

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, work)

    async def render_model_thumbnail(self, model_path: str) -> str:
        """
        3D 模型预览：.fbx 生成 GIF；其它格式仍为占位（空字符串）。
        """
        mp = Path(model_path)
        if mp.suffix.lower() == ".fbx":
            return await self.generate_fbx_preview_gif(str(mp), f"{mp.stem}_preview.gif")
        logging.info("Rendering thumbnail for model (unsupported type): %s", model_path)
        return ""