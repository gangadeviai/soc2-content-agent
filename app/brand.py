"""Stage 1 - brand analysis from the brand's own website.

Controls:
  * Crawl scope is fixed per tenant in ``tenants/brands.yaml`` (change-managed);
    the request body cannot name a URL. Only the configured domain and its
    subdomains, only allowed path prefixes, robots.txt honoured, page and byte
    caps, no IP literals / private hosts (SSRF guard).                (CC6.6, CC6.7)
  * Page text is treated as untrusted data: redacted, screened for injected
    instructions, and delimited before it reaches the model.           (C1.1, CC7.1)
  * The resulting profile is ``pending_confirmation`` until a human who is not
    the requester confirms it; only a confirmed profile can drive strategy. (PI1.2, PI1.4)
  * Every crawl and every confirmation is an immutable audit record.   (CC7.2)
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .audit import AuditRecord, sha256_text
from .auth import Principal
from .injection import screen_input_chunk, wrap_untrusted
from .llm import BRAND_SCHEMA
from .models import BrandProfile, BrandStatus
from .redaction import Redactor

USER_AGENT = "SOC2ContentAgent/1.0 (brand analysis for a contracted marketing agency)"


class CrawlPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrandConfig:
    tenant_id: str
    domain: str  # only this host and its subdomains may be crawled
    wordpress_url: str  # publish target; host must be within ``publish_domain``
    allowed_path_prefixes: tuple[str, ...] = ("/",)
    seed_paths: tuple[str, ...] = ("/",)
    max_pages: int = 25
    # SEO plugin on the WordPress site: "yoast" | "rankmath" | None. Decides which REST
    # meta keys receive the title tag / meta description (plugin must expose them via REST).
    seo_plugin: str | None = None
    # "self_hosted" (application password) or "wordpress_com" (free WordPress.com site, OAuth2 token).
    wordpress_platform: str = "self_hosted"
    # Host the publisher may write to. Defaults to ``domain``; set explicitly (still Git-pinned,
    # still CODEOWNER-reviewed) when the brand's blog lives on another host, e.g. a WordPress.com site.
    publish_domain: str | None = None
    # Host the crawl starts from (e.g. "www.example.com" when the apex does not serve the site).
    # Must be inside ``domain``; the scope check still applies to every link.
    crawl_host: str | None = None

    @property
    def effective_publish_domain(self) -> str:
        return (self.publish_domain or self.domain).lower()

    @property
    def effective_crawl_host(self) -> str:
        return (self.crawl_host or self.domain).lower()

    @classmethod
    def from_yaml(cls, tenant_id: str, raw: dict) -> BrandConfig:
        plugin = raw.get("seo_plugin")
        if plugin not in {None, "yoast", "rankmath"}:
            raise ValueError(f"{tenant_id}: seo_plugin must be yoast, rankmath or omitted")
        platform = raw.get("wordpress_platform", "self_hosted")
        if platform not in {"self_hosted", "wordpress_com"}:
            raise ValueError(f"{tenant_id}: wordpress_platform must be self_hosted or wordpress_com")
        return cls(
            tenant_id=tenant_id,
            domain=str(raw["domain"]).lower(),
            wordpress_url=str(raw["wordpress_url"]),
            allowed_path_prefixes=tuple(raw.get("allowed_path_prefixes", ["/"])),
            seed_paths=tuple(raw.get("seed_paths", ["/"])),
            max_pages=int(raw.get("max_pages", 25)),
            seo_plugin=plugin,
            wordpress_platform=platform,
            publish_domain=(str(raw["publish_domain"]).lower() if raw.get("publish_domain") else None),
            crawl_host=(str(raw["crawl_host"]).lower() if raw.get("crawl_host") else None),
        )


@dataclass
class FetchResponse:
    status: int
    content_type: str
    text: str
    final_url: str


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResponse: ...


@dataclass
class Page:
    url: str
    text: str

    @property
    def sha256(self) -> str:
        return sha256_text(self.text)


# ---- SSRF / scope guards -----------------------------------------------------
def _host_in_domain(host: str | None, domain: str) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def _looks_like_ip_or_local(host: str) -> bool:
    if host in {"localhost"} or host.endswith((".localhost", ".internal", ".local")):
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")  # RFC 6052: IPv6 carrying an embedded IPv4


def _effective_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Unwrap addresses that merely carry an IPv4 inside (NAT64 / IPv4-mapped) so the embedded
    IPv4 is what gets judged - otherwise clients on DNS64 networks see every host as 'reserved'."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64_WELL_KNOWN:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def is_public_ip(raw: str) -> bool:
    ip = _effective_ip(ipaddress.ip_address(raw))
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)


def assert_public_host(host: str | None) -> None:
    """Resolve and refuse private / loopback / link-local targets (SSRF guard).
    Every resolved address must be public: a single private answer is a DNS-rebinding risk."""
    if not host or _looks_like_ip_or_local(host):
        raise CrawlPolicyError(f"host {host!r} is not an allowed public hostname")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise CrawlPolicyError(f"cannot resolve {host!r}: {exc}") from exc
    for info in infos:
        if not is_public_ip(info[4][0]):
            raise CrawlPolicyError(f"host {host!r} resolves to non-public address {info[4][0]}")


def url_allowed(url: str, cfg: BrandConfig) -> bool:
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        return False
    if _looks_like_ip_or_local(p.hostname) or not _host_in_domain(p.hostname, cfg.domain):
        return False
    path = p.path or "/"
    return any(path.startswith(prefix) for prefix in cfg.allowed_path_prefixes)


# ---- fetchers ---------------------------------------------------------------
class HttpFetcher:
    def __init__(self, max_bytes: int = 2_000_000, timeout: float = 15.0):
        self.max_bytes = max_bytes
        self._client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})

    def get(self, url: str) -> FetchResponse:
        assert_public_host(urlparse(url).hostname)
        resp = self._client.get(url)
        text = resp.content[: self.max_bytes].decode(resp.encoding or "utf-8", errors="replace")
        return FetchResponse(resp.status_code, resp.headers.get("content-type", ""), text, str(resp.url))


class FakeFetcher:
    """Offline fetcher for tests/demos: url -> html."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str) -> FetchResponse:
        self.requested.append(url)
        if url in self.pages:
            return FetchResponse(200, "text/html; charset=utf-8", self.pages[url], url)
        return FetchResponse(404, "text/html", "", url)


