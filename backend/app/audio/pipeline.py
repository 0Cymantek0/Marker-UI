"""Evidence-first audio transcript assembly.

This module does not call STT providers. It normalizes raw provider output into
auditable segment records, renders transcript Markdown, and builds deterministic
single/multi-audio documents with source references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_WORDS_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_NEGATION_WORDS = frozenset({
    "no",
    "not",
    "never",
    "cannot",
    "cant",
    "won",
    "wont",
    "didn",
    "didnt",
    "isn",
    "isnt",
    "aren",
    "arent",
    "false",
    "reject",
    "rejected",
    "blocked",
    "failed",
    "cancelled",
    "canceled",
})
_AFFIRMATION_WORDS = frozenset({
    "yes",
    "true",
    "can",
    "will",
    "is",
    "are",
    "approved",
    "approve",
    "accepted",
    "accept",
    "passed",
    "pass",
    "complete",
    "completed",
    "ready",
})
_STOPWORDS = frozenset({
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
    "you",
})


@dataclass(frozen=True)
class AudioSegment:
    segment_id: str
    source_id: str
    source_label: str
    start_ms: int
    end_ms: int
    text: str
    speaker: str = "speaker_0"
    confidence: float | None = None
    confidence_source: str | None = None
    speaker_confidence: float | None = None
    no_speech_probability: float | None = None
    avg_logprob: float | None = None
    compression_ratio: float | None = None
    overlap_warning: bool = False
    warnings: tuple[str, ...] = ()
    words: tuple[dict[str, Any], ...] = ()

    @property
    def start_s(self) -> float:
        return self.start_ms / 1000.0

    @property
    def end_s(self) -> float:
        return self.end_ms / 1000.0

    def source_ref(self) -> str:
        return f"{self.source_label} {format_timestamp_ms(self.start_ms)}-{format_timestamp_ms(self.end_ms)} {self.speaker}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "speaker": self.speaker,
            "text": self.text,
            "confidence": self.confidence,
            "confidence_source": self.confidence_source,
            "speaker_confidence": self.speaker_confidence,
            "no_speech_probability": self.no_speech_probability,
            "avg_logprob": self.avg_logprob,
            "compression_ratio": self.compression_ratio,
            "overlap_warning": self.overlap_warning,
            "warnings": list(self.warnings),
            "words": [dict(word) for word in self.words],
        }


@dataclass(frozen=True)
class AudioTranscript:
    source_id: str
    source_label: str
    language: str | None
    duration_ms: int
    model: str | None
    provider: str
    segments: tuple[AudioSegment, ...]
    warnings: tuple[str, ...] = ()
    risk_summary: dict[str, Any] = field(default_factory=dict)
    media_info: dict[str, Any] = field(default_factory=dict)
    vocabulary_hits: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_label": self.source_label,
            "language": self.language,
            "duration_ms": self.duration_ms,
            "model": self.model,
            "provider": self.provider,
            "segments": [segment.to_dict() for segment in self.segments],
            "warnings": list(self.warnings),
            "risk_summary": dict(self.risk_summary),
            "media_info": dict(self.media_info),
            "vocabulary_hits": list(self.vocabulary_hits),
        }


def slug_source_id(value: str) -> str:
    stem = Path(value.replace("\\", "/")).stem or "audio"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").lower()
    return slug or "audio"


def format_timestamp_ms(ms: int) -> str:
    ms = max(0, int(ms))
    total_seconds, remainder = divmod(ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{remainder:03d}"
    return f"{minutes:02d}:{seconds:02d}.{remainder:03d}"


def normalize_transcript(
    raw: dict[str, Any],
    *,
    source_label: str,
    source_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> AudioTranscript:
    config = config or {}
    source_id = source_id or slug_source_id(source_label)
    threshold = float(config.get("audio_low_confidence_threshold", 0.65))
    language = raw.get("language")
    duration_ms = int(round(float(raw.get("duration") or 0.0) * 1000))
    warnings: list[str] = _normalize_warning_list(raw.get("warnings"))
    segments: list[AudioSegment] = []
    previous_end = 0
    raw_segments = sorted(
        [item for item in raw.get("segments") or [] if isinstance(item, dict)],
        key=lambda item: float(item.get("start") or 0.0),
    )
    for index, item in enumerate(raw_segments, start=1):
        start_ms = max(0, int(round(float(item.get("start") or 0.0) * 1000)))
        end_ms = max(start_ms, int(round(float(item.get("end") or 0.0) * 1000)))
        text = str(item.get("text") or "").strip()
        confidence = _coerce_confidence(item.get("confidence"))
        no_speech_probability = _coerce_confidence(item.get("no_speech_prob"))
        avg_logprob = _coerce_float(item.get("avg_logprob"))
        compression_ratio = _coerce_float(item.get("compression_ratio"))
        overlap_warning = bool(item.get("overlap_warning"))
        segment_warnings: list[str] = []
        if not text:
            segment_warnings.append("empty_text")
        if end_ms <= start_ms:
            segment_warnings.append("zero_duration")
        if confidence is not None and confidence < threshold:
            segment_warnings.append("low_confidence")
        if start_ms < previous_end:
            segment_warnings.append("overlaps_previous")
        if overlap_warning and "overlaps_previous" not in segment_warnings:
            segment_warnings.append("overlap_warning")
        if start_ms > previous_end + int(config.get("audio_gap_warning_ms", 30_000)):
            segment_warnings.append("long_gap_before_segment")
        if no_speech_probability is not None and no_speech_probability >= float(
            config.get("audio_no_speech_warning_threshold", 0.6)
        ):
            segment_warnings.append("high_no_speech_probability")
        if avg_logprob is not None and avg_logprob <= float(config.get("audio_avg_logprob_warning_threshold", -1.0)):
            segment_warnings.append("low_avg_logprob")
        if compression_ratio is not None and compression_ratio >= float(
            config.get("audio_compression_ratio_warning_threshold", 2.4)
        ):
            segment_warnings.append("high_compression_ratio")
        previous_end = max(previous_end, end_ms)
        segments.append(
            AudioSegment(
                segment_id=f"{source_id}_seg_{index:04d}",
                source_id=source_id,
                source_label=source_label,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                speaker=str(item.get("speaker") or "speaker_0"),
                confidence=confidence,
                confidence_source=_confidence_source(item, confidence),
                speaker_confidence=_coerce_confidence(item.get("speaker_confidence")),
                no_speech_probability=no_speech_probability,
                avg_logprob=avg_logprob,
                compression_ratio=compression_ratio,
                overlap_warning=overlap_warning,
                warnings=tuple(segment_warnings),
                words=tuple(_normalize_words(item.get("words"))),
            )
        )
    if not segments:
        warnings.append("no_speech_segments")
    expected_language = config.get("audio_language") or config.get("lang")
    if expected_language and language and str(expected_language).split("-")[0] != str(language).split("-")[0]:
        warnings.append("language_mismatch")
    media_info = dict(raw.get("media_info") or {})
    vocabulary_hits = tuple(_vocabulary_hits(segments, config))
    risk_summary = _risk_summary(segments, warnings, config=config)
    return AudioTranscript(
        source_id=source_id,
        source_label=source_label,
        language=str(language) if language else None,
        duration_ms=duration_ms,
        model=raw.get("model"),
        provider=str(raw.get("provider") or "local_faster_whisper"),
        segments=tuple(segments),
        warnings=tuple(warnings),
        risk_summary=risk_summary,
        media_info=media_info,
        vocabulary_hits=vocabulary_hits,
    )


def render_transcript_markdown(transcript: AudioTranscript, *, title: str) -> str:
    lines = [f"# Audio Transcript: {title}", ""]
    if transcript.language:
        lines.append(f"- **Language:** {transcript.language}")
    lines.append(f"- **Duration:** {format_timestamp_ms(transcript.duration_ms)}")
    if transcript.model:
        lines.append(f"- **Model:** {transcript.model}")
    if transcript.media_info:
        media_bits = _media_info_line(transcript.media_info)
        if media_bits:
            lines.append(f"- **Media:** {media_bits}")
    if transcript.vocabulary_hits:
        lines.append(f"- **Vocabulary hits:** {', '.join(transcript.vocabulary_hits)}")
    if transcript.warnings:
        lines.append(f"- **Warnings:** {', '.join(transcript.warnings)}")
    lines.extend(["", "## Transcript", ""])
    if not transcript.segments:
        lines.append("_No speech segments detected._")
    for segment in transcript.segments:
        line = (
            f"- `{format_timestamp_ms(segment.start_ms)}-{format_timestamp_ms(segment.end_ms)}` "
            f"{segment.text} _({segment.segment_id}, {segment.speaker})_"
        )
        if segment.confidence is not None:
            line += f" confidence={segment.confidence:.2f}"
        if segment.warnings:
            line += f" warnings={','.join(segment.warnings)}"
        lines.append(line)
    lines.extend(["", "## Source Map", ""])
    if not transcript.segments:
        lines.append("- No source segments.")
    for segment in transcript.segments:
        lines.append(f"- `{segment.segment_id}` -> {segment.source_ref()}")
    if transcript.risk_summary:
        lines.extend(["", "## Audio Quality Warnings", ""])
        for key, value in transcript.risk_summary.items():
            lines.append(f"- **{key}:** {value}")
    return "\n".join(lines).strip()


def append_contradiction_section(text: str, contradictions: list[dict[str, Any]]) -> str:
    """Append conservative possible-contradiction findings with source refs."""

    if not contradictions:
        return text
    lines = [text.rstrip(), "", "## Possible Contradictions", ""]
    for item in contradictions:
        left = item["left"]
        right = item["right"]
        terms = ", ".join(item.get("shared_terms") or [])
        lines.append(
            "- Opposing polarity with shared terms"
            f"{f' ({terms})' if terms else ''}: "
            f"`{left['segment_id']}` {left['text']} [{left['source_ref']}] vs "
            f"`{right['segment_id']}` {right['text']} [{right['source_ref']}]"
        )
    return "\n".join(lines).strip()


def render_enhanced_markdown(
    transcript: AudioTranscript,
    *,
    title: str,
    template: str = "structured_notes",
    context: str | None = None,
) -> str:
    lines = [f"# Audio Document: {title}", ""]
    lines.append(f"- **Mode:** local deterministic {template}")
    lines.append("- **Rule:** every note below is copied from transcript text and carries source provenance.")
    if context:
        lines.append("- **Context trust:** organization-only; transcript evidence wins conflicts")
    if transcript.vocabulary_hits:
        lines.append(f"- **Vocabulary hits:** {', '.join(transcript.vocabulary_hits)}")
    notes = build_extractive_notes(transcript)
    lines.extend(["", "## Extractive Summary", ""])
    if notes["summary"]:
        for item in notes["summary"]:
            lines.append(item)
    else:
        lines.append("_No speech segments detected._")
    lines.extend(["", "## Key Points", ""])
    if notes["key_points"]:
        lines.extend(notes["key_points"])
    else:
        lines.append("_No key points found._")
    lines.extend(["", "## Actions And Decisions", ""])
    if notes["actions"]:
        lines.extend(notes["actions"])
    else:
        lines.append("_No explicit action or decision language detected._")
    lines.extend(["", "## Questions", ""])
    if notes["questions"]:
        lines.extend(notes["questions"])
    else:
        lines.append("_No explicit questions detected._")
    lines.extend(["", "## Evidence-First Notes", ""])
    if not transcript.segments:
        lines.append("_No speech segments detected._")
    for segment in transcript.segments:
        if not segment.text:
            continue
        lines.append(f"- {segment.text} [{segment.source_ref()} | `{segment.segment_id}`]")
    lines.extend(["", "## Source Map", ""])
    for segment in transcript.segments:
        lines.append(f"- `{segment.segment_id}` -> {segment.source_ref()}")
    if transcript.risk_summary:
        lines.extend(["", "## Diagnostics", ""])
        for key, value in transcript.risk_summary.items():
            lines.append(f"- **{key}:** {value}")
    lines.extend(["", "## Original Transcript", ""])
    lines.append(render_transcript_markdown(transcript, title=title))
    return "\n".join(lines).strip()


def render_text_enhanced_markdown(
    transcript: AudioTranscript,
    *,
    title: str,
    strength: int = 1,
) -> str:
    """Render a corrected transcript without changing timeline shape.

    This is deliberately conservative. It performs only deterministic readability
    cleanup, keeps every segment/source id, and includes a raw transcript appendix
    plus an audit table for changed segments.
    """

    lines = [f"# Enhanced Transcript: {title}", ""]
    lines.append("- **Mode:** local deterministic transcript cleanup")
    lines.append("- **Rule:** segment order, timestamps, speakers, and source IDs are preserved.")
    if strength > 1:
        lines.append(
            "- **Scope:** strengths above minimal are not LLM-backed in this build; using deterministic cleanup only."
        )
    lines.extend(["", "## Transcript", ""])
    audit_rows: list[tuple[AudioSegment, str, str]] = []
    if not transcript.segments:
        lines.append("_No speech segments detected._")
    for segment in transcript.segments:
        enhanced = _minimal_text_cleanup(segment.text)
        if enhanced != segment.text:
            audit_rows.append((segment, segment.text, enhanced))
        line = (
            f"- `{format_timestamp_ms(segment.start_ms)}-{format_timestamp_ms(segment.end_ms)}` "
            f"{enhanced} _({segment.segment_id}, {segment.speaker})_"
        )
        line += f" [{segment.source_ref()} | `{segment.segment_id}`]"
        if segment.confidence is not None:
            line += f" confidence={segment.confidence:.2f}"
        if segment.warnings:
            line += f" warnings={','.join(segment.warnings)}"
        lines.append(line)
    lines.extend(["", "## Enhancement Audit", ""])
    if audit_rows:
        lines.append("| Segment | Change Type | Raw | Enhanced | Review |")
        lines.append("|---|---|---|---|---|")
        for segment, raw, enhanced in audit_rows:
            review = "yes" if segment.warnings else "no"
            lines.append(
                f"| `{segment.segment_id}` | deterministic cleanup | "
                f"{_table_cell(raw)} | {_table_cell(enhanced)} | {review} |"
            )
    else:
        lines.append("- No text changes made.")
    lines.extend(["", "## Source Map", ""])
    for segment in transcript.segments:
        lines.append(f"- `{segment.segment_id}` -> {segment.source_ref()}")
    lines.extend(["", "## Original Transcript", ""])
    lines.append(render_transcript_markdown(transcript, title=title))
    return "\n".join(lines).strip()


def build_extractive_notes(transcript: AudioTranscript) -> dict[str, list[str]]:
    """Build conservative notes using only transcript text plus citations."""
    non_empty = [segment for segment in transcript.segments if segment.text]
    summary = [
        f"- {segment.text} [{segment.source_ref()} | `{segment.segment_id}`]"
        for segment in non_empty[:3]
    ]
    key_points = [
        f"- {segment.text} [{segment.source_ref()} | `{segment.segment_id}`]"
        for segment in non_empty
    ]
    actions = [
        f"- {segment.text} [{segment.source_ref()} | `{segment.segment_id}`]"
        for segment in non_empty
        if _looks_like_action_or_decision(segment.text)
    ]
    questions = [
        f"- {segment.text} [{segment.source_ref()} | `{segment.segment_id}`]"
        for segment in non_empty
        if _looks_like_question(segment.text)
    ]
    return {
        "summary": summary,
        "key_points": key_points,
        "actions": actions,
        "questions": questions,
    }


def detect_possible_contradictions(transcript: AudioTranscript) -> list[dict[str, Any]]:
    """Find obvious opposing claims without pretending to prove truth.

    The detector is intentionally conservative: it only compares segments that
    have explicit positive/negative polarity and at least two shared meaningful
    terms. The output is a review queue, not an auto-resolution.
    """

    candidates = [
        {
            "segment": segment,
            "tokens": _meaningful_tokens(segment.text),
            "polarity": _segment_polarity(segment.text),
        }
        for segment in transcript.segments
        if segment.text
    ]
    findings: list[dict[str, Any]] = []
    for left_index, left in enumerate(candidates):
        if left["polarity"] == "neutral":
            continue
        for right in candidates[left_index + 1 :]:
            if right["polarity"] == "neutral" or left["polarity"] == right["polarity"]:
                continue
            shared = sorted(left["tokens"] & right["tokens"])
            if len(shared) < 2:
                continue
            findings.append(
                {
                    "type": "opposing_polarity_shared_terms",
                    "shared_terms": shared[:8],
                    "left": _contradiction_segment_payload(left["segment"]),
                    "right": _contradiction_segment_payload(right["segment"]),
                }
            )
    return findings


def _segment_polarity(text: str) -> str:
    tokens = set(_WORDS_RE.findall(text.lower()))
    has_negative = bool(tokens & _NEGATION_WORDS)
    has_positive = bool(tokens & _AFFIRMATION_WORDS)
    if has_negative and not has_positive:
        return "negative"
    if has_positive and not has_negative:
        return "positive"
    if has_negative and has_positive:
        # "not approved" should stay negative.
        return "negative"
    return "neutral"


def _meaningful_tokens(text: str) -> set[str]:
    return {
        token
        for token in _WORDS_RE.findall(text.lower())
        if (
            len(token) > 2
            and token not in _STOPWORDS
            and token not in _NEGATION_WORDS
            and token not in _AFFIRMATION_WORDS
        )
    }


def _contradiction_segment_payload(segment: AudioSegment) -> dict[str, Any]:
    return {
        "segment_id": segment.segment_id,
        "source_ref": segment.source_ref(),
        "speaker": segment.speaker,
        "text": segment.text,
    }


def build_multi_audio_document(
    transcripts: list[AudioTranscript],
    *,
    title: str = "Multi-Audio Document",
    context: str | None = None,
) -> tuple[str, dict[str, Any]]:
    relationship = classify_audio_relationship(transcripts)
    lines = [f"# {title}", ""]
    lines.append(f"- **Relationship:** {relationship['label']}")
    lines.append(f"- **Merge strategy:** {relationship['strategy']}")
    if relationship.get("evidence"):
        lines.append(f"- **Relationship evidence:** {relationship['evidence']}")
    if context:
        lines.append("- **Context trust:** organization-only; transcript evidence wins conflicts")
    lines.extend(["", "## Batch Briefs", ""])
    for transcript in transcripts:
        first = next((segment for segment in transcript.segments if segment.text), None)
        if first:
            lines.append(f"- **{transcript.source_label}:** {first.text} [{first.source_ref()} | `{first.segment_id}`]")
        else:
            lines.append(f"- **{transcript.source_label}:** no speech detected")
    lines.extend(["", "## Timeline", ""])
    for transcript in transcripts:
        lines.append(f"### {transcript.source_label}")
        if transcript.language:
            lines.append(f"- **Language:** {transcript.language}")
        lines.append(f"- **Duration:** {format_timestamp_ms(transcript.duration_ms)}")
        if transcript.risk_summary:
            lines.append(f"- **Risk:** {transcript.risk_summary.get('level', 'unknown')}")
        for segment in transcript.segments:
            if segment.text:
                lines.append(f"- {segment.text} [{segment.source_ref()} | `{segment.segment_id}`]")
        lines.append("")
    lines.extend(["## Source Map", ""])
    for transcript in transcripts:
        for segment in transcript.segments:
            lines.append(f"- `{segment.segment_id}` -> {segment.source_ref()}")
    lines.extend(["", "## Transcript Appendices", ""])
    for transcript in transcripts:
        lines.append(f"### {transcript.source_label}")
        for segment in transcript.segments:
            lines.append(
                f"- `{format_timestamp_ms(segment.start_ms)}-{format_timestamp_ms(segment.end_ms)}` "
                f"{segment.text} _({segment.segment_id})_"
            )
        lines.append("")
    metadata = {
        "relationship": relationship,
        "source_count": len(transcripts),
        "segment_count": sum(len(transcript.segments) for transcript in transcripts),
        "risk_summary": _batch_risk_summary(transcripts),
        "sources": [transcript.to_dict() for transcript in transcripts],
    }
    return "\n".join(lines).strip(), metadata


def classify_audio_relationship(transcripts: list[AudioTranscript]) -> dict[str, Any]:
    if len(transcripts) <= 1:
        return {"label": "single_audio", "strategy": "single_transcript", "overlap": 1.0, "pairwise_overlap": []}
    texts = [" ".join(segment.text for segment in transcript.segments) for transcript in transcripts]
    pairwise = []
    for left in range(len(texts)):
        for right in range(left + 1, len(texts)):
            overlap = _word_overlap(texts[left], texts[right])
            pairwise.append(
                {
                    "left": transcripts[left].source_label,
                    "right": transcripts[right].source_label,
                    "overlap": round(overlap, 6),
                }
            )
    max_overlap = max((item["overlap"] for item in pairwise), default=0.0)
    if max_overlap >= 0.8:
        label = "duplicate_or_retake"
        strategy = "keep_separate_with_duplicate_warning"
    elif max_overlap >= 0.25:
        label = "related_or_follow_up"
        strategy = "merged_chronological_with_source_refs"
    else:
        label = "unrelated_or_low_overlap"
        strategy = "separate_sections_no_forced_narrative"
    evidence = f"max pairwise word overlap {max_overlap:.2f}"
    return {
        "label": label,
        "strategy": strategy,
        "overlap": round(max_overlap, 6),
        "pairwise_overlap": pairwise,
        "evidence": evidence,
    }


def _normalize_words(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    words: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        words.append(
            {
                "word": word,
                "punctuated_word": str(item.get("punctuated_word") or word).strip(),
                "start_ms": int(round(float(item.get("start") or 0.0) * 1000)),
                "end_ms": int(round(float(item.get("end") or 0.0) * 1000)),
                "confidence": _coerce_confidence(item.get("confidence")),
                "speaker": str(item["speaker"]) if item.get("speaker") is not None else None,
                "speaker_confidence": _coerce_confidence(item.get("speaker_confidence")),
            }
        )
    return words


def _normalize_warning_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(warning) for warning in value if warning]


def _confidence_source(item: dict[str, Any], confidence: float | None) -> str | None:
    explicit = item.get("confidence_source")
    if explicit:
        return str(explicit)
    if confidence is None:
        return None
    if item.get("no_speech_prob") is not None:
        return "no_speech_probability"
    if item.get("avg_logprob") is not None:
        return "avg_logprob"
    return "provider"


def _coerce_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _risk_summary(
    segments: list[AudioSegment],
    warnings: list[str],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or {}
    segment_warning_count = sum(1 for segment in segments if segment.warnings)
    low_confidence_count = sum(1 for segment in segments if "low_confidence" in segment.warnings)
    empty_count = sum(1 for segment in segments if "empty_text" in segment.warnings)
    overlap_count = sum(
        1
        for segment in segments
        if "overlaps_previous" in segment.warnings or "overlap_warning" in segment.warnings
    )
    long_gap_count = sum(1 for segment in segments if "long_gap_before_segment" in segment.warnings)
    no_speech_probability_flags = sum(
        1 for segment in segments if "high_no_speech_probability" in segment.warnings
    )
    avg_logprob_flags = sum(1 for segment in segments if "low_avg_logprob" in segment.warnings)
    compression_ratio_flags = sum(1 for segment in segments if "high_compression_ratio" in segment.warnings)
    if not segments:
        level = "no_speech"
    elif (
        warnings
        or low_confidence_count
        or empty_count
        or no_speech_probability_flags
        or avg_logprob_flags
        or compression_ratio_flags
    ):
        level = "review"
    else:
        level = "clean"
    confidence_values = [segment.confidence for segment in segments if segment.confidence is not None]
    unknown_confidence_count = sum(1 for segment in segments if segment.confidence is None)
    word_count = sum(len(_WORDS_RE.findall(segment.text)) for segment in segments)
    speech_ms = sum(max(0, segment.end_ms - segment.start_ms) for segment in segments)
    duration_ms = max((segment.end_ms for segment in segments), default=0)
    mean_confidence = (
        round(sum(confidence_values) / len(confidence_values), 6)
        if confidence_values
        else None
    )
    # Review verdict (plan §8.2): serious timing/provider anomalies always need
    # eyes. Low confidence can be advisory-only unless the user enables the
    # stricter "require review" toggle exposed in CLI/API/UI.
    low_confidence_requires_review = _truthy(config.get("audio_review_required_on_low_confidence"))
    review_required = bool(
        (low_confidence_count and low_confidence_requires_review)
        or overlap_count
        or long_gap_count
        or empty_count
        or no_speech_probability_flags
        or avg_logprob_flags
        or compression_ratio_flags
        or (unknown_confidence_count and unknown_confidence_count > len(segments) // 2)
    )
    return {
        "level": level,
        "segment_warning_count": segment_warning_count,
        "low_confidence_count": low_confidence_count,
        "unknown_confidence_count": unknown_confidence_count,
        "empty_segment_count": empty_count,
        "overlap_count": overlap_count,
        "long_gap_count": long_gap_count,
        "no_speech_probability_flags": no_speech_probability_flags,
        "avg_logprob_flags": avg_logprob_flags,
        "compression_ratio_flags": compression_ratio_flags,
        "provider_warning_count": len(warnings),
        "mean_confidence": mean_confidence,
        "word_count": word_count,
        "speech_seconds": round(speech_ms / 1000.0, 3),
        "words_per_minute": round(word_count / (speech_ms / 60000.0), 3) if speech_ms else None,
        "speech_coverage": round(speech_ms / duration_ms, 6) if duration_ms else None,
        "review_required": review_required,
        "low_confidence_requires_review": low_confidence_requires_review,
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _batch_risk_summary(transcripts: list[AudioTranscript]) -> dict[str, Any]:
    source_levels = [str(transcript.risk_summary.get("level") or "unknown") for transcript in transcripts]
    return {
        "source_count": len(transcripts),
        "review_sources": sum(1 for level in source_levels if level == "review"),
        "no_speech_sources": sum(1 for level in source_levels if level == "no_speech"),
        "total_segments": sum(len(transcript.segments) for transcript in transcripts),
        "total_words": sum(int(transcript.risk_summary.get("word_count") or 0) for transcript in transcripts),
    }


def _vocabulary_hits(segments: list[AudioSegment], config: dict[str, Any]) -> list[str]:
    terms = _parse_vocabulary(config.get("audio_vocabulary") or config.get("audio_vocabulary_terms"))
    text = " ".join(segment.text.lower() for segment in segments)
    return [term for term in terms if term.lower() in text]


def _parse_vocabulary(raw: Any) -> list[str]:
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
    return result[:100]


def _media_info_line(media_info: dict[str, Any]) -> str:
    parts = []
    if media_info.get("codec"):
        parts.append(str(media_info["codec"]))
    if media_info.get("sample_rate"):
        parts.append(f"{media_info['sample_rate']} Hz")
    if media_info.get("channels"):
        parts.append(f"{media_info['channels']} ch")
    if media_info.get("bit_rate"):
        parts.append(f"{media_info['bit_rate']} bps")
    return ", ".join(parts)


def _looks_like_action_or_decision(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "action",
        "assign",
        "decide",
        "decision",
        "follow up",
        "fix",
        "need to",
        "next step",
        "send",
        "ship",
        "should",
        "todo",
        "will",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_question(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped.endswith("?") or stripped.startswith(("who ", "what ", "when ", "where ", "why ", "how ", "can ", "could ", "should "))


def _minimal_text_cleanup(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return cleaned
    cleaned = cleaned[0].upper() + cleaned[1:]
    if not cleaned.endswith((".", "!", "?", ":", ";")):
        cleaned += "."
    return cleaned


def _table_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _word_overlap(left: str, right: str) -> float:
    left_words = set(_WORDS_RE.findall(left.lower()))
    right_words = set(_WORDS_RE.findall(right.lower()))
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)
