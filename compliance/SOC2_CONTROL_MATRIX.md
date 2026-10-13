# SOC 2 Control Matrix — Marketing Content Writer Agent

**Framework status as of October 2026.** The applicable criteria are the AICPA
*2017 Trust Services Criteria* with the *2022 revised Points of Focus*. The AICPA has
not issued AI-specific criteria, points of focus, or an AI addendum. The 2026 AICPA
exposure drafts (Feb/Mar 2026, comments closed 30 June 2026) revise the attestation
standards (AT-C 105/205/210) under which the auditor performs the engagement, not the
criteria the system is measured against. What *has* changed is examination practice:
auditors now request AI-specific evidence under the existing criteria. Column 4 maps
each control to that evidence request so the auditor finds it without improvising.

Trust Services Categories in scope: **Security (Common Criteria), Confidentiality,
Processing Integrity, Privacy.** Availability is in scope only for the review portal
(A1.2 backups), as content generation is not a time-critical service.

| # | Control (what we do) | TSC criteria | 2026 AI evidence request it satisfies | Implementation | Evidence artefact |
|---|---|---|---|---|---|
| C-01 | All model traffic passes through one gateway service; UI/CMS never call a model provider directly | CC6.6, CC6.7 | "Where does data leave the boundary for AI?" | `app/llm.py` is the only module importing the provider SDK; egress allow-list | `infra/network/egress_allowlist.md`, proxy deny logs |
| C-02 | PII, secrets and tenant-confidential terms are redacted before leaving the boundary; originals stored only inside the boundary | C1.1, C1.2, P3.1, P6.1 | "How is sensitive data minimised before reaching the model?" | `app/redaction.py` (Presidio + regex + tenant deny-list); `tests/test_redaction.py`, `test_pii_never_reaches_the_model` | Audit `redaction.counts`; CI test output |
| C-03 | Model provider under Zero Data Retention; no training on inputs; prod refuses to start without ZDR acknowledgement | CC9.2, C1.1 | "AI subprocessor determination; signed ZDR / DPA terms" | `Settings.zdr_contract_confirmed`; `VENDOR_SUBPROCESSOR_ASSESSMENT.md` | Signed ZDR addendum, Anthropic SOC 2 report, startup CONFIG audit record |
| C-04 | Knowledge-base access via SELECT-only DB role, read-only transactions, verified at startup | CC6.1, CC6.3 | "Least privilege for AI service accounts" | `app/rag.py::_assert_read_only`; `infra/iam/rag_readonly_role.sql` | Role grant dump; startup log |
| C-05 | Tenant isolation: tenant comes from verified token; every RAG query, draft read and list is tenant-filtered; RLS as second barrier | CC6.1, C1.1 | "Logical separation between clients' data in AI context" | `app/rag.py`, `workflow._load_for_tenant`; `test_tenant_isolation_in_rag_and_reads` | CI test output; RLS policy |
| C-06 | System prompts stored in Git, SHA-256 pinned in manifest; runtime refuses mismatched or unapproved prompt; CODEOWNERS review required | CC8.1 | "Model/prompt changes under formal change control with approval records" | `app/prompts.py`, `prompts/manifest.json`, `.github/CODEOWNERS`, CI manifest check | PR history for `prompts/`, `prompts/CHANGELOG.md` |
| C-07 | Model ID pinned in config; change = PR + entry in model registry; served model recorded per request | CC8.1, PI1.2 | "Model registry / model lineage: which model produced this output?" | `Settings.model_id`; `MODEL_REGISTRY.md`; audit `model_requested`/`model_served` | Registry, audit records |
| C-08 | Prompt-injection controls: untrusted content delimited, instruction-like chunks dropped and flagged, regression test set in CI | CC7.1, PI1.2 | "Adversarial testing before deployment" | `app/injection.py`, `prompts/1.0.0/injection_guard.md`, `tests/test_injection.py` | CI output per PR |
| C-09 | Agent cannot publish: drafts land as `draft`; only a reviewer (≠ requester) can approve, approval needs a verification note; publish uses a separate credential available only in the publish path | PI1.4, PI1.5, CC6.3 | "Human oversight: evidence staff verify AI output before acting on it" | `app/workflow.py` review/publish; `app/cms.py`; `test_agent_cannot_publish_without_human_approval` | REVIEW & PUBLISH audit records with reviewer ID and note |
| C-10 | Output screening: risky marketing claims, unsourced statistics, hallucinated URLs and leaked placeholders flagged to the reviewer; model must list `claims_to_verify` | PI1.2, PI1.3 | "Output monitoring / hallucination controls" | `app/injection.py::screen_output`; `DRAFT_SCHEMA` structured output | Draft `review_flags`; `/admin/metrics` |
| C-11 | Every generation produces an immutable, hash-chained audit record (requester, tenant, prompt version+hash, model served, chunk IDs+hashes, input/output hashes, usage, stop reason, flags, provider request-id) streamed to S3 Object Lock COMPLIANCE | CC7.2, CC7.3, CC4.1 | "Per-inference logging with model version, prompt hash, context and outcome" | `app/audit.py`; `infra/iam/audit_bucket.tf` | Bucket Object Lock config; `/admin/audit/verify`; sample records |
| C-12 | Audit log contains hashes of redacted inputs, never raw PII | C1.1, P3.2 | "Does AI logging itself create a data-retention problem?" | `workflow.generate` logs `sha256` only; `test_audit_chain_records_full_lineage…` asserts no PII | CI output |
| C-13 | Authentication via corporate IdP (OIDC); roles requester/reviewer/admin; denied access attempts audited; no local accounts | CC6.1, CC6.2 | "Access reviews include AI accounts and agent identities" | `app/auth.py`; `test_access_denied_is_audited` | Quarterly access review incl. service accounts in `AI_INVENTORY.yaml` |
| C-14 | AI inventory with owners for every model, key, service account and tool; egress deny logs reconciled to it | CC3.2, CC6.1 | "AI inventory and shadow-AI detection reconciled to it" | `AI_INVENTORY.yaml`; `egress_allowlist.md` | Monthly reconciliation ticket |
| C-15 | AI-specific risks in the risk register (leakage, injection, over-trust, drift, vendor change, model fallback) with owners and treatments | CC3.1, CC3.2, CC3.4 | "AI risks documented in the enterprise risk assessment" | `AI_RISK_REGISTER.md` | Annual review sign-off |
| C-16 | Output-quality monitoring: flag rate, refusal rate, rejection rate, fallback count exposed and alerted on thresholds | CC7.1, CC4.1 | "Dashboards showing monitoring ran all period; defined misbehaviour thresholds" | `/admin/metrics`; thresholds in `MONITORING.md` | Dashboard screenshots per month; alert history |
| C-17 | AI incident playbook (leak via prompt, injected publish attempt, plagiarism/defamation claim, vendor breach) exercised annually | CC7.3, CC7.4, CC7.5 | "Incident response includes AI scenarios and was exercised" | `INCIDENT_PLAYBOOK_AI.md` | Tabletop record |
| C-18 | Acceptable AI Use policy with version history and staff acknowledgment; AI-disclosure line on every published piece | CC1.1, CC1.4, CC2.2 | "AI use policy, training records, disclosure practice" | `policies/ACCEPTABLE_AI_USE.md`; `ai_disclosure` field | Acknowledgment records; LMS completion |
| C-19 | Draft retention 180 days; admin delete with audited reason; audit log retained 7 years | P4.2, P4.3, CC6.5 | "Retention limits and deletion capability for AI data" | `Settings.draft_retention_days`; `workflow.delete_draft` | DELETE audit records; retention job logs |
| C-20 | CI gates: tests, lint, dependency vuln scan, secret scan, manifest check; protected `main` | CC8.1, CC7.1 | — | `.github/workflows/ci.yml` | CI run history |
| C-21 | Refusal handling: model refusals recorded with category; drafts marked `refused`; optional server-side fallback model recorded and flagged for reviewer | PI1.2, CC7.2 | "Model lineage when a fallback model served the request" | `app/llm.py`; `MODEL_FALLBACK` flag | Audit `refusal_category`, `model_served` |

