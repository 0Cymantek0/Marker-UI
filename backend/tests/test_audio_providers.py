from __future__ import annotations

import pytest

from app.audio.providers.capabilities import (
    ProviderCapability,
    capabilities_payload,
    get_capability,
    list_capabilities,
)
from app.audio.providers.registry import validate_provider_selection
from app.audio.pipeline import normalize_transcript
from app.audio.speakers import (
    apply_speaker_aliases,
    speaker_timeline,
    summarize_speakers,
)
from app.audio.vocabulary import (
    compile_vocabulary_prompt,
    parse_terms,
    resolve_vocabulary_terms,
    vocabulary_report,
)


def test_get_capability_returns_local_default_for_unknown_provider() -> None:
    cap = get_capability("nonexistent")
    assert cap.provider_id == "local_faster_whisper"


def test_all_providers_in_capability_matrix() -> None:
    caps = list_capabilities()
    assert len(caps) >= 7
    ids = {c.provider_id for c in caps}
    for expected in (
        "local_faster_whisper",
        "local_whisperx",
        "openai",
        "groq",
        "deepgram",
        "assemblyai",
        "azure",
        "custom_openai_compatible",
    ):
        assert expected in ids


def test_capabilities_payload_serializes_all_fields() -> None:
    payload = capabilities_payload()
    assert isinstance(payload, list)
    assert len(payload) >= 7
    for entry in payload:
        for key in (
            "provider_id",
            "implementation_state",
            "cloud",
            "supports_diarization",
            "supports_word_timestamps",
            "requires_api_key",
        ):
            assert key in entry
    by_id = {entry["provider_id"]: entry for entry in payload}
    assert by_id["local_faster_whisper"]["implementation_state"] == "implemented"
    assert by_id["openai"]["implementation_state"] == "deferred"
    assert by_id["openai"]["available"] is False


def test_validate_provider_selection_rejects_deferred_provider() -> None:
    with pytest.raises(NotImplementedError, match="not shipped yet"):
        validate_provider_selection("openai", allow_cloud_stt=True)


def test_cloud_providers_require_api_key() -> None:
    for cap in list_capabilities():
        if cap.cloud:
            assert cap.requires_api_key, f"{cap.provider_id} cloud=True but requires_api_key=False"


def test_parse_terms_deduplicates_case_insensitively() -> None:
    result = parse_terms("Hello, hello, HELLO")
    assert result == ["Hello"]


def test_parse_terms_from_string_and_list() -> None:
    from_string = parse_terms("a,b,c")
    from_list = parse_terms(["a", "b", "c"])
    assert from_string == ["a", "b", "c"]
    assert from_list == ["a", "b", "c"]


def test_resolve_vocabulary_terms_merges_field_and_packs() -> None:
    config = {
        "audio_vocabulary": "A,B",
        "audio_vocabulary_packs": [["C", "D"]],
    }
    result = resolve_vocabulary_terms(config)
    assert result == ["A", "B", "C", "D"]


def test_compile_vocabulary_prompt_respects_capability() -> None:
    no_vocab_cap = ProviderCapability(
        provider_id="test",
        provider_label="Test",
        runtime_type="local",
        cloud=False,
        requires_api_key=False,
        requires_model_license_acceptance=False,
        privacy_level="local",
        supports_custom_vocabulary=False,
    )
    result = compile_vocabulary_prompt(terms=["foo", "bar"], capability=no_vocab_cap)
    assert result is None


def test_vocabulary_report_detects_and_misses() -> None:
    report = vocabulary_report(
        terms=["Marker", "LiteParse", "Phantom"],
        transcript_text="We built Marker and tested LiteParse thoroughly",
        truncated=False,
        provider_prompted=True,
    )
    assert "Marker" in report["detected"]
    assert "LiteParse" in report["detected"]
    assert "Phantom" in report["likely_missed"]
    assert report["detected_count"] == 2
    assert report["requested_count"] == 3


def _make_transcript(speakers: list[str]) -> object:
    segments = []
    for i, spk in enumerate(speakers):
        segments.append(
            {"start": float(i), "end": float(i + 1), "text": f"seg {i}", "confidence": 0.9, "speaker": spk}
        )
    return normalize_transcript(
        {"duration": float(len(speakers)), "segments": segments},
        source_label="test.wav",
    )


def test_apply_speaker_aliases_renames_only_mapped_speakers() -> None:
    transcript = _make_transcript(["speaker_0", "speaker_1", "speaker_2"])
    aliased = apply_speaker_aliases(transcript, {"speaker_0": "Alice"})
    labels = [s.speaker for s in aliased.segments]
    assert labels == ["Alice", "speaker_1", "speaker_2"]


def test_apply_speaker_aliases_with_empty_or_none_is_noop() -> None:
    transcript = _make_transcript(["speaker_0", "speaker_1"])
    assert apply_speaker_aliases(transcript, None) is transcript
    assert apply_speaker_aliases(transcript, {}) is transcript


def test_speaker_timeline_groups_by_speaker() -> None:
    transcript = _make_transcript(["speaker_0", "speaker_1", "speaker_0"])
    timeline = speaker_timeline(transcript)
    by_speaker = {entry["speaker"]: entry for entry in timeline}
    assert by_speaker["speaker_0"]["segment_count"] == 2
    assert by_speaker["speaker_1"]["segment_count"] == 1


def test_summarize_speakers_counts_unique() -> None:
    transcript = _make_transcript(["speaker_0", "speaker_1", "speaker_0", "speaker_2"])
    summary = summarize_speakers(transcript)
    assert summary["count"] == 3
    assert sorted(summary["labels"]) == ["speaker_0", "speaker_1", "speaker_2"]
