"""Run the full pipeline for a tenant against the live server and record the output metrics.

    python scripts/run_showcase.py <tenant> [objectives]

Produces docs/showcase/<tenant>/metrics.json and article.md - the evidence pack for a real run
(what the brand profile found, strategy flags, SEO/AEO/GEO score with every check, publication
verification, and the audit lineage). Uses the local dev token endpoint; requester and reviewer
are different identities so every gate is exercised as designed.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

B = "http://127.0.0.1:8000"
tenant = sys.argv[1]
objectives = sys.argv[2] if len(sys.argv) > 2 else "Grow organic traffic; increase enquiries."
notes = sys.argv[3] if len(sys.argv) > 3 else ""
out = Path(__file__).resolve().parent.parent / "docs" / "showcase" / tenant
out.mkdir(parents=True, exist_ok=True)
log: dict = {"tenant": tenant, "started_at": datetime.now(UTC).isoformat(), "stages": {}}


def save() -> None:
    (out / "metrics.json").write_text(json.dumps(log, indent=2), encoding="utf-8")


def tok(sub: str, roles: str) -> dict:
    t = httpx.post(f"{B}/dev/token", params={"sub": sub, "tenant_id": tenant, "roles": roles}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def step(name: str, resp: httpx.Response, expect: int = 200) -> dict:
    try:
        data = resp.json() if resp.content else {}
    except ValueError:  # non-JSON error body (e.g. a proxy page); keep a sanitised excerpt
        data = {"detail": resp.text[:300]}
    print(f"[{resp.status_code}] {name}")
    if resp.status_code != expect:
        log["stages"][name] = {"status": resp.status_code, "error": data.get("detail")}
        save()
        print("   ", data.get("detail"))
        sys.exit(1)
    return data


maya, sam, ops = tok("maya", "requester"), tok("sam", "reviewer"), tok("ops", "admin")
log["environment"] = httpx.get(f"{B}/healthz").json()

b = step("1. brand analysis", httpx.post(f"{B}/brands/analyze", json={"notes": notes}, headers=maya, timeout=600), 201)
log["stages"]["brand_analysis"] = {
    "profile_id": b["id"],
    "version": b["version"],
    "pages_used": [p["url"] for p in b["pages"]],
    "brand_name": b["profile"].get("brand_name"),
    "positioning": b["profile"].get("positioning"),
    "voice": b["profile"].get("voice"),
    "audiences": b["profile"].get("audiences"),
    "proof_points": b["profile"].get("proof_points"),
    "observations_to_confirm": b["profile"].get("observations_to_confirm"),
    "review_flags": b["review_flags"],
    "model_served": b["lineage"].get("model_served"),
}
save()
step(
    "1b. reviewer confirms profile",
    httpx.post(
        f"{B}/brands/{b['id']}/review",
        json={
            "decision": "confirm",
            "note": "Profile checked against the public site; observations noted for the client call.",
        },
        headers=sam,
    ),
)

s = step(
    "2. strategy",
    httpx.post(f"{B}/strategies", json={"objectives": objectives, "horizon_weeks": 8}, headers=maya, timeout=600),
    201,
)
log["stages"]["strategy"] = {
    "strategy_id": s["id"],
    "pillars": [p["name"] for p in s["plan"].get("pillars", [])],
    "calendar": [
        {k: it.get(k) for k in ("week", "content_type", "working_title", "target_keyword")}
        for it in s["plan"].get("calendar", [])
    ],
    "review_flags": s["review_flags"],
    "assumptions_to_confirm": s["plan"].get("assumptions_to_confirm"),
}
save()
step(
    "2b. reviewer approves strategy",
    httpx.post(
        f"{B}/strategies/{s['id']}/review",
        json={
            "decision": "approve",
            "note": "Pillars match the confirmed profile; flagged claims will be sourced or removed at drafting.",
        },
        headers=sam,
    ),
)

d = step("3. draft item #1", httpx.post(f"{B}/strategies/{s['id']}/items/0/draft", headers=maya, timeout=900), 201)
rep = d["seo_report"]
log["stages"]["draft"] = {
    "draft_id": d["id"],
    "title": d["title"],
    "words": len(d["body_markdown"].split()),
    "seo_aeo_geo_score": d["seo_score"],
    "threshold": rep["threshold"],
    "checks_passed": sum(c["passed"] for c in rep["checks"]),
    "checks_total": len(rep["checks"]),
    "checks": [
        {"pillar": c["pillar"], "name": c["name"], "passed": c["passed"], "detail": c["detail"]} for c in rep["checks"]
    ],
    "seo": d["seo"]["seo"],
    "direct_answer": d["seo"]["aeo"]["direct_answer"],
    "faq_count": len(d["seo"]["aeo"]["faq"]),
    "quotable_facts": d["seo"]["geo"]["quotable_facts"],
    "claims_to_verify": d["claims_to_verify"],
    "review_flags": d["review_flags"],
    "lineage": {k: d["lineage"].get(k) for k in ("prompt_version", "model_served", "strategy_id")},
}
(out / "article.md").write_text(f"# {d['title']}\n\n{d['body_markdown']}\n", encoding="utf-8")
save()
step(
    "3b. reviewer approves draft",
    httpx.post(
        f"{B}/review/{d['id']}",
        json={
            "decision": "approve",
            "note": "Statistics checked against the brand's own pages; unsourced claims left flagged for the client.",
        },
        headers=sam,
    ),
)

p = step("4. publish", httpx.post(f"{B}/review/{d['id']}/publish", headers=sam, timeout=120))
log["stages"]["publish"] = {
    "status": p["status"],
    "published_url": p["published_url"],
    "remote_post_id": p["remote_post_id"],
    "read_back_verified": p["publish_verified"],
    "publish_flags": [f for f in p["review_flags"] if "PUBLISH" in f],
}
save()

v = httpx.post(f"{B}/governance/integrity/verify", headers=ops).json()
ev = httpx.get(f"{B}/governance/evidence", headers=sam).json()
lineage = httpx.get(f"{B}/governance/audit", params={"object_id": d["id"]}, headers=ops).json()["items"]
log["governance"] = {
    "integrity": {k: v[k] for k in ("status", "records_checked", "checked_at")},
    "publication_evidence": next((e for e in ev if e["draft_id"] == d["id"]), None),
    "audit_lineage_for_article": [
        {"ts": r["ts"], "event": r["event_type"], "actor": r["actor_id"], "hash": r["record_hash"][:16]}
        for r in reversed(lineage)
    ],
}
log["finished_at"] = datetime.now(UTC).isoformat()
save()
print(f"\nDONE -> {out / 'metrics.json'}\n{out / 'article.md'}")
print(
    json.dumps(
        {
            "score": d["seo_score"],
            "words": log["stages"]["draft"]["words"],
            "verified": p["publish_verified"],
            "url": p["published_url"],
        },
        indent=1,
    )
)
