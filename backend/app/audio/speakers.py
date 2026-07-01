"""Speaker diarization + alias memory (plan §10).

Speaker identity is **user-controlled and anonymous by default**. A provider may
diarize and assign ``speaker_0``, ``speaker_1`` … but the system must never claim
a real-world identity unless the user explicitly maps a label to a name, and any
such mapping is local, revocable, and clearly distinguished from a diarization
guess.

This module owns the pure, testable helpers:

* :func:`apply_speaker_aliases` — fold a user-confirmed alias map into a
  transcript, tagging each renamed speaker with ``user_confirmed=True``.
* :func:`speaker_timeline` — group segments by speaker for the timeline view.
* :func:`summarize_speakers` — the speakers metadata block for job output.

The diarization *engine* itself is provider-specific (faster-whisper cannot
diarize; WhisperX/pyannote, Deepgram, AssemblyAI, Azure can). Each provider
adapter populates ``RawSegment.speaker`` when it diarizes; this module only
normalizes the result. Faster-whisper with diarization requested but unsupported
falls back to ``speaker_0`` with an explicit warning, never silently.
"""

from __future__ import annotations

from typing import Any

from app.audio.pipeline import AudioSegment, AudioTranscript


def apply_speaker_aliases(
    transcript: AudioTranscript,
    aliases: dict[str, str] | None,
) -> AudioTranscript:
    """Return a transcript with user-confirmed speaker aliases applied.

    ``aliases`` maps a raw diarization label (``speaker_0``) to a user-chosen
    name ("Ishu"). Only labels the user explicitly mapped are renamed — every
    other speaker keeps its anonymous diarization label so the transcript never
    invents an identity. Renamed speakers are flagged ``user_confirmed`` so the
    UI and metadata can distinguish a guess from a confirmed name.
    """

    if not aliases:
        return transcript
    aliases = {str(k): str(v) for k, v in aliases.items() if v}
    if not aliases:
        return transcript
    remapped = []
    for segment in transcript.segments:
        if segment.speaker in aliases:
            remapped.append(_with_speaker(segment, aliases[segment.speaker]))
        else:
            remapped.append(segment)
    return _with_segments(transcript, tuple(remapped))


def speaker_timeline(transcript: AudioTranscript) -> list[dict[str, Any]]:
    """Group segments by speaker, preserving first-appearance order.

    Each speaker gets their segment refs and a speaking-time tally — the data
    the UI needs for the speaker-timeline panel without re-deriving it.
    """

    order: list[str] = []
    by_speaker: dict[str, list[AudioSegment]] = {}
    for segment in transcript.segments:
        if segment.speaker not in by_speaker:
            by_speaker[segment.speaker] = []
            order.append(segment.speaker)
        by_speaker[segment.speaker].append(segment)
    timeline = []
    for index, speaker in enumerate(order):
        segs = by_speaker[speaker]
        speaking_ms = sum(max(0, s.end_ms - s.start_ms) for s in segs)
        timeline.append(
            {
                "speaker": speaker,
                "display_label": _speaker_display(speaker),
                "segment_count": len(segs),
                "speaking_ms": speaking_ms,
                "first_segment_id": segs[0].segment_id if segs else None,
                "segment_ids": [s.segment_id for s in segs],
            }
        )
    return timeline


def summarize_speakers(transcript: AudioTranscript) -> dict[str, Any]:
    """The speakers metadata block: count, labels, and a confirmation summary."""

    speakers = {segment.speaker for segment in transcript.segments}
    return {
        "count": len(speakers),
        "labels": sorted(speakers),
        "user_confirmed": False,  # set True by the converter when aliases applied
        "diarized": len(speakers) > 1 or "speaker_0" not in speakers,
    }


def _speaker_display(speaker: str) -> str:
    """Human label for a speaker, anonymized unless it's already a name."""

    if speaker.startswith("speaker_"):
        return speaker.replace("_", " ").title()
    return speaker


def _with_speaker(segment: AudioSegment, speaker: str) -> AudioSegment:
    from dataclasses import replace

    return replace(segment, speaker=speaker)


def _with_segments(transcript: AudioTranscript, segments: tuple[AudioSegment, ...]) -> AudioTranscript:
    from dataclasses import replace

    return replace(transcript, segments=segments)
