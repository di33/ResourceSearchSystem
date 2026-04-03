"""
用真实资源目录跑通：筛选 -> 增量拷贝分类 -> 预览缩略图 -> 生成 resources.json。

在项目根目录执行：
  python Scripts/run_resource_pipeline.py --source D:\\你的资源目录 --work-dir D:\\输出目录

同一 work-dir 重复执行：通过 .pipeline_state.json 记录「源文件绝对路径 + 内容指纹」，
源文件未变化时跳过再次拷贝与再次生成预览图。

预览：位图用 Pillow 生成 PNG 缩略图；.fbx 在 work-dir/previews/ 下生成旋转预览 GIF
（优先自动发现 Blender，也可设置环境变量 BLENDER_EXE；否则使用占位 GIF）。
其它模型格式暂不生成预览。
"""
from __future__ import annotations

import argparse
import os
import sys

# ResourceProcessor 在 Scripts/ 下：将 Scripts 加入 path 才能 import ResourceProcessor
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_SCRIPTS_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ResourceProcessor.deps import ensure_requirements  # noqa: E402

ensure_requirements()

from ResourceProcessor.pipeline_incremental import (  # noqa: E402
    build_index_extra,
    load_state,
    resolve_copies,
    run_previews_sync,
    save_state,
)
from ResourceProcessor.resource_filter import (  # noqa: E402
    filter_resources,
    generate_resource_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="资源处理流程（筛选 / 增量拷贝 / 预览缩略图 / 索引）"
    )
    parser.add_argument(
        "--source",
        required=True,
        help="原始资源根目录（将递归扫描）",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        help="工作输出目录（将创建 images/models/others/previews 等子目录）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="resource_types.json 路径；默认使用 <项目根>/resource_types.json",
    )
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=None,
        help="单文件最大字节数，超出则跳过",
    )
    parser.add_argument(
        "--max-file-count",
        type=int,
        default=None,
        help="最多纳入多少个文件",
    )
    parser.add_argument(
        "--no-previews",
        action="store_true",
        help="不生成 Pillow 缩略图（仍写入/更新状态与索引中的 copied_path）",
    )
    args = parser.parse_args()

    source = os.path.abspath(args.source)
    work_dir = os.path.abspath(args.work_dir)
    config_path = (
        os.path.abspath(args.config)
        if args.config
        else os.path.join(_ROOT, "resource_types.json")
    )

    if not os.path.isdir(source):
        print(f"错误：资源目录不存在: {source}", file=sys.stderr)
        return 1
    if not os.path.isfile(config_path):
        print(
            f"错误：配置文件不存在: {config_path}\n"
            "请创建 JSON，例如：\n"
            '  {"supported_extensions": [".png", ".jpg", ".jpeg", ".fbx", ".obj"]}',
            file=sys.stderr,
        )
        return 1

    os.makedirs(work_dir, exist_ok=True)
    for sub in ("images", "models", "others", "previews"):
        os.makedirs(os.path.join(work_dir, sub), exist_ok=True)

    print(f"配置: {config_path}")
    print(f"扫描: {source}")
    paths = filter_resources(
        source,
        config_path,
        max_file_size=args.max_file_size,
        max_file_count=args.max_file_count,
    )
    print(f"筛选通过: {len(paths)} 个文件")

    if not paths:
        print("没有符合条件的文件（检查扩展名、文件是否至少 4 字节、resource_types.json）。")
        return 0

    state = load_state(work_dir)
    mapping = resolve_copies(paths, work_dir, state)
    skipped = len(paths) - len(mapping)
    if skipped:
        print(f"拷贝失败跳过: {skipped} 个")

    if not args.no_previews and mapping:
        print("生成预览缩略图（已处理且未改动的源文件将自动跳过）…")
        run_previews_sync(mapping, work_dir, state)

    save_state(work_dir, state)

    index_path = os.path.join(work_dir, "resources.json")
    extra = build_index_extra(paths, state)
    statuses = {p: ("completed" if p in mapping else "failed") for p in paths}
    generate_resource_index(
        paths,
        index_path,
        dependencies={p: [] for p in paths},
        statuses=statuses,
        extra=extra,
    )
    print(f"索引已写入: {index_path}")
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
