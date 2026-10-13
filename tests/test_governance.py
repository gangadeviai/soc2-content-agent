"""Governance workspace: evidence views must be computed, tenant-scoped, role-gated and free of confidential data."""

import json
import re

from fastapi.testclient import TestClient

from app.brand import FakeFetcher
from app.cms import FakePublisher, PublishResult, render_html
from app.config import REPO_ROOT
from app.demo_data import DEMO_SITE
from app.main import create_app
from tests.conftest import token

BRIEF = "Write a blog post for engineering leaders about faster deployments. Quote Jane at jane@acme.com."


def _full_pipeline(client):
    """brand -> confirm -> strategy -> approve -> draft -> approve -> publish, as u1 (requester) and u2 (reviewer)."""
    req, rev = token(client, "u1", "client-a"), token(client, "u2", "client-a", "reviewer")
    b = client.post("/brands/analyze", json={"notes": "contact jane@acme.com"}, headers=req).json()
    client.post(f"/brands/{b['id']}/review", json={"decision": "confirm", "note": "checked"}, headers=rev)
    s = client.post("/strategies", json={"objectives": "Grow organic traffic"}, headers=req).json()
    client.post(f"/strategies/{s['id']}/review", json={"decision": "approve", "note": "ok"}, headers=rev)
    d = client.post(f"/strategies/{s['id']}/items/0/draft", headers=req).json()
    client.post(f"/review/{d['id']}", json={"decision": "approve", "note": "claims verified"}, headers=rev)
    pub = client.post(f"/review/{d['id']}/publish", headers=rev).json()
    return b, s, d, pub, req, rev


def test_overview_metrics_are_computed_from_data(client):
    b, s, d, pub, req, rev = _full_pipeline(client)
    o = client.get("/governance/overview", headers=rev).json()
    m = o["metrics"]
    assert m["approval_decisions"]["value"] == 3  # brand confirm + strategy approve + draft approve
    assert m["approvals_pending"]["value"] == 0
    assert m["publication_verification_failures"]["value"] == 0
    assert m["audit_records"]["value"] >= 7
    assert all("source" in v for v in m.values())
    assert o["integrity"]["status"] == "not_yet_performed"  # uptime is not evidence
    # reviewer (non-admin) does not see system/config records in the count
    adm = token(client, "ops", "client-a", "admin")
    assert (
        client.get("/governance/overview", headers=adm).json()["metrics"]["audit_records"]["value"]
        > m["audit_records"]["value"]
    )


def test_approval_history_shows_requester_vs_reviewer_and_filters(client):
    b, s, d, pub, req, rev = _full_pipeline(client)
    p = client.get("/governance/approvals", headers=rev).json()
    assert p["total"] == 3 and {r["stage"] for r in p["items"]} == {"brand_profile", "strategy", "draft"}
    for r in p["items"]:
        assert r["requester_id"] == "u1" and r["reviewer_id"] == "u2" and r["separation_of_duties"] is True
        assert r["audit_hash"] and r["note"]
    only_draft = client.get("/governance/approvals?stage=draft", headers=rev).json()
    assert only_draft["total"] == 1 and only_draft["items"][0]["object_id"] == d["id"]
    assert client.get("/governance/approvals?decision=reject", headers=rev).json()["total"] == 0
    # requester role cannot read approval history
    assert client.get("/governance/approvals", headers=req).status_code == 403


def test_audit_trail_pagination_tenant_isolation_and_detail(client):
    b, s, d, pub, req, rev = _full_pipeline(client)
    adm = token(client, "ops", "client-a", "admin")
    page = client.get("/governance/audit?page=1&page_size=3", headers=adm).json()
    assert len(page["items"]) == 3 and page["total"] >= 8
    assert page["items"][0]["ts"] >= page["items"][-1]["ts"]  # newest first
    pubs = client.get("/governance/audit?event_type=PUBLISH", headers=adm).json()
    assert pubs["total"] == 1 and pubs["items"][0]["object_id"] == d["id"]
    h = pubs["items"][0]["record_hash"]
    detail = client.get(f"/governance/audit/{h}", headers=adm).json()
    assert detail["event_type"] == "PUBLISH" and detail["details"]["verified"] is True
    # another tenant's admin sees none of client-a's workflow records and cannot open the detail
    other = token(client, "lee", "client-b", "admin")
    rows = client.get("/governance/audit", headers=other).json()["items"]
    assert all(r["tenant_id"] in {"client-b", "*"} for r in rows)
    assert client.get(f"/governance/audit/{h}", headers=other).status_code == 404
    # reviewers are not admins
    assert client.get("/governance/audit", headers=rev).status_code == 403


def test_integrity_states_not_performed_verified_failed(client, settings):
    _full_pipeline(client)
    adm = token(client, "ops", "client-a", "admin")
    assert client.get("/governance/integrity", headers=adm).json()["status"] == "not_yet_performed"
    v = client.post("/governance/integrity/verify", headers=adm).json()
    assert v["status"] == "verified" and v["records_checked"] > 0 and v["checked_at"]
    assert "does not prove" in v["explanation"]  # honest scope statement
    p = settings.audit_local_dir / "audit.jsonl"
    p.write_text(p.read_text().replace('"GENERATION"', '"GENERATIOM"', 1))
    v2 = client.post("/governance/integrity/verify", headers=adm).json()
    assert v2["status"] == "failed" and v2["first_bad_index"] is not None


