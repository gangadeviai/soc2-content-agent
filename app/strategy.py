"""Stage 2 - content strategy from a human-confirmed brand profile.

Controls:
  * A strategy can only be generated from a CONFIRMED brand profile (stage gate).
  * Strategy items are screened for risky claims before a human sees them.
  * Approval requires a reviewer who is not the requester plus a note.
  * Drafts can only be created from items of an APPROVED strategy; each draft
    carries the strategy id so lineage runs website -> profile -> strategy ->
    draft -> publish.                                            (PI1.2, PI1.4, CC7.2)
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from . import workflow
from .audit import AuditRecord, sha256_text
from .auth import Principal
from .brand import confirmed_brand
from .injection import screen_input_chunk, screen_output, wrap_untrusted
from .llm import CONTENT_TYPES, STRATEGY_SCHEMA
from .models import ContentStrategy, Draft, StrategyStatus
from .redaction import Redactor


def plan_strategy(db: Session, svc, principal: Principal, objectives: str, horizon_weeks: int) -> ContentStrategy:
    brand = confirmed_brand(db, principal.tenant_id)
    if brand is None:
        raise HTTPException(409, "no confirmed brand profile for this tenant - run and confirm brand analysis first")

    redactor = Redactor(extra_terms=svc.tenant_deny_terms.get(principal.tenant_id, []))
    red = redactor.redact(objectives)

    chunks = svc.retriever.retrieve(principal.tenant_id, red.text, svc.rag_top_k)
    kept = [c for c in chunks if screen_input_chunk(c.text).clean]
    context = "\n".join(
        [wrap_untrusted("brand_profile", json.dumps(brand.profile, indent=1))]
        + [
            wrap_untrusted(
                "knowledge_chunk", f"chunk_id={c.chunk_id} source={c.source}\n{redactor.redact(c.text).text}"
            )
            for c in kept
        ]
    )
    task = f"Build a {horizon_weeks}-week content strategy for the brand. Objectives: {red.text}"
    result = svc.llm.complete(svc.prompts, task, "strategy.md", STRATEGY_SCHEMA, context)

    flags: list[str] = []
    for i, item in enumerate(result.data.get("calendar", [])):
        if item.get("content_type") not in CONTENT_TYPES:
            flags.append(f"item {i}: unsupported content_type {item.get('content_type')!r}")
        if not 1 <= int(item.get("week", 0)) <= horizon_weeks:
            flags.append(f"item {i}: week {item.get('week')} outside {horizon_weeks}-week horizon")
        for f in screen_output(f"{item.get('working_title', '')} {item.get('brief', '')}").flags:
            flags.append(f"item {i}: {f}")
    if result.fallback_used:
        flags.append(f"MODEL_FALLBACK: served by {result.model_served}, not {result.model_requested}")

    strategy = ContentStrategy(
        tenant_id=principal.tenant_id,
        requester_id=principal.sub,
        brand_profile_id=brand.id,
        status=StrategyStatus.REFUSED if result.refused else StrategyStatus.DRAFT,
        plan=result.data,
        review_flags=flags,
        redaction_mapping=red.mapping,
        lineage={
            **svc.prompts.lineage,
            "model_requested": result.model_requested,
            "model_served": result.model_served,
            "provider_request_id": result.provider_request_id,
            "brand_profile_id": brand.id,
            "brand_profile_version": brand.version,
            "chunks": [{"chunk_id": c.chunk_id, "sha256": c.sha256} for c in kept],
        },
    )
    db.add(strategy)
    db.flush()
    rec = svc.audit.write(
        AuditRecord(
            "STRATEGY_GENERATED",
            principal.sub,
            principal.tenant_id,
            strategy.id,
            {
                **strategy.lineage,
                "horizon_weeks": horizon_weeks,
                "redaction": {"engine": red.engine, "counts": red.counts},
                "redacted_input_sha256": sha256_text(task),
                "context_sha256": sha256_text(context),
                "output_sha256": sha256_text(json.dumps(result.data, sort_keys=True)),
                "stop_reason": result.stop_reason,
                "refusal_category": result.refusal_category,
                "usage": result.usage,
                "review_flags": flags,
            },
        )
    )
    strategy.audit_hash = rec.record_hash
    db.commit()
    return strategy


def review_strategy(
    db: Session, svc, principal: Principal, strategy_id: str, decision: str, note: str
) -> ContentStrategy:
    strategy: ContentStrategy = workflow.load_owned(db, ContentStrategy, strategy_id, principal)
    if strategy.requester_id == principal.sub:
        raise HTTPException(403, "segregation of duties: requester cannot approve their own strategy")
    if strategy.status not in {StrategyStatus.DRAFT, StrategyStatus.CHANGES_REQUESTED}:
        raise HTTPException(409, f"strategy is {strategy.status.value}; not reviewable")
    mapping = {
        "approve": StrategyStatus.APPROVED,
        "request_changes": StrategyStatus.CHANGES_REQUESTED,
        "reject": StrategyStatus.REJECTED,
    }
    if decision not in mapping:
        raise HTTPException(422, "decision must be approve | request_changes | reject")
    if decision == "approve" and not note.strip():
        raise HTTPException(422, "approval must include a note confirming assumptions were checked with the client")

    strategy.status = mapping[decision]
    strategy.reviewer_id = principal.sub
    strategy.review_note = note
    svc.audit.write(
        AuditRecord(
            "STRATEGY_REVIEW",
            principal.sub,
            principal.tenant_id,
            strategy.id,
            {
                "decision": decision,
                "note": note,
                "flags_at_review": strategy.review_flags,
                "plan_sha256": sha256_text(json.dumps(strategy.plan, sort_keys=True)),
            },
        )
    )
    db.commit()
    return strategy


def draft_from_item(db: Session, svc, principal: Principal, strategy_id: str, item_index: int) -> Draft:
    strategy: ContentStrategy = workflow.load_owned(db, ContentStrategy, strategy_id, principal)
    if strategy.status != StrategyStatus.APPROVED:
        raise HTTPException(409, "drafts can only be created from an APPROVED strategy")
    calendar = strategy.plan.get("calendar", [])
    if not 0 <= item_index < len(calendar):
        raise HTTPException(404, "strategy item not found")
    item = calendar[item_index]
    if item.get("draft_id"):
        raise HTTPException(409, f"item already drafted as {item['draft_id']}")

    brief = (
        f"Working title: {item['working_title']}\n"
        f"Content pillar: {item['pillar']}\n"
        f"Target keyword: {item['target_keyword']}\n"
        f"Call to action: {item['cta']}\n\n"
        f"{item['brief']}"
    )
    content_type = item["content_type"] if item["content_type"] in CONTENT_TYPES else "blog_post"
    draft = workflow.generate(
        db, svc, principal, brief, content_type, strategy_id=strategy.id, strategy_item_index=item_index
    )
    item["draft_id"] = draft.id
    item["status"] = "drafted"
    flag_modified(strategy, "plan")
    db.commit()
    return draft
