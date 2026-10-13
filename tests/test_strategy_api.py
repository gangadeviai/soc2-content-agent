"""Stage 2 controls: stage gate on confirmed brand, claim screening, SoD approval, lineage into drafts."""

import json

from tests.conftest import token


def _confirmed_brand(client):
    h_req = token(client, "u1", "client-a")
    h_rev = token(client, "u2", "client-a", "reviewer")
    b = client.post("/brands/analyze", json={}, headers=h_req).json()
    client.post(f"/brands/{b['id']}/review", json={"decision": "confirm", "note": "checked"}, headers=h_rev)
    return b, h_req, h_rev


def test_strategy_requires_confirmed_brand(client):
    h_req = token(client, "u1", "client-a")
    r = client.post("/strategies", json={"objectives": "Grow organic traffic"}, headers=h_req)
    assert r.status_code == 409
    # pending (unconfirmed) brand is not enough
    client.post("/brands/analyze", json={}, headers=h_req)
    assert client.post("/strategies", json={"objectives": "Grow organic traffic"}, headers=h_req).status_code == 409


def test_strategy_generation_screens_items_and_links_brand(client, fake_llm, settings):
    brand, h_req, _ = _confirmed_brand(client)
    r = client.post(
        "/strategies",
        json={"objectives": "Grow organic traffic; contact bob@client.com", "horizon_weeks": 8},
        headers=h_req,
    )
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["status"] == "draft" and s["brand_profile_id"] == brand["id"]
    assert s["lineage"]["brand_profile_version"] == brand["version"]
    assert any("item 1" in f and "SUPERLATIVE" in f for f in s["review_flags"])  # "#1" in item brief
    call = next(c for c in fake_llm.calls if c["prompt"] == "strategy.md")
    assert "bob@client.com" not in call["brief"] and "<untrusted_brand_profile>" in call["context"]
    assert '"STRATEGY_GENERATED"' in (settings.audit_local_dir / "audit.jsonl").read_text()


def test_strategy_approval_sod_and_draft_from_item(client, publisher, settings):
    _, h_req, h_rev = _confirmed_brand(client)
    s = client.post("/strategies", json={"objectives": "Grow organic traffic"}, headers=h_req).json()
    # cannot draft from an unapproved strategy
    assert client.post(f"/strategies/{s['id']}/items/0/draft", headers=h_req).status_code == 409
    # requester cannot approve own strategy; approval needs a note
    assert (
        client.post(
            f"/strategies/{s['id']}/review", json={"decision": "approve", "note": "x"}, headers=h_req
        ).status_code
        == 403
    )
    assert client.post(f"/strategies/{s['id']}/review", json={"decision": "approve"}, headers=h_rev).status_code == 422
    ok = client.post(
        f"/strategies/{s['id']}/review", json={"decision": "approve", "note": "assumptions confirmed"}, headers=h_rev
    )
    assert ok.json()["status"] == "approved"

    d = client.post(f"/strategies/{s['id']}/items/0/draft", headers=h_req)
    assert d.status_code == 201, d.text
    draft = d.json()
    assert draft["strategy_id"] == s["id"] and draft["strategy_item_index"] == 0
    assert draft["lineage"]["strategy_id"] == s["id"]
    # item is marked drafted and cannot be drafted twice
    s2 = client.get(f"/strategies/{s['id']}", headers=h_req).json()
    assert s2["plan"]["calendar"][0]["draft_id"] == draft["id"]
    assert client.post(f"/strategies/{s['id']}/items/0/draft", headers=h_req).status_code == 409
    assert client.post(f"/strategies/{s['id']}/items/99/draft", headers=h_req).status_code == 404

    # full chain to publish; PUBLISH audit record carries strategy lineage + verification
    client.post(f"/review/{draft['id']}", json={"decision": "approve", "note": "claims verified"}, headers=h_rev)
    pub = client.post(f"/review/{draft['id']}/publish", headers=h_rev).json()
    assert pub["status"] == "published" and pub["publish_verified"] is True
    records = [json.loads(line) for line in (settings.audit_local_dir / "audit.jsonl").read_text().splitlines()]
    publish_rec = next(r for r in records if r["event_type"] == "PUBLISH")
    assert publish_rec["details"]["strategy_id"] == s["id"] and publish_rec["details"]["verified"] is True
    assert publish_rec["details"]["approved_body_sha256"] and publish_rec["details"]["published_html_sha256"]
