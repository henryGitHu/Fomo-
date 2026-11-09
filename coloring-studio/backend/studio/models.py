"""SQLAlchemy ORM models: the permanent project library."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300))
    subtitle: Mapped[str] = mapped_column(String(500), default="")
    author: Mapped[str] = mapped_column(String(300), default="")
    theme: Mapped[str] = mapped_column(String(500), default="")
    audience_age: Mapped[str] = mapped_column(String(20), default="6-9")
    illustration_count: Mapped[int] = mapped_column(Integer, default=20)
    trim_size: Mapped[str] = mapped_column(String(20), default="8.5x11")
    paper_type: Mapped[str] = mapped_column(String(30), default="white")
    bleed: Mapped[bool] = mapped_column(Boolean, default=False)
    illustration_style: Mapped[str] = mapped_column(String(100), default="clean cartoon line art")
    caption_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    budget_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    generation_mode: Mapped[str] = mapped_column(String(30), default="manual")  # manual|auto|quality_first
    routing_preference: Mapped[str] = mapped_column(String(30), default="balanced")
    default_provider: Mapped[str] = mapped_column(String(50), default="mock")
    default_model: Mapped[str] = mapped_column(String(100), default="mock-lineart-v1")
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    fallback_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enhance_images: Mapped[bool] = mapped_column(Boolean, default=True)
    batch_size: Mapped[int] = mapped_column(Integer, default=5)
    autopilot: Mapped[bool] = mapped_column(Boolean, default=False)
    education_progression: Mapped[bool] = mapped_column(Boolean, default=True)
    intro_pages: Mapped[int] = mapped_column(Integer, default=4)
    back_description: Mapped[str] = mapped_column(Text, default="")
    cover_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    publication_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    sections: Mapped[list["Section"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Section.position")
    pages: Mapped[list["Page"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Page.position")


class StyleProfile(Base):
    __tablename__ = "style_profiles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="Book style")
    is_library: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    anchor_revision_ids: Mapped[list] = mapped_column(JSON, default=list)
    anchor_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Section(Base):
    __tablename__ = "sections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(300))
    illustration_count: Mapped[int] = mapped_column(Integer)
    theme_instructions: Mapped[str] = mapped_column(Text, default="")
    desired_scenes: Mapped[list] = mapped_column(JSON, default=list)
    objects: Mapped[list] = mapped_column(JSON, default=list)
    complexity: Mapped[str] = mapped_column(String(30), default="medium")
    educational_goals: Mapped[str] = mapped_column(Text, default="")
    caption_preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    reference_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    excluded_content: Mapped[str] = mapped_column(Text, default="")
    preferred_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    preferred_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    learning_stage: Mapped[str] = mapped_column(String(50), default="")
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)

    project: Mapped[Project] = relationship(back_populates="sections")


class Page(Base):
    """One coloring illustration slot (recto) in the book."""
    __tablename__ = "pages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    section_id: Mapped[str | None] = mapped_column(ForeignKey("sections.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer)  # 1..illustration_count, book order
    subject: Mapped[str] = mapped_column(String(300), default="")
    concept: Mapped[str] = mapped_column(Text, default="")
    fact: Mapped[str] = mapped_column(Text, default="")
    vocabulary: Mapped[str] = mapped_column(String(200), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    concept_variant: Mapped[int] = mapped_column(Integer, default=0)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    prompt: Mapped[str] = mapped_column(Text, default="")
    prompt_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="planned")
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    caption_text: Mapped[str] = mapped_column(Text, default="")
    caption_version: Mapped[int] = mapped_column(Integer, default=0)
    caption_status: Mapped[str] = mapped_column(String(30), default="missing")
    caption_validated_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_override: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_override: Mapped[str | None] = mapped_column(String(100), nullable=True)
    line_weight_adjust: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="pages")


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    storage_key: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Revision(Base):
    """An image revision of a page (generated, enhanced, edited or a battle candidate)."""
    __tablename__ = "revisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30), default="generated")
    parent_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    original_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_id: Mapped[str] = mapped_column(String(50), default="")
    model_id: Mapped[str] = mapped_column(String(100), default="")
    effective_params: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt: Mapped[str] = mapped_column(Text, default="")
    prompt_version: Mapped[int] = mapped_column(Integer, default=1)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_is_estimate: Mapped[bool] = mapped_column(Boolean, default=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    battle_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaptionRevision(Base):
    __tablename__ = "caption_revisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30), default="generated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    progress_message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CostEntry(Base):
    __tablename__ = "cost_entries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    page_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    section_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_id: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(100), default="")
    operation: Mapped[str] = mapped_column(String(30), default="generate")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=True)
    is_retry: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderConfig(Base):
    __tablename__ = "provider_configs"
    provider_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BenchmarkResult(Base):
    __tablename__ = "benchmark_results"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(50))
    prompt: Mapped[str] = mapped_column(Text)
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Battle(Base):
    __tablename__ = "battles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    prompt: Mapped[str] = mapped_column(Text)
    contenders: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    winner_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchReport(Base):
    __tablename__ = "research_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(String(500))
    mode: Mapped[str] = mapped_column(String(20))  # web | offline | mixed
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EditHistory(Base):
    __tablename__ = "edit_history"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    page_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    source: Mapped[str] = mapped_column(String(30), default="ui")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExportRecord(Base):
    __tablename__ = "exports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))  # interior|cover|preview|zip|qa_report
    asset_id: Mapped[str] = mapped_column(String(32))
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