### Stage 1 – Brand analysis (crawl of the brand's own website)

| # | Control (what we do) | TSC criteria | 2026 AI evidence request it satisfies | Implementation | Evidence artefact |
|---|---|---|---|---|---|
| C-22 | Crawl scope is fixed per tenant in Git (`tenants/brands.yaml`): domain + subdomains, allowed path prefixes, seed paths, page cap. API callers cannot supply a URL | CC6.6, CC6.7, CC8.1 | "What external data does the AI ingest and who controls the scope?" | `app/brand.py::url_allowed`, `BrandConfig`; CODEOWNERS on `tenants/` | PR history; `test_crawl_stays_in_scope_and_honours_robots` |
| C-23 | SSRF guard: IP literals, localhost/.internal, and hosts resolving to private/loopback/link-local ranges are refused before any request; off-domain redirects discarded | CC6.6 | — | `app/brand.py::assert_public_host`, `HttpFetcher` | `test_ssrf_guard_rejects_private_targets` |
| C-24 | robots.txt honoured; identifying User-Agent; byte cap per page; only the brand's own content is read (no third-party sites) | CC6.7, CC9.2 | "Lawful/authorised collection of data used by AI" | `app/brand.py::crawl` | crawl notes in BRAND_ANALYSIS audit record |
| C-25 | Crawled pages are untrusted data: redacted (PII + tenant deny-terms), screened for injected instructions (dropped + flagged), delimited | C1.1, CC7.1 | "Indirect prompt-injection controls on ingested content" | `analyze_brand` → `screen_input_chunk`, `wrap_untrusted` | `PAGES_DROPPED_FOR_INJECTION` flags; test |
| C-26 | Brand profile is `pending_confirmation` until a reviewer (≠ requester) confirms with a note; confirming a new version supersedes the previous; only a CONFIRMED profile can drive strategy | PI1.2, PI1.4 | "Human validation of AI inferences before downstream use" | `review_brand`, `confirmed_brand`, `plan_strategy` gate | BRAND_REVIEW audit records; `test_brand_confirmation_gate_and_supersession` |
| C-27 | Model must separate site evidence from inference (`observations_to_confirm`, `proof_points` verbatim) | PI1.2 | "Hallucination controls" | `BRAND_SCHEMA`, `prompts/1.1.0/brand_analysis.md` | profile JSON |

