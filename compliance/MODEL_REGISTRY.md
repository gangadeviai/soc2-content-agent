# Model Registry (CC8.1 change management for models)

A model change is any change to `AGENT_MODEL_ID`, `AGENT_MODEL_EFFORT`, `AGENT_ENABLE_REFUSAL_FALLBACK`, or the output schema in `app/llm.py`. It requires: PR, CODEOWNER approval, injection regression tests green, a 20-brief side-by-side evaluation signed off by a marketing lead, vendor assessment unchanged (or re-done), and an entry here.

| Date | Model ID | Effort | Fallback | Prompt version | Eval result | Approved by | PR | Rollback plan |
|---|---|---|---|---|---|---|---|---|
| 2026-10-09 | claude-opus-5-5 | high | default (server-side) | 1.0.0 | baseline | (fill in) | #1 | n/a (initial) |

Operational note: the audit record stores both `model_requested` and `model_served`. A monthly query of distinct `model_served` values against this registry is part of the monitoring evidence (`MONITORING.md`).
