"""Persistence for the pipeline: brand profiles, strategies, drafts (PI1.4, PI1.5)."""

from __future__ import annotations

import enum
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class BrandStatus(enum.StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    REFUSED = "refused"


class StrategyStatus(enum.StrEnum):
    DRAFT = "draft"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    REFUSED = "refused"


class DraftStatus(enum.StrEnum):
    DRAFT = "draft"  # generated, awaiting review
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"  # human approved, not yet live
    PUBLISHED = "published"
    REJECTED = "rejected"
    REFUSED = "refused"  # model declined; nothing to review


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid.uuid4())


class BrandProfile(Base):
    __tablename__ = "brand_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    requester_id: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[BrandStatus] = mapped_column(Enum(BrandStatus), default=BrandStatus.PENDING_CONFIRMATION, index=True)

    profile: Mapped[dict] = mapped_column(JSON, default=dict)  # BRAND_SCHEMA output
    pages: Mapped[list] = mapped_column(JSON, default=list)  # [{url, sha256}] crawled & used
    review_flags: Mapped[list] = mapped_column(JSON, default=list)
    lineage: Mapped[dict] = mapped_column(JSON, default=dict)
    audit_hash: Mapped[str] = mapped_column(String(64), default="")

    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "requester_id": self.requester_id,
            "version": self.version,
            "status": self.status.value,
            "profile": self.profile,
            "pages": self.pages,
            "review_flags": self.review_flags,
            "lineage": self.lineage,
            "audit_hash": self.audit_hash,
            "reviewer_id": self.reviewer_id,
            "review_note": self.review_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ContentStrategy(Base):
    __tablename__ = "content_strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    requester_id: Mapped[str] = mapped_column(String(128))
    brand_profile_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[StrategyStatus] = mapped_column(Enum(StrategyStatus), default=StrategyStatus.DRAFT, index=True)

    plan: Mapped[dict] = mapped_column(JSON, default=dict)  # STRATEGY_SCHEMA output (+ per-item draft_id/status)
    review_flags: Mapped[list] = mapped_column(JSON, default=list)
    redaction_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    lineage: Mapped[dict] = mapped_column(JSON, default=dict)
    audit_hash: Mapped[str] = mapped_column(String(64), default="")

    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "requester_id": self.requester_id,
            "brand_profile_id": self.brand_profile_id,
            "status": self.status.value,
            "plan": self.plan,
            "review_flags": self.review_flags,
            "lineage": self.lineage,
            "audit_hash": self.audit_hash,
            "reviewer_id": self.reviewer_id,
            "review_note": self.review_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    requester_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[DraftStatus] = mapped_column(Enum(DraftStatus), default=DraftStatus.DRAFT, index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    strategy_item_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    title: Mapped[str] = mapped_column(String(512), default="")
    body_markdown: Mapped[str] = mapped_column(Text, default="")  # redacted form (placeholders)
    claims_to_verify: Mapped[list] = mapped_column(JSON, default=list)
    review_flags: Mapped[list] = mapped_column(JSON, default=list)  # from injection.screen_output
    ai_disclosure: Mapped[str] = mapped_column(Text, default="")
    # placeholder -> original. Application-level encryption expected in prod (KMS envelope).
    redaction_mapping: Mapped[dict] = mapped_column(JSON, default=dict)

    lineage: Mapped[dict] = mapped_column(JSON, default=dict)  # prompt version/hash, model, chunk ids
    generation_audit_hash: Mapped[str] = mapped_column(String(64), default="")

    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    published_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    remote_post_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    publish_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # SEO / AEO / GEO elements produced by the model and the deterministic evaluation of them
    seo: Mapped[dict] = mapped_column(JSON, default=dict)  # {"seo": {...}, "aeo": {...}, "geo": {...}}
    seo_report: Mapped[dict] = mapped_column(JSON, default=dict)
    seo_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "requester_id": self.requester_id,
            "status": self.status.value,
            "strategy_id": self.strategy_id,
            "strategy_item_index": self.strategy_item_index,
            "title": self.title,
            "body_markdown": self.body_markdown,
            "claims_to_verify": self.claims_to_verify,
            "review_flags": self.review_flags,
            "ai_disclosure": self.ai_disclosure,
            "lineage": self.lineage,
            "generation_audit_hash": self.generation_audit_hash,
            "reviewer_id": self.reviewer_id,
            "review_note": self.review_note,
            "published_url": self.published_url,
            "remote_post_id": self.remote_post_id,
            "publish_verified": self.publish_verified,
            "seo": self.seo,
            "seo_report": self.seo_report,
            "seo_score": self.seo_score,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def make_session_factory(database_url: str):
    engine = create_engine(database_url, json_serializer=lambda o: json.dumps(o, default=str))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
