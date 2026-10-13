"""Stage 1 controls: crawl scope, SSRF guard, robots, injection screening, confirmation gate."""

import pytest

from app.brand import BrandConfig, CrawlPolicyError, FakeFetcher, assert_public_host, crawl, url_allowed
from tests.conftest import DEMO_SITE, token

CFG = BrandConfig("client-a", "client-a.example", "https://client-a.example", ("/",), ("/",), 25)


def test_crawl_stays_in_scope_and_honours_robots():
    fetcher = FakeFetcher(DEMO_SITE)
    pages, notes = crawl(CFG, fetcher)
    urls = {p.url for p in pages}
    assert "https://client-a.example/about" in urls and "https://client-a.example/products" in urls
    assert not any("evil.example" in u or "127.0.0.1" in u or "/admin" in u for u in urls)
    assert not any("evil.example" in r or "127.0.0.1" in r for r in fetcher.requested)  # never even fetched
    assert any("robots.txt disallows https://client-a.example/admin" in n for n in notes)
    assert any("out-of-scope" in n for n in notes)


def test_path_prefix_and_host_rules():
    cfg = BrandConfig("t", "brand.example", "https://brand.example", ("/blog",))
    assert url_allowed("https://www.brand.example/blog/post", cfg)
    assert not url_allowed("https://brand.example/pricing", cfg)  # outside allowed prefix
    assert not url_allowed("https://brand.example.evil.net/blog", cfg)  # suffix trick
    assert not url_allowed("https://10.0.0.5/blog", cfg)
    assert not url_allowed("ftp://brand.example/blog", cfg)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "169.254.169.254", "[::1]", "db.internal", "10.1.2.3"])
def test_ssrf_guard_rejects_private_targets(host):
    with pytest.raises(CrawlPolicyError):
        assert_public_host(host)


def test_brand_analysis_requires_configured_tenant(client):
    r = client.post("/brands/analyze", json={"notes": ""}, headers=token(client, "u1", "unknown-tenant"))
    assert r.status_code == 409


def test_brand_analysis_drops_injected_pages_and_needs_confirmation(client, fake_llm, settings):
    h_req = token(client, "u1", "client-a")
    r = client.post("/brands/analyze", json={"notes": "Focus on the platform; contact jane@acme.com"}, headers=h_req)
    assert r.status_code == 201, r.text
    profile = r.json()
    assert profile["status"] == "pending_confirmation" and profile["version"] == 1
    assert any("PAGES_DROPPED_FOR_INJECTION" in f and "/blog/injected" in f for f in profile["review_flags"])
    assert all("/blog/injected" not in p["url"] for p in profile["pages"])
    sent = fake_llm.calls[0]
    assert sent["prompt"] == "brand_analysis.md"
    assert "Ignore all previous instructions" not in sent["context"]
    assert "jane@acme.com" not in sent["brief"] and "Project Nimbus" not in sent["context"]  # redaction applied
    log = (settings.audit_local_dir / "audit.jsonl").read_text()
    assert '"BRAND_ANALYSIS"' in log and "jane@acme.com" not in log


def test_brand_confirmation_gate_and_supersession(client):
    h_req = token(client, "u1", "client-a")
    h_rev = token(client, "u2", "client-a", "reviewer")
    first = client.post("/brands/analyze", json={}, headers=h_req).json()
    # requester cannot confirm their own analysis; requester role cannot review at all
    assert (
        client.post(
            f"/brands/{first['id']}/review", json={"decision": "confirm", "note": "x"}, headers=h_req
        ).status_code
        == 403
    )
    # confirmation needs a note
    assert client.post(f"/brands/{first['id']}/review", json={"decision": "confirm"}, headers=h_rev).status_code == 422
    ok = client.post(
        f"/brands/{first['id']}/review", json={"decision": "confirm", "note": "checked with client"}, headers=h_rev
    )
    assert ok.json()["status"] == "confirmed"
    # a newer confirmed version supersedes the old one
    second = client.post("/brands/analyze", json={}, headers=h_req).json()
    assert second["version"] == 2
    client.post(f"/brands/{second['id']}/review", json={"decision": "confirm", "note": "re-checked"}, headers=h_rev)
    statuses = {b["version"]: b["status"] for b in client.get("/brands", headers=h_req).json()}
    assert statuses == {1: "superseded", 2: "confirmed"}
    # other tenant cannot see it
    assert client.get(f"/brands/{first['id']}", headers=token(client, "u9", "client-b")).status_code == 404
