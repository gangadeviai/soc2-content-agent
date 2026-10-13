"""Provider adapter controls: Gemini schema translation, refusal mapping, secret guard, prod refusal."""

import json

import httpx
import pytest

from app.config import REPO_ROOT, Settings
from app.llm import DRAFT_SCHEMA, GeminiClient, _gemini_schema
from app.prompts import PromptRegistry


def _settings(**kw) -> Settings:
    return Settings(env="dev", provider="gemini", _env_file=None, **kw)


def _prompts():
    return PromptRegistry(REPO_ROOT / "prompts").load(Settings(_env_file=None).prompt_version)


def test_gemini_schema_drops_unsupported_keys_but_keeps_structure():
    s = _gemini_schema(DRAFT_SCHEMA)
    assert "additionalProperties" not in json.dumps(s)
    assert s["properties"]["seo"]["required"] == DRAFT_SCHEMA["properties"]["seo"]["required"]
    assert s["properties"]["aeo"]["properties"]["faq"]["items"]["properties"]["question"] == {"type": "string"}


def test_gemini_client_parses_structured_response_and_lineage(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": json.dumps({"ok": True})}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30},
                "modelVersion": "gemini-2.5-flash-001",
                "responseId": "abc123",
            },
        )

    c = GeminiClient(_settings(), transport=httpx.MockTransport(handler))
    r = c.complete(
        _prompts(), "task text", "system.md", {"type": "object", "properties": {"ok": {"type": "boolean"}}}, "<ctx/>"
    )
    assert r.data == {"ok": True} and r.stop_reason == "end_turn" and not r.refused
    assert r.model_requested == "gemini-3.8-flash" and r.model_served == "gemini-2.5-flash-001"
    assert r.usage == {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0, "thinking_tokens": 0}
    assert r.provider_request_id == "abc123"
    assert "gemini-3.8-flash:generateContent" in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "test-key" and "key=" not in seen["url"]  # key in header, not URL
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "<brief>" in seen["body"]["contents"][0]["parts"][0]["text"]
    assert len(seen["body"]["systemInstruction"]["parts"]) == 2  # stage prompt + injection guard


def test_gemini_safety_block_becomes_refusal(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    blocked = httpx.MockTransport(lambda req: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    r = GeminiClient(_settings(), transport=blocked).complete(_prompts(), "t", "system.md", {}, "")
    assert r.refused and r.refusal_category == "SAFETY" and r.data == {}


def test_gemini_client_refuses_to_send_secrets(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    c = GeminiClient(_settings(), transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(ValueError, match="secret"):
        c.complete(_prompts(), "key AKIAABCDEFGHIJKLMNOP", "system.md", {}, "")


def test_gemini_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiClient(_settings())


def test_free_tier_provider_is_refused_in_prod_and_labelled_elsewhere():
    s = Settings(
        env="prod",
        provider="gemini",
        zdr_contract_confirmed=True,
        audit_s3_bucket="b",
        jwt_secret="x" * 20,
        database_url="postgresql://u:p@h/db",
        _env_file=None,
    )
    with pytest.raises(RuntimeError, match="free-tier providers are not permitted in prod"):
        s.validate_for_environment()
    assert _settings().data_retention_class == "FREE_TIER_NOT_ZDR_MAY_BE_USED_FOR_TRAINING"
    assert (
        Settings(provider="anthropic", zdr_contract_confirmed=True, _env_file=None).data_retention_class
        == "ZDR_CONTRACT"
    )
