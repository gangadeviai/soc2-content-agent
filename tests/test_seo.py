"""SEO / AEO / GEO fitness checker: deterministic, model cannot self-certify."""

import json

from app.llm import FakeLLMClient
from app.seo import SCORE_THRESHOLD, evaluate, safe_jsonld
from tests.conftest import token

GOOD_BODY = (
    "Automated rollback restores the last known-good release the moment a deployment fails health checks, "
    "so Client A customers recover in minutes instead of waiting for an engineer to write a hotfix.\n\n"
    "## Why automated rollback matters\n\n"
    + (
        "Teams that rely on hotfixes spend the incident writing code under pressure. "
        "Automated rollback removes that step. Client A reverts the release first and fixes later. "
    )
    * 12
    + "\n\n## Preview environments reduce rollbacks\n\n"
    + (
        "Preview environments let every change run in isolation before release. "
        "That catches most failures early, so Client A teams roll back less often. "
    )
    * 12
    + "\n\n### What to measure\n\nTrack time to recover and rollback frequency.\n"
    + ("Short sentences keep this readable for busy leaders. " * 60)
)


def _good_draft() -> dict:
    d = FakeLLMClient(body=GOOD_BODY).complete(None, "t", "system.md", {}, "")  # schema falls to draft branch
    return d.data


def test_well_formed_blog_post_passes_threshold():
    report = evaluate(_good_draft(), "blog_post")
    failed = [c.name for c in report.checks if not c.passed]
    assert report.score >= SCORE_THRESHOLD, failed
    assert not report.flags, failed
    pillars = {c.pillar for c in report.checks}
    assert pillars == {"SEO", "AEO", "GEO"}


def test_each_pillar_detects_its_failures():
    d = _good_draft()
    d["seo"]["title_tag"] = "Short"  # SEO
    d["seo"]["slug"] = "Bad Slug!"  # SEO
    d["aeo"]["direct_answer"] = "Too short."  # AEO
    d["aeo"]["faq"] = d["aeo"]["faq"][:1]  # AEO
    d["geo"]["quotable_facts"][0]["source"] = ""  # GEO
    d["geo"]["schema_org_jsonld"] = "{not json"  # GEO
    report = evaluate(d, "blog_post")
    names = {c.name for c in report.checks if not c.passed}
    assert {
        "title_tag_length",
        "slug_format",
        "direct_answer_length",
        "faq_quality",
        "sourced_quotable_facts",
        "schema_org_jsonld",
    } <= names
    assert any(f.startswith("SEO_AEO_GEO_SCORE_BELOW_THRESHOLD") for f in report.flags)
    assert any(f.startswith("SEO:") for f in report.flags)
    assert any(f.startswith("AEO:") for f in report.flags)
    assert any(f.startswith("GEO:") for f in report.flags)


def test_short_formats_skip_long_form_checks():
    d = _good_draft()
    d["body_markdown"] = (
        "Client A ships automated rollback so you recover in minutes. Try preview environments free today."
    )
    d["geo"]["schema_org_jsonld"] = ""
    d["aeo"]["faq"] = d["aeo"]["faq"][:1]
    names = {c.name for c in evaluate(d, "social_post").checks}
    assert "schema_org_jsonld" not in names and "h2_structure" not in names
    assert next(c for c in evaluate(d, "social_post").checks if c.name == "word_count").passed


def test_jsonld_is_resanitised_before_embedding():
    assert safe_jsonld("{not json") is None
    out = safe_jsonld(json.dumps({"@type": "Article", "x": "</script><script>alert(1)</script>"}))
    assert "</script>" not in out and '"@type": "Article"' in out


def test_generation_records_seo_score_and_flags(client, settings):
    h = token(client, "u1", "client-a")
    r = client.post(
        "/drafts", json={"brief": "Write a blog post about automated rollback for engineering leaders."}, headers=h
    )
    d = r.json()
    assert isinstance(d["seo_score"], int) and d["seo_report"]["checks"]
    assert d["seo"]["seo"]["primary_keyword"] == "automated rollback"
    # FakeLLM default body is far too short for a blog post -> flagged, not blocked
    assert any("SEO:word_count" in f for f in d["review_flags"]) and d["status"] == "draft"
    log = (settings.audit_local_dir / "audit.jsonl").read_text()
    gen = next(json.loads(line) for line in log.splitlines() if '"GENERATION"' in line)
    assert gen["details"]["seo_score"] == d["seo_score"] and "word_count" in gen["details"]["seo_checks_failed"]
