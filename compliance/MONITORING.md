# Monitoring & Thresholds (CC4.1, CC7.1, CC7.2)

Auditors ask for proof that monitoring ran across the whole observation period and that "misbehaving model" is defined. Source: `GET /admin/metrics` and the audit stream.

| Signal | Source | Threshold → action | Alert to |
|---|---|---|---|
| Review flag rate (drafts with ≥1 flag / drafts) | `/admin/metrics.flag_rate` | > 0.40 over 7 days → open ticket, review prompt | platform-leads |
| Rejection rate | `rejected / drafts_total` | > 0.25 over 7 days → prompt/model review | platform-leads |
| Refusals | `refused` | any `refusal_category` other than expected → investigate within 1 business day | platform-security |
| Model fallbacks | `model_fallbacks` | > 5 % of drafts → check model registry, consider disabling fallback | platform-security |
| Distinct `model_served` not in MODEL_REGISTRY | audit query | any → P2 incident (unapproved model change) | platform-security |
| Audit chain integrity | `/healthz` → `AUDIT_CHAIN_BROKEN`, `/admin/audit/verify` | any failure → P1 incident | platform-infra |
| Redaction counts per draft | audit `redaction.counts` | spike in `API_KEY`/`CREDIT_CARD` → user training follow-up | GRC |
| Injection chunks dropped | `RAG_CHUNKS_DROPPED_FOR_INJECTION` flag | any → inspect knowledge-base ingestion source | platform-security |
| Egress proxy denies to AI endpoints | SIEM | any → reconcile with `AI_INVENTORY.yaml` | GRC |
| Access denied events | audit `ACCESS_DENIED` | > 20 / user / day → review | platform-security |
| SEO/AEO/GEO score | `/admin/metrics.seo_score_avg`, `seo_below_threshold` | 7-day avg < 75 or > 30 % below threshold → review prompt/model (not the threshold) | platform-leads |
| Published but unverified posts | `/admin/metrics.published_unverified` | any → investigate same day (playbook H) | platform-leads |
| Brand pages dropped for injection | `PAGES_DROPPED_FOR_INJECTION` flag | any → inspect client site; inform client | platform-security |
| Crawl policy notes (out-of-scope / redirect) | BRAND_ANALYSIS `crawl_notes` | > 10 per crawl → review `tenants/brands.yaml` scope | platform-leads |
| Brand profiles pending > 5 business days | `/admin/metrics.brands_pending_confirmation` | → chase reviewer; stale profile must not be confirmed without re-crawl | marketing managers |

Evidence routine: export the metrics snapshot and audit-verify result on the 1st of each month to the evidence folder (`EVIDENCE_COLLECTION.md`).
