"""Publishing to the brand's own WordPress site.

Controls:
  * The agent runtime identity has no publish capability. WordPress application
    passwords are per tenant, read from the secrets manager only inside the
    reviewer-triggered publish path (``load_wordpress_credentials``).  (CC6.1, CC6.3)
  * The target host must sit inside the brand's configured domain, so a
    mis-configured tenant cannot publish to someone else's site.      (C1.1)
  * After publishing, the post is read back and compared byte-for-byte with
    what the reviewer approved; the result is recorded in the audit trail.
                                                                     (PI1.4, PI1.5)
  * SEO/AEO/GEO elements travel with the post: slug and excerpt natively, FAQ
    rendered into the body, JSON-LD embedded (re-serialised, never raw model
    output), title tag / meta description via the site's SEO plugin keys.
"""

from __future__ import annotations

import base64
import html
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx
import markdown

from .audit import sha256_text
from .seo import safe_jsonld


class PublishCredentialsError(RuntimeError):
    pass


@dataclass
class PublishResult:
    url: str
    remote_id: str
    published_sha256: str  # hash of the exact HTML sent
    verified: bool
    verification_note: str


class Publisher(Protocol):
    def publish(
        self,
        tenant_id: str,
        title: str,
        body_markdown: str,
        disclosure: str,
        seo: dict | None = None,
        remote_id: str | None = None,
    ) -> PublishResult: ...


def load_wordpress_credentials(tenant_id: str) -> tuple[str, str]:
    """Per-tenant WordPress application password, e.g. for tenant ``client-a``:
    AGENT_WP_USER__CLIENT_A / AGENT_WP_APP_PASSWORD__CLIENT_A.
    The WordPress user should hold only the Author/Editor role on that site."""
    key = tenant_id.upper().replace("-", "_")
    user = os.environ.get(f"AGENT_WP_USER__{key}", "")
    password = os.environ.get(f"AGENT_WP_APP_PASSWORD__{key}")
    if not password:  # WordPress.com uses a bearer token only; self-hosted also needs the user
        raise PublishCredentialsError(f"WordPress publish credentials not configured for tenant {tenant_id!r}")
    return user, password


def render_html(body_markdown: str, disclosure: str, seo: dict | None = None, embed_jsonld: bool = True) -> str:
    """Approved markdown -> HTML, plus FAQ block, disclosure and (optionally) JSON-LD. Deterministic.

    embed_jsonld=False for platforms that strip <script> (WordPress.com without a plan that
    allows custom code): embedding there leaves raw JSON visible to readers and makes the
    read-back check fail. The JSON-LD stays on the draft record for a plugin or theme to use."""
    parts = [markdown.markdown(body_markdown, extensions=["extra"])]
    faq = ((seo or {}).get("aeo") or {}).get("faq") or []
    if faq:
        items = "".join(
            f"<h3>{html.escape(str(q.get('question', '')))}</h3>\n<p>{html.escape(str(q.get('answer', '')))}</p>\n"
            for q in faq
        )
        parts.append(f'<section class="faq">\n<h2>Frequently asked questions</h2>\n{items}</section>')
    parts.append(f"<hr />\n<p><em>{html.escape(disclosure)}</em></p>")
    jsonld = safe_jsonld(((seo or {}).get("geo") or {}).get("schema_org_jsonld", "")) if embed_jsonld else None
    if jsonld:
        parts.append(f'<script type="application/ld+json">{jsonld}</script>')
    return "\n".join(parts)


_SEO_META_KEYS = {
    "yoast": ("_yoast_wpseo_title", "_yoast_wpseo_metadesc"),
    "rankmath": ("rank_math_title", "rank_math_description"),
}


def wordpress_payload(title: str, html_content: str, seo: dict | None, seo_plugin: str | None) -> dict:
    payload: dict = {"title": title, "content": html_content, "status": "publish"}
    s = (seo or {}).get("seo") or {}
    if s.get("slug"):
        payload["slug"] = s["slug"]
    if s.get("meta_description"):
        payload["excerpt"] = s["meta_description"]
    if seo_plugin in _SEO_META_KEYS and (s.get("title_tag") or s.get("meta_description")):
        t_key, d_key = _SEO_META_KEYS[seo_plugin]
        payload["meta"] = {t_key: s.get("title_tag", ""), d_key: s.get("meta_description", "")}
    return payload


