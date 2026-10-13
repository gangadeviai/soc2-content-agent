"""End-to-end control tests: these are the tests an auditor can watch run."""

import json

from tests.conftest import token

BRIEF = (
    "Write a blog post for engineering leaders about faster deployments. "
    "Quote our customer Jane at jane@acme.com and mention Project Nimbus."
)


def test_pii_never_reaches_the_model(client, fake_llm):
    r = client.post("/drafts", json={"brief": BRIEF}, headers=token(client, "u1", "client-a"))
    assert r.status_code == 201, r.text
    sent = json.dumps(fake_llm.calls[0])
    assert "jane@acme.com" not in sent
    assert "Project Nimbus" not in sent  # tenant deny-list term
    assert "[REDACTED_EMAIL_1]" in sent


def test_tenant_isolation_in_rag_and_reads(client, fake_llm):
    h_a = token(client, "u1", "client-a")
    draft = client.post("/drafts", json={"brief": BRIEF}, headers=h_a).json()
    ctx = fake_llm.calls[0]["context"]
    assert "chunk_id=a-" in ctx and "chunk_id=b-" not in ctx
    assert all(c["chunk_id"].startswith("a-") for c in draft["lineage"]["chunks"])
    # Client B cannot see client A's draft; response is indistinguishable from not-found.
    assert client.get(f"/drafts/{draft['id']}", headers=token(client, "u9", "client-b")).status_code == 404
    assert client.get("/drafts", headers=token(client, "u9", "client-b")).json() == []


def test_agent_cannot_publish_without_human_approval(client, publisher):
    h_req = token(client, "u1", "client-a")
    h_rev = token(client, "u2", "client-a", "reviewer")
    draft = client.post("/drafts", json={"brief": BRIEF}, headers=h_req).json()
    assert draft["status"] == "draft"
    assert client.post(f"/review/{draft['id']}/publish", headers=h_rev).status_code == 409
    assert publisher.published == []
    # requester may not review their own draft
    assert (
        client.post(f"/review/{draft['id']}", json={"decision": "approve", "note": "ok"}, headers=h_req).status_code
        == 403
    )
    # requester role has no publish permission at all
    assert client.post(f"/review/{draft['id']}/publish", headers=h_req).status_code == 403
    # approval requires a note
    assert client.post(f"/review/{draft['id']}", json={"decision": "approve"}, headers=h_rev).status_code == 422
    ok = client.post(
        f"/review/{draft['id']}",
        json={"decision": "approve", "note": "claims verified vs. benchmark doc"},
        headers=h_rev,
    )
    assert ok.json()["status"] == "approved"
    pub = client.post(f"/review/{draft['id']}/publish", headers=h_rev).json()
    assert pub["status"] == "published" and publisher.published[0]["tenant_id"] == "client-a"


def test_audit_chain_records_full_lineage_and_is_tamper_evident(client, settings):
    h = token(client, "u1", "client-a")
    draft = client.post("/drafts", json={"brief": BRIEF}, headers=h).json()
    lines = [json.loads(line) for line in (settings.audit_local_dir / "audit.jsonl").read_text().splitlines()]
    gen = next(r for r in lines if r["event_type"] == "GENERATION")
    assert gen["record_hash"] == draft["generation_audit_hash"]
    for key in [
        "prompt_version",
        "system_sha256",
        "model_served",
        "redacted_input_sha256",
        "output_sha256",
        "usage",
        "chunks",
    ]:
        assert key in gen["details"], key
    assert "jane@acme.com" not in json.dumps(gen)  # no raw PII in audit
    admin = token(client, "adm", "client-a", "admin")
    assert client.get("/admin/audit/verify", headers=admin).json()["chain_intact"] is True
    # tamper
    p = settings.audit_local_dir / "audit.jsonl"
    p.write_text(p.read_text().replace("GENERATION", "GENERATIOM", 1))
    assert client.get("/admin/audit/verify", headers=admin).json()["chain_intact"] is False


def test_access_denied_is_audited(client, settings):
    client.get("/admin/metrics", headers=token(client, "u1", "client-a"))
    log = (settings.audit_local_dir / "audit.jsonl").read_text()
    assert '"ACCESS_DENIED"' in log
