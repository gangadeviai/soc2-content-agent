"""Stage 3/4 - writing, review and publishing. The control logic auditors test.

generate():  redact -> screen & retrieve (tenant-scoped) -> LLM -> screen output
             -> persist as DRAFT -> audit (GENERATION)
review():    reviewer != requester -> APPROVED / CHANGES_REQUESTED / REJECTED -> audit
publish():   only APPROVED drafts, only by a reviewer, with the per-tenant publish
             credential -> WordPress -> read-back verification -> PUBLISHED -> audit
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .audit import AuditRecord, AuditSink, sha256_text
from .auth import Principal
from .brand import BrandConfig, Fetcher, confirmed_brand
from .cms import PublishCredentialsError, Publisher
from .injection import screen_input_chunk, screen_output, wrap_untrusted
from .llm import DRAFT_SCHEMA, LLMClient
from .models import Draft, DraftStatus
from .prompts import PromptBundle
from .rag import Retriever
from .redaction import Redactor, rehydrate
from .seo import evaluate as evaluate_seo

_URL = re.compile(r"https?://[^\s)\]]+", re.IGNORECASE)


@dataclass
class Services:
    prompts: PromptBundle
    llm: LLMClient
    retriever: Retriever
    audit: AuditSink
    # tenant_id -> Publisher; constructed lazily so the publish credential is only read on that path
    publisher_factory: Callable[[str], Publisher]
    fetcher: Fetcher
    brands: dict[str, BrandConfig] = field(default_factory=dict)
    tenant_deny_terms: dict[str, list[str]] = field(default_factory=dict)
    rag_top_k: int = 6
    brand_context_max_chars: int = 60_000


def load_owned(db: Session, model, obj_id: str, principal: Principal):
    """Tenant-scoped fetch. Same response for 'not found' and 'other tenant' - no existence oracle."""
    obj = db.get(model, obj_id)
    if obj is None or obj.tenant_id != principal.tenant_id:
        raise HTTPException(404, f"{model.__tablename__[:-1].replace('_', ' ')} not found")
    return obj


def _load_for_tenant(db: Session, draft_id: str, principal: Principal) -> Draft:
    return load_owned(db, Draft, draft_id, principal)


def generate(
    db: Session,
    svc: Services,
    principal: Principal,
    brief: str,
    content_type: str,
    *,
    strategy_id: str | None = None,
    strategy_item_index: int | None = None,
) -> Draft:
    # 1. Redact before anything leaves the boundary.
    redactor = Redactor(extra_terms=svc.tenant_deny_terms.get(principal.tenant_id, []))
    red = redactor.redact(brief)

    # 2. Tenant-scoped retrieval; drop chunks that look like injected instructions.
    chunks = svc.retriever.retrieve(principal.tenant_id, red.text, svc.rag_top_k)
    kept, dropped = [], []
    for c in chunks:
        (kept if screen_input_chunk(c.text).clean else dropped).append(c)
    blocks = [
        wrap_untrusted("knowledge_chunk", f"chunk_id={c.chunk_id} source={c.source}\n{redactor.redact(c.text).text}")
        for c in kept
    ]
    # The human-confirmed brand profile (if any) anchors voice, entities and proof points.
    brand = confirmed_brand(db, principal.tenant_id)
    if brand is not None:
        blocks.insert(0, wrap_untrusted("brand_profile", json.dumps(brand.profile, indent=1)))
    context_block = "\n".join(blocks) or "<untrusted_knowledge_chunk>none</untrusted_knowledge_chunk>"

    # 3. Generate.
    task = f"Content type: {content_type}\n\n{red.text}"
    result = svc.llm.complete(svc.prompts, task, "system.md", DRAFT_SCHEMA, context_block)

    # 4. Screen output; flags go to the reviewer, never auto-fixed.
    allowed_urls = set(_URL.findall(brief))
    body = result.data.get("body_markdown", "")
    flags = screen_output(body, allowed_urls).flags
    # 4b. Deterministic SEO / AEO / GEO fitness check - the model does not self-certify.
    seo_payload = {k: result.data.get(k, {}) for k in ("seo", "aeo", "geo")}
    seo_report = evaluate_seo(result.data, content_type) if not result.refused else None
    if seo_report is not None:
        flags.extend(seo_report.flags)
        for fact in (seo_payload["geo"] or {}).get("quotable_facts", []):
            if str(fact.get("source", "")).strip().lower() in {"", "needs source"}:
                flags.append(f"GEO:unsourced_fact: {fact.get('fact', '')!r}")
    if dropped:
        flags.append(f"RAG_CHUNKS_DROPPED_FOR_INJECTION: {[c.chunk_id for c in dropped]}")
    if result.fallback_used:
        flags.append(f"MODEL_FALLBACK: served by {result.model_served}, not {result.model_requested}")

    draft = Draft(
        tenant_id=principal.tenant_id,
        requester_id=principal.sub,
        status=DraftStatus.REFUSED if result.refused else DraftStatus.DRAFT,
        strategy_id=strategy_id,
        strategy_item_index=strategy_item_index,
        title=result.data.get("title", ""),
        body_markdown=body,
        claims_to_verify=result.data.get("claims_to_verify", []),
        review_flags=flags,
        ai_disclosure=result.data.get("ai_disclosure", ""),
        seo=seo_payload,
        seo_report=seo_report.to_dict() if seo_report else {},
        seo_score=seo_report.score if seo_report else None,
        redaction_mapping=red.mapping,
        lineage={
            **svc.prompts.lineage,
            "model_requested": result.model_requested,
            "model_served": result.model_served,
            "chunks": [{"chunk_id": c.chunk_id, "sha256": c.sha256} for c in kept],
            "provider_request_id": result.provider_request_id,
            "strategy_id": strategy_id,
        },
    )
    db.add(draft)
    db.flush()

    # 5. Immutable audit record - hashes, never raw PII.
    rec = svc.audit.write(
        AuditRecord(
            "GENERATION",
            principal.sub,
            principal.tenant_id,
            draft.id,
            {
                **draft.lineage,
                "content_type": content_type,
                "strategy_item_index": strategy_item_index,
                "redaction": {"engine": red.engine, "counts": red.counts},
                "redacted_input_sha256": sha256_text(task),
                "context_sha256": sha256_text(context_block),
                "output_sha256": sha256_text(body),
                "stop_reason": result.stop_reason,
                "refusal_category": result.refusal_category,
                "usage": result.usage,
                "review_flags": flags,
                "seo_score": seo_report.score if seo_report else None,
                "seo_checks_failed": [c.name for c in seo_report.checks if not c.passed] if seo_report else [],
                "brand_profile_version": brand.version if brand else None,
            },
        )
    )
    draft.generation_audit_hash = rec.record_hash
    db.commit()
    return draft


def review(db: Session, svc: Services, principal: Principal, draft_id: str, decision: str, note: str) -> Draft:
    draft = _load_for_tenant(db, draft_id, principal)
    if draft.requester_id == principal.sub:
        raise HTTPException(403, "segregation of duties: requester cannot review their own draft")
    if draft.status not in {DraftStatus.DRAFT, DraftStatus.CHANGES_REQUESTED}:
        raise HTTPException(409, f"draft is {draft.status.value}; not reviewable")
    mapping = {
        "approve": DraftStatus.APPROVED,
        "request_changes": DraftStatus.CHANGES_REQUESTED,
        "reject": DraftStatus.REJECTED,
    }
    if decision not in mapping:
        raise HTTPException(422, "decision must be approve | request_changes | reject")
    if decision == "approve" and not note.strip():
        raise HTTPException(422, "an approval must include a note confirming claims were verified")

    draft.status = mapping[decision]
    draft.reviewer_id = principal.sub
    draft.review_note = note
    svc.audit.write(
        AuditRecord(
            "REVIEW",
            principal.sub,
            principal.tenant_id,
            draft.id,
            {
                "decision": decision,
                "note": note,
                "flags_at_review": draft.review_flags,
                "claims_at_review": draft.claims_to_verify,
                "output_sha256": sha256_text(draft.body_markdown),
            },
        )
    )
    db.commit()
    return draft


def publish(db: Session, svc: Services, principal: Principal, draft_id: str) -> Draft:
    draft = _load_for_tenant(db, draft_id, principal)
    # A published-but-unverified draft may be re-published IN PLACE (same remote post) so the
    # reviewer can fix a mismatch without creating duplicates. Verified posts are final.
    republish = draft.status == DraftStatus.PUBLISHED and draft.publish_verified is False
    if draft.status != DraftStatus.APPROVED and not republish:
        raise HTTPException(409, "only APPROVED drafts (or unverified published ones) can be published")
    if draft.requester_id == principal.sub:
        raise HTTPException(403, "segregation of duties: requester cannot publish their own draft")

    try:
        publisher = svc.publisher_factory(draft.tenant_id)  # publish-scoped credential lives only here
    except PublishCredentialsError as exc:
        raise HTTPException(503, str(exc)) from exc
    final_body = rehydrate(draft.body_markdown, draft.redaction_mapping)
    result = publisher.publish(
        draft.tenant_id,
        draft.title,
        final_body,
        draft.ai_disclosure,
        seo=draft.seo,
        remote_id=draft.remote_post_id if republish else None,
    )

    draft.status = DraftStatus.PUBLISHED
    draft.published_url = result.url
    draft.remote_post_id = result.remote_id
    draft.publish_verified = result.verified
    flags = [f for f in draft.review_flags if not f.startswith("PUBLISH_VERIFICATION_FAILED")]
    if not result.verified:
        flags.append(f"PUBLISH_VERIFICATION_FAILED: {result.verification_note}")
    draft.review_flags = flags
    svc.audit.write(
        AuditRecord(
            "PUBLISH",
            principal.sub,
            principal.tenant_id,
            draft.id,
            {
                "url": result.url,
                "remote_post_id": result.remote_id,
                "approved_by": draft.reviewer_id,
                "approved_body_sha256": sha256_text(final_body),
                "published_html_sha256": result.published_sha256,
                "verified": result.verified,
                "verification_note": result.verification_note,
                "republish": republish,
                "strategy_id": draft.strategy_id,
            },
        )
    )
    db.commit()
    return draft


def delete_draft(db: Session, svc: Services, principal: Principal, draft_id: str, reason: str) -> None:
    """Privacy disposal (P4.3). The audit record of the deletion is itself immutable."""
    draft = _load_for_tenant(db, draft_id, principal)
    svc.audit.write(
        AuditRecord(
            "DELETE",
            principal.sub,
            principal.tenant_id,
            draft.id,
            {"reason": reason, "status_at_delete": draft.status.value},
        )
    )
    db.delete(draft)
    db.commit()
