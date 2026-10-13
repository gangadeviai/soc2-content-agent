# SOC 2-ready Marketing Content Agent

> **Status (2026-10-12):** full pipeline verified end-to-end on a real run at $0 — real crawl of a
> public brand site, real Gemini free-tier model, human gates at every stage, real publish to a
> WordPress.com site with read-back verification, tamper-evident audit trail. 49 control tests pass.
> Built as a learning project for **SOC 2 Type II in an AI-agent context**; see [LEARNING.md](LEARNING.md).
> One real exception was caught and remediated during that run — see the field note under control C-37.

An agency-grade pipeline that **analyzes a brand from its own website, builds a
content strategy, writes the content, and publishes it to the brand's WordPress
site** — with a human gate and an immutable audit record at every stage, mapped to
the controls a SOC 2 auditor tests in 2026.

```
tenants/brands.yaml (Git)          OIDC token (sub, tenant_id, roles)
        │                                     │
        ▼                                     ▼
┌─ Stage 1 ─────────────┐   ┌─ Stage 2 ─────────────┐   ┌─ Stage 3 ─────────────┐   ┌─ Stage 4 ──────────────────┐
│ crawl brand site      │   │ strategy from         │   │ draft from strategy   │   │ reviewer approves          │
│ (scope-pinned, SSRF   │──►│ CONFIRMED profile     │──►│ item (or free brief)  │──►│ publish → WordPress        │
│ guard, robots, redact,│   │ + tenant RAG          │   │ redact → RAG → LLM →  │   │ (per-tenant app password)  │
│ injection screen)     │   │ item claim screening  │   │ output screening      │   │ read-back verification     │
│ → BrandProfile        │   │ → ContentStrategy     │   │ → Draft               │   │ → PUBLISHED (+verified)    │
│ ✋ reviewer confirms  │   │ ✋ reviewer approves  │   │ ✋ reviewer approves  │   │ ✋ reviewer triggers       │
└───────────────────────┘   └───────────────────────┘   └───────────────────────┘   └────────────────────────────┘
          │                           │                           │                            │
          └───────────── hash-chained audit records → S3 Object Lock (COMPLIANCE) ─────────────┘
```

All model calls go through one gateway (`app/llm.py`) with redaction before the
boundary, a pinned model, Git-pinned prompts, and structured outputs that force
the model to list what a human must verify.

## Compliance status (October 2026)

* Criteria: AICPA **2017 Trust Services Criteria with 2022 Points of Focus** — still the operative version. No AI-specific TSC exists.
* The AICPA's 2026 exposure drafts (Feb/Mar 2026) change the **attestation standards** the auditor follows (AT-C 105/205/210), not the criteria you are measured against.
* What changed in practice: auditors now ask for AI-specific evidence under the existing criteria — AI inventory, per-inference logs with model/prompt lineage, model-change control, subprocessor/ZDR determinations, shadow-AI detection, human-oversight records, AI incident playbooks. All 34 controls are mapped in [compliance/SOC2_CONTROL_MATRIX.md](compliance/SOC2_CONTROL_MATRIX.md).

## Layout

| Path | Purpose |
|---|---|
| `app/brand.py` | Stage 1: scoped crawler + brand analysis + confirmation gate |
| `app/strategy.py` | Stage 2: strategy planning, approval, draft-from-item |
| `app/workflow.py` | Stage 3/4: generation, review, publish with verification |
| `app/cms.py` | WordPress REST publisher, per-tenant credentials, read-back check |
| `app/llm.py`, `app/redaction.py`, `app/injection.py`, `app/rag.py`, `app/audit.py`, `app/auth.py` | Gateway controls shared by every stage |
| `prompts/` | Versioned stage prompts + `manifest.json` (SHA-256 pinned, approval status) |
| `tenants/brands.yaml` | Per-client crawl scope and WordPress URL (the only source of targets) |
| `tenants/deny_terms.yaml` | Per-client confidential terms always redacted |
| `tests/` | 33 control tests an auditor can watch run |
| `compliance/` | Control matrix, system description, AI inventory, risk register, vendor assessment, model registry, monitoring, incident playbook, AI use policy, evidence schedule |
| `infra/` | Read-only DB role + RLS, S3 Object Lock bucket (Terraform), egress allow-list |
| `.github/` | CI gates and CODEOWNERS for prompt / model / tenant-config changes |

## Zero-cost real run (learning setup)

Everything real — real crawl of a public brand site, real model, real WordPress — for $0:

| Piece | Free option | Compliance note |
|---|---|---|
| Model | Google AI Studio free tier: `AGENT_PROVIDER=gemini`, `GEMINI_API_KEY=…` | **Not** zero-data-retention. The agent stamps `data_retention_class=FREE_TIER_NOT_ZDR…` into the audit log and refuses this provider in `prod`. Use a public brand, never client material. |
| Brand website | Any public site, set in `tenants/brands.yaml` | Scope pinned in Git, robots.txt honoured |
| WordPress | Local Docker: `docker compose -f docker-compose.wordpress.yml up -d` → http://localhost:8080 | `allow_local` is granted only outside `prod` |

```bash
pip install -e ".[dev]"
```

Edit [.env](.env) (git-ignored): paste `GEMINI_API_KEY`, and after the WordPress install paste the Editor user's application password. Put the brand in `tenants/brands.yaml`, then:

```bash
uvicorn app.main:app --reload
```

Open **http://localhost:8000/demo** for the walkthrough UI (or `/docs` for the raw API). Study guide: [LEARNING.md](LEARNING.md).

