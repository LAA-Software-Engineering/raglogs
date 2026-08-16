import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False, default="file")
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    jobs: Mapped[list["IngestionJob"]] = relationship("IngestionJob", back_populates="source")
    log_entries: Mapped[list["LogEntry"]] = relationship("LogEntry", back_populates="source")


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    parsed_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_adapter: Mapped[str] = mapped_column(String(50), nullable=False, default="file")
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped["Source"] = relationship("Source", back_populates="jobs")
    log_entries: Mapped[list["LogEntry"]] = relationship("LogEntry", back_populates="ingestion_job")


class LogEntry(Base):
    __tablename__ = "log_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id"), nullable=True)
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ingestion_jobs.id"), nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    service: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    environment: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    level: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    parser_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    extra_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_adapter: Mapped[str] = mapped_column(String(50), nullable=False, default="file")
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped["Source"] = relationship("Source", back_populates="log_entries")
    ingestion_job: Mapped["IngestionJob"] = relationship("IngestionJob", back_populates="log_entries")
    embedding: Mapped["LogEmbedding | None"] = relationship("LogEmbedding", back_populates="log_entry", uselist=False)
    cluster_members: Mapped[list["ClusterMember"]] = relationship("ClusterMember", back_populates="log_entry")

    __table_args__ = (
        Index("ix_log_entries_timestamp_service", "timestamp", "service"),
        Index("ix_log_entries_source_adapter", "source_adapter"),
    )


class LogEmbedding(Base):
    __tablename__ = "log_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    log_entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("log_entries.id"), nullable=False, unique=True)
    embedding: Mapped[Any] = mapped_column(Vector(1536), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    log_entry: Mapped["LogEntry"] = relationship("LogEntry", back_populates="embedding")


class ClusterRun(Base):
    __tablename__ = "cluster_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_filter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    environment_filter: Mapped[str | None] = mapped_column(String(100), nullable=True)
    algorithm: Mapped[str] = mapped_column(String(50), default="fingerprint")
    status: Mapped[str] = mapped_column(String(50), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    clusters: Mapped[list["Cluster"]] = relationship("Cluster", back_populates="cluster_run")


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cluster_runs.id"), nullable=False)
    cluster_key: Mapped[str] = mapped_column(String(64), nullable=False)
    representative_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    services_json: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    levels_json: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline_count: Mapped[int] = mapped_column(Integer, default=0)
    change_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    importance_score: Mapped[float] = mapped_column(Float, default=0.0)
    cluster_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    cluster_run: Mapped["ClusterRun"] = relationship("ClusterRun", back_populates="clusters")
    members: Mapped[list["ClusterMember"]] = relationship("ClusterMember", back_populates="cluster")


class ClusterMember(Base):
    __tablename__ = "cluster_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False)
    log_entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("log_entries.id"), nullable=False)

    cluster: Mapped["Cluster"] = relationship("Cluster", back_populates="members")
    log_entry: Mapped["LogEntry"] = relationship("LogEntry", back_populates="cluster_members")


class Explanation(Base):
    __tablename__ = "explanations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service_filter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    environment_filter: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="rules")
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSONB, nullable=True)


class WorkerJob(Base):
    __tablename__ = "worker_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_jobs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
