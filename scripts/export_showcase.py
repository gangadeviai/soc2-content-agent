"""Export the evidence pack for an article that was produced earlier (no re-run).

    python scripts/export_showcase.py <tenant> <draft_id-prefix>

Reads the stored draft and the governance endpoints of the running server and writes
docs/showcase/<tenant>/metrics.json + article.md in the same shape as run_showcase.py.
Nothing is generated or modified - this is a read-only export of what the run recorded.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

B = "http://127.0.0.1:8000"
tenant, prefix = sys.argv[1], sys.argv[2]
out = Path(__file__).resolve().parent.parent / "docs" / "showcase" / tenant
out.mkdir(parents=True, exist_ok=True)


def tok(sub: str, roles: str) -> dict:
    t = httpx.post(f"{B}/dev/token", params={"sub": sub, "tenant_id": tenant, "roles": roles}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


sam, ops = tok("sam", "reviewer"), tok("ops", "admin")
d = next(x for x in httpx.get(f"{B}/drafts", headers=ops).json() if x["id"].startswith(prefix))
s = httpx.get(f"{B}/strategies/{d['strategy_id']}", headers=ops).json() if d.get("strategy_id") else None
b = httpx.get(f"{B}/brands/{s['brand_profile_id']}", headers=ops).json() if s else None
rep = d["seo_report"]
approvals = httpx.get(f"{B}/governance/approvals", headers=sam).json()["items"]
lineage = httpx.get(f"{B}/governance/audit", params={"object_id": d["id"]}, headers=ops).json()["items"]
ev = httpx.get(f"{B}/governance/evidence", headers=sam).json()
v = httpx.post(f"{B}/governance/integrity/verify", headers=ops).json()

log = {
    "tenant": tenant,
    "exported_at": datetime.now(UTC).isoformat(),
    "note": "Exported from stored records of an earlier run; no content was regenerated.",
    "environment": httpx.get(f"{B}/healthz").json(),
    "stages": {
        "brand_analysis": b
        and {
            "profile_id": b["id"],
            "version": b["version"],
            "pages_used": [p["url"] for p in b["pages"]],
            "brand_name": b["profile"].get("brand_name"),
            "positioning": b["profile"].get("positioning"),
            "voice": b["profile"].get("voice"),
            "observations_to_confirm": b["profile"].get("observations_to_confirm"),
            "review_flags": b["review_flags"],
            "model_served": b["lineage"].get("model_served"),
        },
        "strategy": s
        and {
            "strategy_id": s["id"],
            "pillars": [p["name"] for p in s["plan"].get("pillars", [])],
            "calendar": [
                {k: it.get(k) for k in ("week", "content_type", "working_title", "target_keyword")}
                for it in s["plan"].get("calendar", [])
            ],
            "review_flags": s["review_flags"],
        },
        "draft": {
            "draft_id": d["id"],
            "title": d["title"],
            "words": len(d["body_markdown"].split()),
            "seo_aeo_geo_score": d["seo_score"],
            "threshold": rep["threshold"],
            "checks_passed": sum(c["passed"] for c in rep["checks"]),
            "checks_total": len(rep["checks"]),
            "checks": [{k: c[k] for k in ("pillar", "name", "passed", "detail")} for c in rep["checks"]],
            "seo": d["seo"]["seo"],
            "direct_answer": d["seo"]["aeo"]["direct_answer"],
            "faq_count": len(d["seo"]["aeo"]["faq"]),
            "quotable_facts": d["seo"]["geo"]["quotable_facts"],
            "claims_to_verify": d["claims_to_verify"],
            "review_flags": d["review_flags"],
            "lineage": {k: d["lineage"].get(k) for k in ("prompt_version", "model_served", "strategy_id")},
        },
        "publish": {
            "status": d["status"],
            "published_url": d["published_url"],
            "remote_post_id": d["remote_post_id"],
            "read_back_verified": d["publish_verified"],
        },
    },
    "governance": {
        "approvals": [
            {k: a[k] for k in ("ts", "stage", "requester_id", "reviewer_id", "decision", "separation_of_duties")}
            for a in approvals
        ],
        "integrity": {k: v[k] for k in ("status", "records_checked", "checked_at")},
        "publication_evidence": next((e for e in ev if e["draft_id"] == d["id"]), None),
        "audit_lineage_for_article": [
            {"ts": r["ts"], "event": r["event_type"], "actor": r["actor_id"], "hash": r["record_hash"][:16]}
            for r in reversed(lineage)
        ],
    },
}
(out / "metrics.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
(out / "article.md").write_text(f"# {d['title']}\n\n{d['body_markdown']}\n", encoding="utf-8")
print(f"exported {d['title']!r} -> {out}")