## Offline mode (no API key, no network)

```bash
pip install -e ".[dev]"
```

```bash
python scripts/run_demo.py
```

Open **http://localhost:8000/demo**. It runs with a simulated model, a simulated brand
website and a simulated WordPress, so every control runs for real while nothing external is
touched. Switch persona (requester / reviewer / admin / other tenant), walk the four stages,
press the "Try to break it" buttons, then verify the audit chain and pull the lineage pack.

For a terminal walkthrough that prints every control as it fires (good for an audit meeting):

```bash
python scripts/demo_walkthrough.py
```

| Mode (`AGENT_ENV`) | Model | Brand website | WordPress | Use |
|---|---|---|---|---|
| `demo` | fake | fake (`app/demo_data.py`) | fake | demos, training, audit walkthroughs |
| `test` | fake | fake | fake | `pytest` |
| `dev` | **Claude (real)** | **real crawl** of the domain in `tenants/brands.yaml` | **real WordPress** if `AGENT_WP_*` set, else 503 | integration with a client site |
| `prod` | real, ZDR enforced | real | real | production; start-gate checks apply |

## Run locally

```bash
pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

```bash
uvicorn app.main:app --reload
```

Dev tokens (prod tokens come from your IdP):

```bash
curl -s -X POST "localhost:8000/dev/token?sub=u1&tenant_id=client-a&roles=requester"
```

```bash
curl -s -X POST "localhost:8000/dev/token?sub=u2&tenant_id=client-a&roles=reviewer"
```

Pipeline (requester `u1`, reviewer `u2`; reviewer must differ from requester at every gate):

| Step | Call |
|---|---|
| 1. Analyze brand | `POST /brands/analyze` `{"notes": "..."}` |
| 1b. Confirm profile | `POST /brands/{id}/review` `{"decision":"confirm","note":"checked with client"}` |
| 2. Plan strategy | `POST /strategies` `{"objectives":"...","horizon_weeks":8}` |
| 2b. Approve strategy | `POST /strategies/{id}/review` `{"decision":"approve","note":"..."}` |
| 3. Draft an item | `POST /strategies/{id}/items/0/draft` (or `POST /drafts` with a free brief) |
| 4a. Approve draft | `POST /review/{draft_id}` `{"decision":"approve","note":"claims verified against ..."}` |
| 4b. Publish | `POST /review/{draft_id}/publish` → WordPress, read-back verified |

Monitoring: `GET /admin/metrics`, `GET /admin/audit/verify`, `GET /healthz`.

Run the control tests:

```bash
pytest -q
```

## SEO / AEO / GEO fitness

Every draft must be fit for classic search, answer engines and generative (AI) search:

| Pillar | What the model must produce (`DRAFT_SCHEMA`) | What `app/seo.py` checks deterministically |
|---|---|---|
| **SEO** | title tag, meta description, slug, primary + secondary keywords | lengths (30-60 / 70-155), keyword in title tag, headline, slug and first 100 words, H2 structure, heading hierarchy, word count by content type, alt text, sentence length, secondary keywords used |
| **AEO** | 30-70 word direct answer, 3-5 FAQ Q&As, 3-6 key takeaways | answer length, opening paragraph restates the answer, FAQ well-formedness, takeaway count |
| **GEO** | canonical brand entity, named entities, ≥2 sourced quotable facts, schema.org JSON-LD | every fact carries a source, entities appear in body, valid Article (+FAQPage) JSON-LD |

Score 0-100; below **70** the draft is flagged. Failures never block — they're shown to the reviewer and recorded in the GENERATION audit record. On publish, slug and excerpt go natively, the FAQ is rendered into the post, JSON-LD is re-serialised and embedded, and title tag / meta description are sent to the site's SEO plugin (`seo_plugin: yoast | rankmath` in `tenants/brands.yaml`). If WordPress strips the JSON-LD (publishing user lacks `unfiltered_html`), read-back verification fails and the reviewer is told.

## WordPress setup per brand

1. Create a dedicated WordPress user (Author or Editor role — not Administrator) on the brand's site.
2. Generate an application password for that user.
3. Store as `AGENT_WP_USER__<TENANT>` / `AGENT_WP_APP_PASSWORD__<TENANT>` in the secrets manager, mounted only into the publish workload.
4. Add the brand to `tenants/brands.yaml` via PR (domain, `wordpress_url`, crawl scope).

## Production checklist

1. `AGENT_ENV=prod` refuses to start unless `AGENT_ZDR_CONTRACT_CONFIRMED=true`, an Object Lock bucket is configured, the JWT secret is not the dev default, and the DB is not SQLite.
2. Switch `app/auth.py` to JWKS/RS256 verification against your IdP.
3. Envelope-encrypt `redaction_mapping` columns with KMS.
4. Configure the embedding provider for `PgVectorRetriever` (must also be under ZDR) and add it to `compliance/AI_INVENTORY.yaml`.
5. Schedule the draft-retention purge job.

See the "Known gaps" table in the control matrix for owners and targets.

## Model note

The service pins `claude-opus-5-5` and enables Anthropic's server-side refusal
fallback (`fallbacks: "default"`). When a fallback model serves a request, the
audit record stores both `model_requested` and `model_served` and the artefact gets a
`MODEL_FALLBACK` flag for the reviewer. Changing the model or disabling fallback is a
change-managed event recorded in `compliance/MODEL_REGISTRY.md`. Claude Fable 5.1 is
not eligible for Zero Data Retention by default, so moving to it requires a fresh
vendor assessment.
