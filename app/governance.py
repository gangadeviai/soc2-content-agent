"""Governance workspace: read-only views over the audit trail and workflow state.

Design rules (they are what make the workspace evidence rather than decoration):
  * Every number here is computed from audit records or persisted workflow rows - nothing
    is hard-coded and nothing is written to the audit log to populate a view.
  * Everything is tenant-scoped from the caller's verified token. Records with tenant "*"
    are system/config events and are visible to administrators only.
  * Outputs are allow-listed field by field. Raw briefs, page text and redaction mappings
    never reach the browser (they are not in the audit log either).
  * Chain verification is an explicit action with a recorded result and time; the UI
    distinguishes "not yet performed" from "verified" from "failed".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from .audit import AuditSink
from .models import BrandProfile, BrandStatus, ContentStrategy, Draft, DraftStatus, StrategyStatus

APPROVAL_EVENTS = {"BRAND_REVIEW": "brand_profile", "STRATEGY_REVIEW": "strategy", "REVIEW": "draft"}
SYSTEM_TENANT = "*"
CONTROL_STATUSES = ("implemented", "partial", "planned", "unverified")

INTEGRITY_EXPLANATION = (
    "Each audit record stores the hash of the previous record and a hash of its own contents. "
    "Re-computing the chain detects any record that was altered, removed or inserted in the local "
    "mirror after it was written. It does not prove that the mirror itself was never replaced "
    "wholesale; that protection comes from the write-once object store (S3 Object Lock) that the "
    "mirror is streamed to, and from access controls on the host."
)


# ---- helpers ------------------------------------------------------------------------------
def visible_records(sink: AuditSink, tenant_id: str, include_system: bool) -> list[dict]:
    allowed = {tenant_id} | ({SYSTEM_TENANT} if include_system else set())
    return [r for r in sink.read_all() if r.get("tenant_id") in allowed]


def paginate(items: list, page: int, page_size: int) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    start = (page - 1) * page_size
    return {"items": items[start : start + page_size], "page": page, "page_size": page_size, "total": len(items)}


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---- verification state -----------------------------------------------------------------------
@dataclass
class VerificationState:
    status: str = "not_yet_performed"  # not_yet_performed | verified | failed | unavailable
    checked_at: str | None = None
    records_checked: int | None = None
    first_bad_index: int | None = None
    error: str | None = None
    explanation: str = INTEGRITY_EXPLANATION

    def perform(self, sink: AuditSink) -> VerificationState:
        try:
            ok, n = sink.verify_chain()
        except Exception as exc:  # unreadable mirror, permissions, etc.
            self.status, self.error, self.checked_at = "unavailable", f"{type(exc).__name__}: {exc}"[:200], _now()
            return self
        self.status = "verified" if ok else "failed"
        self.records_checked = n
        self.first_bad_index = None if ok else n  # index of the first record that failed
        self.error = None
        self.checked_at = _now()
        return self

    def to_dict(self) -> dict:
        return asdict(self)


# ---- overview --------------------------------------------------------------------------------
def overview(sink: AuditSink, db: Session, tenant_id: str, verification: VerificationState, is_admin: bool) -> dict:
    recs = visible_records(sink, tenant_id, include_system=is_admin)
    approvals = [r for r in recs if r["event_type"] in APPROVAL_EVENTS]
    pending = (
        db.query(BrandProfile)
        .filter(BrandProfile.tenant_id == tenant_id, BrandProfile.status == BrandStatus.PENDING_CONFIRMATION)
        .count()
        + db.query(ContentStrategy)
        .filter(
            ContentStrategy.tenant_id == tenant_id,
            ContentStrategy.status.in_([StrategyStatus.DRAFT, StrategyStatus.CHANGES_REQUESTED]),
        )
        .count()
        + db.query(Draft)
        .filter(Draft.tenant_id == tenant_id, Draft.status.in_([DraftStatus.DRAFT, DraftStatus.CHANGES_REQUESTED]))
        .count()
    )
    return {
        "tenant_id": tenant_id,
        "metrics": {
            "audit_records": {
                "value": len(recs),
                "source": "audit mirror, tenant-scoped" + (" + system" if is_admin else ""),
            },
            "approval_decisions": {
                "value": len(approvals),
                "source": "BRAND_REVIEW + STRATEGY_REVIEW + REVIEW records",
            },
            "approvals_pending": {"value": pending, "source": "workflow rows in pending/draft/changes_requested"},
            "publication_verification_failures": {
                "value": sum(1 for r in recs if r["event_type"] == "PUBLISH" and r["details"].get("verified") is False),
                "source": "PUBLISH records with verified=false",
            },
            "security_events": {
                "value": len(security_events(sink, tenant_id, is_admin)),
                "source": "derived; see Security Events",
            },
        },
        "integrity": verification.to_dict(),
    }


# ---- approval history --------------------------------------------------------------------------
_OBJECT_MODEL = {"brand_profile": BrandProfile, "strategy": ContentStrategy, "draft": Draft}


def approval_history(
    sink: AuditSink,
    db: Session,
    tenant_id: str,
    *,
    stage: str | None = None,
    decision: str | None = None,
    reviewer: str | None = None,
    object_id: str | None = None,
) -> list[dict]:
    rows = []
    cache: dict[tuple[str, str], object] = {}
    for r in visible_records(sink, tenant_id, include_system=False):
        kind = APPROVAL_EVENTS.get(r["event_type"])
        if not kind:
            continue
        key = (kind, r["draft_id"])
        if key not in cache:
            cache[key] = db.get(_OBJECT_MODEL[kind], r["draft_id"])
        obj = cache[key]
        requester = getattr(obj, "requester_id", None)
        row = {
            "audit_hash": r["record_hash"],
            "ts": r["ts"],
            "stage": kind,
            "object_id": r["draft_id"],
            "object_title": getattr(obj, "title", None) or (f"v{obj.version}" if hasattr(obj, "version") else None),
            "requester_id": requester,
            "reviewer_id": r["actor_id"],
            "decision": r["details"].get("decision"),
            "note": r["details"].get("note", ""),
            "separation_of_duties": (requester is not None and requester != r["actor_id"]),
            "flags_at_review": r["details"].get("flags_at_review", []),
        }
        if stage and row["stage"] != stage:
            continue
        if decision and row["decision"] != decision:
            continue
        if reviewer and row["reviewer_id"] != reviewer:
            continue
        if object_id and row["object_id"] != object_id:
            continue
        rows.append(row)
    rows.sort(key=lambda x: x["ts"], reverse=True)
    return rows


# ---- audit trail --------------------------------------------------------------------------------
def audit_records(
    sink: AuditSink,
    tenant_id: str,
    is_admin: bool,
    *,
    event_type: str | None = None,
    object_id: str | None = None,
    actor: str | None = None,
) -> list[dict]:
    out = []
    for r in visible_records(sink, tenant_id, include_system=is_admin):
        if event_type and r["event_type"] != event_type:
            continue
        if object_id and r.get("draft_id") != object_id:
            continue
        if actor and r["actor_id"] != actor:
            continue
        out.append(_summary(r))
    out.reverse()  # newest first
    return out


def _summary(r: dict) -> dict:
    return {
        "record_hash": r["record_hash"],
        "prev_hash": r["prev_hash"],
        "ts": r["ts"],
        "event_type": r["event_type"],
        "actor_id": r["actor_id"],
        "tenant_id": r["tenant_id"],
        "object_id": r.get("draft_id"),
        "detail_keys": sorted(r.get("details", {}).keys()),
    }


def record_detail(sink: AuditSink, tenant_id: str, is_admin: bool, record_hash: str) -> dict | None:
    for r in visible_records(sink, tenant_id, include_system=is_admin):
        if r["record_hash"] == record_hash:
            return r  # details already contain hashes/ids only, never raw content
    return None


# ---- security events --------------------------------------------------------------------------
def security_events(sink: AuditSink, tenant_id: str, is_admin: bool) -> list[dict]:
    """Derived from existing audit records. No new records are written to build this view."""
    events: list[dict] = []

    def add(r: dict, category: str, severity: str, outcome: str, explanation: str) -> None:
        events.append(
            {
                "ts": r["ts"],
                "category": category,
                "severity": severity,
                "outcome": outcome,
                "explanation": explanation,
                "tenant_id": r["tenant_id"],
                "actor_id": r["actor_id"],
                "object_id": r.get("draft_id"),
                "audit_hash": r["record_hash"],
                "source_event": r["event_type"],
            }
        )

    for r in visible_records(sink, tenant_id, include_system=is_admin):
        t, d = r["event_type"], r.get("details", {})
        if t == "ACCESS_DENIED":
            add(
                r, "authorization", "medium", "refused", f"role {d.get('required_role')!r} required for {d.get('path')}"
            )
        elif t == "AUTH_FAILED":
            add(r, "authentication", "medium", "refused", f"{d.get('reason')} on {d.get('path')}")
        elif t == "CRAWL_POLICY_REFUSED":
            add(r, "egress_policy", "high", "refused", f"crawl of {d.get('domain')} refused: {d.get('reason')}")
        elif t == "BRAND_ANALYSIS" and d.get("pages_dropped"):
            add(
                r,
                "prompt_injection",
                "medium",
                "mitigated",
                f"{len(d['pages_dropped'])} crawled page(s) dropped for instruction-like content",
            )
        elif t in {"GENERATION", "STRATEGY_GENERATED"} and any(
            str(f).startswith("RAG_CHUNKS_DROPPED_FOR_INJECTION") for f in d.get("review_flags", [])
        ):
            add(
                r,
                "prompt_injection",
                "medium",
                "mitigated",
                "retrieved knowledge chunk(s) dropped for instruction-like content",
            )
        if t in {"GENERATION", "STRATEGY_GENERATED", "BRAND_ANALYSIS"} and (d.get("redaction") or {}).get("counts"):
            counts = d["redaction"]["counts"]
            add(
                r,
                "redaction",
                "info",
                "applied",
                "redacted before model call: " + ", ".join(f"{k}x{v}" for k, v in sorted(counts.items())),
            )
        if t == "PUBLISH" and d.get("verified") is False:
            add(
                r,
                "publication_integrity",
                "high",
                "flagged",
                f"live content differed from approved content: {d.get('verification_note')}",
            )
        if (
            t == "CONFIG"
            and d.get("event") == "startup"
            and str(d.get("data_retention_class", "")).startswith("FREE_TIER")
        ):
            add(
                r,
                "vendor_data_retention",
                "medium",
                "accepted_non_production",
                f"provider {d.get('provider')} runs without zero-data-retention terms (env {d.get('env')})",
            )
        if t == "CONFIG" and d.get("event") == "wordpress_publish_credential_stored":
            add(
                r,
                "credential_change",
                "info",
                "stored",
                f"publishing credential stored for tenant {r['tenant_id']} ({d.get('source')})",
            )
        if t == "DELETE":
            add(r, "data_disposal", "info", "deleted", f"draft deleted: {d.get('reason')}")
        if t == "MODEL_PROVIDER_UNAVAILABLE":
            add(
                r,
                "availability",
                "medium",
                "service_degraded",
                f"model provider unavailable on {d.get('path')}: {d.get('reason')}",
            )
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events


# ---- publication evidence ---------------------------------------------------------------------
def publication_evidence(sink: AuditSink, db: Session, tenant_id: str) -> list[dict]:
    recs = visible_records(sink, tenant_id, include_system=False)
    by_draft: dict[str, list[dict]] = {}
    for r in recs:
        if r["event_type"] in {"REVIEW", "PUBLISH"} and r.get("draft_id"):
            by_draft.setdefault(r["draft_id"], []).append(r)
    rows = []
    drafts = (
        db.query(Draft)
        .filter(Draft.tenant_id == tenant_id, Draft.status.in_([DraftStatus.APPROVED, DraftStatus.PUBLISHED]))
        .order_by(Draft.updated_at.desc())
        .all()
    )
    for d in drafts:
        events = by_draft.get(d.id, [])
        approval = next(
            (r for r in reversed(events) if r["event_type"] == "REVIEW" and r["details"].get("decision") == "approve"),
            None,
        )
        publishes = [r for r in events if r["event_type"] == "PUBLISH"]
        last = publishes[-1] if publishes else None
        if d.status == DraftStatus.APPROVED:
            state = "approved"
        elif last and last["details"].get("verified") is True:
            state = "published_verified"
        else:
            state = "published_unverified"
        rows.append(
            {
                "draft_id": d.id,
                "title": d.title,
                "state": state,
                "approved_by": approval["actor_id"] if approval else d.reviewer_id,
                "approved_at": approval["ts"] if approval else None,
                "approval_audit_hash": approval["record_hash"] if approval else None,
                "publish_attempts": len(publishes),
                "published_at": last["ts"] if last else None,
                "published_url": d.published_url,
                "remote_post_id": d.remote_post_id,
                "approved_body_sha256": last["details"].get("approved_body_sha256") if last else None,
                "published_html_sha256": last["details"].get("published_html_sha256") if last else None,
                "verified": d.publish_verified,
                "verification_note": last["details"].get("verification_note") if last else None,
                "republished": any(p["details"].get("republish") for p in publishes),
                "publish_audit_hashes": [p["record_hash"] for p in publishes],
            }
        )
    return rows


# ---- control mapping ---------------------------------------------------------------------------
@dataclass
class ControlSet:
    controls: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = {s: 0 for s in CONTROL_STATUSES}
        for ctl in self.controls:
            c[ctl["status"]] = c.get(ctl["status"], 0) + 1
        return c


def load_controls(path: Path) -> ControlSet:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    controls = raw.get("controls", [])
    for c in controls:
        if c.get("status") not in CONTROL_STATUSES:
            raise ValueError(f"control {c.get('id')} has invalid status {c.get('status')!r}")
    return ControlSet(controls=controls, gaps=raw.get("gaps", []))
