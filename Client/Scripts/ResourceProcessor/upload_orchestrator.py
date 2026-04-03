from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ResourceProcessor.cloud_client import (
    BaseCloudClient,
    RegisterRequest,
    CommitRequest,
)
from ResourceProcessor.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import ProcessState


@dataclass
class UploadTask:
    """一个上传任务的描述。"""
    task_id: int
    content_md5: str
    resource_type: str
    file_name: str
    file_size: int
    file_path: str
    preview_path: str
    preview_format: str
    description_main: str
    description_detail: str
    description_full: str
    embedding_dimension: int
    embedding_vector_data: list


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
        2. upload_file + upload_preview
        3. commit
        4. 更新本地缓存状态

        任一步骤失败则停止，记录错误并返回。
        """
        try:
            # Step 1: Register
            reg_req = RegisterRequest(
                content_md5=task.content_md5,
                resource_type=task.resource_type,
                file_name=task.file_name,
                file_size=task.file_size,
                preview_format=task.preview_format,
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

            # Step 2: Upload files
            file_result = await self.client.upload_file(
                resource_id, task.file_path, task.file_size
            )
            if not file_result.success:
                self._fail(task.task_id, "UPLOAD_FILE_FAILED", file_result.error_message)
                return UploadOutcome(
                    task_id=task.task_id,
                    success=False,
                    resource_id=resource_id,
                    error_message=f"文件上传失败: {file_result.error_message}",
                )

            preview_result = await self.client.upload_preview(
                resource_id, task.preview_path
            )
            if not preview_result.success:
                self._fail(task.task_id, "UPLOAD_PREVIEW_FAILED", preview_result.error_message)
                return UploadOutcome(
                    task_id=task.task_id,
                    success=False,
                    resource_id=resource_id,
                    error_message=f"预览上传失败: {preview_result.error_message}",
                )

            self.cache.update_task_state(task.task_id, ProcessState.UPLOADED)
            self.cache.add_log(task.task_id, "uploaded", "文件和预览上传完成")

            # Step 3: Commit
            commit_req = CommitRequest(
                resource_id=resource_id,
                resource_type=task.resource_type,
                description_main=task.description_main,
                description_detail=task.description_detail,
                description_full=task.description_full,
                embedding_dimension=task.embedding_dimension,
                embedding_vector_data=task.embedding_vector_data,
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
