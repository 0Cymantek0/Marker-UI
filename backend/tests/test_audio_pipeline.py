"""Tests for evidence-first audio pipeline helpers."""

from __future__ import annotations

from app.audio.pipeline import (
    build_multi_audio_document,
    normalize_transcript,
    render_enhanced_markdown,
)


def test_normalize_transcript_sorts_segments_tracks_words_and_risk() -> None:
    transcript = normalize_transcript(
        {
            "language": "en",
            "duration": 3.0,
            "model": "tiny.en",
            "media_info": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
            "segments": [
                {"start": 1.0, "end": 2.0, "text": "Should ship Marker today", "confidence": 0.9},
                {
                    "start": 0.5,
                    "end": 1.2,
                    "text": "What is LiteParse?",
                    "confidence": 0.4,
                    "words": [
                        {"word": "What", "start": 0.5, "end": 0.7, "confidence": 0.8},
                        {"word": "LiteParse", "start": 0.9, "end": 1.2, "confidence": 0.7},
                    ],
                },
            ],
        },
        source_label="meeting.wav",
        config={"audio_vocabulary": "Marker, LiteParse"},
    )

    assert [segment.text for segment in transcript.segments] == [
        "What is LiteParse?",
        "Should ship Marker today",
    ]
    assert transcript.segments[0].warnings == ("low_confidence",)
    assert transcript.segments[1].warnings == ("overlaps_previous",)
    assert transcript.segments[0].words[1]["word"] == "LiteParse"
    assert transcript.vocabulary_hits == ("Marker", "LiteParse")
    assert transcript.media_info["codec"] == "pcm_s16le"
    assert transcript.risk_summary["level"] == "review"
    assert transcript.risk_summary["word_count"] == 7


def test_enhanced_markdown_builds_extractively_with_source_refs() -> None:
    transcript = normalize_transcript(
        {
            "language": "en",
            "duration": 2.0,
            "model": "tiny.en",
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "Should ship the parser fix", "confidence": 0.9},
                {"start": 1.0, "end": 2.0, "text": "What is next?", "confidence": 0.9},
            ],
        },
        source_label="call.wav",
    )

    text = render_enhanced_markdown(transcript, title="call", template="meeting_notes")

    assert "## Extractive Summary" in text
    assert "## Actions And Decisions" in text
    assert "Should ship the parser fix [call.wav 00:00.000-00:01.000 speaker_0 | `call_seg_0001`]" in text
    assert "## Questions" in text
    assert "What is next? [call.wav 00:01.000-00:02.000 speaker_0 | `call_seg_0002`]" in text
    assert "## Original Transcript" in text


def test_multi_audio_builder_reports_relationship_evidence_and_batch_risk() -> None:
    one = normalize_transcript(
        {
            "duration": 1.0,
            "segments": [{"start": 0.0, "end": 1.0, "text": "project alpha kickoff", "confidence": 0.9}],
        },
        source_label="part1.wav",
    )
    two = normalize_transcript(
        {
            "duration": 1.0,
            "segments": [{"start": 0.0, "end": 1.0, "text": "project alpha follow up", "confidence": 0.9}],
        },
        source_label="part2.wav",
    )

    text, metadata = build_multi_audio_document([one, two], title="Batch")

    assert "## Batch Briefs" in text
    assert "Relationship evidence" in text
    assert metadata["relationship"]["pairwise_overlap"][0]["left"] == "part1.wav"
    assert metadata["relationship"]["label"] == "related_or_follow_up"
    assert metadata["risk_summary"]["source_count"] == 2
    assert metadata["risk_summary"]["total_words"] == 7


def test_risk_summary_reports_overlap_long_gap_unknown_confidence_and_review_verdict() -> None:
    """Plan §8.3 quality block: overlap/gap/unknown-confidence counts + review_required.

    A segment that overlaps the previous one, a long gap before another, and a
    segment with no confidence must each be counted, and the transcript must be
    flagged for review even though none is individually low-confidence.
    """

    transcript = normalize_transcript(
        {
            "duration": 45.0,
            "segments": [
                # 0.0–1.0, confident — baseline.
                {"start": 0.0, "end": 1.0, "text": "first segment here", "confidence": 0.9},
                # 0.5–1.5 overlaps the previous segment → overlaps_previous warning.
                {"start": 0.5, "end": 1.5, "text": "overlapping talk", "confidence": 0.85},
                # 40.0–41.0, jump from 1.5→40.0 (>30s default) → long_gap_before_segment.
                {"start": 40.0, "end": 41.0, "text": "after a long silence", "confidence": 0.8},
                # 41.0–42.0, no confidence → unknown_confidence_count.
                {"start": 41.0, "end": 42.0, "text": "unsure here", "confidence": None},
            ],
        },
        source_label="quality.wav",
    )

    risk = transcript.risk_summary
    assert risk["overlap_count"] == 1
    assert risk["long_gap_count"] == 1
    assert risk["unknown_confidence_count"] == 1
    assert risk["low_confidence_count"] == 0
    assert risk["review_required"] is True


def test_risk_summary_clean_transcript_needs_no_review() -> None:
    """A confident, gap-free, single-speaker transcript is not flagged for review."""

    transcript = normalize_transcript(
        {
            "duration": 2.0,
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "clear speech one", "confidence": 0.95},
                {"start": 1.0, "end": 2.0, "text": "clear speech two", "confidence": 0.93},
            ],
        },
        source_label="clean.wav",
    )

    risk = transcript.risk_summary
    assert risk["overlap_count"] == 0
    assert risk["long_gap_count"] == 0
    assert risk["unknown_confidence_count"] == 0
    assert risk["review_required"] is False


def test_transcribe_audio_file_normalizes_and_applies_speaker_aliases() -> None:
    """The shared video/audio transcription seam returns a normalized transcript.

    Video demuxes audio then calls this helper; it must resolve the provider,
    transcribe through the adapter, normalize, and fold in speaker aliases —
    without the caller touching the converter or provider registry directly.
    """

    from app.audio.transcribe import transcribe_audio_file

    raw_payload = {
        "language": "en",
        "duration": 2.0,
        "model": "tiny.en",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "hello world", "confidence": 0.9, "speaker": "speaker_0"},
            {"start": 1.0, "end": 2.0, "text": "second segment", "confidence": 0.85, "speaker": "speaker_1"},
        ],
    }

    class _StubProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            from app.audio.providers.base import RawTranscript

            return RawTranscript.from_provider_dict(raw_payload)

    import app.audio.transcribe as transcribe_mod

    original_build = transcribe_mod.build_provider
    transcribe_mod.build_provider = lambda _pid: _StubProvider()
    try:
        transcript = transcribe_audio_file(
            "/tmp/fake.wav",
            {"audio_speaker_aliases": {"speaker_0": "Alice"}},
            source_label="video.mp4",
            source_id="video_audio",
        )
    finally:
        transcribe_mod.build_provider = original_build

    assert transcript.segments[0].speaker == "Alice"
    assert transcript.segments[1].speaker == "speaker_1"
    assert transcript.source_id == "video_audio"
    assert transcript.segments[0].confidence == 0.9

