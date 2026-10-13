"""Stage 4 controls: per-tenant credentials, host pinning, read-back verification."""

import json

import httpx
import pytest

from app.cms import PublishCredentialsError, WordPressPublisher, load_wordpress_credentials, render_html
from app.llm import FakeLLMClient


def _wp_transport(store: dict, tamper: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Basic ")
        if request.method == "POST" and request.url.path == "/wp-json/wp/v2/posts":
            body = json.loads(request.content)
            store["payload"] = body
            store["content"] = body["content"]
            store["title"] = body["title"]
            return httpx.Response(
                201, json={"id": 77, "link": "https://client-a.example/blog/hello", "status": "publish"}
            )
        if request.method == "GET" and request.url.path == "/wp-json/wp/v2/posts/77":
            assert request.url.params["context"] == "edit"
            raw = store["content"] + ("<p>injected</p>" if tamper else "")
            return httpx.Response(200, json={"id": 77, "status": "publish", "content": {"raw": raw}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_publish_and_verify_roundtrip():
    store: dict = {}
    pub = WordPressPublisher(
        "https://client-a.example", "agent", "xxxx", allowed_host="client-a.example", transport=_wp_transport(store)
    )
    res = pub.publish("client-a", "Hello", "## Hi\n\nBody with **bold**.", "AI-assisted, human reviewed.")
    assert res.remote_id == "77" and res.url.endswith("/blog/hello")
    assert res.verified is True
    assert store["content"] == render_html("## Hi\n\nBody with **bold**.", "AI-assisted, human reviewed.")
    assert "<em>AI-assisted, human reviewed.</em>" in store["content"]


def test_seo_elements_travel_with_the_post():
    store: dict = {}
    seo = FakeLLMClient().complete(None, "t", "system.md", {}, "").data
    seo = {k: seo[k] for k in ("seo", "aeo", "geo")}
    pub = WordPressPublisher(
        "https://client-a.example",
        "agent",
        "xxxx",
        allowed_host="client-a.example",
        seo_plugin="yoast",
        transport=_wp_transport(store),
    )
    res = pub.publish("client-a", "Hello", "Body", "disclosure", seo=seo)
    assert res.verified is True
    payload = store["payload"]
    assert payload["slug"] == "automated-rollback-vs-hotfixes"
    assert payload["excerpt"] == seo["seo"]["meta_description"]
    assert payload["meta"]["_yoast_wpseo_title"] == seo["seo"]["title_tag"]
    assert "<h2>Frequently asked questions</h2>" in payload["content"]
    assert "<h3>What is automated rollback?</h3>" in payload["content"]
    assert '<script type="application/ld+json">' in payload["content"] and '"FAQPage"' in payload["content"]
    # without a plugin configured, no plugin meta keys are sent
    pub2 = WordPressPublisher(
        "https://client-a.example", "a", "p", allowed_host="client-a.example", transport=_wp_transport(store)
    )
    pub2.publish("client-a", "Hello", "Body", "disclosure", seo=seo)
    assert "meta" not in store["payload"] and store["payload"]["slug"]


def test_verification_detects_tampered_live_content():
    pub = WordPressPublisher(
        "https://client-a.example",
        "agent",
        "xxxx",
        allowed_host="client-a.example",
        transport=_wp_transport({}, tamper=True),
    )
    res = pub.publish("client-a", "Hello", "Body", "disclosure")
    assert res.verified is False and "differs" in res.verification_note


def test_publish_target_must_be_inside_brand_domain():
    with pytest.raises(ValueError, match="outside the configured publish domain"):
        WordPressPublisher("https://other-brand.example", "u", "p", allowed_host="client-a.example")
    with pytest.raises(ValueError, match="https"):
        WordPressPublisher("http://client-a.example", "u", "p", allowed_host="client-a.example")
    WordPressPublisher("https://www.client-a.example", "u", "p", allowed_host="client-a.example")  # subdomain ok


def test_wordpress_com_uses_bearer_token_and_public_api():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["Authorization"]
        if req.method == "POST":
            return httpx.Response(
                201, json={"id": 5, "link": "https://mylearn.wordpress.com/2026/10/x", "status": "publish"}
            )
        return httpx.Response(200, json={"id": 5, "status": "publish", "content": {"raw": render_html("Body", "d")}})

    pub = WordPressPublisher(
        "https://mylearn.wordpress.com",
        "",
        "oauth-token",
        allowed_host="mylearn.wordpress.com",
        platform="wordpress_com",
        transport=httpx.MockTransport(handler),
    )
    res = pub.publish("t", "Hello", "Body", "d")
    assert res.verified and seen["auth"] == "Bearer oauth-token"
    assert seen["url"].startswith("https://public-api.wordpress.com/wp/v2/sites/mylearn.wordpress.com/posts/5")


def test_wordpress_com_omits_inline_jsonld_and_republish_updates_in_place():
    seen = {"posts": []}
    seo = FakeLLMClient().complete(None, "t", "system.md", {}, "").data
    seo = {k: seo[k] for k in ("seo", "aeo", "geo")}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            seen["posts"].append(req.url.path)
            seen["content"] = json.loads(req.content)["content"]
            return httpx.Response(200, json={"id": 9, "link": "https://mylearn.wordpress.com/p", "status": "publish"})
        return httpx.Response(200, json={"id": 9, "status": "publish", "content": {"raw": seen["content"]}})

    pub = WordPressPublisher(
        "https://mylearn.wordpress.com",
        "",
        "tok",
        allowed_host="mylearn.wordpress.com",
        platform="wordpress_com",
        transport=httpx.MockTransport(handler),
    )
    res = pub.publish("t", "Hello", "Body", "d", seo=seo)
    assert res.verified and "application/ld+json" not in seen["content"]  # WP.com strips <script>
    assert "<h2>Frequently asked questions</h2>" in seen["content"]  # FAQ still carried
    res2 = pub.publish("t", "Hello", "Body", "d", seo=seo, remote_id="9")
    assert res2.remote_id == "9" and seen["posts"][-1].endswith("/posts/9")  # update, not create


def test_publish_domain_is_git_pinned_not_request_controlled():
    from app.brand import BrandConfig

    cfg = BrandConfig.from_yaml(
        "learn",
        {
            "domain": "python.org",
            "wordpress_url": "https://mylearn.wordpress.com",
            "publish_domain": "mylearn.wordpress.com",
            "wordpress_platform": "wordpress_com",
        },
    )
    assert cfg.effective_publish_domain == "mylearn.wordpress.com"
    WordPressPublisher(
        cfg.wordpress_url, "", "t", allowed_host=cfg.effective_publish_domain, platform=cfg.wordpress_platform
    )
    with pytest.raises(ValueError, match="outside the configured publish domain"):
        WordPressPublisher("https://someone-else.wordpress.com", "", "t", allowed_host=cfg.effective_publish_domain)
    with pytest.raises(ValueError, match="wordpress_platform"):
        BrandConfig.from_yaml("x", {"domain": "a.com", "wordpress_url": "https://a.com", "wordpress_platform": "wix"})


def test_local_wordpress_only_with_explicit_flag():
    with pytest.raises(ValueError, match="local WordPress"):
        WordPressPublisher("http://localhost:8080", "u", "p", allowed_host="brand.example")
    WordPressPublisher("http://localhost:8080", "u", "p", allowed_host="brand.example", allow_local=True)


def test_credentials_are_per_tenant_and_fail_closed(monkeypatch):
    monkeypatch.delenv("AGENT_WP_USER__CLIENT_A", raising=False)
    with pytest.raises(PublishCredentialsError):
        load_wordpress_credentials("client-a")
    monkeypatch.setenv("AGENT_WP_USER__CLIENT_A", "agent")
    monkeypatch.setenv("AGENT_WP_APP_PASSWORD__CLIENT_A", "abcd efgh")
    assert load_wordpress_credentials("client-a") == ("agent", "abcd efgh")
    with pytest.raises(PublishCredentialsError):
        load_wordpress_credentials("client-b")  # no cross-tenant reuse
