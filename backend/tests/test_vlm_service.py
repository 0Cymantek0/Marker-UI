"""Unit tests for VLMService (Phase 1, plan §6.5 + §6.10).

All tests use ``unittest.mock.MagicMock`` for the ``http_client`` parameter —
NO real VLM network calls are made. Behavior assertions exercise real code
paths through ``VLMService.classify`` / ``VLMService.extract`` and the module-
level ``validate_mermaid`` helper.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import pytest

from app.models.image_understanding import (
    ClassificationResult,
    DescriptionPayload,
    ExtractionResult,
    ImageType,
)
from app.models.schemas import LLMProvider, ModelConfig
from app.services.vlm_service import VLMService, validate_mermaid


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

def _mk_resp(content: str) -> MagicMock:
    """Build a fake OpenAI-shaped chat completion response."""
    resp = MagicMock(name="ChatCompletion")
    msg = MagicMock(name="Message")
    msg.content = content
    choice = MagicMock(name="Choice")
    choice.message = msg
    resp.choices = [choice]
    return resp


def _mk_provider() -> LLMProvider:
    """Build a fake LLMProvider with a vision-capable model."""
    return LLMProvider(
        id="openai-test",
        type="openai",
        label="OpenAI Test",
        api_key="secret:test-key",
        base_url="https://api.openai.com/v1",
        models=[
            ModelConfig(model_id="gpt-4o", vision_capable=True),
            ModelConfig(model_id="gpt-3.5-turbo", vision_capable=False),
        ],
    )


@pytest.fixture
def provider() -> LLMProvider:
    return _mk_provider()


@pytest.fixture
def http_client() -> MagicMock:
    return MagicMock(name="OpenAICompatClient")


@pytest.fixture
def service(provider: LLMProvider, http_client: MagicMock) -> VLMService:
    return VLMService(provider=provider, model_id="gpt-4o", http_client=http_client)


def _b64(image_bytes: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"


# ---------------------------------------------------------------------------
# classify() — happy path + retry + fallback
# ---------------------------------------------------------------------------

class TestClassify:
    def test_classify_happy_path(self, service: VLMService, http_client: MagicMock):
        """Valid JSON response on first attempt round-trips into ClassificationResult."""
        http_client.chat.completions.create.return_value = _mk_resp(
            json.dumps(
                {
                    "image_type": "chart_bar",
                    "confidence": 0.9,
                    "rationale": "bars and axis",
                }
            )
        )

        result = service.classify(
            image_bytes=b"\x89PNG fake bytes",
            mime_type="image/png",
            heading_chain="H1: Revenue",
            surrounding_paragraphs="The chart below shows Q4 revenue.",
        )

        assert isinstance(result, ClassificationResult)
        assert result.image_type == ImageType.chart_bar
        assert result.confidence == 0.9
        assert result.rationale == "bars and axis"
        assert http_client.chat.completions.create.call_count == 1

    def test_classify_malformed_json_retries_once(
        self, service: VLMService, http_client: MagicMock
    ):
        """First call returns non-JSON; second call returns valid JSON — result is from second call."""
        http_client.chat.completions.create.side_effect = [
            _mk_resp("not json"),
            _mk_resp(
                json.dumps(
                    {
                        "image_type": "chart_line",
                        "confidence": 0.8,
                        "rationale": "line trend",
                    }
                )
            ),
        ]

        result = service.classify(
            image_bytes=b"fake",
            mime_type="image/png",
            heading_chain="H1: Trend",
            surrounding_paragraphs="Trend over time.",
        )

        assert result.image_type == ImageType.chart_line
        assert result.confidence == 0.8
        assert http_client.chat.completions.create.call_count == 2

    def test_classify_second_attempt_fails_falls_back_to_other(
        self, service: VLMService, http_client: MagicMock
    ):
        """Both attempts return malformed JSON → fallback to ImageType.other, confidence 0.0."""
        http_client.chat.completions.create.side_effect = [
            _mk_resp("totally not json"),
            _mk_resp("still not json"),
        ]

        result = service.classify(
            image_bytes=b"fake",
            mime_type="image/png",
            heading_chain="",
            surrounding_paragraphs="",
        )

        assert result.image_type == ImageType.other
        assert result.confidence == 0.0
        assert http_client.chat.completions.create.call_count == 2

    def test_classify_invalid_enum_falls_back(
        self, service: VLMService, http_client: MagicMock
    ):
        """Valid JSON but unknown image_type value triggers a retry, then fallback if still invalid."""
        http_client.chat.completions.create.side_effect = [
            _mk_resp(
                json.dumps(
                    {
                        "image_type": "not_a_real_type",
                        "confidence": 0.5,
                        "rationale": "bogus",
                    }
                )
            ),
            _mk_resp(
                json.dumps(
                    {
                        "image_type": "also_bogus",
                        "confidence": 0.5,
                        "rationale": "bogus2",
                    }
                )
            ),
        ]

        result = service.classify(
            image_bytes=b"fake",
            mime_type="image/png",
            heading_chain="",
            surrounding_paragraphs="",
        )

        assert result.image_type == ImageType.other
        assert result.confidence == 0.0
        assert http_client.chat.completions.create.call_count == 2

    def test_classify_provider_exception_never_raises(
        self, service: VLMService, http_client: MagicMock
    ):
        """If the underlying client raises, classify() catches and returns fallback — never raises."""
        http_client.chat.completions.create.side_effect = ConnectionError("network down")

        result = service.classify(
            image_bytes=b"fake",
            mime_type="image/png",
            heading_chain="",
            surrounding_paragraphs="",
        )

        assert result.image_type == ImageType.other
        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# extract() — short-circuit, happy path, retry, demote, exception
# ---------------------------------------------------------------------------

class TestExtract:
    def test_extract_decorative_short_circuits(
        self, service: VLMService, http_client: MagicMock
    ):
        """Decorative images skip the VLM call entirely — payload is empty dict."""
        result = service.extract(
            image_bytes=b"\x89PNG fake",
            mime_type="image/png",
            image_type=ImageType.decorative,
            heading_chain="",
            surrounding_paragraphs="",
        )

        assert isinstance(result, ExtractionResult)
        assert result.image_type == ImageType.decorative
        assert result.payload == {}
        assert result.confidence == 1.0
        assert result.error is None
        assert http_client.chat.completions.create.call_count == 0

    def test_extract_happy_path_chart(
        self, service: VLMService, http_client: MagicMock
    ):
        """Valid ChartPayload JSON parses into ExtractionResult.payload."""
        payload_dict = {
            "title": "Revenue",
            "x_label": "Q1",
            "y_label": "$M",
            "series": [{"name": "2024", "points": [{"x": "Q1", "y": 10}]}],
            "notes": "",
        }
        http_client.chat.completions.create.return_value = _mk_resp(
            json.dumps(payload_dict)
        )

        result = service.extract(
            image_bytes=b"fake",
            mime_type="image/png",
            image_type=ImageType.chart_bar,
            heading_chain="H1: Revenue",
            surrounding_paragraphs="Q1 numbers.",
        )

        assert result.image_type == ImageType.chart_bar
        assert result.payload == payload_dict
        assert result.error is None
        assert result.confidence > 0.0
        assert http_client.chat.completions.create.call_count == 1

    def test_extract_malformed_json_retries_then_fails_gracefully(
        self, service: VLMService, http_client: MagicMock
    ):
        """Both attempts malformed → error field set, payload empty dict."""
        http_client.chat.completions.create.side_effect = [
            _mk_resp("not json"),
            _mk_resp("also not json"),
        ]

        result = service.extract(
            image_bytes=b"fake",
            mime_type="image/png",
            image_type=ImageType.chart_bar,
            heading_chain="",
            surrounding_paragraphs="",
        )

        assert result.image_type == ImageType.chart_bar
        assert result.payload == {}
        assert result.confidence == 0.0
        assert result.error is not None
        assert "JSON" in result.error or "parse" in result.error.lower()
        assert http_client.chat.completions.create.call_count == 2

    def test_extract_diagram_validates_mermaid(
        self, service: VLMService, http_client: MagicMock
    ):
        """Valid Mermaid returned by VLM survives validation — returned as-is."""
        valid_mermaid = "graph TD\n    A-->B\n    B-->C"
        http_client.chat.completions.create.return_value = _mk_resp(
            json.dumps({"mermaid": valid_mermaid, "caption": "flow"})
        )

        result = service.extract(
            image_bytes=b"fake",
            mime_type="image/png",
            image_type=ImageType.diagram_flow,
            heading_chain="",
            surrounding_paragraphs="",
        )

        assert result.image_type == ImageType.diagram_flow
        assert result.payload["mermaid"] == valid_mermaid
        assert result.payload["caption"] == "flow"
        assert result.error is None
        # Single call — no retry needed because Mermaid is valid
        assert http_client.chat.completions.create.call_count == 1

    def test_extract_diagram_invalid_mermaid_retries_then_demotes_to_description(
        self, service: VLMService, http_client: MagicMock
    ):
        """Invalid Mermaid on both attempts → demote payload to DescriptionPayload shape."""
        invalid_mermaid = "this is not mermaid syntax at all"
        http_client.chat.completions.create.side_effect = [
            _mk_resp(json.dumps({"mermaid": invalid_mermaid, "caption": "diag"})),
            _mk_resp(json.dumps({"mermaid": invalid_mermaid, "caption": "diag2"})),
        ]

        result = service.extract(
            image_bytes=b"fake",
            mime_type="image/png",
            image_type=ImageType.diagram_flow,
            heading_chain="",
            surrounding_paragraphs="",
        )

        assert result.image_type == ImageType.diagram_flow
        # Demoted payload must match DescriptionPayload shape: {alt_text, details}
        assert "alt_text" in result.payload
        assert "details" in result.payload
        assert isinstance(result.payload["details"], list)
        # Confirm shape matches DescriptionPayload model fields
        _ = DescriptionPayload(**result.payload)
        assert http_client.chat.completions.create.call_count == 2

    def test_extract_provider_exception_never_raises(
        self, service: VLMService, http_client: MagicMock
    ):
        """Client raising → ExtractionResult with error string, never propagates."""
        http_client.chat.completions.create.side_effect = TimeoutError("VLM timed out")

        result = service.extract(
            image_bytes=b"fake",
            mime_type="image/png",
            image_type=ImageType.chart_bar,
            heading_chain="",
            surrounding_paragraphs="",
        )

        assert result.image_type == ImageType.chart_bar
        assert result.payload == {}
        assert result.confidence == 0.0
        assert result.error is not None
        assert "timeout" in result.error.lower() or "timed out" in result.error.lower()


# ---------------------------------------------------------------------------
# validate_mermaid() — module-level helper
# ---------------------------------------------------------------------------

class TestValidateMermaid:
    def test_validate_mermaid_valid_graph(self):
        assert validate_mermaid("graph TD\n    A-->B\n    B-->C") is True

    def test_validate_mermaid_valid_sequence(self):
        assert validate_mermaid("sequenceDiagram\n    A->>B: hello\n    B-->>A: ok") is True

    def test_validate_mermaid_unbalanced_brackets(self):
        # Unclosed [ opens a bracket that never closes
        assert validate_mermaid("graph TD\n    A-->B[unclosed") is False

    def test_validate_mermaid_missing_arrow(self):
        # Valid keyword + balanced brackets but no arrow
        assert validate_mermaid("graph TD\n    A B") is False

    def test_validate_mermaid_unknown_keyword(self):
        assert validate_mermaid("not_a_keyword stuff and things without any arrow") is False

    def test_validate_mermaid_too_short(self):
        # Length < 20 chars
        assert validate_mermaid("graph TD") is False


# ---------------------------------------------------------------------------
# Constructor wiring — model_id fallback to vision_capable model
# ---------------------------------------------------------------------------

class TestConstructorWiring:
    def test_model_id_falls_back_to_first_vision_capable_model(
        self, http_client: MagicMock
    ):
        """If model_id is None, default to the provider's first vision_capable model."""
        prov = _mk_provider()
        svc = VLMService(provider=prov, model_id=None, http_client=http_client)

        # Drive one classify call so we can inspect the kwargs passed to the client
        http_client.chat.completions.create.return_value = _mk_resp(
            json.dumps({"image_type": "chart_bar", "confidence": 0.9, "rationale": "x"})
        )
        svc.classify(b"fake", "image/png", "", "")

        kwargs = http_client.chat.completions.create.call_args.kwargs
        assert kwargs.get("model") == "gpt-4o"