# ---- crawl ------------------------------------------------------------------
_TAG_BLOCKS = re.compile(r"<(script|style|noscript|svg|template)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)""", re.IGNORECASE)


def html_to_text(markup: str) -> str:
    markup = _COMMENTS.sub(" ", _TAG_BLOCKS.sub(" ", markup))
    return _WS.sub(" ", html.unescape(_TAGS.sub(" ", markup))).strip()


def _links(markup: str, base: str) -> list[str]:
    out = []
    for href in _HREF.findall(markup):
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        out.append(urljoin(base, href).split("#")[0])
    return out


def crawl(cfg: BrandConfig, fetcher: Fetcher) -> tuple[list[Page], list[str]]:
    """Breadth-first crawl of the configured brand site. Returns (pages, policy_notes)."""
    if not _host_in_domain(cfg.effective_crawl_host, cfg.domain):
        raise CrawlPolicyError(f"crawl_host {cfg.effective_crawl_host!r} is outside domain {cfg.domain!r}")
    base = f"https://{cfg.effective_crawl_host}"
    robots = RobotFileParser()
    notes: list[str] = []
    try:
        robots_resp = fetcher.get(f"{base}/robots.txt")
        robots.parse(robots_resp.text.splitlines() if robots_resp.status == 200 else [])
    except httpx.HTTPError as exc:  # unreachable robots.txt: proceed with no extra restrictions, but say so
        robots.parse([])
        notes.append(f"robots.txt unreachable ({type(exc).__name__}); crawled without site-specific rules")

    queue = [urljoin(base, p) for p in cfg.seed_paths]
    seen: set[str] = set()
    pages: list[Page] = []
    while queue and len(pages) < cfg.max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not url_allowed(url, cfg):
            notes.append(f"skipped out-of-scope url {url}")
            continue
        if not robots.can_fetch(USER_AGENT, url):
            notes.append(f"robots.txt disallows {url}")
            continue
        try:
            resp = fetcher.get(url)
        except CrawlPolicyError as exc:  # a linked host that fails the SSRF/scope guard
            notes.append(f"policy refused {url}: {exc}")
            continue
        except httpx.HTTPError as exc:  # one slow or broken page must not abort the crawl
            notes.append(f"fetch failed {url}: {type(exc).__name__}")
            continue
        if resp.status != 200 or "html" not in resp.content_type.lower():
            continue
        if not url_allowed(resp.final_url, cfg):
            notes.append(f"{url} redirected out of scope to {resp.final_url}; discarded")
            continue
        text = html_to_text(resp.text)
        if text:
            pages.append(Page(url, text))
        for link in _links(resp.text, url):
            if link not in seen:
                queue.append(link)
    return pages, notes


