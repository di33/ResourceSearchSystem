"""Render previews by calling the preview-renderer HTTP service."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

import requests

from ResourceProcessor.pipeline_common import Report, env, make_arg_parser
from ResourceProcessor.submit_processing_manifest import load_manifest_records


_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "data" / "previews"
_FILENAME_RE = re.compile(r"filename\*?=(?P<value>[^;]+)", re.IGNORECASE)
_CONTENT_TYPE_EXT = {
    "image/webp": ".webp",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}
_SOURCE_OBJECT_KEYS = {
    "storage_profile_id",
    "object_key",
    "file_name",
    "file_format",
    "size",
    "checksum",
    "etag",
    "is_primary",
}


def _safe_part(value: str, fallback: str = "resource") -> str:
    text = str(value or "").strip().strip(" .")
    for char in '<>:"/\\|?*\x00':
        text = text.replace(char, "_")
    return text if text and text not in {".", ".."} else fallback


def _safe_file_name(value: str, fallback: str = "preview.webp") -> str:
    name = Path(str(value or "")).name
    name = _safe_part(name, fallback)
    if "/" in name or "\\" in name:
        raise ValueError(f"unsafe preview file name: {value}")
    return name


def _preview_file_name(value: str, file_prefix: str = "") -> str:
    name = _safe_file_name(value, "preview.webp")
    if not file_prefix:
        return name
    prefix = _safe_part(file_prefix)
    if name.startswith(f"{prefix}_"):
        return name
    return f"{prefix}_{name}"


def _filename_from_content_disposition(value: str) -> str:
    match = _FILENAME_RE.search(value or "")
    if not match:
        return ""
    raw = match.group("value").strip().strip('"')
    if raw.lower().startswith("utf-8''"):
        raw = unquote(raw[7:])
    return raw


def _resource_output_dir(output_root: Path, manifest: dict[str, Any]) -> Path:
    resource_type = str(manifest.get("resource_type") or "other")
    return output_root / _safe_part(resource_type, "other")


def _resource_file_prefix(manifest: dict[str, Any]) -> str:
    return _safe_part(str(manifest.get("client_resource_id") or "resource"))


def _file_ref_as_source_object(item: dict[str, Any]) -> dict[str, Any]:
    source_object = {key: item[key] for key in _SOURCE_OBJECT_KEYS if item.get(key) not in (None, "")}
    if "size" not in source_object and item.get("file_size") not in (None, ""):
        source_object["size"] = item["file_size"]
    if "checksum" not in source_object:
        checksum = item.get("content_md5") or item.get("content_hash")
        if checksum:
            source_object["checksum"] = checksum
    if "file_name" not in source_object:
        source_object["file_name"] = Path(str(item.get("object_key") or "")).name
    if "file_format" not in source_object:
        source_object["file_format"] = Path(str(source_object.get("file_name") or "")).suffix.lstrip(".").lower()
    return source_object


def _source_object_for_renderer(manifest: dict[str, Any]) -> dict[str, Any]:
    source_object = manifest.get("source_object")
    if isinstance(source_object, dict) and source_object.get("object_key"):
        return source_object

    source_files = [item for item in manifest.get("source_files") or [] if isinstance(item, dict)]
    file_refs = [item for item in source_files if item.get("object_key")]
    if file_refs:
        primary = next((item for item in file_refs if item.get("is_primary")), file_refs[0])
        return _file_ref_as_source_object(primary)

    package_object = manifest.get("package_object")
    if isinstance(package_object, dict) and package_object.get("object_key"):
        return package_object

    return source_object if isinstance(source_object, dict) else {}


def _source_object_url_for_renderer(source_object: dict[str, Any]) -> str:
    object_key = str(source_object.get("object_key") or "").strip()
    if not object_key:
        return ""
    from ObjectStorageUpload.uploader import ObjectStorageUploader

    uploader = ObjectStorageUploader(storage_profile_id=str(source_object.get("storage_profile_id") or None))
    return uploader.generate_download_url(object_key, expires=900)


def _extract_preview_zip(content: bytes, output_dir: Path, *, file_prefix: str = "") -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except KeyError as exc:
            raise ValueError("preview zip did not include manifest.json") from exc
        previews = []
        for item in manifest.get("previews") or []:
            if not isinstance(item, dict):
                continue
            archive_name = _safe_file_name(str(item.get("file_name") or ""), "preview.webp")
            file_name = _preview_file_name(archive_name, file_prefix)
            target = output_dir / file_name
            try:
                data = archive.read(archive_name)
            except KeyError as exc:
                raise ValueError(f"preview file listed in manifest is missing: {archive_name}") from exc
            target.write_bytes(data)
            saved = dict(item)
            saved["file_name"] = file_name
            saved["path"] = str(target)
            saved.setdefault("size", target.stat().st_size)
            previews.append(saved)
    if not previews:
        raise ValueError("preview zip contained no preview files")
    return {
        "client_resource_id": manifest.get("client_resource_id", ""),
        "preview_count": len(previews),
        "output_dir": str(output_dir),
        "previews": previews,
    }


def _save_primary_response(response: requests.Response, output_dir: Path, *, file_prefix: str = "") -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {}
    if response.headers.get("X-Preview-Metadata"):
        metadata = json.loads(response.headers["X-Preview-Metadata"])
    content_type = (
        response.headers.get("Content-Type")
        or metadata.get("content_type")
        or "application/octet-stream"
    ).split(";")[0]
    fallback = f"primary{_CONTENT_TYPE_EXT.get(content_type, '.webp')}"
    file_name = (
        metadata.get("file_name")
        or _filename_from_content_disposition(response.headers.get("Content-Disposition", ""))
        or fallback
    )
    file_name = _preview_file_name(str(file_name), file_prefix)
    target = output_dir / file_name
    target.write_bytes(response.content)
    saved = dict(metadata)
    saved.setdefault("role", "primary")
    saved.setdefault("file_name", file_name)
    saved.setdefault("content_type", content_type)
    saved.setdefault("size", target.stat().st_size)
    saved.setdefault("origin", "generated")
    saved.setdefault("renderer", "preview-renderer")
    saved["path"] = str(target)
    return {
        "client_resource_id": "",
        "preview_count": 1,
        "output_dir": str(output_dir),
        "previews": [saved],
    }


def _raise_for_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = response.text.strip()
        if body:
            raise requests.HTTPError(f"{exc} ({body[:500]})", response=response) from exc
        raise


def render_preview_manifest(
    manifest: dict[str, Any],
    *,
    preview_renderer: str,
    client_id: str,
    output_root: Path,
    api_key: str = "",
    primary_only: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    source_object = _source_object_for_renderer(manifest)
    payload = {
        "client_resource_id": manifest.get("client_resource_id", ""),
        "resource_type": manifest.get("resource_type", ""),
        "source_object": source_object,
        "source_object_url": _source_object_url_for_renderer(source_object),
        "source_files": manifest.get("source_files") or [],
    }
    output_dir = _resource_output_dir(output_root, manifest)
    file_prefix = _resource_file_prefix(manifest)
    endpoint = "render/primary" if primary_only else "render"
    headers = {"X-Client-Id": client_id}
    if api_key:
        headers["X-API-Key"] = api_key
    if not primary_only:
        headers["Accept"] = "application/zip"
    response = http.post(
        f"{preview_renderer.rstrip('/')}/previews/{endpoint}",
        json=payload,
        headers=headers,
        timeout=300,
    )
    _raise_for_status(response)
    if primary_only:
        result = _save_primary_response(response, output_dir, file_prefix=file_prefix)
        result["client_resource_id"] = payload["client_resource_id"]
        return result
    return _extract_preview_zip(response.content, output_dir, file_prefix=file_prefix)


def render_manifest_records(
    records: Iterable[tuple[int | None, dict[str, Any]]],
    *,
    preview_renderer: str,
    client_id: str,
    output_root: Path,
    api_key: str = "",
    primary_only: bool = False,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    results = []
    close_session = session is None
    http = session or requests.Session()
    try:
        for task_id, manifest in records:
            if dry_run:
                results.append({
                    "task_id": task_id,
                    "client_resource_id": manifest.get("client_resource_id", ""),
                    "output_dir": str(_resource_output_dir(output_root, manifest)),
                    "state": "dry_run",
                })
                continue
            result = render_preview_manifest(
                manifest,
                preview_renderer=preview_renderer,
                client_id=client_id,
                api_key=api_key,
                output_root=output_root,
                primary_only=primary_only,
                session=http,
            )
            if task_id is not None:
                result["task_id"] = task_id
            results.append(result)
    finally:
        if close_session:
            http.close()
    return results


def main() -> int:
    parser = make_arg_parser(
        "通过 preview-renderer 服务生成预览并保存到本地",
        extra_args=[
            ("--manifest", {"action": "append", "default": [], "help": "manifest JSON/JSONL 文件，可重复传入；未传则从本地 DB 读取"}),
            ("--preview-renderer", {"default": None, "help": "preview-renderer 地址，默认 PREVIEW_RENDERER_URL 或 http://localhost:8200"}),
            ("--client-id", {"default": None, "help": "客户端 ID，会写入 X-Client-Id 请求头"}),
            ("--api-key", {"default": None, "help": "preview-renderer API key，默认 PR_PREVIEW_RENDERER_API_KEY/PR_API_KEY"}),
            ("--output-dir", {"default": None, "help": "本地预览保存根目录；默认 data/previews，实际写入 <output-dir>/<resource_type>/<client_resource_id>_<preview_name>"}),
            ("--primary-only", {"action": "store_true", "help": "只调用 /previews/render/primary 并保存主预览"}),
            ("--submit-state", {"default": "pending", "help": "从 DB 读取时筛选 manifest 提交状态，默认 pending；传空字符串则不过滤"}),
            ("--dry-run", {"action": "store_true", "help": "只读取 manifest，不提交"}),
        ],
    )
    args = parser.parse_args()
    report = Report(label="preview-renderer 预览生成")
    preview_renderer = args.preview_renderer or env("PREVIEW_RENDERER_URL", "http://localhost:8200")
    client_id = args.client_id or env("CLIENT_ID", "client")
    api_key = args.api_key or env("PR_PREVIEW_RENDERER_API_KEY", env("PR_API_KEY", ""))
    output_root = Path(args.output_dir or env("PREVIEW_RENDERER_OUTPUT_DIR", str(_DEFAULT_OUTPUT_DIR))).resolve()

    cache = None
    if args.manifest:
        records = [(None, manifest) for manifest in load_manifest_records(args.manifest)]
    else:
        from ResourceProcessor.cache.local_cache import LocalCacheStore

        cache = LocalCacheStore(args.db_path)
        records = [
            (int(row["task_id"]), row["manifest"])
            for row in cache.iter_object_manifests(
                limit=args.limit,
                resource_type=args.resource_type,
                source=args.source_filter,
                submit_state=args.submit_state,
            )
        ]

    rendered = 0
    failed = 0
    with requests.Session() as session:
        for task_id, manifest in records:
            try:
                result = render_manifest_records(
                    [(task_id, manifest)],
                    preview_renderer=preview_renderer,
                    client_id=client_id,
                    api_key=api_key,
                    output_root=output_root,
                    primary_only=args.primary_only,
                    dry_run=args.dry_run,
                    session=session,
                )[0]
                rendered += 1
                print(json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                failed += 1
                report.fail("预览生成失败", f"task_id={task_id}: {str(exc)[:160]}")

    if cache is not None:
        cache.close()
    report.ok("完成", f"请求 {rendered}, 失败 {failed}, 输出 {output_root}")
    return 0 if report.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
