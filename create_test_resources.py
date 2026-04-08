"""
生成一组测试用资源文件，用于验证完整流水线。

用法：
  python create_test_resources.py                        # 默认在 ./test_resources 下生成
  python create_test_resources.py --output D:\test_res   # 指定输出目录
"""
from __future__ import annotations

import argparse
import os
import struct

from PIL import Image


def create_loose_files(base_dir: str):
    """Create loose files directly in source root — each becomes its own resource."""
    configs = [
        ("brick_wall.png", (256, 256), (180, 100, 60)),
        ("grass_tile.png", (128, 128), (80, 160, 50)),
        ("sky_gradient.png", (256, 128), (100, 150, 220)),
    ]
    for name, size, color in configs:
        img = Image.new("RGB", size, color)
        for x in range(0, size[0], 16):
            for y in range(0, size[1], 16):
                shade = ((x + y) % 64) - 32
                r = max(0, min(255, color[0] + shade))
                g = max(0, min(255, color[1] + shade))
                b = max(0, min(255, color[2] + shade))
                for dx in range(min(8, size[0] - x)):
                    for dy in range(min(8, size[1] - y)):
                        img.putpixel((x + dx, y + dy), (r, g, b))
        path = os.path.join(base_dir, name)
        img.save(path)
        print(f"  [loose] {path} ({size[0]}x{size[1]})")


def create_sample_model_dir(base_dir: str):
    """Create a directory with a fake .obj file (valid enough for pipeline scanning)."""
    model_dir = os.path.join(base_dir, "character_model")
    os.makedirs(model_dir, exist_ok=True)

    obj_content = """# Simple cube OBJ
v  0.0  0.0  0.0
v  1.0  0.0  0.0
v  1.0  1.0  0.0
v  0.0  1.0  0.0
v  0.0  0.0  1.0
v  1.0  0.0  1.0
v  1.0  1.0  1.0
v  0.0  1.0  1.0
f 1 2 3 4
f 5 6 7 8
f 1 2 6 5
f 2 3 7 6
f 3 4 8 7
f 4 1 5 8
"""
    obj_path = os.path.join(model_dir, "cube.obj")
    with open(obj_path, "w") as f:
        f.write(obj_content)
    print(f"  创建: {obj_path}")

    tex_path = os.path.join(model_dir, "cube_texture.png")
    img = Image.new("RGB", (64, 64), (200, 180, 100))
    img.save(tex_path)
    print(f"  创建: {tex_path}")


def create_mixed_dir(base_dir: str):
    """Create a directory with mixed file types."""
    mix_dir = os.path.join(base_dir, "ui_assets")
    os.makedirs(mix_dir, exist_ok=True)

    for name, size, color in [
        ("button_normal.png", (200, 60), (60, 120, 200)),
        ("button_hover.png", (200, 60), (80, 140, 230)),
        ("icon_star.png", (48, 48), (255, 200, 50)),
    ]:
        img = Image.new("RGBA", size, color + (255,))
        path = os.path.join(mix_dir, name)
        img.save(path)
        print(f"  创建: {path}")


def main():
    parser = argparse.ArgumentParser(description="生成测试资源文件")
    parser.add_argument("--output", default="test_resources", help="输出目录")
    args = parser.parse_args()

    output = os.path.abspath(args.output)
    os.makedirs(output, exist_ok=True)

    print(f"生成测试资源到: {output}\n")
    print("--- 散文件（每个文件 = 独立资源）---")
    create_loose_files(output)
    print("\n--- 子文件夹（整个文件夹 = 一个资源）---")
    create_sample_model_dir(output)
    create_mixed_dir(output)
    print(f"\n完成！")
    print(f"  散文件: 3 个独立资源")
    print(f"  文件夹: 2 个多文件资源 (character_model, ui_assets)")
    print(f"  共 5 个资源。使用方法：")
    print(f"  python test_pipeline.py --source {output}")


if __name__ == "__main__":
    main()
