"""PII / secret redaction applied *before* any text leaves the trust boundary.

Uses Microsoft Presidio when installed (install extra ``pii``); otherwise falls
back to a conservative regex engine so the gateway never runs without redaction.

Redaction is deterministic and reversible *only* inside our boundary: the
mapping placeholder -> original value is returned to the caller and stored
encrypted with the draft so a reviewer can re-hydrate approved copy. The LLM
provider only ever sees placeholders. (TSC C1.1, P3.1, P6.1)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Conservative patterns. False positives are acceptable; false negatives are not.
_REGEX_RECOGNIZERS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "PHONE": re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{3}\)?[\s-]?)\d{3}[\s-]?\d{4}(?!\d)"),
    "CREDIT_CARD": re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
    "US_SSN": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "IPV4": re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    "URL_WITH_TOKEN": re.compile(r"https?://\S*?(?:token|key|secret|sig)=\S+", re.IGNORECASE),
    # Secrets that marketers paste by accident
    "API_KEY": re.compile(
        r"(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,})"
    ),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # Embargo / confidentiality markers - the whole line is withheld
    "EMBARGOED_LINE": re.compile(
        r"^.*\b(EMBARGO(?:ED)?|DO NOT (?:SHARE|PUBLISH)|CONFIDENTIAL)\b.*$", re.IGNORECASE | re.MULTILINE
    ),
}


@dataclass
class RedactionResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    counts: dict[str, int] = field(default_factory=dict)
    engine: str = "regex"

    @property
    def redacted_any(self) -> bool:
        return bool(self.mapping)


class Redactor:
    def __init__(self, extra_terms: list[str] | None = None, use_presidio: bool = True):
        """extra_terms: tenant-specific deny-list (unreleased product codenames, customer names)."""
        self.extra_terms = [t for t in (extra_terms or []) if t.strip()]
        self._presidio = None
        if use_presidio:
            try:  # optional dependency; OSError when the spaCy model is not downloaded
                from presidio_analyzer import AnalyzerEngine  # type: ignore

                self._presidio = AnalyzerEngine()
            except (ImportError, OSError) as exc:
                # The engine name is written to every audit record, so a silent
                # downgrade to regex-only is still visible to the auditor.
                logger.warning("Presidio unavailable, regex-only redaction in effect: %s", exc)
                self._presidio = None

    def redact(self, text: str) -> RedactionResult:
        result = RedactionResult(text=text, engine="presidio+regex" if self._presidio else "regex")
        if self._presidio is not None:
            result = self._apply_presidio(result)
        result = self._apply_regex(result)
        result = self._apply_terms(result)
        return result

    # ---- engines -----------------------------------------------------------
    def _placeholder(self, result: RedactionResult, kind: str, original: str) -> str:
        for ph, orig in result.mapping.items():  # stable placeholder for repeat values
            if orig == original:
                return ph
        n = result.counts.get(kind, 0) + 1
        result.counts[kind] = n
        ph = f"[REDACTED_{kind}_{n}]"
        result.mapping[ph] = original
        return ph

    def _apply_presidio(self, result: RedactionResult) -> RedactionResult:
        findings = self._presidio.analyze(text=result.text, language="en", score_threshold=0.6)
        text = result.text
        for f in sorted(findings, key=lambda f: f.start, reverse=True):
            original = text[f.start : f.end]
            text = text[: f.start] + self._placeholder(result, f.entity_type, original) + text[f.end :]
        result.text = text
        return result

    def _apply_regex(self, result: RedactionResult) -> RedactionResult:
        for kind, pattern in _REGEX_RECOGNIZERS.items():
            result.text = pattern.sub(lambda m, k=kind: self._placeholder(result, k, m.group(0)), result.text)
        return result

    def _apply_terms(self, result: RedactionResult) -> RedactionResult:
        for term in self.extra_terms:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            result.text = pattern.sub(lambda m: self._placeholder(result, "CONFIDENTIAL_TERM", m.group(0)), result.text)
        return result


def rehydrate(text: str, mapping: dict[str, str]) -> str:
    """Restore originals inside the trust boundary (reviewer view / publish)."""
    for ph, original in mapping.items():
        text = text.replace(ph, original)
    return text
