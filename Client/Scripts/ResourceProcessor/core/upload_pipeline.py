from __future__ import annotations

import io
import os
import zipfile
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

import requests

from ResourceProcessor.preview_metadata import ResourceProcessingEntity


Reporter = Callable[[str, str, str], None]


@dataclass
class UploadSummary:
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    skipped_no_files: int = 0
    skipped_no_description: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _report(reporter: Reporter | None, status: str, step: str, detail: str) -> None:
    if reporter is not None:
        reporter(status, step, detail)


def _resource_label(resource: ResourceProcessingEntity | dict[str, Any]) -> str:
    if isinstance(resource, ResourceProcessingEntity):
        if resource.title:
            return resource.title
        if resource.resource_path:
            return resource.resource_path
        return resource.content_md5[:12]
    title = resource.get("title") or resource.get("resource_path") or resource.get("source_directory")
    return os.path.basename(str(title)) or str(resource.get("content_md5", ""))[:12]


def _resource_files(resource: ResourceProcessingEntity | dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(resource, ResourceProcessingEntity):
        return [file_info.to_dict() for file_info in resource.files]
    return list(resource.get("files", []))


def _resource_text(resource: ResourceProcessingEntity | dict[str, Any], attr: str) -> str:
    if isinstance(resource, ResourceProcessingEntity):
        value = getattr(resource, attr, "")
    else:
        value = resource.get(attr, "")
    return str(value or "")


def _resource_list(resource: ResourceProcessingEntity | dict[str, Any], attr: str) -> list[str]:
    if isinstance(resource, ResourceProcessingEntity):
        value = getattr(resource, attr, [])
    else:
        value = resource.get(attr, [])
    return [str(item) for item in value] if isinstance(value, list) else []


def _resource_int(resource: ResourceProcessingEntity | dict[str, Any], attr: str) -> int:
    if isinstance(resource, ResourceProcessingEntity):
        value = getattr(resource, attr, 0)
    else:
        value = resource.get(attr, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _build_download_package(resource: ResourceProcessingEntity | dict[str, Any]) -> tuple[str, bytes, str] | None:
    files = _resource_files(resource)
    if not files:
        return None

    if isinstance(resource, ResourceProcessingEntity):
        title = resource.title or resource.resource_path or resource.source_resource_id or resource.content_md5[:12]
        preferred_name = resource.download_file_name
        needs_zip = resource.resource_type == "pack" or len(files) > 1
        root_dir = resource.source_directory
    else:
        title = str(resource.get("title") or resource.get("resource_path") or resource.get("content_md5", "")[:12])
        preferred_name = str(resource.get("download_file_name") or "")
        needs_zip = str(resource.get("resource_type", "")) == "pack" or len(files) > 1
        root_dir = str(resource.get("source_directory") or "")

    if not needs_zip:
        return None

    safe_base = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in title).strip("_") or "resource"
    file_name = preferred_name or f"{safe_base}.zip"
    if not file_name.lower().endswith(".zip"):
        file_name += ".zip"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_info in files:
            file_path = str(file_info.get("file_path", ""))
            if not file_path or not os.path.isfile(file_path):
                continue
            arcname = str(file_info.get("file_name") or os.path.basename(file_path))
            if root_dir:
                try:
                    arcname = os.path.relpath(file_path, root_dir)
                except ValueError:
                    arcname = str(file_info.get("file_name") or os.path.basename(file_path))
            zf.write(file_path, arcname=arcname)
    payload = buffer.getvalue()
    if not payload:
        return None
    return file_name, payload, "application/zip"


def infer_upload_resource_type(resource: ResourceProcessingEntity | dict[str, Any], fallback: str = "") -> str:
    if isinstance(resource, ResourceProcessingEntity) and resource.resource_type:
        return resource.resource_type
    if isinstance(resource, dict) and resource.get("resource_type"):
        return str(resource["resource_type"])

    files = _resource_files(resource)
    formats = {str(file_info.get("file_format", "")).lower() for file_info in files}
    if formats & {"fbx", "obj", "gltf", "glb", "blend"}:
        return "model"
    if formats & {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}:
        return "image"
    return fallback or "other"


def upload_enriched_resources(
    enriched: Iterable[dict[str, Any]],
    server: str,
    reporter: Reporter | None = None,
) -> UploadSummary:
    summary = UploadSummary()

    try:
        health_resp = requests.get(f"{server}/health", timeout=5)
        health = health_resp.json()
        if health.get("status") != "ok":
            _report(reporter, "FAIL", "服务端健康检查", f"状态: {health.get('status')}")
            summary.failed_count += 1
            return summary
        _report(reporter, "OK", "服务端健康检查", "所有组件正常")
    except Exception as exc:
        _report(reporter, "FAIL", "服务端健康检查", f"无法连接: {exc}")
        summary.failed_count += 1
        return summary

    for item in enriched:
        resource = item["resource"]
        resource_type = item.get("resource_type") or infer_upload_resource_type(resource)
        desc = item.get("description", {})
        files_info = _resource_files(resource)
        content_md5 = resource.content_md5 if isinstance(resource, ResourceProcessingEntity) else resource.get("content_md5", "")
        previews = resource.previews if isinstance(resource, ResourceProcessingEntity) else resource.get("previews", [])
        label = _resource_label(resource)

        if not desc.get("full"):
            summary.skipped_count += 1
            summary.skipped_no_description += 1
            _report(reporter, "OK", f"跳过 [{label}]", "描述为空")
            continue

        if not files_info:
            summary.skipped_count += 1
            summary.skipped_no_files += 1
            _report(reporter, "OK", f"跳过 [{label}]", "无可上传原始文件（metadata-only 资源）")
            continue

        try:
            register_body = {
                "content_md5": content_md5,
                "resource_type": resource_type,
                "source_resource_id": _resource_text(resource, "source_resource_id"),
                "parent_source_resource_id": _resource_text(resource, "parent_resource_id"),
                "child_source_resource_ids": _resource_list(resource, "child_resource_ids"),
                "child_resource_count": _resource_int(resource, "child_resource_count"),
                "contains_resource_types": _resource_list(resource, "contains_resource_types"),
                "title": _resource_text(resource, "title"),
                "source": _resource_text(resource, "source"),
                "pack_name": _resource_text(resource, "pack_name"),
                "resource_path": _resource_text(resource, "resource_path"),
                "source_url": _resource_text(resource, "source_url"),
                "original_download_url": _resource_text(resource, "download_url"),
                "category": _resource_text(resource, "category"),
                "license_name": _resource_text(resource, "license_name"),
                "source_description": _resource_text(resource, "source_description"),
                "tags": _resource_list(resource, "tags"),
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
            package = _build_download_package(resource)
            if package is not None:
                package_name, package_bytes, package_content_type = package
                register_body["download_file_name"] = package_name
                register_body["download_content_type"] = package_content_type
                register_body["download_file_size"] = len(package_bytes)
            else:
                register_body["download_file_name"] = files_info[0]["file_name"]
                register_body["download_content_type"] = "application/octet-stream"
                register_body["download_file_size"] = files_info[0]["file_size"]
            resp = requests.post(f"{server}/resources/register", json=register_body, timeout=30)
            resp.raise_for_status()
            register_data = resp.json()
            resource_id = register_data["resource_id"]
            if isinstance(resource, ResourceProcessingEntity):
                resource.resource_id = resource_id
            if register_data.get("exists"):
                _report(reporter, "OK", f"上传 [{label}]", f"resource_id={resource_id} (云端已存在，跳过)")
                summary.success_count += 1
                continue
            _report(reporter, "OK", f"注册 [{label}]", f"resource_id={resource_id}")
        except Exception as exc:
            summary.failed_count += 1
            _report(reporter, "FAIL", f"注册 [{label}]", str(exc)[:120])
            continue

        upload_files = []
        download_file = None
        try:
            for f in files_info:
                file_path = f.get("file_path", "")
                if file_path and os.path.isfile(file_path):
                    upload_files.append(("files", (f["file_name"], open(file_path, "rb"), "application/octet-stream")))
            package = _build_download_package(resource)
            if package is not None:
                package_name, package_bytes, package_content_type = package
                download_file = ("download_file", (package_name, io.BytesIO(package_bytes), package_content_type))
            if upload_files:
                files_payload = list(upload_files)
                if download_file is not None:
                    files_payload.append(download_file)
                resp = requests.post(f"{server}/resources/{resource_id}/upload-batch", files=files_payload, timeout=120)
                resp.raise_for_status()
                upload_data = resp.json()
                if not upload_data.get("success"):
                    raise RuntimeError(upload_data.get("error_message", "unknown"))
                _report(
                    reporter,
                    "OK",
                    f"上传文件 [{label}]",
                    f"{upload_data.get('file_count', 0)} 个文件, {upload_data.get('uploaded_bytes', 0)} bytes",
                )
        except Exception as exc:
            summary.failed_count += 1
            _report(reporter, "FAIL", f"上传文件 [{label}]", str(exc)[:120])
            continue
        finally:
            for _, (_, file_obj, _) in upload_files:
                file_obj.close()
            if download_file is not None:
                download_file[1][1].close()

        preview_files = []
        try:
            roles: list[str] = []
            for preview in previews:
                preview_path = preview.path if hasattr(preview, "path") else preview.get("path", "")
                if preview_path and os.path.isfile(preview_path):
                    file_name = os.path.basename(preview_path)
                    content_type = "image/gif" if preview_path.endswith(".gif") else "image/webp"
                    preview_files.append(("files", (file_name, open(preview_path, "rb"), content_type)))
                    roles.append(preview.role if hasattr(preview, "role") else preview.get("role", "primary"))
            if preview_files:
                data = {"roles": ",".join(roles)} if roles else None
                resp = requests.post(
                    f"{server}/resources/{resource_id}/previews",
                    files=preview_files,
                    data=data,
                    timeout=60,
                )
                resp.raise_for_status()
                preview_data = resp.json()
                _report(reporter, "OK", f"上传预览 [{label}]", f"{preview_data.get('preview_count', 0)} 个预览")
        except Exception as exc:
            summary.failed_count += 1
            _report(reporter, "FAIL", f"上传预览 [{label}]", str(exc)[:120])
        finally:
            for _, (_, file_obj, _) in preview_files:
                file_obj.close()

        try:
            commit_body = {
                "resource_type": resource_type,
                "description_main": desc.get("main", ""),
                "description_detail": desc.get("detail", ""),
                "description_full": desc.get("full", ""),
            }
            resp = requests.post(f"{server}/resources/{resource_id}/commit", json=commit_body, timeout=30)
            resp.raise_for_status()
            commit_data = resp.json()
            if commit_data.get("state") == "committed":
                _report(reporter, "OK", f"提交 [{label}]", f"resource_id={resource_id}")
                summary.success_count += 1
            else:
                raise RuntimeError(commit_data.get("error_message", f"state={commit_data.get('state')}"))
        except Exception as exc:
            summary.failed_count += 1
            _report(reporter, "FAIL", f"提交 [{label}]", str(exc)[:120])

    return summary
