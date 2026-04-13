from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CLIENT_SCRIPTS = _SCRIPT_DIR
if _CLIENT_SCRIPTS not in sys.path:
    sys.path.insert(0, _CLIENT_SCRIPTS)


def _load_dotenv(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def _init_env() -> dict[str, str]:
    project_root = Path(_SCRIPT_DIR).resolve().parents[1]
    dotenv = _load_dotenv(str(project_root / ".env"))
    for key, value in dotenv.items():
        if value and key not in os.environ:
            os.environ[key] = value
    return dotenv


_DOTENV = _init_env()


def _env(key: str, fallback: str = "") -> str:
    return os.environ.get(key, _DOTENV.get(key, fallback))


from ResourceProcessor.crawler.catalog_loader import load_crawler_catalog  # noqa: E402
from ResourceProcessor.crawler.resource_adapter import build_description_input, build_processing_entity  # noqa: E402
from ResourceProcessor.core.upload_pipeline import upload_enriched_resources  # noqa: E402
from ResourceProcessor.description.description_generator import generate_resource_description  # noqa: E402
from ResourceProcessor.preview.crawler_thumbnail_policy import CrawlerThumbnailPolicy  # noqa: E402
from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy  # noqa: E402

try:
    import ResourceProcessor.description.dashscope_llm_provider  # noqa: F401,E402
except Exception:
    pass
try:
    import ResourceProcessor.description.zhipu_llm_provider  # noqa: F401,E402
except Exception:
    pass
try:
    import ResourceProcessor.description.ksyun_llm_provider  # noqa: F401,E402
except Exception:
    pass


class Report:
    def __init__(self):
        self.steps: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.t0 = time.time()

    def ok(self, step: str, detail: str = ""):
        self.steps.append({"step": step, "status": "OK", "detail": detail})
        self._print("OK", step, detail)

    def fail(self, step: str, detail: str = ""):
        self.steps.append({"step": step, "status": "FAIL", "detail": detail})
        self.errors.append(f"{step}: {detail}")
        self._print("FAIL", step, detail)

    def _print(self, status: str, step: str, detail: str):
        color = "\033[92m" if status == "OK" else "\033[91m"
        reset = "\033[0m"
        msg = f"  {color}[{status}]{reset} {step}"
        if detail:
            msg += f"  ({detail})"
        print(msg)

    def summary(self) -> bool:
        elapsed = time.time() - self.t0
        print("\n" + "=" * 60)
        print(f"  Crawler 资源流程完成  耗时 {elapsed:.1f}s")
        print(f"  通过: {sum(1 for s in self.steps if s['status'] == 'OK')}  失败: {len(self.errors)}")
        if self.errors:
            print("  失败详情：")
            for error in self.errors:
                print(f"    - {error}")
        print("=" * 60)
        return not self.errors


async def _generate_previews(resources, previews_dir: str) -> int:
    policy = CrawlerThumbnailPolicy(previews_dir)
    count = 0
    for resource in resources:
        resource.previews = await policy.generate_previews(resource)
        count += sum(1 for preview in resource.previews if preview.path)
    return count


async def _generate_descriptions(resources, provider_name: str, report: Report) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    total = len(resources)
    for index, resource in enumerate(resources, start=1):
        desc_input = build_description_input(resource)
        try:
            result = await generate_resource_description(desc_input, provider_name=provider_name)
            resource.description_main = result.main_content
            resource.description_detail = result.detail_content
            resource.description_full = result.full_description
            resource.prompt_version = result.prompt_version
            enriched.append(
                {
                    "resource": resource,
                    "resource_type": resource.resource_type,
                    "description": {
                        "main": result.main_content,
                        "detail": result.detail_content,
                        "full": result.full_description,
                    },
                }
            )
        except Exception as exc:
            report.fail(f"描述 [{resource.title or resource.resource_path or resource.content_md5[:12]}]", str(exc)[:120])
            enriched.append(
                {
                    "resource": resource,
                    "resource_type": resource.resource_type,
                    "description": {"main": "", "detail": "", "full": ""},
                }
            )
        if index % 5 == 0 or index == total:
            print(f"    描述进度: {index}/{total}")
    return enriched


def _iter_count(catalog, limit: int | None, resource_type: str, source_filter: str) -> int:
    count = 0
    for _ in catalog.iter_resources(limit=limit, resource_type=resource_type, source_filter=source_filter):
        count += 1
    return count


def _append_jsonl(path: str, row: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _iter_jsonl(path: str):
    if not os.path.isfile(path):
        return
    with open(path, "rb") as f:
        for raw_line in f:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue


def _load_resume_state(resources_jsonl_path: str, results_jsonl_path: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    preview_state: dict[str, dict[str, Any]] = {}
    result_state: dict[str, dict[str, Any]] = {}

    for row in _iter_jsonl(resources_jsonl_path) or []:
        key = _resume_key(row)
        if key:
            preview_state[key] = row

    for row in _iter_jsonl(results_jsonl_path) or []:
        key = _resume_key(row)
        if key:
            result_state[key] = row

    return preview_state, result_state


def _has_valid_previews(row: dict[str, Any]) -> bool:
    paths = row.get("preview_paths", [])
    if not isinstance(paths, list) or not paths:
        return False
    return all(isinstance(path, str) and os.path.isfile(path) for path in paths)


def _has_valid_description(row: dict[str, Any]) -> bool:
    return bool(str(row.get("description_full") or "").strip())


def _restore_preview_paths(resource, row: dict[str, Any]) -> None:
    preview_paths = row.get("preview_paths", [])
    if not isinstance(preview_paths, list):
        return
    resource.previews = []
    for idx, path in enumerate(preview_paths):
        if isinstance(path, str) and os.path.isfile(path):
            resource.previews.append(
                PreviewInfo(
                    strategy=PreviewStrategy.STATIC,
                    path=path,
                    role="primary" if idx == 0 else "gallery",
                    mode="resume",
                    confidence="high",
                )
            )


def _resource_summary_row(resource) -> dict[str, Any]:
    return {
        "resource_id": resource.resource_id,
        "source_resource_id": resource.source_resource_id,
        "content_md5": resource.content_md5,
        "pack_name": resource.pack_name,
        "title": resource.title,
        "resource_type": resource.resource_type,
        "resource_path": resource.resource_path,
        "file_count": len(resource.files),
        "missing_file_count": len(resource.missing_files),
        "preview_paths": [preview.path for preview in resource.previews if preview.path],
    }


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


async def _generate_description_with_retry(
    resource,
    llm_provider: str,
    report: Report,
    max_attempts: int = 6,
    base_delay_seconds: float = 3.0,
    success_delay_seconds: float = 0.35,
) -> dict[str, str]:
    desc_input = build_description_input(resource)
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = await generate_resource_description(desc_input, provider_name=llm_provider)
            resource.description_main = result.main_content
            resource.description_detail = result.detail_content
            resource.description_full = result.full_description
            resource.prompt_version = result.prompt_version
            if success_delay_seconds > 0:
                await asyncio.sleep(success_delay_seconds)
            return {
                "main": result.main_content,
                "detail": result.detail_content,
                "full": result.full_description,
            }
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_rate_limit_error(exc):
                break
            delay = base_delay_seconds * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0)
            report.ok(
                f"限流退避 [{resource.title or resource.resource_path or resource.content_md5[:12]}]",
                f"第 {attempt} 次重试，等待 {delay:.1f}s",
            )
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("description generation failed without exception")


def _result_row(item: dict[str, Any]) -> dict[str, Any]:
    resource = item["resource"]
    description = item["description"]
    return {
        "resource_id": resource.resource_id,
        "source_resource_id": resource.source_resource_id,
        "content_md5": resource.content_md5,
        "pack_name": resource.pack_name,
        "title": resource.title,
        "resource_type": item["resource_type"],
        "resource_path": resource.resource_path,
        "file_count": len(resource.files),
        "missing_file_count": len(resource.missing_files),
        "preview_count": len(resource.previews),
        "preview_paths": [preview.path for preview in resource.previews if preview.path],
        "description_main": description["main"],
        "description_detail": description["detail"],
        "description_full": description["full"],
    }


def _resume_key(value: Any) -> str:
    def _pick(name: str) -> str:
        if isinstance(value, dict):
            return str(value.get(name, "") or "")
        return str(getattr(value, name, "") or "")

    source_resource_id = _pick("source_resource_id")
    if source_resource_id:
        return f"source:{source_resource_id}"

    content_md5 = _pick("content_md5")
    if content_md5:
        return f"md5:{content_md5}"

    resource_path = _pick("resource_path")
    if resource_path:
        return f"path:{resource_path}"

    title = _pick("title")
    if title:
        return f"title:{title}"

    return ""


async def _process_all_resources(
    *,
    catalog,
    limit: int | None,
    resource_type: str,
    source_filter: str,
    previews_dir: str,
    llm_provider: str,
    report: Report,
    resources_jsonl_path: str,
    results_jsonl_path: str,
    no_previews: bool,
    no_upload: bool,
    server: str,
    resume: bool,
) -> dict[str, Any]:
    total = _iter_count(catalog, limit=limit, resource_type=resource_type, source_filter=source_filter)
    if total <= 0:
        report.fail("装载资源目录", "未找到匹配的资源记录")
        return {"total": 0, "preview_count": 0, "desc_ok": 0, "upload_summary": None}

    report.ok("装载资源目录", f"读取 {total} 条资源记录")
    policy = CrawlerThumbnailPolicy(previews_dir)
    preview_state, result_state = _load_resume_state(resources_jsonl_path, results_jsonl_path) if resume else ({}, {})
    resumed_preview_count = sum(1 for row in preview_state.values() if _has_valid_previews(row))
    resumed_desc_count = sum(1 for row in result_state.values() if _has_valid_description(row))
    if resume:
        report.ok("断点续跑状态", f"已有预览 {resumed_preview_count} 条，已有描述 {resumed_desc_count} 条")
    processed = 0
    preview_count = resumed_preview_count
    desc_ok = resumed_desc_count
    upload_totals = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "skipped_no_files": 0,
        "skipped_no_description": 0,
    }

    for record in catalog.iter_resources(limit=limit, resource_type=resource_type, source_filter=source_filter):
        resource = build_processing_entity(record)
        resource_key = _resume_key(resource)
        existing_preview = preview_state.get(resource_key, {})
        existing_result = result_state.get(resource_key, {})
        has_preview = _has_valid_previews(existing_preview)
        has_description = _has_valid_description(existing_result)

        if not no_previews and not has_preview:
            resource.previews = await policy.generate_previews(resource)
            preview_count += sum(1 for preview in resource.previews if preview.path)
            preview_state[resource_key] = _resource_summary_row(resource)
            _append_jsonl(resources_jsonl_path, preview_state[resource_key])
        elif has_preview:
            _restore_preview_paths(resource, existing_preview)

        desc_payload = {"main": "", "detail": "", "full": ""}
        if has_description:
            desc_payload = {
                "main": str(existing_result.get("description_main") or ""),
                "detail": str(existing_result.get("description_detail") or ""),
                "full": str(existing_result.get("description_full") or ""),
            }
            resource.description_main = desc_payload["main"]
            resource.description_detail = desc_payload["detail"]
            resource.description_full = desc_payload["full"]
        else:
            try:
                desc_payload = await _generate_description_with_retry(
                    resource,
                    llm_provider=llm_provider,
                    report=report,
                )
                if desc_payload["full"]:
                    desc_ok += 1
            except Exception as exc:
                report.fail(f"描述 [{resource.title or resource.resource_path or resource.content_md5[:12]}]", str(exc)[:120])

        item = {
            "resource": resource,
            "resource_type": resource.resource_type,
            "description": desc_payload,
        }

        if not has_preview and no_previews:
            preview_state[resource_key] = _resource_summary_row(resource)
            _append_jsonl(resources_jsonl_path, preview_state[resource_key])
        if not has_description:
            result_state[resource_key] = _result_row(item)
            _append_jsonl(results_jsonl_path, result_state[resource_key])

        if not no_upload:
            summary = upload_enriched_resources(
                [item],
                server,
                reporter=lambda status, step, detail: report.ok(step, detail) if status == "OK" else report.fail(step, detail),
            )
            upload_totals["success_count"] += summary.success_count
            upload_totals["failed_count"] += summary.failed_count
            upload_totals["skipped_count"] += summary.skipped_count
            upload_totals["skipped_no_files"] += summary.skipped_no_files
            upload_totals["skipped_no_description"] += summary.skipped_no_description

        processed += 1
        if processed % 25 == 0 or processed == total:
            print(
                f"    进度: {processed}/{total} | "
                f"预览累计 {preview_count} | 描述成功 {desc_ok}"
            )

    return {
        "total": processed,
        "preview_count": preview_count,
        "desc_ok": desc_ok,
        "upload_summary": upload_totals if not no_upload else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="基于 ResourceCrawler output 的资源级处理流水线")
    parser.add_argument("--crawler-output", required=True, help="ResourceCrawler output 根目录")
    parser.add_argument("--work-dir", default=None, help="工作输出目录（默认 ./test_workdir_crawler）")
    parser.add_argument("--server", default=None, help="服务端地址")
    parser.add_argument("--llm-provider", default=None, help="描述生成 provider")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少个资源")
    parser.add_argument("--resource-type", default="", help="只处理指定资源类型")
    parser.add_argument("--source-filter", default="", help="只处理指定来源站点")
    parser.add_argument("--no-previews", action="store_true", help="跳过预览生成")
    parser.add_argument("--no-upload", action="store_true", help="仅本地生成，不上传")
    parser.add_argument("--resume", action="store_true", help="从已有 jsonl 状态断点续跑")
    args = parser.parse_args()

    crawler_output = os.path.abspath(args.crawler_output)
    if not os.path.isdir(crawler_output):
        print(f"错误：crawler output 目录不存在: {crawler_output}", file=sys.stderr)
        return 1

    project_root = Path(_SCRIPT_DIR).resolve().parents[1]
    work_dir = os.path.abspath(args.work_dir) if args.work_dir else str(project_root / "test_workdir_crawler")
    previews_dir = os.path.join(work_dir, "previews")
    os.makedirs(previews_dir, exist_ok=True)

    server = args.server or _env("TEST_SERVER_URL", "http://localhost:8000")
    llm_provider = args.llm_provider or _env("CLIENT_LLM_PROVIDER", "mock")

    report = Report()
    print("=" * 60)
    print("  ResourceCrawler 资源级处理流程")
    print(f"  Crawler Output: {crawler_output}")
    print(f"  工作目录:       {work_dir}")
    print(f"  服务端:         {server}")
    print(f"  LLM:            {llm_provider}")
    print("=" * 60)

    catalog = load_crawler_catalog(crawler_output)
    resources_jsonl_path = os.path.join(work_dir, "crawler_resources.jsonl")
    results_jsonl_path = os.path.join(work_dir, "test_results.jsonl")
    if not args.resume:
        for path in (resources_jsonl_path, results_jsonl_path):
            if os.path.exists(path):
                os.remove(path)
    report.ok(
        "检查索引输出",
        f"{resources_jsonl_path} + {results_jsonl_path}" + (" (resume)" if args.resume else ""),
    )

    processing = asyncio.run(
        _process_all_resources(
            catalog=catalog,
            limit=args.limit,
            resource_type=args.resource_type,
            source_filter=args.source_filter,
            previews_dir=previews_dir,
            llm_provider=llm_provider,
            report=report,
            resources_jsonl_path=resources_jsonl_path,
            results_jsonl_path=results_jsonl_path,
            no_previews=args.no_previews,
            no_upload=args.no_upload,
            server=server,
            resume=args.resume,
        )
    )

    if processing["total"] <= 0:
        report.summary()
        return 1

    if args.no_previews:
        report.ok("预览生成", "已跳过 (--no-previews)")
    else:
        report.ok("预览生成", f"生成 {processing['preview_count']} 个预览")
    report.ok("描述生成", f"{processing['desc_ok']}/{processing['total']} 成功")

    if args.no_upload:
        report.ok("上传", "已跳过 (--no-upload)")
    else:
        upload_summary = processing["upload_summary"] or {}
        report.ok(
            "上传汇总",
            f"{upload_summary.get('success_count', 0)}/{processing['total']} 成功, "
            f"metadata-only 跳过 {upload_summary.get('skipped_no_files', 0)}, "
            f"描述失败跳过 {upload_summary.get('skipped_no_description', 0)}",
        )

    results_path = os.path.join(work_dir, "test_results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total": processing["total"],
                "preview_count": processing["preview_count"],
                "description_success_count": processing["desc_ok"],
                "resources_jsonl": resources_jsonl_path,
                "results_jsonl": results_jsonl_path,
                "steps": report.steps,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"\n详细结果已写入: {results_path}")
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
