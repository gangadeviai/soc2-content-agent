"""SOC 2 control walkthrough - runs the whole pipeline offline and prints each control as it fires.

    python scripts/demo_walkthrough.py

Use it in an audit meeting: it exercises the positive path (brand → strategy → draft →
review → publish) and the negative path (every refusal), then verifies the audit chain and
prints the lineage pack for the published article.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["AGENT_ENV"] = "demo"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="content-agent-demo-"))
settings = Settings(
    env="demo", database_url=f"sqlite:///{tmp / 'demo.db'}", audit_local_dir=tmp / "audit", _env_file=None
)
client = TestClient(create_app(settings))
client.__enter__()

PASS, FAIL = "✔", "✖"


def tok(sub, tenant, roles):
    t = client.post("/dev/token", params={"sub": sub, "tenant_id": tenant, "roles": roles}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


maya, sam, ops, lee = (
    tok("maya", "client-a", "requester"),
    tok("sam", "client-a", "reviewer"),
    tok("ops", "client-a", "admin"),
    tok("lee", "client-b", "requester,reviewer"),
)


def step(title, resp, expect, control):
    ok = resp.status_code == expect
    print(f"  {PASS if ok else FAIL} {resp.status_code:<4} {title:<58} {control}")
    if not ok:
        print("       ", resp.text[:200])
    return resp.json() if resp.content else None


print("\n== Stage 1: brand analysis (CC6.6/CC6.7 scope, C1.1 redaction, PI1.2 confirmation) ==")
brand = step(
    "Maya analyzes client-a.example",
    client.post("/brands/analyze", json={"notes": "contact jane@acme.com"}, headers=maya),
    201,
    "C-22..C-25",
)
print(f"       pages used: {[p['url'].split('.example')[1] for p in brand['pages']]}")
print(f"       flags: {brand['review_flags']}")
step(
    "Maya tries to confirm her own analysis",
    client.post(f"/brands/{brand['id']}/review", json={"decision": "confirm", "note": "x"}, headers=maya),
    403,
    "C-26 segregation of duties",
)
step(
    "Sam confirms without a note",
    client.post(f"/brands/{brand['id']}/review", json={"decision": "confirm"}, headers=sam),
    422,
    "C-26 note required",
)
step(
    "Sam confirms with a note",
    client.post(
        f"/brands/{brand['id']}/review", json={"decision": "confirm", "note": "checked with client"}, headers=sam
    ),
    200,
    "C-26",
)

print("\n== Stage 2: strategy (PI1.2 stage gate, PI1.3 claim screening, PI1.4 approval) ==")
strat = step(
    "Maya plans an 8-week strategy",
    client.post("/strategies", json={"objectives": "Grow organic traffic", "horizon_weeks": 8}, headers=maya),
    201,
    "C-28, C-29",
)
print(f"       flags: {strat['review_flags']}")
step(
    "Maya drafts from an unapproved strategy",
    client.post(f"/strategies/{strat['id']}/items/0/draft", headers=maya),
    409,
    "C-30 stage gate",
)
step(
    "Sam approves the strategy",
    client.post(
        f"/strategies/{strat['id']}/review", json={"decision": "approve", "note": "assumptions confirmed"}, headers=sam
    ),
    200,
    "C-30",
)

print("\n== Stage 3: writing (C1.1 redaction, CC6.1 tenant RAG, CC8.1 prompt hash, PI1.3 SEO/AEO/GEO) ==")
draft = step(
    "Maya drafts calendar item #1",
    client.post(f"/strategies/{strat['id']}/items/0/draft", headers=maya),
    201,
    "C-02, C-05, C-06, C-10, C-35",
)
lin = draft["lineage"]
print(f"       prompt {lin['prompt_version']} system sha {lin['system_sha256'][:12]}…  model {lin['model_served']}")
failed = [c["name"] for c in draft["seo_report"]["checks"] if not c["passed"]]
print(f"       SEO/AEO/GEO score {draft['seo_score']}  failed: {failed}")
leaky_brief = (
    "Blog about rollback. Quote jane@acme.com, mention Project Nimbus, "
    "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
)
raw = step(
    "Maya pastes PII + an API key in a free brief",
    client.post("/drafts", json={"brief": leaky_brief}, headers=maya),
    201,
    "C-02 redaction",
)
gen = [json.loads(line) for line in (settings.audit_local_dir / "audit.jsonl").read_text().splitlines()][-1]
print(f"       audit redaction counts: {gen['details']['redaction']['counts']}")
print(f"       raw PII in audit log: {'jane@acme.com' in json.dumps(gen)}")

print("\n== Stage 4: review & publish (PI1.4 SoD, CC6.3 publish scope, PI1.5 verification) ==")
step("Sam publishes before approval", client.post(f"/review/{draft['id']}/publish", headers=sam), 409, "C-09")
step(
    "Maya approves her own draft",
    client.post(f"/review/{draft['id']}", json={"decision": "approve", "note": "ok"}, headers=maya),
    403,
    "C-09 segregation of duties",
)
step(
    "Lee (client-b) reads client-a's draft",
    client.get(f"/drafts/{draft['id']}", headers=lee),
    404,
    "C-05 tenant isolation",
)
step(
    "Sam approves with verification note",
    client.post(
        f"/review/{draft['id']}", json={"decision": "approve", "note": "claims verified vs product page"}, headers=sam
    ),
    200,
    "C-09",
)
step("Maya tries to publish", client.post(f"/review/{draft['id']}/publish", headers=maya), 403, "C-13 role")
pub = step("Sam publishes to WordPress", client.post(f"/review/{draft['id']}/publish", headers=sam), 200, "C-31..C-34")
print(f"       url {pub['published_url']}  verified={pub['publish_verified']}")

print("\n== Monitoring & audit (CC4.1, CC7.1, CC7.2) ==")
step("Maya reads admin metrics", client.get("/admin/metrics", headers=maya), 403, "C-13 (denial audited)")
m = step("Ops reads metrics", client.get("/admin/metrics", headers=ops), 200, "C-16, C-35")
print(f"       {json.dumps(m)}")
v = step("Ops verifies the audit chain", client.get("/admin/audit/verify", headers=ops), 200, "C-11")
print(f"       chain_intact={v['chain_intact']} records={v['records']}")
path = settings.audit_local_dir / "audit.jsonl"
path.write_text(path.read_text().replace('"GENERATION"', '"GENERATIOM"', 1))
v2 = client.get("/admin/audit/verify", headers=ops).json()
mark = PASS if not v2["chain_intact"] else FAIL
print(f"  {mark} tamper with one byte of the local mirror → chain_intact={v2['chain_intact']}")
print("       (the S3 Object Lock copy is authoritative)")

print("\n== Lineage pack for the published article ==")
recs = client.get(
    "/admin/audit/records", params={"object_ids": ",".join([brand["id"], strat["id"], draft["id"]])}, headers=ops
).json()
for r in recs:
    chain = f"hash {r['record_hash'][:12]}… prev {r['prev_hash'][:12]}…"
    print(f"  {r['ts'][:19]}  {r['event_type']:<20} by {r['actor_id']:<5} {chain}")
print(f"\nArtefacts in {tmp}\n")