class WordPressPublisher:
    def __init__(
        self,
        site_url: str,
        username: str,
        app_password: str,
        allowed_host: str,
        seo_plugin: str | None = None,
        transport: httpx.BaseTransport | None = None,
        allow_local: bool = False,
        platform: str = "self_hosted",
    ):
        """platform: "self_hosted" (Basic auth with an application password, /wp-json/wp/v2) or
        "wordpress_com" (free WordPress.com site; OAuth2 bearer token via public-api.wordpress.com)."""
        parsed = urlparse(site_url)
        host = (parsed.hostname or "").lower()
        is_local = host in {"localhost", "127.0.0.1"}
        if is_local and not allow_local:
            raise ValueError("publishing to a local WordPress is only allowed outside prod (allow_local)")
        if parsed.scheme != "https" and not is_local:
            raise ValueError("WordPress site_url must use https")
        if not is_local and not (host == allowed_host or host.endswith("." + allowed_host)):
            raise ValueError(f"publish target {host!r} is outside the configured publish domain {allowed_host!r}")
        if platform not in {"self_hosted", "wordpress_com"}:
            raise ValueError(f"unknown WordPress platform {platform!r}")
        self.platform = platform
        self.seo_plugin = seo_plugin
        if platform == "wordpress_com":
            self.api = f"https://public-api.wordpress.com/wp/v2/sites/{host}"
            auth = f"Bearer {app_password}"
        else:
            self.api = f"{site_url.rstrip('/')}/wp-json/wp/v2"
            auth = "Basic " + base64.b64encode(f"{username}:{app_password}".encode()).decode()
        self._client = httpx.Client(
            timeout=30,
            headers={"Authorization": auth, "User-Agent": "SOC2ContentAgent/1.0"},
            transport=transport,
        )

    @property
    def supports_inline_jsonld(self) -> bool:
        return self.platform != "wordpress_com"  # WP.com strips <script> unless the plan allows custom code

    def publish(
        self,
        tenant_id: str,
        title: str,
        body_markdown: str,
        disclosure: str,
        seo: dict | None = None,
        remote_id: str | None = None,
    ) -> PublishResult:
        """Create the post, or update the existing one when ``remote_id`` is given (re-publish
        after a failed verification must not create duplicates)."""
        content = render_html(body_markdown, disclosure, seo, embed_jsonld=self.supports_inline_jsonld)
        target = f"{self.api}/posts/{remote_id}" if remote_id else f"{self.api}/posts"
        resp = self._client.post(target, json=wordpress_payload(title, content, seo, self.seo_plugin))
        resp.raise_for_status()
        post = resp.json()
        post_id = str(post["id"])

        # Read-back verification: what is live must equal what was approved.
        # (If WordPress strips the JSON-LD <script> because the publishing user lacks
        # `unfiltered_html`, this check fails and the reviewer is told - by design.)
        check = self._client.get(f"{self.api}/posts/{post_id}", params={"context": "edit"})
        if check.status_code != 200:
            verified, note = False, f"read-back failed with HTTP {check.status_code}"
        else:
            live = check.json()
            raw = (live.get("content") or {}).get("raw", "")
            if raw != content:
                verified, note = False, "live content differs from approved content"
            elif live.get("status") != "publish":
                verified, note = False, f"post status is {live.get('status')!r}, expected 'publish'"
            else:
                verified, note = True, "live content matches approved content"
        return PublishResult(
            url=post.get("link", ""),
            remote_id=post_id,
            published_sha256=sha256_text(content),
            verified=verified,
            verification_note=note,
        )


class FakePublisher:
    def __init__(self):
        self.published: list[dict] = []

    def publish(
        self,
        tenant_id: str,
        title: str,
        body_markdown: str,
        disclosure: str,
        seo: dict | None = None,
        remote_id: str | None = None,
    ) -> PublishResult:
        content = render_html(body_markdown, disclosure, seo)
        self.published.append(
            {"tenant_id": tenant_id, "title": title, "content": content, "seo": seo, "remote_id": remote_id}
        )
        return PublishResult(
            url=f"https://{tenant_id}.example/blog/{len(self.published)}",
            remote_id=str(len(self.published)),
            published_sha256=sha256_text(content),
            verified=True,
            verification_note="fake publisher",
        )
