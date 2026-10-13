"""SEO / AEO / GEO fitness evaluation for generated content (PI1.2, PI1.3).

The model is *asked* for search-fit elements via ``DRAFT_SCHEMA`` (title tag,
meta description, slug, direct answer, FAQ, quotable sourced facts, JSON-LD).
This module *checks* them deterministically, so the model never self-certifies.
Failed checks become reviewer flags; the score and every check are written to the
GENERATION audit record. Thresholds are code (change-managed), not prompt text.

  SEO - classic search: title/meta lengths, keyword placement, slug, headings,
        length by content type, alt text, readability.
  AEO - answer engines / featured snippets / voice: a 30-70 word direct answer
        up front, a real FAQ, key takeaways.
  GEO - generative engines (AI Overviews, Perplexity, ChatGPT search): citable,
        sourced facts, consistent entity naming, valid schema.org JSON-LD.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

SCORE_THRESHOLD = 70  # below this the draft is flagged for the reviewer

_WORD_RANGES = {  # min, max words of body_markdown by content type
    "blog_post": (600, 2200),
    "landing_page": (300, 1200),
    "press_release": (300, 900),
    "email": (80, 400),
    "social_post": (10, 90),
}
_LONG_FORM = {"blog_post", "landing_page", "press_release"}

_H1 = re.compile(r"^# (.+)$", re.MULTILINE)
_H2 = re.compile(r"^## (.+)$", re.MULTILINE)
_H3 = re.compile(r"^### (.+)$", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6}) ", re.MULTILINE)
_IMG = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_SENTENCE_END = re.compile(r"[.!?]+(\s|$)")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class Check:
    pillar: str  # SEO | AEO | GEO
    name: str
    weight: int
    passed: bool
    detail: str


@dataclass
class SEOReport:
    score: int
    checks: list[Check] = field(default_factory=list)

    @property
    def flags(self) -> list[str]:
        out = [f"{c.pillar}:{c.name}: {c.detail}" for c in self.checks if not c.passed]
        if self.score < SCORE_THRESHOLD:
            out.insert(0, f"SEO_AEO_GEO_SCORE_BELOW_THRESHOLD: {self.score} < {SCORE_THRESHOLD}")
        return out

    def to_dict(self) -> dict:
        return {"score": self.score, "threshold": SCORE_THRESHOLD, "checks": [asdict(c) for c in self.checks]}


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9'’-]+", text)


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and needle.lower() in haystack.lower()


def evaluate(draft: dict, content_type: str) -> SEOReport:
    checks: list[Check] = []

    def add(pillar: str, name: str, weight: int, passed: bool, detail: str) -> None:
        checks.append(Check(pillar, name, weight, passed, detail))

    body = draft.get("body_markdown", "") or ""
    title = draft.get("title", "") or ""
    seo = draft.get("seo") or {}
    aeo = draft.get("aeo") or {}
    geo = draft.get("geo") or {}
    kw = (seo.get("primary_keyword") or "").strip()
    long_form = content_type in _LONG_FORM
    words = _words(body)
    first_100 = " ".join(words[:100])

    # ---- SEO ----------------------------------------------------------------------
    tt = seo.get("title_tag", "") or ""
    add("SEO", "title_tag_length", 10, 30 <= len(tt) <= 60, f"{len(tt)} chars (want 30-60)")
    md = seo.get("meta_description", "") or ""
    add("SEO", "meta_description_length", 10, 70 <= len(md) <= 155, f"{len(md)} chars (want 70-155)")
    add("SEO", "primary_keyword_present", 6, bool(kw), "missing" if not kw else kw)
    add("SEO", "keyword_in_title_tag", 6, _contains(tt, kw), f"{kw!r} in title tag")
    add("SEO", "keyword_in_headline", 5, _contains(title, kw), f"{kw!r} in headline")
    add("SEO", "keyword_in_first_100_words", 5, _contains(first_100, kw), f"{kw!r} in opening")
    slug = seo.get("slug", "") or ""
    add(
        "SEO",
        "slug_format",
        4,
        bool(_SLUG.match(slug)) and len(slug) <= 75 and _contains(slug.replace("-", " "), kw),
        f"{slug!r} (lowercase-hyphenated, <=75 chars, contains keyword)",
    )
    if long_form:
        h2s = _H2.findall(body)
        add("SEO", "h2_structure", 5, len(h2s) >= 2, f"{len(h2s)} H2 headings (want >=2)")
        levels = [len(m) for m in _HEADING.findall(body)]
        skips = any(b - a > 1 for a, b in zip(levels, levels[1:], strict=False))
        add("SEO", "heading_hierarchy", 3, not skips, "no skipped heading levels" if not skips else "skipped level")
        add(
            "SEO", "single_h1", 2, len(_H1.findall(body)) <= 1, f"{len(_H1.findall(body))} H1 in body (title is the H1)"
        )
    lo, hi = _WORD_RANGES.get(content_type, (100, 5000))
    add("SEO", "word_count", 8, lo <= len(words) <= hi, f"{len(words)} words (want {lo}-{hi} for {content_type})")
    imgs = _IMG.findall(body)
    add(
        "SEO",
        "image_alt_text",
        3,
        all(a.strip() for a in imgs),
        f"{sum(1 for a in imgs if not a.strip())} images without alt",
    )
    sentences = [s for s in _SENTENCE_END.split(body) if s and len(_words(s)) > 2]
    avg = (len(words) / len(sentences)) if sentences else 0
    add("SEO", "readability_sentence_length", 4, 0 < avg <= 22, f"avg {avg:.1f} words/sentence (want <=22)")
    sec = seo.get("secondary_keywords") or []
    add(
        "SEO",
        "secondary_keywords_used",
        3,
        bool(sec) and all(_contains(body, k) for k in sec),
        f"{len(sec)} secondary keywords, all used in body",
    )

    # ---- AEO ------------------------------------------------------------------------
    da = aeo.get("direct_answer", "") or ""
    n = len(_words(da))
    add("AEO", "direct_answer_length", 6, 30 <= n <= 70, f"{n} words (want 30-70)")
    first_para = body.strip().split("\n\n")[0] if body.strip() else ""
    overlap = len(set(w.lower() for w in _words(da)) & set(w.lower() for w in _words(first_para)))
    add(
        "AEO",
        "answer_up_front",
        5,
        n > 0 and overlap / max(n, 1) >= 0.4,
        "opening paragraph restates the direct answer",
    )
    faq = aeo.get("faq") or []
    good_faq = [
        q
        for q in faq
        if str(q.get("question", "")).strip().endswith("?") and 15 <= len(_words(q.get("answer", ""))) <= 90
    ]
    want_faq = 3 if long_form else 1
    add(
        "AEO",
        "faq_quality",
        8,
        len(good_faq) >= want_faq,
        f"{len(good_faq)}/{len(faq)} well-formed Q&A (want >={want_faq})",
    )
    kt = aeo.get("key_takeaways") or []
    add("AEO", "key_takeaways", 4, 3 <= len(kt) <= 6, f"{len(kt)} takeaways (want 3-6)")

    # ---- GEO --------------------------------------------------------------------------
    facts = geo.get("quotable_facts") or []
    sourced = [f for f in facts if str(f.get("source", "")).strip()]
    add(
        "GEO",
        "sourced_quotable_facts",
        8,
        len(sourced) >= 2 and len(sourced) == len(facts),
        f"{len(sourced)}/{len(facts)} facts carry a source (want >=2, all sourced)",
    )
    brand = (geo.get("brand_entity") or "").strip()
    add(
        "GEO",
        "brand_entity_consistency",
        5,
        bool(brand) and _contains(body, brand) and _contains(da, brand),
        f"brand entity {brand!r} named in body and direct answer",
    )
    ents = geo.get("entities") or []
    add(
        "GEO",
        "entities_named_in_body",
        3,
        bool(ents) and all(_contains(body, e) for e in ents),
        f"{len(ents)} entities, all present in body",
    )
    if long_form:
        ld = geo.get("schema_org_jsonld", "") or ""
        ok, detail = _validate_jsonld(ld, want_faq=bool(good_faq))
        add("GEO", "schema_org_jsonld", 6, ok, detail)

    total = sum(c.weight for c in checks) or 1
    score = round(100 * sum(c.weight for c in checks if c.passed) / total)
    return SEOReport(score=score, checks=checks)


def _validate_jsonld(raw: str, want_faq: bool) -> tuple[bool, str]:
    if not raw.strip():
        return False, "missing"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON ({exc.msg})"
    items = data.get("@graph", [data]) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return False, "unexpected shape"
    types = {str(i.get("@type", "")) for i in items if isinstance(i, dict)}
    if not types & {"Article", "BlogPosting", "NewsArticle", "WebPage"}:
        return False, f"no Article-type node (found {sorted(types)})"
    if want_faq and "FAQPage" not in types:
        return False, "FAQ present in content but no FAQPage node"
    return True, f"valid JSON-LD with {sorted(types)}"


def safe_jsonld(raw: str) -> str | None:
    """Re-serialise model-supplied JSON-LD so only valid, escaped JSON is embedded in HTML."""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=True).replace("</", "<\\/")
    except (json.JSONDecodeError, TypeError):
        return None
