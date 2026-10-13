# Evidence Collection Schedule

| Cadence | Evidence | How | Control |
|---|---|---|---|
| Per PR | CI run (tests, manifest check, lint, vuln scan, secret scan) | GitHub Actions history | C-06, C-08, C-20 |
| Per change | PR with CODEOWNER approval for `prompts/`, `app/llm.py`, `app/config.py` | GitHub PR export | C-06, C-07 |
| Continuous | Audit records in S3 Object Lock | bucket listing + sample records | C-11 |
| Monthly | `/admin/metrics` snapshot, `/admin/audit/verify` result, distinct `model_served` query | export to evidence folder | C-16, C-07 |
| Monthly | Egress deny logs reconciled with `AI_INVENTORY.yaml` | ticket | C-14 |
| Quarterly | Access review incl. service accounts and API keys; key rotation evidence | IdP + secrets-manager export | C-13 |
| Annually | Risk register review, vendor assessment refresh, incident tabletop, policy re-acknowledgment, training completion | signed documents | C-15, C-03, C-17, C-18 |
| On demand | Lineage pack for any published article: GENERATION + REVIEW + PUBLISH records | audit query by `draft_id` | C-09, C-11 |