### Stage 2 – Content strategy

| # | Control (what we do) | TSC criteria | 2026 AI evidence request it satisfies | Implementation | Evidence artefact |
|---|---|---|---|---|---|
| C-28 | Strategy generation requires a CONFIRMED brand profile; strategy lineage records profile id + version | PI1.2, CC7.2 | "Lineage across chained AI steps" | `app/strategy.py::plan_strategy` | STRATEGY_GENERATED record; `test_strategy_requires_confirmed_brand` |
| C-29 | Each calendar item is validated (content type enum, horizon) and screened for unsubstantiated claims before a human sees it | PI1.2, PI1.3 | "Output monitoring" | `plan_strategy` flags | `review_flags`; test |
| C-30 | Strategy approval by reviewer ≠ requester with a note; drafts can only be created from APPROVED strategy items; each item drafted once; draft carries `strategy_id` | PI1.4, CC6.3 | "Human oversight at each stage" | `review_strategy`, `draft_from_item` | STRATEGY_REVIEW records; `test_strategy_approval_sod_and_draft_from_item` |

### Stage 3 – Writing: SEO / AEO / GEO fitness

| # | Control (what we do) | TSC criteria | 2026 AI evidence request it satisfies | Implementation | Evidence artefact |
|---|---|---|---|---|---|
| C-35 | The output schema requires search-fit elements (title tag, meta description, slug, keyword set, direct answer, FAQ, key takeaways, brand entity, sourced quotable facts, JSON-LD); a **deterministic, versioned checker** scores them (SEO/AEO/GEO pillars, threshold 70). The model never self-certifies. Failures are reviewer flags, never auto-fixed; score + failed checks go into the GENERATION audit record and `/admin/metrics` | PI1.2, PI1.3, CC4.1 | "Output-quality controls defined in code with measurable thresholds; monitoring evidence" | `app/seo.py`, `DRAFT_SCHEMA`, `workflow.generate`, `prompts/1.2.0/system.md` | `seo_report` on every draft; `tests/test_seo.py`; monthly `seo_score_avg` snapshot |
| C-36 | GEO facts without a source are flagged individually so unsupported "citable" statements cannot reach publication unnoticed; JSON-LD is re-serialised before embedding (no raw model markup in the page) | PI1.2, CC6.8 | "Hallucination / integrity of AI-generated structured data" | `workflow.generate` (`GEO:unsourced_fact`), `seo.safe_jsonld`, `cms.render_html` | flags; `test_jsonld_is_resanitised_before_embedding` |
| C-37 | SEO elements are published through supported channels only (slug, excerpt, plugin meta keys declared per tenant in Git); inline JSON-LD is omitted on platforms that strip `<script>` (WordPress.com); read-back verification catches any other CMS rewrite. A published-but-unverified post may be re-published **in place** (same remote id) by a reviewer; verified posts are final | PI1.5, CC8.1 | — | `cms.wordpress_payload`, `supports_inline_jsonld`, `workflow.publish` (`republish`) | `test_seo_elements_travel_with_the_post`, `test_wordpress_com_omits_inline_jsonld_and_republish_updates_in_place`; PUBLISH `verified`, `republish` |

