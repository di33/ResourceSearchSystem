from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResourceTask(Base):
    __tablename__ = "resource_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_md5: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_directory: Mapped[str] = mapped_column(Text, nullable=False, default="")
    process_state: Mapped[str] = mapped_column(String(32), nullable=False, default="discovered")
    resource_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str] = mapped_column(String(64), default="")
    last_error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    files: Mapped[list[ResourceFile]] = relationship(back_populates="task", lazy="selectin", cascade="all, delete-orphan")
    previews: Mapped[list[ResourcePreview]] = relationship(back_populates="task", lazy="selectin", cascade="all, delete-orphan")
    descriptions: Mapped[list[ResourceDescription]] = relationship(back_populates="task", lazy="selectin", cascade="all, delete-orphan")
    embeddings: Mapped[list[ResourceEmbedding]] = relationship(back_populates="task", lazy="selectin", cascade="all, delete-orphan")
    upload_jobs: Mapped[list[ResourceUploadJob]] = relationship(back_populates="task", lazy="selectin", cascade="all, delete-orphan")
    logs: Mapped[list[ProcessLog]] = relationship(back_populates="task", lazy="selectin", cascade="all, delete-orphan")


class ResourceFile(Base):
    __tablename__ = "resource_file"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("resource_task.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    file_format: Mapped[str] = mapped_column(String(16), nullable=False)
    content_md5: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_role: Mapped[str] = mapped_column(String(32), nullable=False, default="main")
    ks3_key: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[ResourceTask] = relationship(back_populates="files")


class ResourcePreview(Base):
    __tablename__ = "resource_preview"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("resource_task.id"), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="primary")
    path: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str | None] = mapped_column(String(16))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    size: Mapped[int | None] = mapped_column(Integer)
    renderer: Mapped[str | None] = mapped_column(String(64))
    used_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    fail_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[ResourceTask] = relationship(back_populates="previews")


class ResourceDescription(Base):
    __tablename__ = "resource_description"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("resource_task.id"), nullable=False)
    main_content: Mapped[str] = mapped_column(Text, default="")
    detail_content: Mapped[str] = mapped_column(Text, default="")
    full_description: Mapped[str] = mapped_column(Text, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    quality_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[ResourceTask] = relationship(back_populates="descriptions")


class ResourceEmbedding(Base):
    __tablename__ = "resource_embedding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("resource_task.id"), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(128), default="")
    generate_time: Mapped[float] = mapped_column(Float, default=0.0)
    model_version: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[ResourceTask] = relationship(back_populates="embeddings")


class ResourceUploadJob(Base):
    __tablename__ = "resource_upload_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("resource_task.id"), nullable=False)
    upload_state: Mapped[str] = mapped_column(String(32), default="pending")
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    task: Mapped[ResourceTask] = relationship(back_populates="upload_jobs")


class ProcessLog(Base):
    __tablename__ = "process_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("resource_task.id"), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[ResourceTask] = relationship(back_populates="logs")
