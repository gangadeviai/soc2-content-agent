# Vendor / Subprocessor Assessment — Model Provider (CC9.2, P6.4)

**Vendor:** Anthropic, PBC  **Service:** Claude API (Messages)  **Assessment date:** 2026-10-09  **Next review:** 2027-10-09

## Subservice organization determination
The model provider processes client-confidential (redacted) text on our behalf and its controls over retention and training are relied upon to meet our confidentiality commitments. **Determination: subservice organization, carve-out method.** Rationale: we cannot test their controls directly; we rely on their SOC 2 Type II report and contractual ZDR terms, and monitor via the CSOCs in `SYSTEM_DESCRIPTION.md` §5.

## Required contractual terms (attach signed copies)
- [ ] Enterprise agreement with **Zero Data Retention** for API inputs/outputs (confirm the org-level ZDR configuration is active in the console; `AGENT_ZDR_CONTRACT_CONFIRMED=true` records that this was checked).
- [ ] No use of inputs/outputs for model training.
- [ ] Data Processing Addendum incl. subprocessor list and breach-notification SLA.
- [ ] Most recent SOC 2 Type II report reviewed; exceptions noted below.

## Model-specific note
`claude-opus-5-5` is served under the org's ZDR configuration. **Claude Fable 5.1 (`claude-fable-5-1`) requires 30-day retention unless Anthropic expressly authorises ZDR** — switching to it is a vendor-assessment change, not just a config change. Server-side refusal fallback (`fallbacks: "default"`) may route a refused request to a different Anthropic model within the same ZDR org; the served model is recorded in the audit trail.

## AI-specific due-diligence questions (answered from vendor documentation; cite source)
| Question | Answer | Source |
|---|---|---|
| Are API inputs retained? | | |
| Are inputs used for training? | | |
| Where is data processed (region)? `inference_geo` pinned? | | |
| Breach notification window? | | |
| Subprocessors used for inference? | | |
| Model deprecation notice period? | | |

## Data flow
Marketer → Gateway (redaction) → **Anthropic API (TLS, ZDR)** → Gateway → Drafts DB. Personal data touches the AI boundary only as placeholders.

## Alternative provider assessed: Google AI Studio (Gemini) free tier — `AGENT_PROVIDER=gemini`

| Question | Answer | Consequence |
|---|---|---|
| Are inputs retained / used for training? | **Yes — free-tier terms permit Google to use prompts and outputs to improve products.** Paid tier has different terms. | **Fails control C-03 (ZDR).** |
| Contract / DPA available on free tier? | No negotiated terms. | No subservice-organization reliance possible. |
| Determination | **Not approved for client data.** Permitted only in `dev`/`demo` environments with public, non-client material for learning and evaluation. `Settings.validate_for_environment` refuses it in `prod`; every CONFIG audit record carries `data_retention_class = FREE_TIER_NOT_ZDR_MAY_BE_USED_FOR_TRAINING`. | Documented, visible, bounded. |

## Other vendors in the AI path
| Vendor | Role | Determination |
|---|---|---|
| [Cloud provider] | compute, S3 Object Lock, KMS | subservice org (carve-out) |
| [Embedding provider] | vectors for RAG | **TBD — must be assessed and under ZDR before prod** |
| [IdP] | authentication | subservice org (carve-out) |
