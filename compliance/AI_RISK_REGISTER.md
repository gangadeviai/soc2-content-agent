# AI Risk Register (CC3.1 – CC3.4)

Reviewed annually and on any model/prompt/vendor change. Likelihood/impact: L/M/H.

| ID | Risk | L | I | Treatment (control ref) | Residual | Owner |
|---|---|---|---|---|---|---|
| R-01 | Client PII or embargoed data sent to model provider | M | H | Redaction gateway (C-02), deny-list terms, ZDR contract (C-03), egress allow-list (C-01) | L | platform-security |
| R-02 | Cross-tenant leakage via RAG | L | H | Tenant filter from token, RLS, startup read-only check, test (C-05) | L | platform-security |
| R-03 | Prompt injection via knowledge base or brief leads to off-brand/defamatory copy or data exfiltration | M | M | Delimiting, chunk screening, injection guard prompt, regression tests (C-08); human review (C-09) | L | platform-leads |
| R-04 | Hallucinated statistics, quotes, URLs published ("over-trust in outputs") | M | H | Structured `claims_to_verify`, output flags (C-10), mandatory reviewer note (C-09) | L | marketing managers |
| R-05 | Unauthorised prompt change alters behaviour | L | M | Manifest hashes, CODEOWNERS, CI (C-06) | L | platform-security |
| R-06 | Silent model change / drift (provider update, fallback model) | M | M | Pinned model ID, served-model logging, MODEL_FALLBACK flag, metrics thresholds (C-07, C-16, C-21) | L | platform-security |
| R-07 | Agent publishes directly (credential misuse) | L | H | No publish scope on agent identity; separate token in publish path; SoD (C-09) | L | platform-infra |
| R-08 | Audit log altered or deleted after an incident | L | H | Hash chain + S3 Object Lock COMPLIANCE, write-only IAM (C-11) | L | platform-infra |
| R-09 | Vendor incident at model provider | L | M | Subservice org monitoring, SOC 2 report review, incident playbook (C-17) | M | GRC |
| R-10 | Shadow AI: staff paste client briefs into consumer tools | M | H | Egress blocking, AI use policy + training, inventory reconciliation (C-14, C-18) | M | GRC |
| R-11 | Copyright/plagiarism claim on generated copy | L | M | Reviewer checklist, lineage record proves inputs, disclosure line | M | legal |
| R-12 | Model refusal blocks legitimate work (availability of service) | M | L | Refusal category logged; server-side fallback; manual escalation | L | platform-leads |
| R-13 | Crawler reaches internal/metadata endpoints or third-party sites (SSRF, unauthorised collection) | L | H | Git-pinned crawl scope, SSRF guard, robots.txt, redirect check (C-22..C-24) | L | platform-security |
| R-14 | Brand website contains injected instructions that steer analysis or strategy | M | M | Page screening + delimiting, confirmation gate (C-25, C-26) | L | platform-leads |
| R-15 | Agency acts on hallucinated brand facts (wrong audience, invented proof points) | M | H | `observations_to_confirm`, human confirmation before strategy (C-26, C-27) | L | marketing managers |
| R-16 | Strategy embeds unsubstantiated claims that propagate into many drafts | M | M | Item-level claim screening, strategy approval gate (C-29, C-30) | L | marketing managers |
| R-17 | Publish credential misuse: wrong site, cross-tenant publish, over-privileged WP user | L | H | Host pinned to brand domain, per-tenant app password with Author/Editor role, fail-closed (C-31, C-32) | L | platform-infra |
| R-18 | Live content differs from approved content (plugin rewrite, MITM, CMS tampering) | L | H | Read-back verification + flag + metric (C-33) | L | platform-leads |