**Field note (2026-10-12, learning run):** first real publish to WordPress.com failed verification — the platform stripped the JSON-LD `<script>` wrapper, leaving raw JSON visible in the post. The control flagged it (`PUBLISH_VERIFICATION_FAILED`), the cause was diagnosed from the approved-vs-live hashes, the publisher was corrected, and the post was re-published in place. This is the exception → root cause → corrective action loop a Type II auditor wants to see documented.

### Stage 4 – Publishing to the brand's WordPress site

| # | Control (what we do) | TSC criteria | 2026 AI evidence request it satisfies | Implementation | Evidence artefact |
|---|---|---|---|---|---|
| C-31 | Per-tenant WordPress application password (Author/Editor role only), read from secrets only inside the reviewer-triggered publish path; missing creds fail closed (503); no cross-tenant reuse | CC6.1, CC6.3 | "Least privilege for AI publishing actions" | `cms.load_wordpress_credentials`, `publisher_factory` | `test_credentials_are_per_tenant_and_fail_closed` |
| C-32 | Publish target host must be within the tenant's configured brand domain and use HTTPS | C1.1, CC6.7 | — | `WordPressPublisher.__init__` | `test_publish_target_must_be_inside_brand_domain` |
| C-33 | Post-publish read-back: live post content (context=edit raw) compared to the approved HTML; mismatch flagged `PUBLISH_VERIFICATION_FAILED`, `publish_verified=false`, surfaced in `/admin/metrics.published_unverified` | PI1.4, PI1.5, CC7.1 | "Evidence that what the AI pipeline published equals what the human approved" | `WordPressPublisher.publish`, `workflow.publish` | PUBLISH audit record (`approved_body_sha256`, `published_html_sha256`, `verified`) |
| C-34 | AI-assistance disclosure appended to every published post | CC2.3 | "Disclosure practice for AI-generated content" | `cms.render_html` | live post footer |

### End-to-end lineage (what the auditor can pull for any live article)

`BRAND_ANALYSIS` (pages + hashes, prompt version, model) → `BRAND_REVIEW` (who confirmed) → `STRATEGY_GENERATED` (profile version, chunks) → `STRATEGY_REVIEW` → `GENERATION` (strategy id, item, input/output hashes) → `REVIEW` → `PUBLISH` (URL, remote post id, approved vs live hash, verified). All records hash-chained and in S3 Object Lock.

### Machine-readable source

The Governance workspace (`/governance` → Control Mapping) reads `compliance/controls.yaml`, which carries each control's status (`implemented` / `partial` / `planned` / `unverified`), evidence pointers, gap and owner. `tests/test_governance.py` asserts the YAML and this table list the same control IDs.

## Known gaps (to close before Type II observation period)

| Gap | Owner | Target |
|---|---|---|
| `redaction_mapping` must be envelope-encrypted with KMS at rest (currently plaintext JSON in the drafts table) | platform-infra | before prod |
| Production JWT verification must use the IdP's JWKS (RS256) rather than the HS256 shared secret used for local dev | marketing-platform-security | before prod |
| Scheduled job to purge drafts older than `draft_retention_days` (currently manual via `DELETE /drafts/{id}`) | marketing-platform-leads | before prod |
| Embedding provider for pgvector must also be under ZDR and added to the inventory | marketing-platform-security | before prod |