# ---- stage logic -------------------------------------------------------------
def analyze_brand(db: Session, svc, principal: Principal, agency_notes: str) -> BrandProfile:
    cfg = svc.brands.get(principal.tenant_id)
    if cfg is None:
        raise HTTPException(409, "no brand website configured for this tenant - add it to tenants/brands.yaml via PR")

    redactor = Redactor(extra_terms=svc.tenant_deny_terms.get(principal.tenant_id, []))
    try:
        pages, notes = crawl(cfg, svc.fetcher)
    except CrawlPolicyError as exc:
        # A refused crawl target is a security event (CC6.6 / CC6.7): record it, then fail closed.
        svc.audit.write(
            AuditRecord(
                "CRAWL_POLICY_REFUSED",
                principal.sub,
                principal.tenant_id,
                None,
                {"domain": cfg.domain, "reason": str(exc)[:200]},
            )
        )
        raise HTTPException(502, f"crawl refused by policy: {exc}") from exc
    refused = [n for n in notes if n.startswith("policy refused")]
    if refused:  # per-page refusals are still security events, recorded once per crawl
        svc.audit.write(
            AuditRecord(
                "CRAWL_POLICY_REFUSED",
                principal.sub,
                principal.tenant_id,
                None,
                {"domain": cfg.domain, "reason": "; ".join(refused)[:400], "count": len(refused)},
            )
        )
    if not pages:
        raise HTTPException(502, "crawl returned no readable pages: " + "; ".join(notes[-3:])[:300])

    kept, dropped, blocks, total = [], [], [], 0
    redaction_counts: dict[str, int] = {}

    def _redact(text: str) -> str:  # accumulate counts so the audit record shows redaction happened
        res = redactor.redact(text)
        for k, v in res.counts.items():
            redaction_counts[k] = redaction_counts.get(k, 0) + v
        return res.text

    for page in pages:
        if not screen_input_chunk(page.text).clean:
            dropped.append(page.url)
            continue
        block = wrap_untrusted("brand_page", f"url={page.url}\n{_redact(page.text)}")
        if total + len(block) > svc.brand_context_max_chars:
            notes.append(f"context capped after {len(kept)} pages")
            break
        blocks.append(block)
        total += len(block)
        kept.append(page)

    task = (
        "Analyze this brand strictly from its own website pages provided as untrusted data. "
        f"Agency notes: {_redact(agency_notes) or 'none'}"
    )
    context = "\n".join(blocks)
    result = svc.llm.complete(svc.prompts, task, "brand_analysis.md", BRAND_SCHEMA, context)

    flags = list(notes)
    if dropped:
        flags.append(f"PAGES_DROPPED_FOR_INJECTION: {dropped}")
    if result.fallback_used:
        flags.append(f"MODEL_FALLBACK: served by {result.model_served}, not {result.model_requested}")

    version = (
        db.query(func.max(BrandProfile.version)).filter(BrandProfile.tenant_id == principal.tenant_id).scalar() or 0
    ) + 1
    profile = BrandProfile(
        tenant_id=principal.tenant_id,
        requester_id=principal.sub,
        version=version,
        status=BrandStatus.REFUSED if result.refused else BrandStatus.PENDING_CONFIRMATION,
        profile=result.data,
        pages=[{"url": p.url, "sha256": p.sha256} for p in kept],
        review_flags=flags,
        lineage={
            **svc.prompts.lineage,
            "model_requested": result.model_requested,
            "model_served": result.model_served,
            "provider_request_id": result.provider_request_id,
            "crawl_domain": cfg.domain,
        },
    )
    db.add(profile)
    db.flush()
    rec = svc.audit.write(
        AuditRecord(
            "BRAND_ANALYSIS",
            principal.sub,
            principal.tenant_id,
            profile.id,
            {
                **profile.lineage,
                "version": version,
                "pages": profile.pages,
                "pages_dropped": dropped,
                "crawl_notes": notes,
                "redaction": {"engine": redactor.redact("").engine, "counts": redaction_counts},
                "context_sha256": sha256_text(context),
                "output_sha256": sha256_text(str(sorted(result.data.items()))),
                "stop_reason": result.stop_reason,
                "refusal_category": result.refusal_category,
                "usage": result.usage,
            },
        )
    )
    profile.audit_hash = rec.record_hash
    db.commit()
    return profile


def review_brand(db: Session, svc, principal: Principal, brand_id: str, decision: str, note: str) -> BrandProfile:
    from .workflow import load_owned  # local import avoids a cycle

    profile: BrandProfile = load_owned(db, BrandProfile, brand_id, principal)
    if profile.requester_id == principal.sub:
        raise HTTPException(403, "segregation of duties: requester cannot confirm their own brand analysis")
    if profile.status != BrandStatus.PENDING_CONFIRMATION:
        raise HTTPException(409, f"brand profile is {profile.status.value}; not reviewable")
    if decision not in {"confirm", "reject"}:
        raise HTTPException(422, "decision must be confirm | reject")
    if decision == "confirm" and not note.strip():
        raise HTTPException(422, "confirmation must include a note stating observations were checked with the client")

    if decision == "confirm":
        for old in (
            db.query(BrandProfile)
            .filter(BrandProfile.tenant_id == principal.tenant_id, BrandProfile.status == BrandStatus.CONFIRMED)
            .all()
        ):
            old.status = BrandStatus.SUPERSEDED
        profile.status = BrandStatus.CONFIRMED
    else:
        profile.status = BrandStatus.REJECTED
    profile.reviewer_id = principal.sub
    profile.review_note = note
    svc.audit.write(
        AuditRecord(
            "BRAND_REVIEW",
            principal.sub,
            principal.tenant_id,
            profile.id,
            {"decision": decision, "note": note, "version": profile.version, "flags_at_review": profile.review_flags},
        )
    )
    db.commit()
    return profile


def confirmed_brand(db: Session, tenant_id: str) -> BrandProfile | None:
    return (
        db.query(BrandProfile)
        .filter(BrandProfile.tenant_id == tenant_id, BrandProfile.status == BrandStatus.CONFIRMED)
        .order_by(BrandProfile.version.desc())
        .first()
    )
