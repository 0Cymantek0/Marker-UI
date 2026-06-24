"""Lightweight PDF probing for conversion routing.

The probe is deliberately cheap: it samples first pages plus last page, reads
text with pypdf, inspects embedded image objects, and estimates simple layout
signals from text coordinates. It does not render pages or load Marker models.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Literal

try:  # pypdf is an explicit dependency for smart PDF routing.
    from pypdf import PdfReader
except Exception:  # pragma: no cover - exercised by dependency status checks
    PdfReader = None  # type: ignore[assignment]


PdfEngineRecommendation = Literal["liteparse", "marker"]


@dataclass
class PageProbeResult:
    page_number: int
    text_layer_score: float
    text_quality_score: float
    scan_likelihood: float
    sandwich_likelihood: float
    layout_complexity_score: float
    visual_complexity_score: float
    recommended_engine: PdfEngineRecommendation
    reasons: list[str] = field(default_factory=list)
    text_chars: int = 0
    image_count: int = 0
    full_page_image: bool = False

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PageProbeResult":
        return cls(
            page_number=int(data.get("page_number") or 0),
            text_layer_score=float(data.get("text_layer_score") or 0.0),
            text_quality_score=float(data.get("text_quality_score") or 0.0),
            scan_likelihood=float(data.get("scan_likelihood") or 0.0),
            sandwich_likelihood=float(data.get("sandwich_likelihood") or 0.0),
            layout_complexity_score=float(data.get("layout_complexity_score") or 0.0),
            visual_complexity_score=float(data.get("visual_complexity_score") or 0.0),
            recommended_engine=(
                "liteparse" if data.get("recommended_engine") == "liteparse" else "marker"
            ),
            reasons=list(data.get("reasons") or []),
            text_chars=int(data.get("text_chars") or 0),
            image_count=int(data.get("image_count") or 0),
            full_page_image=bool(data.get("full_page_image")),
        )


@dataclass
class PdfRoutingSegment:
    pages: list[int]
    engine: PdfEngineRecommendation
    reasons: list[str] = field(default_factory=list)
    fallback_chain: list[PdfEngineRecommendation] = field(default_factory=list)
    source_probe_ids: list[int] = field(default_factory=list)


@dataclass
class PdfProbeResult:
    page_count: int
    text_layer_score: float
    text_quality_score: float
    scan_likelihood: float
    sandwich_likelihood: float
    layout_complexity_score: float
    visual_complexity_score: float
    recommended_engine: PdfEngineRecommendation
    reasons: list[str] = field(default_factory=list)
    sampled_pages: list[int] = field(default_factory=list)
    sampled_text_chars: int = 0
    sampled_image_count: int = 0
    full_page_image_pages: int = 0
    page_results: list[PageProbeResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PdfProbeResult":
        return cls(
            page_count=int(data.get("page_count") or 0),
            text_layer_score=float(data.get("text_layer_score") or 0.0),
            text_quality_score=float(data.get("text_quality_score") or 0.0),
            scan_likelihood=float(data.get("scan_likelihood") or 0.0),
            sandwich_likelihood=float(data.get("sandwich_likelihood") or 0.0),
            layout_complexity_score=float(data.get("layout_complexity_score") or 0.0),
            visual_complexity_score=float(data.get("visual_complexity_score") or 0.0),
            recommended_engine=(
                "liteparse" if data.get("recommended_engine") == "liteparse" else "marker"
            ),
            reasons=list(data.get("reasons") or []),
            sampled_pages=[int(p) for p in data.get("sampled_pages") or []],
            sampled_text_chars=int(data.get("sampled_text_chars") or 0),
            sampled_image_count=int(data.get("sampled_image_count") or 0),
            full_page_image_pages=int(data.get("full_page_image_pages") or 0),
            page_results=[
                PageProbeResult.from_mapping(item)
                for item in data.get("page_results") or []
                if isinstance(item, dict)
            ],
        )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _sample_indices(page_count: int) -> list[int]:
    if page_count <= 0:
        return []
    indices = list(range(min(3, page_count)))
    last = page_count - 1
    if last not in indices:
        indices.append(last)
    return indices


def _text_quality(text: str) -> float:
    if not text:
        return 0.0
    total = len(text)
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t\r")
    controls = sum(1 for ch in text if (ord(ch) < 32 and ch not in "\n\t\r") or ch == "\ufffd")
    whitespace = sum(1 for ch in text if ch.isspace())
    words = [w for w in text.split() if any(c.isalnum() for c in w)]
    repeated_runs = 0
    last = ""
    run = 0
    for ch in text:
        if ch == last:
            run += 1
            if run >= 8:
                repeated_runs += 1
        else:
            last = ch
            run = 1

    printable_score = printable / total
    control_penalty = min(0.5, controls / max(total, 1) * 8)
    repeated_penalty = min(0.3, repeated_runs / max(total, 1) * 20)
    whitespace_ratio = whitespace / total
    whitespace_penalty = 0.25 if whitespace_ratio < 0.03 or whitespace_ratio > 0.45 else 0.0
    word_score = min(1.0, len(words) / max(1, total / 12))
    return _clamp((printable_score * 0.65) + (word_score * 0.35) - control_penalty - repeated_penalty - whitespace_penalty)


def _page_images(page: Any) -> tuple[int, int]:
    """Return (image_count, full_page_like_image_count)."""
    count = 0
    full_page_like = 0
    try:
        media_box = page.mediabox
        page_area = max(1.0, float(media_box.width) * float(media_box.height))
    except Exception:
        page_area = 1.0

    try:
        xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
        for xobj in xobjects.values():
            obj = xobj.get_object()
            if obj.get("/Subtype") != "/Image":
                continue
            count += 1
            width = float(obj.get("/Width") or 0)
            height = float(obj.get("/Height") or 0)
            pixel_area = width * height
            # Pixel dimensions are not PDF user units, but this catches common
            # scan pages where one large raster dominates and text is absent.
            if pixel_area >= page_area * 0.75:
                full_page_like += 1
    except Exception:
        # pypdf also exposes page.images in newer versions.
        try:
            images = list(page.images)
            count += len(images)
            full_page_like += len(images)
        except Exception:
            pass
    return count, full_page_like


def _layout_complexity(page: Any) -> float:
    positions: list[tuple[float, float, str]] = []

    def visitor_text(text: str, _cm: Any, tm: Any, *_args: Any) -> None:
        stripped = text.strip()
        if not stripped:
            return
        try:
            positions.append((float(tm[4]), float(tm[5]), stripped))
        except Exception:
            return

    try:
        page.extract_text(visitor_text=visitor_text)
    except Exception:
        return 0.0
    if len(positions) < 12:
        return 0.0

    xs = sorted(x for x, _y, text in positions if len(text) > 2)
    if not xs:
        return 0.0
    clusters = [xs[0]]
    for x in xs[1:]:
        if abs(x - clusters[-1]) > 90:
            clusters.append(x)
    column_score = _clamp((len(clusters) - 1) / 5)
    short_line_ratio = sum(1 for _x, _y, text in positions if len(text) < 12) / len(positions)
    return _clamp(column_score * 0.65 + short_line_ratio * 0.35)


def _routing_reasons(
    *,
    text_layer_score: float,
    text_quality_score: float,
    scan_likelihood: float,
    sandwich_likelihood: float,
    visual_complexity_score: float,
    layout_complexity_score: float,
) -> tuple[PdfEngineRecommendation, list[str]]:
    reasons: list[str] = []
    if text_layer_score >= 0.70:
        reasons.append("strong extractable text layer")
    else:
        reasons.append("weak or missing extractable text layer")
    if text_quality_score >= 0.80:
        reasons.append("text quality is high")
    else:
        reasons.append("text quality is poor or sparse")
    if scan_likelihood > 0.20:
        reasons.append("scan likelihood is high")
    if sandwich_likelihood > 0.40:
        reasons.append("image/text sandwich likelihood is high")
    if visual_complexity_score > 0.35:
        reasons.append("embedded image density is high")
    if layout_complexity_score > 0.45:
        reasons.append("layout complexity is high")

    liteparse_safe = (
        text_layer_score >= 0.70
        and text_quality_score >= 0.80
        and scan_likelihood <= 0.20
        and sandwich_likelihood <= 0.40
        and visual_complexity_score <= 0.35
        and layout_complexity_score <= 0.45
    )
    if liteparse_safe:
        reasons.append("LiteParse fast path is safe")
        return "liteparse", reasons
    reasons.append("Marker deep path is safer")
    return "marker", reasons


def _page_probe_result(
    *,
    page_number: int,
    text: str,
    image_count: int,
    full_page_like: int,
    layout_complexity_score: float,
) -> PageProbeResult:
    text_chars = len(text.strip())
    text_layer_score = _clamp(min(text_chars / 900, 1.0))
    text_quality_score = _clamp(_text_quality(text))
    full_page_image = full_page_like > 0
    visual_complexity_score = _clamp(
        (image_count / 3.0) * 0.45 + (0.55 if full_page_image else 0.0)
    )
    scan_likelihood = _clamp((1.0 - text_layer_score) * 0.65 + (0.35 if full_page_image else 0.0))
    sandwich_likelihood = _clamp(
        visual_complexity_score * 0.55
        + text_layer_score * 0.25
        + (1.0 - text_quality_score) * 0.20
    )
    recommended_engine, reasons = _routing_reasons(
        text_layer_score=text_layer_score,
        text_quality_score=text_quality_score,
        scan_likelihood=scan_likelihood,
        sandwich_likelihood=sandwich_likelihood,
        visual_complexity_score=visual_complexity_score,
        layout_complexity_score=layout_complexity_score,
    )
    return PageProbeResult(
        page_number=page_number,
        text_layer_score=round(text_layer_score, 3),
        text_quality_score=round(text_quality_score, 3),
        scan_likelihood=round(scan_likelihood, 3),
        sandwich_likelihood=round(sandwich_likelihood, 3),
        layout_complexity_score=round(layout_complexity_score, 3),
        visual_complexity_score=round(visual_complexity_score, 3),
        recommended_engine=recommended_engine,
        reasons=reasons,
        text_chars=text_chars,
        image_count=image_count,
        full_page_image=full_page_image,
    )


def probe_pdf(filepath: str | Path, *, max_deep_pages: int = 4) -> PdfProbeResult:
    path = Path(filepath)
    if PdfReader is None:
        return PdfProbeResult(
            page_count=0,
            text_layer_score=0.0,
            text_quality_score=0.0,
            scan_likelihood=1.0,
            sandwich_likelihood=0.0,
            layout_complexity_score=1.0,
            visual_complexity_score=1.0,
            recommended_engine="marker",
            reasons=["pypdf is not installed; using Marker"],
        )

    try:
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
    except Exception as exc:
        return PdfProbeResult(
            page_count=0,
            text_layer_score=0.0,
            text_quality_score=0.0,
            scan_likelihood=1.0,
            sandwich_likelihood=0.0,
            layout_complexity_score=1.0,
            visual_complexity_score=1.0,
            recommended_engine="marker",
            reasons=[f"PDF probe failed ({type(exc).__name__}); using Marker"],
        )

    sampled_pages = _sample_indices(page_count)[:max_deep_pages]
    page_text_lengths: list[int] = []
    qualities: list[float] = []
    image_counts: list[int] = []
    full_page_image_pages = 0
    layout_scores: list[float] = []
    page_results: list[PageProbeResult] = []

    for idx in sampled_pages:
        page = reader.pages[idx]
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        page_text_lengths.append(len(text.strip()))
        qualities.append(_text_quality(text))
        image_count, full_page_like = _page_images(page)
        image_counts.append(image_count)
        if full_page_like:
            full_page_image_pages += 1
        layout_complexity_score = _layout_complexity(page)
        layout_scores.append(layout_complexity_score)
        page_results.append(_page_probe_result(
            page_number=idx + 1,
            text=text,
            image_count=image_count,
            full_page_like=full_page_like,
            layout_complexity_score=layout_complexity_score,
        ))

    sampled = max(1, len(sampled_pages))
    avg_chars = mean(page_text_lengths) if page_text_lengths else 0.0
    pages_with_text = sum(1 for n in page_text_lengths if n >= 80)
    text_layer_score = _clamp((pages_with_text / sampled) * 0.55 + min(avg_chars / 900, 1.0) * 0.45)
    text_quality_score = _clamp(mean(qualities) if qualities else 0.0)
    avg_images = mean(image_counts) if image_counts else 0.0
    full_page_ratio = full_page_image_pages / sampled
    visual_complexity_score = _clamp((avg_images / 3.0) * 0.45 + full_page_ratio * 0.55)
    layout_complexity_score = _clamp(mean(layout_scores) if layout_scores else 0.0)
    scan_likelihood = _clamp((1.0 - text_layer_score) * 0.65 + full_page_ratio * 0.35)
    sandwich_likelihood = _clamp(
        visual_complexity_score * 0.55
        + text_layer_score * 0.25
        + (1.0 - text_quality_score) * 0.20
    )

    recommended_engine, reasons = _routing_reasons(
        text_layer_score=text_layer_score,
        text_quality_score=text_quality_score,
        scan_likelihood=scan_likelihood,
        sandwich_likelihood=sandwich_likelihood,
        visual_complexity_score=visual_complexity_score,
        layout_complexity_score=layout_complexity_score,
    )

    return PdfProbeResult(
        page_count=page_count,
        text_layer_score=round(text_layer_score, 3),
        text_quality_score=round(text_quality_score, 3),
        scan_likelihood=round(scan_likelihood, 3),
        sandwich_likelihood=round(sandwich_likelihood, 3),
        layout_complexity_score=round(layout_complexity_score, 3),
        visual_complexity_score=round(visual_complexity_score, 3),
        recommended_engine=recommended_engine,
        reasons=reasons,
        sampled_pages=[p + 1 for p in sampled_pages],
        sampled_text_chars=sum(page_text_lengths),
        sampled_image_count=sum(image_counts),
        full_page_image_pages=full_page_image_pages,
        page_results=page_results,
    )


def plan_pdf_routing_segments(probe: PdfProbeResult) -> list[PdfRoutingSegment]:
    """Group page-level probe results into contiguous same-engine segments.

    This is groundwork for future mixed-engine routing. It intentionally uses
    only page probes already present in ``probe`` and does not imply current
    conversion uses mixed engines.
    """
    if not probe.page_results:
        return []

    ordered = sorted(probe.page_results, key=lambda item: item.page_number)
    segments: list[PdfRoutingSegment] = []
    current_engine = ordered[0].recommended_engine
    current_pages: list[int] = []
    current_reasons: list[str] = []
    source_ids: list[int] = []

    def flush() -> None:
        if not current_pages:
            return
        fallback_chain = ["liteparse", "marker"] if current_engine == "liteparse" else []
        segments.append(PdfRoutingSegment(
            pages=list(current_pages),
            engine=current_engine,
            reasons=list(dict.fromkeys(current_reasons)),
            fallback_chain=fallback_chain,
            source_probe_ids=list(source_ids),
        ))

    previous_page = ordered[0].page_number - 1
    for page in ordered:
        contiguous = page.page_number == previous_page + 1
        if page.recommended_engine != current_engine or not contiguous:
            flush()
            current_engine = page.recommended_engine
            current_pages = []
            current_reasons = []
            source_ids = []
        current_pages.append(page.page_number)
        current_reasons.extend(page.reasons)
        source_ids.append(page.page_number)
        previous_page = page.page_number
    flush()
    return segments
