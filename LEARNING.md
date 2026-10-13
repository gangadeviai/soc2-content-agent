# Learning SOC 2 Type II through a real AI agent

This repository is a working marketing-content agent (brand analysis → strategy → writing →
publishing) built so that every SOC 2 concept shows up as running code, a test, or an audit
record you can open. This document is the study guide.

## 1. SOC 2 in one page

| Term | Meaning | Where you see it here |
|---|---|---|
| **Trust Services Criteria (TSC)** | The AICPA criteria a system is judged against: Security (Common Criteria CC1-CC9), Availability, Processing Integrity (PI), Confidentiality (C), Privacy (P). Current version: 2017 criteria, 2022 Points of Focus. | Column 3 of `compliance/SOC2_CONTROL_MATRIX.md` |
| **Control** | Something you *do* to meet a criterion. You choose them; the auditor tests them. | 37 controls, C-01 … C-37, each tied to code and a test |
| **Type I** | Auditor checks controls are *designed* properly at a point in time. | Reading this repo + running `pytest` is Type I-style evidence |
| **Type II** | Auditor checks controls *operated effectively* over an observation period (typically 3-12 months). Needs evidence that each control fired, every time, all period. | The audit chain, `/admin/metrics`, and `compliance/EVIDENCE_COLLECTION.md` |
| **Evidence** | Artefacts that prove operation: logs, tickets, approvals, screenshots, test runs. | `audit_log/audit.jsonl` (mirror of the S3 Object Lock bucket), CI history, PR approvals |
| **Exception** | A control that didn't operate (e.g. an approval without a note). Auditors sample and look for these. | Try it: an approval without a note is refused with HTTP 422 and nothing is written |
| **Subservice organization** | A vendor whose controls you rely on (model provider, cloud). Carve-out = you don't test them; you rely on their SOC 2 report + contract. | `compliance/VENDOR_SUBPROCESSOR_ASSESSMENT.md` |
| **CUEC** | Complementary User Entity Controls: things your *customer* must do. | System description §5 |

## 2. Why an AI agent makes SOC 2 harder (and what 2026 auditors ask)

Auditors have not been given AI-specific criteria. They apply the old criteria to new risks and ask for specific evidence. Each row below is something a 2026 auditor asks and where this repo answers it.

| Auditor question (2026 practice) | Underlying criterion | Control here | Try it |
|---|---|---|---|
| "Where does client data leave your boundary for AI, and what is removed first?" | C1.1, CC6.7 | C-01, C-02 redaction gateway | Paste an email + API key in a brief; open the GENERATION audit record: counts only, no PII |
| "Is the model vendor a subservice org? What are the retention terms?" | CC9.2 | C-03; `data_retention_class` in every CONFIG record | Run with `AGENT_PROVIDER=gemini` and read `/healthz` — it says the free tier is NOT ZDR |
| "Can one client's data reach another's output?" | CC6.1, C1.1 | C-05 tenant isolation | Read a draft as a client-b user: 404 |
| "How do you control prompt changes?" | CC8.1 | C-06 manifest hashes + CODEOWNERS | Edit `prompts/1.2.0/system.md` and start the app: it refuses |
| "Which model actually produced this article?" | CC8.1, PI1.2 | C-07, C-21 `model_requested` vs `model_served` | Lineage on any draft |
| "What stops injected instructions in retrieved content?" | CC7.1, PI1.2 | C-08, C-25 screening + delimiting | Brand analysis drops `/blog/injected` and flags it |
| "Who approved this before it went live, and what did they check?" | PI1.4 | C-09, C-26, C-30 human gates with notes, SoD | Requester approving own work: 403, and the denial itself is audited |
| "Does what went live equal what was approved?" | PI1.5 | C-33 read-back verification | PUBLISH record: `approved_body_sha256` vs `published_html_sha256`, `verified` |
| "Show monitoring ran all period with defined thresholds." | CC4.1, CC7.1 | C-16, C-35 `/admin/metrics` + `MONITORING.md` | Monthly snapshot per `EVIDENCE_COLLECTION.md` |
| "Can your logs be altered?" | CC7.2 | C-11 hash chain + Object Lock | Change one byte in `audit.jsonl`, call `/admin/audit/verify` |
| "Is the AI pipeline's external data collection lawful and scoped?" | CC6.6, CC9.2 | C-22..C-24 Git-pinned crawl scope, robots.txt, SSRF guard | Try to pass a URL in the request: ignored |
| "Do you have an AI inventory and shadow-AI detection?" | CC3.2, CC6.1 | C-14 `AI_INVENTORY.yaml` + egress allow-list | Compare inventory to `/healthz` provider |

## 3. The Type II mindset: design is 20 %, operation is 80 %

Type I asks "is the gate there?" Type II asks "did the gate close every single time for six months, and can you prove it?" Practise it with this repo:

1. **Run the pipeline regularly** (weekly is realistic). Every run leaves hash-chained records.
2. **Collect evidence on a schedule** — `compliance/EVIDENCE_COLLECTION.md`. Export `/admin/metrics` and `/admin/audit/verify` on the 1st of each month into an `evidence/YYYY-MM/` folder (git-ignored or a separate private repo).
3. **Create exceptions on purpose, then handle them.** Approve a draft with a vague note; later decide whether that is an exception and what the corrective action is. Auditors care more about how you handle exceptions than that none exist.
4. **Change something through the proper channel.** Bump a prompt → new version dir → manifest → PR → CI. The PR *is* the change-management evidence.
5. **Hold a quarterly access review.** List who has requester/reviewer/admin roles and the service accounts in `AI_INVENTORY.yaml`; sign and date it.
6. **Run the incident tabletop once** using `INCIDENT_PLAYBOOK_AI.md`; record it at the bottom of the file.
7. **Re-assess the vendor annually** — `VENDOR_SUBPROCESSOR_ASSESSMENT.md`. For this learning setup the honest assessment is: *the free-tier model provider fails C-03; acceptable only because no client data is used.* Writing that sentence down, instead of ignoring it, is the whole discipline.

## 4. Reading the audit log like an auditor

```bash
python -c "import json;[print(r['ts'][:19],r['event_type'],r['actor_id'],r['record_hash'][:10]) for r in map(json.loads,open('audit_log/audit.jsonl'))]"
```

For any published article pull the lineage pack: `GET /admin/audit/records?object_ids=<brand_id>,<strategy_id>,<draft_id>`. You should be able to answer, from records alone: who requested, which pages of the brand site were read (hashes), which prompt version and model, which knowledge chunks, what the model produced (hash), who approved and what they said, what went live and whether it matched.

## 5. Honest limits of this learning setup

- The free-tier model provider is **not** ZDR. Never point this configuration at a real client's material.
- The S3 Object Lock bucket is Terraform only; locally the JSONL mirror is the evidence. Tamper-evidence works; tamper-*proofing* needs the bucket.
- Authentication is a dev-only HS256 token minted by `/dev/token`. Production needs the IdP.
- The redaction mapping is stored in plaintext; production needs KMS envelope encryption.

Those four items are the "Known gaps" table in the control matrix — a real Type II readiness plan always has one.
