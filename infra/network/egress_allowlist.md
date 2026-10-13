# Egress allow-list (shadow-AI control, CC6.6 / CC6.7)

The agent runtime runs in a subnet whose egress proxy permits **only**:

| Destination | Purpose | Owner |
|---|---|---|
| `api.anthropic.com:443` | Model inference (ZDR org) | marketing-platform-security |
| `content-agent-audit-worm.s3.<region>.amazonaws.com:443` | WORM audit writes | platform-infra |
| `<rag-db-host>:5432` | Read-only knowledge base | platform-infra |
| `cms.example.com:443` | Publish path only (separate egress policy attached to the publish workload identity) | marketing-platform-leads |

Any other model provider, embedding API, or SaaS endpoint is blocked. Proxy deny
logs are shipped to the SIEM and reviewed weekly; a denied request to an AI
endpoint opens a ticket (evidence for the "shadow AI detection reconciled to the
AI inventory" expectation in `compliance/AI_INVENTORY.yaml`).
