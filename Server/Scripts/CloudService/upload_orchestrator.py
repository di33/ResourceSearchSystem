from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from CloudService.cloud_client import (
    BaseCloudClient,
    FileInfo,
    PreviewFileInfo,
    RegisterRequest,
    CommitRequest,
)
from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import (
    PreviewInfo,
    ProcessState,
)


@dataclass
class UploadTask:
    """一个上传任务的描述，支持多文件和多预览。"""
    task_id: int
    content_md5: str  # composite fingerprint
    resource_type: str
    files: List[FileInfo] = field(default_factory=list)
    previews: List[PreviewInfo] = field(default_factory=list)
    description_main: str = ""
    description_detail: str = ""
    description_full: str = ""
    embedding_dimension: int = 0
    embedding_vector_data: list = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(f.file_size for f in self.files)


@dataclass
class UploadOutcome:
    """上传结果。"""
    task_id: int
    success: bool
    resource_id: str = ""
    final_state: str = ""
    error_message: str = ""


class UploadOrchestrator:
    """编排 register → upload → commit 的完整上传流程。"""

    def __init__(self, cloud_client: BaseCloudClient, cache: LocalCacheStore):
        self.client = cloud_client
        self.cache = cache

    async def execute(self, task: UploadTask) -> UploadOutcome:
        """
        执行完整的上传流程：
        1. register
        2. upload_files + upload_previews
        3. commit
        4. 更新本地缓存状态

        任一步骤失败则停止，记录错误并返回。
        """
        try:
            # Step 1: Register
            reg_req = RegisterRequest(
                content_md5=task.content_md5,
                resource_type=task.resource_type,
                files=task.files,
            )
            reg_resp = await self.client.register(reg_req)
            resource_id = reg_resp.resource_id

            self.cache.update_task_state(task.task_id, ProcessState.REGISTERED)
            self.cache.add_log(task.task_id, "registered", f"resource_id={resource_id}")

            if reg_resp.exists:
                self.cache.update_task_state(task.task_id, ProcessState.SYNCED)
                self.cache.add_log(task.task_id, "exists_skip", "云端已存在，跳过上传")
                return UploadOutcome(
                    task_id=task.task_id,
                    success=True,
                    resource_id=resource_id,
                    final_state="synced",
                )

            # Step 2: Upload all files
            file_result = await self.client.upload_files(resource_id, task.files)
            if not file_result.success:
                self._fail(task.task_id, "UPLOAD_FILES_FAILED", file_result.error_message)
                return UploadOutcome(
                    task_id=task.task_id,
                    success=False,
                    resource_id=resource_id,
                    error_message=f"文件上传失败: {file_result.error_message}",
                )

            # Step 2b: Upload all previews
            if task.previews:
                preview_file_infos = [
                    PreviewFileInfo(
                        file_path=p.path or "",
                        file_name=p.path.split("/")[-1] if p.path else "preview",
                        content_type="image/webp",
                        role=p.role,
                    )
                    for p in task.previews
                ]
                preview_result = await self.client.upload_previews(resource_id, preview_file_infos)
                if not preview_result.success:
                    self._fail(task.task_id, "UPLOAD_PREVIEWS_FAILED", preview_result.error_message)
                    return UploadOutcome(
                        task_id=task.task_id,
                        success=False,
                        resource_id=resource_id,
                        error_message=f"预览上传失败: {preview_result.error_message}",
                    )

            self.cache.update_task_state(task.task_id, ProcessState.UPLOADED)
            self.cache.add_log(task.task_id, "uploaded", f"已上传 {len(task.files)} 个文件, {len(task.previews)} 个预览")

            # Step 3: Commit
            commit_req = CommitRequest(
                resource_id=resource_id,
                resource_type=task.resource_type,
                description_main=task.description_main,
                description_detail=task.description_detail,
                description_full=task.description_full,
            )
            commit_resp = await self.client.commit(commit_req)

            if commit_resp.state != "committed":
                self._fail(task.task_id, "COMMIT_FAILED", commit_resp.error_message)
                return UploadOutcome(
                    task_id=task.task_id,
                    success=False,
                    resource_id=resource_id,
                    error_message=f"提交失败: {commit_resp.error_message}",
                )

            self.cache.update_task_state(task.task_id, ProcessState.COMMITTED)
            self.cache.add_log(task.task_id, "committed", "提交完成")

            return UploadOutcome(
                task_id=task.task_id,
                success=True,
                resource_id=resource_id,
                final_state="committed",
            )

        except Exception as exc:
            logging.error("上传编排异常 (task_id=%d): %s", task.task_id, exc)
            self._fail(task.task_id, "ORCHESTRATOR_ERROR", str(exc))
            return UploadOutcome(
                task_id=task.task_id,
                success=False,
                error_message=str(exc),
            )

    def _fail(self, task_id: int, error_code: str, error_message: str):
        task = self.cache.get_task_by_id(task_id)
        current = ProcessState(task["process_state"]) if task else ProcessState.DISCOVERED
        self.cache.update_task_state(task_id, current, error_code, error_message)
        self.cache.increment_retry(task_id)
        self.cache.add_log(task_id, "error", f"{error_code}: {error_message}")
