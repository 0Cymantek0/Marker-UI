"""Vocabulary pack + prompt compilation (plan §9).

Vocabulary terms are reused across many recordings, so they deserve a
first-class pack model rather than a free-text field retyped every time. A
pack is just a named, categorized list of terms persisted in settings; this
module owns the small set of pure functions that parse, dedupe, and compile
packs into a provider-appropriate prompt.

Compilation is **capability-aware** (plan §9.3): faster-whisper wants an
``initial_prompt`` string; OpenAI/Groq want a ``prompt`` within a size limit;
Deepgram wants ``keyterm``s; a provider without vocabulary support gets no
prompt at all and the terms are instead surfaced in diagnostics/correction
only. The route layer never needs to know the difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.audio.providers.capabilities import ProviderCapability


# Hard ceiling on prompt length handed to any single STT call. Providers that
# publish their own limits (OpenAI/Groq ~ a few hundred chars) are clamped
# further inside their adapter; this is the shared backstop.
_MAX_VOCABULARY_PROMPT_CHARS = 1000
_MAX_TERMS = 100


@dataclass(frozen=True)
class VocabularyPack:
    """A reusable named collection of domain terms (plan §9.2)."""

    id: str
    name: str
    terms: tuple[str, ...] = ()
    category: str = "general"
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "terms": list(self.terms),
            "category": self.category,
            "description": self.description,
        }


def parse_terms(raw: Any) -> list[str]:
    """Parse a vocabulary value (string/list/tuple) into ordered unique terms.

    Mirrors the historical parsing in ``AudioConverter._audio_initial_prompt``
    so the free-text field keeps its old semantics; packs compile through the
    same path.
    """

    if isinstance(raw, str):
        terms = [part.strip() for part in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        terms = [str(part).strip() for part in raw]
    else:
        terms = []
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            result.append(term)
    return result[:_MAX_TERMS]


def resolve_vocabulary_terms(config: dict[str, Any]) -> list[str]:
    """Merge all configured vocabulary sources into one ordered term list.

    Honours the free-text ``audio_vocabulary`` field (backward compatible) plus
    any named pack ids the caller resolved into ``config["audio_vocabulary_packs"]``
    (already-flattened term lists) and the legacy ``audio_vocabulary_terms`` alias.
    Duplicates are removed case-insensitively; order is preserved (free-text first,
    then packs), capped at :data:`_MAX_TERMS`.
    """

    combined: list[str] = []
    for key in ("audio_vocabulary", "audio_vocabulary_terms"):
        combined.extend(parse_terms(config.get(key)))
    packs = config.get("audio_vocabulary_packs")
    if isinstance(packs, (list, tuple)):
        for pack in packs:
            combined.extend(parse_terms(pack if isinstance(pack, (list, tuple)) else [pack]))
    # De-dup preserving order, cap length. ``parse_terms`` already de-dups, but the
    # cross-source merge above can reintroduce duplicates between field and packs.
    seen: set[str] = set()
    result: list[str] = []
    for term in combined:
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            result.append(term)
    return result[:_MAX_TERMS]


def compile_vocabulary_prompt(
    *,
    terms: list[str],
    capability: ProviderCapability,
) -> str | None:
    """Compile terms into a provider-appropriate prompt, or ``None``.

    Returns ``None`` (no prompt) when the provider does not support custom
    vocabulary at all — the terms then live only in diagnostics/correction,
    never silently injected into a provider that ignores them.
    """

    if not capability.supports_custom_vocabulary or not terms:
        return None
    prompt = "Vocabulary terms: " + ", ".join(terms)
    if len(prompt) > _MAX_VOCABULARY_PROMPT_CHARS:
        # Truncate at the boundary; the diagnostic report records the cut.
        prompt = prompt[:_MAX_VOCABULARY_PROMPT_CHARS].rstrip(", ")
    return prompt


def vocabulary_report(
    *,
    terms: list[str],
    transcript_text: str,
    truncated: bool,
    provider_prompted: bool,
) -> dict[str, Any]:
    """Build the plan §9.4 vocabulary diagnostics block.

    Surfaces which requested terms actually appeared in the transcript, which
    likely did not, whether the prompt was truncated, and whether the terms
    were only usable in correction (provider could not accept a prompt).
    """

    lowered = transcript_text.lower()
    detected = [term for term in terms if term.lower() in lowered]
    missed = [term for term in terms if term.lower() not in lowered]
    return {
        "requested_count": len(terms),
        "detected": detected,
        "detected_count": len(detected),
        "likely_missed": missed,
        "prompt_truncated": truncated,
        "used_in_first_pass": provider_prompted,
    }
