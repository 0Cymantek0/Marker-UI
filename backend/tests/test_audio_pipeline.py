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