def test_security_events_are_derived_and_sanitised(client, settings):
    b, s, d, pub, req, rev = _full_pipeline(client)
    client.get("/governance/audit", headers=req)  # ACCESS_DENIED
    r = client.get("/drafts", headers={"Authorization": "Bearer not.a.jwt"})  # AUTH_FAILED
    assert r.status_code == 401
    adm = token(client, "ops", "client-a", "admin")
    ev = client.get("/governance/security-events", headers=adm).json()["items"]
    cats = {e["category"] for e in ev}
    assert {"authorization", "authentication", "prompt_injection", "redaction"} <= cats
    assert all(e["audit_hash"] and e["severity"] in {"high", "medium", "info"} for e in ev)
    body = json.dumps(ev)
    assert "jane@acme.com" not in body and "Ignore all previous instructions" not in body and "not.a.jwt" not in body
    # audit record count did not grow because of viewing (derived view writes nothing)
    before = sum(1 for _ in open(settings.audit_local_dir / "audit.jsonl"))
    client.get("/governance/security-events", headers=adm)
    assert sum(1 for _ in open(settings.audit_local_dir / "audit.jsonl")) == before


def test_crawl_policy_refusal_is_audited_as_security_event(settings):
    class RefusingFetcher(FakeFetcher):
        def get(self, url):
            from app.brand import CrawlPolicyError

            raise CrawlPolicyError("host resolves to non-public address 10.0.0.5")

    app = create_app(settings, fetcher=RefusingFetcher({}), publisher_factory=lambda t: FakePublisher())
    with TestClient(app) as c:
        req = token(c, "u1", "client-a")
        assert c.post("/brands/analyze", json={}, headers=req).status_code == 502
        adm = token(c, "ops", "client-a", "admin")
        ev = c.get("/governance/security-events", headers=adm).json()["items"]
        assert any(e["category"] == "egress_policy" and e["severity"] == "high" for e in ev)


def test_publication_evidence_states(client, settings):
    b, s, d, pub, req, rev = _full_pipeline(client)
    rows = client.get("/governance/evidence", headers=rev).json()
    assert len(rows) == 1
    r = rows[0]
    assert r["state"] == "published_verified" and r["approved_by"] == "u2" and r["approval_audit_hash"]
    assert r["approved_body_sha256"] and r["published_html_sha256"] and r["publish_attempts"] == 1

    # an unverified publication is reported as such, never as success
    class Unverified(FakePublisher):
        def publish(self, *a, **k):
            res = super().publish(*a, **k)
            return PublishResult(res.url, res.remote_id, res.published_sha256, False, "live content differs")

    app = create_app(settings, fetcher=FakeFetcher(DEMO_SITE), publisher_factory=lambda t: Unverified())
    with TestClient(app) as c:
        req2, rev2 = token(c, "u1", "client-a"), token(c, "u2", "client-a", "reviewer")
        d2 = c.post("/drafts", json={"brief": BRIEF}, headers=req2).json()
        c.post(f"/review/{d2['id']}", json={"decision": "approve", "note": "ok"}, headers=rev2)
        c.post(f"/review/{d2['id']}/publish", headers=rev2)
        ev = c.get("/governance/evidence", headers=rev2).json()
        mine = next(x for x in ev if x["draft_id"] == d2["id"])
        assert mine["state"] == "published_unverified" and mine["verified"] is False
        assert (
            c.get("/governance/overview", headers=rev2).json()["metrics"]["publication_verification_failures"]["value"]
            == 1
        )


def test_control_mapping_matches_matrix_and_is_readable_by_any_role(client):
    rev_only = token(client, "u2", "client-a", "reviewer")
    c = client.get("/governance/controls", headers=rev_only).json()
    ids = [x["id"] for x in c["controls"]]
    assert len(ids) == 37 and len(set(ids)) == 37
    assert set(c["counts"]) == {"implemented", "partial", "planned", "unverified"}
    md = (REPO_ROOT / "compliance" / "SOC2_CONTROL_MATRIX.md").read_text(encoding="utf-8")
    md_ids = set(re.findall(r"^\| (C-\d+) ", md, re.M))
    assert set(ids) == md_ids  # YAML and narrative matrix stay in step
    assert all(x["gap"] for x in c["controls"] if x["status"] in {"partial", "planned", "unverified"})
    assert c["gaps"] and all(g["owner"] and g["target"] for g in c["gaps"])


def test_governance_pages_and_unauthenticated_access(client):
    assert client.get("/governance").status_code == 200 and "Governance Workspace" in client.get("/governance").text
    assert client.get("/governance/overview").status_code in {401, 403}  # no token
    assert client.get("/governance/controls").status_code in {401, 403}


def test_render_html_unchanged_for_evidence_hashes():
    # The evidence view compares the hash of what was sent; rendering must stay deterministic.
    assert render_html("Body", "d") == render_html("Body", "d")
