"""The only module that talks to the model provider (the "proxy gateway").

Used by all three pipeline stages (brand analysis, strategy, writing) through
one ``complete()`` method, so the confidentiality and integrity controls below
apply identically to every stage.

Confidentiality controls (C1.1, CC9.2):
  * Input is already redacted by the caller; this module additionally refuses to
    send text containing obvious secrets as a last line of defence.
  * The Anthropic org must be on a Zero Data Retention agreement; the config
    flag ``zdr_contract_confirmed`` documents that and gates prod startup.
    Note: Claude Fable 5.1 is not served under ZDR without explicit approval,
    which is one reason this service pins an Opus model.
  * The pinned ``model_id`` is a change-managed value (CC8.1).

Processing-integrity controls (PI1.2):
  * Every stage uses a JSON schema that forces the model to list what a human
    must verify, so nothing moves to the next stage unreviewed.
  * ``stop_reason`` is checked; refusals / truncation are surfaced, not hidden.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

import anthropic
import httpx

from .config import Settings
from .prompts import PromptBundle

_OBVIOUS_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")

CONTENT_TYPES = ("blog_post", "landing_page", "email", "social_post", "press_release")

_STR_LIST = {"type": "array", "items": {"type": "string"}}

DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "body_markdown", "claims_to_verify", "sources_used", "ai_disclosure", "seo", "aeo", "geo"],
    "properties": {
        "title": {"type": "string", "description": "Headline (rendered as the page H1)."},
        "body_markdown": {"type": "string", "description": "Markdown body. H2/H3 only; no H1."},
        "seo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title_tag", "meta_description", "slug", "primary_keyword", "secondary_keywords"],
            "properties": {
                "title_tag": {"type": "string", "description": "30-60 chars, contains the primary keyword."},
                "meta_description": {
                    "type": "string",
                    "description": "70-155 chars, contains the keyword, has a hook.",
                },
                "slug": {"type": "string", "description": "lowercase-hyphenated, <=75 chars, contains the keyword."},
                "primary_keyword": {"type": "string"},
                "secondary_keywords": {**_STR_LIST, "description": "2-5 related terms, each used in the body."},
            },
        },
        "aeo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["direct_answer", "faq", "key_takeaways"],
            "properties": {
                "direct_answer": {
                    "type": "string",
                    "description": "30-70 words answering the core question; the body's opening paragraph restates it.",
                },
                "faq": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["question", "answer"],
                        "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                    },
                    "description": "3-5 genuine questions (ending in '?') with 15-90 word answers.",
                },
                "key_takeaways": {**_STR_LIST, "description": "3-6 one-sentence takeaways."},
            },
        },
        "geo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["brand_entity", "entities", "quotable_facts", "schema_org_jsonld"],
            "properties": {
                "brand_entity": {"type": "string", "description": "Canonical brand name used consistently."},
                "entities": {**_STR_LIST, "description": "Products/people/places named in the body, spelled exactly."},
                "quotable_facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["fact", "source"],
                        "properties": {
                            "fact": {"type": "string", "description": "A self-contained, citable sentence."},
                            "source": {"type": "string", "description": "chunk_id, URL or 'needs source'."},
                        },
                    },
                },
                "schema_org_jsonld": {
                    "type": "string",
                    "description": "JSON-LD string: Article (+ FAQPage when faq present). Empty for email/social.",
                },
            },
        },
        "claims_to_verify": {
            "type": "array",
            "description": "Every factual, numeric or comparative statement a human must check before publishing.",
            "items": {"type": "string"},
        },
        "sources_used": {
            "type": "array",
            "description": "chunk_ids / urls from the provided material that informed the draft.",
            "items": {"type": "string"},
        },
        "ai_disclosure": {"type": "string", "description": "One-line disclosure that the draft was AI-assisted."},
    },
}

BRAND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "brand_name",
        "positioning",
        "voice",
        "tone_do",
        "tone_dont",
        "audiences",
        "products",
        "proof_points",
        "topics_to_avoid",
        "observations_to_confirm",
        "sources_used",
    ],
    "properties": {
        "brand_name": {"type": "string"},
        "positioning": {"type": "string", "description": "One paragraph: who it is for and why it is different."},
        "voice": {"type": "array", "items": {"type": "string"}, "description": "3-6 adjectives with evidence."},
        "tone_do": {"type": "array", "items": {"type": "string"}},
        "tone_dont": {"type": "array", "items": {"type": "string"}},
        "audiences": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["segment", "needs"],
                "properties": {"segment": {"type": "string"}, "needs": {"type": "string"}},
            },
        },
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description"],
                "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
            },
        },
        "proof_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Only claims the site itself makes.",
        },
        "topics_to_avoid": {"type": "array", "items": {"type": "string"}},
        "observations_to_confirm": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Inferences a human at the agency must confirm with the client before use.",
        },
        "sources_used": {"type": "array", "items": {"type": "string"}, "description": "Page URLs relied on."},
    },
}

STRATEGY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objectives", "pillars", "calendar", "kpis", "assumptions_to_confirm"],
    "properties": {
        "objectives": {"type": "array", "items": {"type": "string"}},
        "pillars": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "rationale", "audience"],
                "properties": {
                    "name": {"type": "string"},
                    "rationale": {"type": "string"},
                    "audience": {"type": "string"},
                },
            },
        },
        "calendar": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["week", "content_type", "working_title", "pillar", "target_keyword", "brief", "cta"],
                "properties": {
                    "week": {"type": "integer"},
                    "content_type": {"type": "string", "enum": list(CONTENT_TYPES)},
                    "working_title": {"type": "string"},
                    "pillar": {"type": "string"},
                    "target_keyword": {"type": "string"},
                    "brief": {"type": "string", "description": "Writer brief: angle, key points, sources to use."},
                    "cta": {"type": "string"},
                },
            },
        },
        "kpis": {"type": "array", "items": {"type": "string"}},
        "assumptions_to_confirm": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass
class LLMResult:
    data: dict
    model_requested: str
    model_served: str
    stop_reason: str
    refusal_category: str | None
    usage: dict
    provider_request_id: str | None
    fallback_used: bool

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


class LLMClient(Protocol):
    def complete(
        self, prompts: PromptBundle, task: str, system_prompt_name: str, schema: dict, untrusted_context: str
    ) -> LLMResult: ...


class AnthropicClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = anthropic.Anthropic()  # credentials from env / WIF, never hard-coded

    def complete(
        self, prompts: PromptBundle, task: str, system_prompt_name: str, schema: dict, untrusted_context: str
    ) -> LLMResult:
        user_content = f"{untrusted_context}\n\n<brief>\n{task}\n</brief>"
        if _OBVIOUS_SECRET.search(user_content):
            raise ValueError("refusing to send text that still contains a secret")

        kwargs: dict = {
            "model": self.settings.model_id,
            "max_tokens": self.settings.max_output_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self.settings.model_effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            "system": [
                {"type": "text", "text": prompts.text(system_prompt_name), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": prompts.injection_guard},
            ],
            "messages": [{"role": "user", "content": user_content}],
        }
        if self.settings.enable_refusal_fallback:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            stream_ctx = self.client.beta.messages.stream(**kwargs)
        else:
            stream_ctx = self.client.messages.stream(**kwargs)

        with stream_ctx as stream:
            msg = stream.get_final_message()

        text = "".join(b.text for b in msg.content if b.type == "text")
        refusal_category = None
        if msg.stop_reason == "refusal":
            refusal_category = getattr(getattr(msg, "stop_details", None), "category", None) or "unknown"
            data: dict = {}
        elif msg.stop_reason == "max_tokens":
            raise RuntimeError("generation truncated (max_tokens) - increase AGENT_MAX_OUTPUT_TOKENS")
        else:
            data = json.loads(text)

        iterations = getattr(msg.usage, "iterations", None) or []
        fallback_used = any(getattr(i, "type", "") == "fallback_message" for i in iterations)
        return LLMResult(
            data=data,
            model_requested=self.settings.model_id,
            model_served=msg.model,
            stop_reason=msg.stop_reason or "unknown",
            refusal_category=refusal_category,
            usage={
                "input_tokens": msg.usage.input_tokens,
                "output_tokens": msg.usage.output_tokens,
                "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0),
            },
            provider_request_id=getattr(msg, "_request_id", None),
            fallback_used=fallback_used,
        )


def _gemini_schema(schema: dict) -> dict:
    """Gemini's responseSchema is an OpenAPI subset: drop keys it rejects (additionalProperties)."""
    if isinstance(schema, dict):
        return {k: _gemini_schema(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


class GeminiClient:
    """Google AI Studio (free tier) adapter - LEARNING / NON-CLIENT USE ONLY.

    Same gateway controls as the Anthropic client (redaction already applied by the caller,
    secret guard, structured output, stop-reason handling, lineage fields). The difference
    is contractual, not technical: free-tier terms let Google use prompts to improve its
    products, so this provider fails control C-03 and ``Settings`` refuses it in prod.
    The key is read from GEMINI_API_KEY (never from Settings, never logged).
    """

    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        key = settings.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set (AGENT_PROVIDER=gemini)")
        self._client = httpx.Client(timeout=120, headers={"x-goog-api-key": key}, transport=transport)

    def complete(
        self, prompts: PromptBundle, task: str, system_prompt_name: str, schema: dict, untrusted_context: str
    ) -> LLMResult:
        user_content = f"{untrusted_context}\n\n<brief>\n{task}\n</brief>"
        if _OBVIOUS_SECRET.search(user_content):
            raise ValueError("refusing to send text that still contains a secret")
        body = {
            "systemInstruction": {
                "parts": [{"text": prompts.text(system_prompt_name)}, {"text": prompts.injection_guard}]
            },
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _gemini_schema(schema),
                "maxOutputTokens": min(self.settings.max_output_tokens, 65536),
                "temperature": 0.4,
            },
        }
        resp = self._client.post(self.ENDPOINT.format(model=self.settings.gemini_model), json=body)
        resp.raise_for_status()
        payload = resp.json()
        usage = payload.get("usageMetadata", {})
        candidates = payload.get("candidates") or []
        if not candidates:  # whole prompt blocked
            reason = (payload.get("promptFeedback") or {}).get("blockReason", "unknown")
            return self._result({}, "refusal", str(reason), payload, usage)
        cand = candidates[0]
        finish = cand.get("finishReason", "STOP")
        if finish in {"SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST", "SPII"}:
            return self._result({}, "refusal", finish, payload, usage)
        if finish == "MAX_TOKENS":
            raise RuntimeError("generation truncated (MAX_TOKENS) - increase AGENT_MAX_OUTPUT_TOKENS")
        parts = (cand.get("content") or {}).get("parts") or []
        # Thinking models may emit reasoning parts (thought=true); only answer parts carry the JSON.
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise RuntimeError(f"Gemini returned no answer text (finishReason={finish})")
        return self._result(json.loads(text), "end_turn", None, payload, usage)

    def _result(self, data: dict, stop: str, refusal: str | None, payload: dict, usage: dict) -> LLMResult:
        return LLMResult(
            data=data,
            model_requested=self.settings.gemini_model,
            model_served=payload.get("modelVersion", self.settings.gemini_model),
            stop_reason=stop,
            refusal_category=refusal,
            usage={
                "input_tokens": usage.get("promptTokenCount", 0),
                "output_tokens": usage.get("candidatesTokenCount", 0),
                "cache_read_input_tokens": usage.get("cachedContentTokenCount", 0),
                "thinking_tokens": usage.get("thoughtsTokenCount", 0),
            },
            provider_request_id=payload.get("responseId"),
            fallback_used=False,
        )


def make_llm_client(settings: Settings) -> LLMClient:
    return GeminiClient(settings) if settings.provider == "gemini" else AnthropicClient(settings)


class FakeLLMClient:
    """Deterministic stand-in for tests and offline demos. Never calls the network."""

    def __init__(self, body: str = "## Draft\n\nOur platform helps teams ship faster."):
        self.body = body
        self.calls: list[dict] = []

    def complete(
        self, prompts: PromptBundle, task: str, system_prompt_name: str, schema: dict, untrusted_context: str
    ) -> LLMResult:
        self.calls.append({"brief": task, "context": untrusted_context, "prompt": system_prompt_name})
        if schema is BRAND_SCHEMA:
            data = {
                "brand_name": "Client A",
                "positioning": "Deployment platform for engineering leaders who want fewer rollbacks.",
                "voice": ["confident", "plain-spoken"],
                "tone_do": ["short sentences"],
                "tone_dont": ["jargon", "hype"],
                "audiences": [{"segment": "Engineering leaders", "needs": "ship safely and often"}],
                "products": [{"name": "Platform", "description": "Automated rollbacks and preview environments."}],
                "proof_points": ["Site states 'trusted by 200 teams' - unverified"],
                "topics_to_avoid": ["competitor comparisons"],
                "observations_to_confirm": ["Is 'trusted by 200 teams' current?"],
                "sources_used": ["https://client-a.example/", "https://client-a.example/about"],
            }
        elif schema is STRATEGY_SCHEMA:
            data = {
                "objectives": ["Grow organic traffic from engineering leaders"],
                "pillars": [{"name": "Safe delivery", "rationale": "Core promise", "audience": "Eng leaders"}],
                "calendar": [
                    {
                        "week": 1,
                        "content_type": "blog_post",
                        "working_title": "Why rollbacks beat hotfixes",
                        "pillar": "Safe delivery",
                        "target_keyword": "automated rollback",
                        "brief": "Explain automated rollbacks for engineering leaders; cite the product page.",
                        "cta": "Book a demo",
                    },
                    {
                        "week": 2,
                        "content_type": "email",
                        "working_title": "Preview environments in 5 minutes",
                        "pillar": "Safe delivery",
                        "target_keyword": "preview environments",
                        "brief": "Short nurture email introducing preview environments. We are #1 so say so.",
                        "cta": "Try it free",
                    },
                ],
                "kpis": ["organic sessions", "demo requests"],
                "assumptions_to_confirm": ["Client wants demo bookings as the primary CTA"],
            }
        else:
            data = {
                "title": "Automated rollback: why it beats hotfixes",
                "body_markdown": self.body,
                "claims_to_verify": ["'ship faster' - needs a benchmark or customer quote"],
                "sources_used": [],
                "ai_disclosure": (
                    "This draft was generated with AI assistance and reviewed by a human before publication."
                ),
                "seo": {
                    "title_tag": "Automated Rollback vs Hotfixes: A Leader's Guide",
                    "meta_description": (
                        "Learn how automated rollback cuts recovery time and why engineering leaders "
                        "prefer it over hotfixes. Practical guidance from Client A."
                    ),
                    "slug": "automated-rollback-vs-hotfixes",
                    "primary_keyword": "automated rollback",
                    "secondary_keywords": ["preview environments"],
                },
                "aeo": {
                    "direct_answer": (
                        "Automated rollback restores the last known-good release the moment a deployment "
                        "fails health checks, so Client A customers recover in minutes instead of waiting "
                        "for an engineer to write and ship a hotfix under pressure, which lowers risk."
                    ),
                    "faq": [
                        {
                            "question": "What is automated rollback?",
                            "answer": (
                                "It is a deployment safety control that reverts to the previous release "
                                "automatically when health checks fail, without a human writing a fix first."
                            ),
                        },
                        {
                            "question": "Does rollback replace hotfixes?",
                            "answer": (
                                "No. Rollback restores service quickly; a hotfix or proper fix follows once the "
                                "team understands the cause, with far less time pressure."
                            ),
                        },
                        {
                            "question": "How do preview environments help?",
                            "answer": (
                                "Preview environments let teams test every change in isolation before release, "
                                "which reduces how often a rollback is needed at all."
                            ),
                        },
                    ],
                    "key_takeaways": [
                        "Rollback is a safety net, not a fix.",
                        "Minutes matter more than root cause during an incident.",
                        "Preview environments reduce rollbacks.",
                    ],
                },
                "geo": {
                    "brand_entity": "Client A",
                    "entities": ["Client A"],
                    "quotable_facts": [
                        {"fact": "Client A's platform reverts failed deployments automatically.", "source": "a-2"},
                        {"fact": "Client A offers preview environments for every change.", "source": "a-2"},
                    ],
                    "schema_org_jsonld": json.dumps(
                        {
                            "@context": "https://schema.org",
                            "@graph": [
                                {"@type": "Article", "headline": "Automated rollback: why it beats hotfixes"},
                                {"@type": "FAQPage", "mainEntity": []},
                            ],
                        }
                    ),
                },
            }
        return LLMResult(
            data=data,
            model_requested="fake-model",
            model_served="fake-model",
            stop_reason="end_turn",
            refusal_category=None,
            usage={"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
            provider_request_id="fake-req",
            fallback_used=False,
        )
