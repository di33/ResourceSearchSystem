"""
端到端测试流程：扫描资源 -> 生成预览 -> 生成描述 -> 上传到服务端 -> 服务端自动生成向量 -> 汇报结果。

所有默认配置从项目根目录 .env 文件读取，命令行参数可覆盖。

用法：
  python test_pipeline.py --source D:\你的资源目录
  python test_pipeline.py --source D:\资源目录 --work-dir D:\输出目录
  python test_pipeline.py --source D:\资源目录 --no-upload

修改 .env 中的 CLIENT_LLM_PROVIDER / API Key 即可切换 provider，
无需改命令行。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CLIENT_SCRIPTS = os.path.join(_SCRIPT_DIR, "Client", "Scripts")
_SERVER_SCRIPTS = os.path.join(_SCRIPT_DIR, "Server", "Scripts")
for p in (_CLIENT_SCRIPTS, _SERVER_SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_dotenv(path: str) -> dict[str, str]:
    """Parse a .env file into a dict."""
    env: dict[str, str] = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            env[key] = value
    return env


def _init_env():
    """Load .env and set missing os.environ entries so providers pick up API keys."""
    dotenv_path = os.path.join(_SCRIPT_DIR, ".env")
    dotenv = _load_dotenv(dotenv_path)
    for k, v in dotenv.items():
        if v and k not in os.environ:
            os.environ[k] = v
    return dotenv


_dotenv = _init_env()


def _env(key: str, fallback: str = "") -> str:
    """Read a config value: os.environ > .env > fallback."""
    return os.environ.get(key, _dotenv.get(key, fallback))

from ResourceProcessor.core.deps import ensure_requirements  # noqa: E402
ensure_requirements()

from ResourceProcessor.preview.pipeline_incremental import (  # noqa: E402
    build_index_extra,
    get_resource_entities,
    load_state,
    resolve_copies,
    run_previews_sync,
    save_state,
)
from ResourceProcessor.core.resource_filter import (  # noqa: E402
    filter_resources,
)
from ResourceProcessor.description.description_generator import (  # noqa: E402
    DescriptionInput,
    generate_resource_description,
)

# Register real providers so the factories know about them.
try:
    import ResourceProcessor.description.dashscope_llm_provider  # noqa: F401
except Exception:
    pass
try:
    import ResourceProcessor.description.zhipu_llm_provider  # noqa: F401
except Exception:
    pass


# ── Helpers ──────────────────────────────────────────────────────────────

def _determine_resource_type(files: list[dict]) -> str:
    """Infer resource type from file extensions."""
    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
    model_exts = {".fbx", ".obj", ".gltf", ".glb", ".blend"}
    has_image = any(f.get("file_format", "").lower() in {e.lstrip(".") for e in image_exts} for f in files)
    has_model = any(f.get("file_format", "").lower() in {e.lstrip(".") for e in model_exts} for f in files)
    if has_model:
        return "model"
    if has_image:
        return "image"
    return "other"


class Report:
    """Accumulate step-by-step results for final summary."""

    def __init__(self):
        self.steps: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.t0 = time.time()

    def ok(self, step: str, detail: str = ""):
        self.steps.append({"step": step, "status": "OK", "detail": detail})
        _print_step("OK", step, detail)

    def fail(self, step: str, detail: str = ""):
        self.steps.append({"step": step, "status": "FAIL", "detail": detail})
        self.errors.append(f"{step}: {detail}")
        _print_step("FAIL", step, detail)

    def summary(self):
        elapsed = time.time() - self.t0
        ok_count = sum(1 for s in self.steps if s["status"] == "OK")
        fail_count = len(self.errors)
        print("\n" + "=" * 60)
        print(f"  测试流程完成  耗时 {elapsed:.1f}s")
        print(f"  通过: {ok_count}  失败: {fail_count}")
        if self.errors:
            print("\n  失败详情：")
            for e in self.errors:
                print(f"    - {e}")
        print("=" * 60)
        return fail_count == 0


def _print_step(status: str, step: str, detail: str):
    color = "\033[92m" if status == "OK" else "\033[91m"
    reset = "\033[0m"
    tag = f"{color}[{status}]{reset}"
    line = f"  {tag} {step}"
    if detail:
        line += f"  ({detail})"
    print(line)


# ── Step 1: Scan & Filter ───────────────────────────────────────────────

def step_scan(source: str, config_path: str, max_file_size: int | None, max_file_count: int | None, report: Report):
    print("\n── 步骤 1: 扫描与筛选资源 ──")
    paths = filter_resources(source, config_path, max_file_size=max_file_size, max_file_count=max_file_count)
    if not paths:
        report.fail("扫描", "未找到符合条件的资源文件")
        return []
    report.ok("扫描", f"找到 {len(paths)} 个文件")
    return paths


# ── Step 2: Copy & Preview ──────────────────────────────────────────────

def step_preview(paths: list[str], work_dir: str, no_previews: bool, source_root: str, report: Report):
    print("\n── 步骤 2: 增量拷贝与预览生成 ──")
    state = load_state(work_dir)
    mapping = resolve_copies(paths, work_dir, state)
    report.ok("拷贝", f"成功 {len(mapping)}/{len(paths)} 个")

    if not no_previews and mapping:
        run_previews_sync(mapping, work_dir, state)
        preview_count = sum(1 for v in state.get("by_source", {}).values() if v.get("preview_paths"))
        report.ok("预览", f"生成 {preview_count} 个预览")
    elif no_previews:
        report.ok("预览", "已跳过 (--no-previews)")
    else:
        report.ok("预览", "无可预览文件")

    save_state(work_dir, state)

    resources = get_resource_entities(state)
    report.ok("资源分组", f"{len(resources)} 个资源实体")
    return state, resources


# ── Step 3: Description only (embedding is server-side now) ─────────────

async def _gen_desc(resource: dict, llm_provider: str) -> dict:
    """Generate description for a single resource entity.

    Never raises — returns a degraded result on failure.
    """
    rtype = _determine_resource_type(resource.get("files", []))

    _empty_result = {
        "resource": resource,
        "resource_type": rtype,
        "description": {"main": "", "detail": "", "full": ""},
    }

    previews = resource.get("previews", [])
    preview_path = ""
    preview_strategy = "none"
    if previews:
        first = previews[0]
        preview_path = first.get("path", "")
        preview_strategy = first.get("strategy", "static")

    fmt = resource["files"][0]["file_format"] if resource.get("files") else "unknown"
    desc_input = DescriptionInput(
        preview_path=preview_path,
        resource_type=rtype,
        preview_strategy=preview_strategy,
        auxiliary_metadata={"format": fmt, "file_count": len(resource.get("files", []))},
    )

    try:
        desc_result = await generate_resource_description(desc_input, provider_name=llm_provider)
    except Exception as exc:
        _empty_result["description"]["full"] = ""
        return _empty_result

    return {
        "resource": resource,
        "resource_type": rtype,
        "description": {
            "main": desc_result.main_content,
            "detail": desc_result.detail_content,
            "full": desc_result.full_description,
        },
    }


_API_CONCURRENCY = 3


def step_describe(resources: list[dict], llm_provider: str, report: Report):
    print(f"\n── 步骤 3: 生成描述 ({len(resources)} 个资源, 并发={_API_CONCURRENCY}) ──")
    if not resources:
        report.fail("描述生成", "无资源实体")
        return []

    enriched = asyncio.run(_batch_describe(resources, llm_provider))

    desc_ok = sum(1 for e in enriched if e["description"]["full"])
    report.ok("描述生成", f"{desc_ok}/{len(resources)} 成功 (provider={llm_provider})")
    return enriched


async def _batch_describe(resources, llm_provider):
    sem = asyncio.Semaphore(_API_CONCURRENCY)
    done = 0
    total = len(resources)

    async def _wrapped(r):
        nonlocal done
        async with sem:
            result = await _gen_desc(r, llm_provider)
            done += 1
            if done % 5 == 0 or done == total:
                print(f"    进度: {done}/{total}")
            return result

    return await asyncio.gather(*[_wrapped(r) for r in resources])


# ── Step 4: Upload to Server ────────────────────────────────────────────

def step_upload(enriched: list[dict], server: str, report: Report):
    print("\n── 步骤 4: 上传到服务端（描述 + 文件 + 预览 -> 服务端自动生成向量） ──")
    if not enriched:
        report.fail("上传", "无可上传的资源")
        return

    # Health check first
    try:
        r = requests.get(f"{server}/health", timeout=5)
        health = r.json()
        if health.get("status") != "ok":
            report.fail("服务端健康检查", f"状态: {health.get('status')}")
            return
        report.ok("服务端健康检查", "所有组件正常")
    except Exception as e:
        report.fail("服务端健康检查", f"无法连接: {e}")
        return

    success_count = 0
    skip_count = 0
    for item in enriched:
        resource = item["resource"]
        rtype = item["resource_type"]
        desc = item["description"]
        files_info = resource.get("files", [])
        content_md5 = resource.get("content_md5", "")

        src_dir = resource.get("source_directory", "unknown")
        label = os.path.basename(src_dir) or content_md5[:12]

        if not desc.get("full"):
            skip_count += 1
            continue

        # 4a. Register
        try:
            reg_body = {
                "content_md5": content_md5,
                "resource_type": rtype,
                "files": [
                    {
                        "file_name": f["file_name"],
                        "file_size": f["file_size"],
                        "file_format": f["file_format"],
                        "content_md5": f["content_md5"],
                        "file_role": f.get("file_role", "main"),
                        "is_primary": f.get("is_primary", False),
                    }
                    for f in files_info
                ],
            }
            resp = requests.post(f"{server}/resources/register", json=reg_body, timeout=30)
            resp.raise_for_status()
            reg = resp.json()
            resource_id = reg["resource_id"]

            if reg.get("exists"):
                report.ok(f"上传 [{label}]", f"resource_id={resource_id} (云端已存在，跳过)")
                success_count += 1
                continue

            report.ok(f"注册 [{label}]", f"resource_id={resource_id}")
        except Exception as e:
            report.fail(f"注册 [{label}]", str(e)[:120])
            continue

        # 4b. Upload files
        try:
            upload_files = []
            for f in files_info:
                fp = f.get("file_path", "")
                if fp and os.path.isfile(fp):
                    upload_files.append(("files", (f["file_name"], open(fp, "rb"), "application/octet-stream")))
            if upload_files:
                resp = requests.post(f"{server}/resources/{resource_id}/upload-batch", files=upload_files, timeout=120)
                resp.raise_for_status()
                ub = resp.json()
                if not ub.get("success"):
                    report.fail(f"上传文件 [{label}]", ub.get("error_message", "unknown"))
                    continue
                report.ok(f"上传文件 [{label}]", f"{ub.get('file_count', 0)} 个文件, {ub.get('uploaded_bytes', 0)} bytes")
                for _, (_, fobj, _) in upload_files:
                    fobj.close()
        except Exception as e:
            report.fail(f"上传文件 [{label}]", str(e)[:120])
            continue

        # 4c. Upload previews
        try:
            previews = resource.get("previews", [])
            preview_files = []
            for p in previews:
                pp = p.get("path", "")
                if pp and os.path.isfile(pp):
                    fname = os.path.basename(pp)
                    ct = "image/gif" if pp.endswith(".gif") else "image/webp"
                    preview_files.append(("files", (fname, open(pp, "rb"), ct)))
            if preview_files:
                resp = requests.post(f"{server}/resources/{resource_id}/previews", files=preview_files, timeout=60)
                resp.raise_for_status()
                pu = resp.json()
                report.ok(f"上传预览 [{label}]", f"{pu.get('preview_count', 0)} 个预览")
                for _, (_, fobj, _) in preview_files:
                    fobj.close()
        except Exception as e:
            report.fail(f"上传预览 [{label}]", str(e)[:120])

        # 4d. Commit (server generates embedding automatically)
        try:
            commit_body = {
                "resource_type": rtype,
                "description_main": desc.get("main", ""),
                "description_detail": desc.get("detail", ""),
                "description_full": desc.get("full", ""),
            }
            resp = requests.post(f"{server}/resources/{resource_id}/commit", json=commit_body, timeout=30)
            resp.raise_for_status()
            cm = resp.json()
            if cm.get("state") == "committed":
                report.ok(f"提交 [{label}]", f"resource_id={resource_id}")
                success_count += 1
            else:
                report.fail(f"提交 [{label}]", cm.get("error_message", f"state={cm.get('state')}"))
        except Exception as e:
            report.fail(f"提交 [{label}]", str(e)[:120])

    detail = f"{success_count}/{len(enriched)} 个资源上传成功"
    if skip_count:
        detail += f", {skip_count} 个因描述失败而跳过"
    report.ok("上传汇总", detail)


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="端到端资源处理与上传测试（默认值从 .env 读取）",
    )
    parser.add_argument("--source", required=True, help="原始资源根目录")
    parser.add_argument("--work-dir", default=None, help="工作输出目录（默认 ./test_workdir）")
    parser.add_argument("--server", default=None, help="服务端地址")
    parser.add_argument("--config", default=None, help="resource_types.json 路径")
    parser.add_argument("--llm-provider", default=None, help="描述生成 provider")
    parser.add_argument("--max-file-size", type=int, default=None)
    parser.add_argument("--max-file-count", type=int, default=None)
    parser.add_argument("--no-upload", action="store_true", help="仅本地处理，不上传")
    parser.add_argument("--no-previews", action="store_true", help="跳过预览生成")
    args = parser.parse_args()

    server = args.server or _env("TEST_SERVER_URL", "http://localhost:8000")
    llm_provider = args.llm_provider or _env("CLIENT_LLM_PROVIDER", "mock")

    source = os.path.abspath(args.source)
    work_dir = os.path.abspath(args.work_dir) if args.work_dir else os.path.join(_SCRIPT_DIR, "test_workdir")
    config_path = os.path.abspath(args.config) if args.config else os.path.join(_SCRIPT_DIR, "Client", "resource_types.json")

    if not os.path.isdir(source):
        print(f"错误：资源目录不存在: {source}", file=sys.stderr)
        return 1
    if not os.path.isfile(config_path):
        print(f"错误：配置文件不存在: {config_path}", file=sys.stderr)
        return 1

    os.makedirs(work_dir, exist_ok=True)
    for sub in ("images", "models", "others", "previews"):
        os.makedirs(os.path.join(work_dir, sub), exist_ok=True)

    report = Report()
    print("=" * 60)
    print("  ResourceUpload 端到端测试流程")
    print(f"  资源目录: {source}")
    print(f"  工作目录: {work_dir}")
    print(f"  服务端:   {server}")
    print(f"  LLM:      {llm_provider}")
    print("  Embedding:  服务端自动生成（SERVER_EMBEDDING_* 配置）")
    print("=" * 60)

    # Step 1
    paths = step_scan(source, config_path, args.max_file_size, args.max_file_count, report)
    if not paths:
        report.summary()
        return 1

    # Step 2
    state, resources = step_preview(paths, work_dir, args.no_previews, source, report)

    # Step 3
    enriched = step_describe(resources, llm_provider, report)

    # Step 4
    if not args.no_upload:
        step_upload(enriched, server, report)
    else:
        report.ok("上传", "已跳过 (--no-upload)")

    # Write results
    results_path = os.path.join(work_dir, "test_results.json")
    serializable = []
    for item in enriched:
        entry = {
            "source_directory": item["resource"].get("source_directory", ""),
            "content_md5": item["resource"].get("content_md5", ""),
            "resource_type": item["resource_type"],
            "file_count": len(item["resource"].get("files", [])),
            "preview_count": len(item["resource"].get("previews", [])),
            "description_main": item["description"]["main"],
            "description_detail": item["description"]["detail"],
            "description_full": item["description"]["full"],
        }
        serializable.append(entry)

    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({"results": serializable, "steps": report.steps}, f, indent=2, ensure_ascii=False)
    print(f"\n详细结果已写入: {results_path}")

    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
