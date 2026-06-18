"""Real-marker integration test (opt-in, slow).

WHY THIS FILE EXISTS
--------------------
Every other image-pipeline test drives a hand-written ``FakeDocument`` whose
``get_prev_block`` indexes a flat list of block *objects* that always contains
the block. Real marker indexes ``block.id`` inside the page's ``structure``
list, which can *not* contain a Figure that was dropped post-layout -- and then
raises ``ValueError: /page/1/Figure/0 is not in list``. That divergence between
the fake's data model and marker's real one let a hard crash ship green through
147 passing tests (first reproduced on sample/openskill.pdf, 2026-06-17).

This test closes that gap the only way it can be closed: by running the actual
processor against a real marker ``Document`` built from a real PDF. It is the
one layer that catches marker-contract drift.

COST / GATING
-------------
Loads the full marker model dict and runs detection+OCR over 2 pages -> minutes,
needs models on disk. So it is opt-in: set ``RUN_MARKER_INTEGRATION=1`` to run.
The default suite skips it (keeps CI fast and green without model weights).

    RUN_MARKER_INTEGRATION=1 python -m pytest tests/test_marker_integration.py -q

No network / API key needed: ``gather_local_context`` runs per-picture BEFORE
any route decision or VLM send, so ``allow_cloud_vlm=False`` reproduces the
crash path fully offline.

FIXTURE
-------
``fixtures/openskill_p0_p1.pdf`` = pages 0 and 1 of sample/openskill.pdf.
Page 1 (0-based) is where the original crash fired (``/page/1/Figure/0``);
page 0 adds the title page's raster images for decorative/dedup coverage.
Picked 2 pages on purpose -- the full 20-page doc takes ~30 min.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

RUN = os.environ.get("RUN_MARKER_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    not RUN,
    reason="slow real-marker test; set RUN_MARKER_INTEGRATION=1 to run",
)

FIXTURE = Path(__file__).parent / "fixtures" / "openskill_p0_p1.pdf"


@pytest.mark.parametrize("mode", ["extraction", "understanding", "both"])
def test_real_pdf_converts_without_crashing(mode: str) -> None:
    """A real 2-page PDF with Figure/Picture blocks must convert, not raise.

    Regression guard for the ``ValueError: ... is not in list`` crash in
    ``gather_local_context``. ``allow_cloud_vlm=False`` keeps it offline: the
    router degrades visual routes to local OCR / skip, so no provider is needed.
    The bug we are guarding fires before routing, so all three handling modes
    exercise the per-picture context-gather path.
    """
    from app.services.marker_service import MarkerService

    assert FIXTURE.is_file(), f"missing fixture: {FIXTURE}"

    service = MarkerService()
    options = {
        "output_format": "markdown",
        "image_handling_mode": mode,
        "allow_cloud_vlm": False,
    }

    result = service.convert_file(str(FIXTURE), dict(options))

    assert isinstance(result, dict)
    assert isinstance(result.get("text", ""), str)
    # The doc has real body text on both pages; a successful convert returns it.
    assert result["text"].strip()
