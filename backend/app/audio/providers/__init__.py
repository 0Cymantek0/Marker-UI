"""Speech-to-text provider adapter layer (plan §5.1, §5.3).

The plan's long-term direction is to evolve audio from "local transcription
works" into a first-class, multi-provider, evidence-preserving pipeline. We
earn the right to add cloud STT providers later by building the adapter seam
*now* and migrating the existing local faster-whisper path behind it.

``AudioTranscriptionProvider`` is a structural :class:`typing.Protocol`: any
object exposing ``transcribe(...) -> RawTranscript`` and an ``id`` satisfies it,
so adding OpenAI/Groq/Deepgram/AssemblyAI/Azure later means writing one adapter,
not touching the converter.

``PROVIDER_CAPABILITIES`` is the single source of truth for what each provider
supports. The UI consumes it via the capabilities endpoint so controls are
enabled/disabled adaptively rather than pretending every provider supports
diarization, word timestamps, or confidence.
"""

from app.audio.providers.base import (
    AudioTranscriptionProvider,
    RawSegment,
    RawTranscript,
    RawWord,
)
from app.audio.providers.capabilities import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_CAPABILITIES,
    ProviderCapability,
    capabilities_payload,
    cap_to_dict,
    get_capability,
    list_capabilities,
)
from app.audio.providers.registry import (
    all_advertised_provider_ids,
    available_provider_ids,
    build_provider,
)

__all__ = [
    "DEFAULT_PROVIDER_ID",
    "PROVIDER_CAPABILITIES",
    "AudioTranscriptionProvider",
    "ProviderCapability",
    "RawSegment",
    "RawTranscript",
    "RawWord",
    "all_advertised_provider_ids",
    "available_provider_ids",
    "build_provider",
    "capabilities_payload",
    "cap_to_dict",
    "get_capability",
    "list_capabilities",
]
