# How the showcase metrics are calculated

Every number in a `docs/showcase/<tenant>/metrics.json` file is produced by the application
during a real run (`scripts/run_showcase.py` only *reads* API responses; it computes nothing of
its own). This page states, for each metric, where it comes from and how it is derived, so the
figures can be reproduced or challenged.

## Deliverable 1 — the generated article

| Field | Source | Notes |
|---|---|---|
| `title`, body (`article.md`) | model output, `DRAFT_SCHEMA` (`app/llm.py`) | Stored exactly as generated; the reviewer approves this text and its hash is what publication is verified against |
| `seo.title_tag`, `meta_description`, `slug`, `primary_keyword`, `secondary_keywords` | model output | Validated, never rewritten by the application |
| `direct_answer`, FAQ, key takeaways | model output (`aeo` block) | FAQ is rendered into the published post (`cms.render_html`) |
| `quotable_facts[].source` | model output (`geo` block) | A fact without a source is flagged `GEO:unsourced_fact`; nothing is auto-sourced |
| `claims_to_verify` | model output | The model is instructed to list every factual/numeric statement; a human must check them before approval |

## Deliverable 2 — the SEO / AEO / GEO score

Computed by `app/seo.py::evaluate` — deterministic code, no model involvement.

**Score** = round(100 × Σ weight(passed checks) / Σ weight(all applicable checks)). Threshold for a
reviewer flag: **70**. Checks that do not apply to a content type (e.g. JSON-LD for a social post)
are excluded from the denominator rather than counted as passes.

| Pillar | Check | Rule | Weight |
|---|---|---|---|
| SEO | title_tag_length | 30–60 characters | 10 |
| SEO | meta_description_length | 70–155 characters | 10 |
| SEO | primary_keyword_present | non-empty | 6 |
| SEO | keyword_in_title_tag | case-insensitive substring | 6 |
| SEO | keyword_in_headline | in `title` | 5 |
| SEO | keyword_in_first_100_words | in the first 100 body words | 5 |
| SEO | slug_format | `^[a-z0-9]+(-[a-z0-9]+)*$`, ≤ 75 chars, contains keyword | 4 |
| SEO | h2_structure (long-form only) | ≥ 2 `##` headings | 5 |
| SEO | heading_hierarchy (long-form) | no skipped levels | 3 |
| SEO | single_h1 (long-form) | ≤ 1 `#` in body (title is the H1) | 2 |
| SEO | word_count | blog 600–2200 · landing 300–1200 · press 300–900 · email 80–400 · social 10–90 | 8 |
| SEO | image_alt_text | every `![alt](…)` has non-empty alt | 3 |
| SEO | readability_sentence_length | average ≤ 22 words/sentence | 4 |
| SEO | secondary_keywords_used | ≥ 1 secondary keyword, each present in body | 3 |
| AEO | direct_answer_length | 30–70 words | 6 |
| AEO | answer_up_front | ≥ 40 % word overlap between direct answer and first paragraph | 5 |
| AEO | faq_quality | ≥ 3 (long-form) / ≥ 1 Q&As; question ends with `?`, answer 15–90 words | 8 |
| AEO | key_takeaways | 3–6 items | 4 |
| GEO | sourced_quotable_facts | ≥ 2 facts, every fact has a source | 8 |
| GEO | brand_entity_consistency | brand entity named in body and in direct answer | 5 |
| GEO | entities_named_in_body | every listed entity appears in body | 3 |
| GEO | schema_org_jsonld (long-form) | valid JSON; an `Article`-type node; `FAQPage` node when a FAQ exists | 6 |

What the score is **not**: a Google ranking prediction, a guarantee of AI-search citation, or a
compliance score. It measures whether the content carries the structural elements those systems
consume.

Other Deliverable-2 fields: `words` = whitespace-split count of the body; `model_served` and
`prompt_version` come from the GENERATION audit record (`app/workflow.py`).

## Deliverable 3 — governance evidence

| Field | Source | Derivation |
|---|---|---|
| approval records | `GET /governance/approvals` | `BRAND_REVIEW`, `STRATEGY_REVIEW`, `REVIEW` audit records joined to the workflow row to show the requester beside the reviewer (`app/governance.py::approval_history`) |
| `integrity.status` | `POST /governance/integrity/verify` | Re-computes every record's SHA-256 and checks each `prev_hash` link (`app/audit.py::verify_chain`); result is stored with a timestamp. "verified" is shown only after the check ran |
| `publication_evidence` | `GET /governance/evidence` | Draft row + its `PUBLISH` records: `approved_body_sha256` is the hash of the approved HTML sent; `published_html_sha256` is the hash of the content read back from WordPress (`context=edit`); `verified` is their equality plus `status == publish` |
| `audit_lineage_for_article` | `GET /governance/audit?object_id=<draft>` | Every audit record carrying this draft id, oldest first, with its hash prefix |

Limits stated plainly: the hash chain detects alteration of the local mirror; it does not by
itself prevent replacement of the whole mirror (that is the role of write-once storage). The free
model tier used in learning runs is not under zero-data-retention terms, which is why such runs
use public brands only.
