"""Prompt-injection and output-safety screening (PI1.2 valid/accurate processing, CC7.1).

Two layers:
1. Input screening - retrieved RAG chunks and pasted briefs are *data*. They are
   wrapped in delimiters, and chunks containing instruction-like text are flagged
   and dropped before the model sees them.
2. Output screening - generated copy is scanned for risky marketing claims,
   leaked placeholders, and unexpected URLs. Flags are attached to the draft so
   the human reviewer sees them (they do not block generation - humans decide).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INJECTION_PATTERNS = [
    re.compile(r"ignore (all |any )?(previous|prior|above) instructions", re.IGNORECASE),
    re.compile(r"you are now\b", re.IGNORECASE),
    re.compile(r"\bsystem prompt\b", re.IGNORECASE),
    re.compile(r"</?(system|assistant|instructions?)>", re.IGNORECASE),
    re.compile(r"\b(reveal|print|output) (your|the) (system|hidden) (prompt|instructions)", re.IGNORECASE),
    re.compile(r"\bdo not (tell|inform) the (user|reviewer)\b", re.IGNORECASE),
    re.compile(r"\bBEGIN (SYSTEM|ADMIN) OVERRIDE\b", re.IGNORECASE),
]

# Claims that need substantiation under FTC / ASA style advertising rules.
_RISKY_CLAIM_PATTERNS = {
    # (?<!\w)/(?!\w) instead of \b: "#1" and "100%" begin/end with non-word characters.
    "ABSOLUTE_CLAIM": re.compile(
        r"(?<!\w)(guarantee[ds]?|100%|risk[- ]free|never fails?|always works)(?!\w)", re.IGNORECASE
    ),
    "SUPERLATIVE": re.compile(
        r"(?<!\w)(#1|number one|the best|world'?s (best|leading|first)|fastest|cheapest)(?!\w)", re.IGNORECASE
    ),
    "MEDICAL_OR_FINANCIAL": re.compile(
        r"\b(cure[sd]?|clinically proven|FDA[- ]approved|guaranteed returns?|risk[- ]free investment)\b", re.IGNORECASE
    ),
    "COMPETITOR_DISPARAGEMENT": re.compile(
        r"\b(unlike|worse than|inferior)\b.*\b(competitor|rival)s?\b", re.IGNORECASE
    ),
    "UNSOURCED_STATISTIC": re.compile(r"(?<!\w)\d{1,3}(?:\.\d+)?%"),
}
_PLACEHOLDER = re.compile(r"\[REDACTED_[A-Z_]+_\d+\]")
_URL = re.compile(r"https?://[^\s)\]]+", re.IGNORECASE)


@dataclass
class ScreenResult:
    clean: bool
    flags: list[str] = field(default_factory=list)


def screen_input_chunk(text: str) -> ScreenResult:
    flags = [f"INJECTION:{p.pattern[:40]}" for p in _INJECTION_PATTERNS if p.search(text)]
    return ScreenResult(clean=not flags, flags=flags)


def wrap_untrusted(label: str, text: str) -> str:
    """Delimit untrusted content so the model treats it as data, not instructions."""
    safe = text.replace("<", "&lt;")
    return f"<untrusted_{label}>\n{safe}\n</untrusted_{label}>"


def screen_output(text: str, allowed_urls: set[str] | None = None) -> ScreenResult:
    flags: list[str] = []
    for name, pattern in _RISKY_CLAIM_PATTERNS.items():
        for m in pattern.finditer(text):
            flags.append(f"{name}: '{m.group(0)}'")
    if _PLACEHOLDER.search(text):
        flags.append("PLACEHOLDER_IN_OUTPUT: model echoed a redaction placeholder - rehydrate or remove")
    for url in _URL.findall(text):
        if allowed_urls is not None and url.rstrip(".,") not in allowed_urls:
            flags.append(f"UNEXPECTED_URL: {url} (not in brief) - possible hallucinated link")
    return ScreenResult(clean=not flags, flags=flags)
