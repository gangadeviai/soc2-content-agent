# AI Incident Playbook (CC7.3 – CC7.5)

Exercised annually (tabletop); record date, participants and findings at the bottom.

## Scenarios

### A. Confidential data reached the model provider unredacted
1. Pull the GENERATION audit record by `draft_id`; confirm via `redaction.counts` and `redacted_input_sha256`.
2. Disable the affected tenant's generation (feature flag) and add the missed pattern/term to `tenants/deny_terms.yaml` or `redaction.py` via emergency PR.
3. Invoke the vendor's ZDR terms: confirm with Anthropic that no retention occurred; retain their written confirmation as evidence.
4. Notify client per contract; notify DPO if personal data involved (P6.3).

### B. Prompt injection attempted to publish / alter content
1. Identify the chunk via `RAG_CHUNKS_DROPPED_FOR_INJECTION` or reviewer report; quarantine the knowledge-base source.
2. Verify no publish occurred: PUBLISH audit records for the tenant in the window.
3. Add the sample to `tests/test_injection.py` (regression set) and ship via PR.

### C. Published content contains a false claim, plagiarism, or defamation
1. Retrieve full lineage: GENERATION (inputs, chunks, model), REVIEW (reviewer, note), PUBLISH (final hash).
2. Unpublish via CMS; preserve the audit chain (never edit — it is WORM).
3. Root-cause: missed `claims_to_verify`? reviewer note inadequate? → retrain reviewers / tighten checklist.

### D. Unapproved model served (fallback or provider change)
1. Query distinct `model_served`; compare to `MODEL_REGISTRY.md`.
2. If unapproved, set `AGENT_ENABLE_REFUSAL_FALLBACK=false` via emergency PR; re-run eval before re-enabling.

### E. Vendor (model provider) security incident
1. Rotate the API key; review vendor notice against DPA SLA.
2. Assess exposure using audit records in the window (what tenants, how many drafts, redaction counts).

### G. Crawler fetched something it should not have (SSRF alert, third-party host, robots violation)
1. Pull BRAND_ANALYSIS records for the window; `crawl_notes` and `pages` list every URL touched.
2. Confirm the egress proxy deny log; if a non-allow-listed host was reached, treat as P1 and rotate the runtime identity.
3. Fix `tenants/brands.yaml` scope or `app/brand.py` guard via emergency PR; add the URL to `tests/test_brand.py`.

### H. Live WordPress post differs from approved content (`PUBLISH_VERIFICATION_FAILED`)
1. Unpublish via WordPress admin; keep the PUBLISH audit record (approved vs live hashes) as evidence.
2. Check for CMS plugins rewriting content, compromised WP user, or MITM; rotate the tenant's application password.
3. Re-publish only after root cause is fixed and read-back verifies.

### F. Audit chain broken
1. Treat as P1. Compare local mirror to S3 Object Lock copies (authoritative).
2. Identify first mismatching record; investigate host access logs.

## Tabletop record
| Date | Scenario | Participants | Findings | Actions |
|---|---|---|---|---|
| | | | | |
