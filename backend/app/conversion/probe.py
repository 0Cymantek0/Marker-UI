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
    full_page_coverage: bool = False
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
            full_page_coverage=bool(
                data.get("full_page_coverage")
                if "full_page_coverage" in data
                else _mapping_has_full_page_coverage(data)
            ),
            page_results=[
                PageProbeResult.from_mapping(item)
                for item in data.get("page_results") or []
                if isinstance(item, dict)
            ],
        )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _analyze_metadata(reader: PdfReader) -> tuple[float, list[str]]:
    """Analyze PDF metadata to detect physical scans vs digital origins.

    Returns (metadata_scan_likelihood_adjustment, reasons).
    """
    try:
        meta = reader.metadata
        if not meta:
            return 0.0, []
    except Exception:
        return 0.0, []

    # Document metadata values tend to be string-like but can be custom types
    creator = str(meta.get("/Creator") or "").lower()
    producer = str(meta.get("/Producer") or "").lower()

    scanner_keywords = [
        "scan", "twain", "wia", "scanner", "fujitsu", "ricoh", "canon", "hp",
        "xerox", "brother", "epson", "lexmark", "adobe scan", "camscanner",
        "genius scan", "office lens", "scanbot", "paperport", "abbyy",
        "omnipage", "readiris", "kofax"
    ]

    digital_keywords = [
        "pdftex", "latex", "microsoft word", "google docs", "acrobat distiller",
        "adobe pdf library", "openoffice", "libreoffice", "indesign",
        "quartz pdfcontext", "itext", "pdfbox", "openpdf", "reportlab",
        "fpdf", "wkhtmltopdf", "dompdf", "tcpdf", "hpdf", "spire.pdf"
    ]

    reasons = []
    # 1. Scanner check is higher priority (scans are a physical constraint)
    for kw in scanner_keywords:
        if kw in creator or kw in producer:
            reasons.append(f"metadata matches scanner keyword '{kw}'")
            return 0.40, reasons

    # 2. Digital generator check
    for kw in digital_keywords:
        if kw in creator or kw in producer:
            reasons.append(f"metadata matches digital generator '{kw}'")
            return -0.30, reasons

    return 0.0, []


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


def multiply_matrices(m1: list[float], m2: list[float]) -> list[float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2
    ]


def _page_dimensions(page: Any) -> tuple[float, float]:
    try:
        box = page.mediabox
        return float(box.width), float(box.height)
    except Exception:
        return 612.0, 792.0 # letter default


def _analyze_page_images_and_sandwich(
    page: Any,
    text_positions: list[tuple[float, float, int]]
) -> tuple[int, int, float]:
    """Return (image_count, full_page_like_count, sandwich_ratio)."""
    width, height = _page_dimensions(page)
    page_area = max(1.0, width * height)

    bboxes = []
    content = page.get_contents()

    fallback_image_count = 0
    try:
        fallback_image_count = len(page.images)
    except Exception:
        pass

    if content is None:
        return fallback_image_count, (1 if fallback_image_count > 0 else 0), -1.0

    xobjects = {}
    try:
        xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
    except Exception:
        pass

    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack = []

    try:
        from pypdf.generic import ContentStream
        cs = ContentStream(content, page.pdf)
        for operands, operator in cs.operations:
            operator_str = operator.decode("ascii") if isinstance(operator, bytes) else str(operator)
            
            if operator_str == "q":
                stack.append(list(ctm))
            elif operator_str == "Q":
                if stack:
                    ctm = stack.pop()
            elif operator_str == "cm":
                if len(operands) == 6:
                    m_new = [float(x) for x in operands]
                    ctm = multiply_matrices(m_new, ctm)
            elif operator_str == "Do":
                if len(operands) == 1:
                    name = operands[0]
                    name_str = name.decode("ascii") if isinstance(name, bytes) else str(name)
                    is_image = False
                    if name_str in xobjects:
                        obj = xobjects[name_str].get_object()
                        if obj.get("/Subtype") == "/Image":
                            is_image = True
                    
                    if is_image:
                        a, b, c, d, e, f = ctm
                        pts = [
                            (e, f),
                            (a + e, b + f),
                            (c + e, d + f),
                            (a + c + e, b + d + f)
                        ]
                        xs = [pt[0] for pt in pts]
                        ys = [pt[1] for pt in pts]
                        bbox = (min(xs), min(ys), max(xs), max(ys))
                        bboxes.append(bbox)
    except Exception:
        return fallback_image_count, (1 if fallback_image_count > 0 else 0), -1.0

    image_count = len(bboxes)
    if image_count == 0 and fallback_image_count > 0:
        return fallback_image_count, 1, -1.0

    full_page_like_count = 0
    large_images = []
    for bbox in bboxes:
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        area = w * h
        if area >= page_area * 0.70:
            full_page_like_count += 1
        if area >= page_area * 0.40:
            large_images.append(bbox)

    if not large_images or not text_positions:
        return image_count, full_page_like_count, 0.0

    total_chars = sum(chars for x, y, chars in text_positions)
    if total_chars == 0:
        return image_count, full_page_like_count, 0.0

    overlap_chars = 0
    for x, y, chars in text_positions:
        in_image = False
        for bbox in large_images:
            if bbox[0] - 5 <= x <= bbox[2] + 5 and bbox[1] - 5 <= y <= bbox[3] + 5:
                in_image = True
                break
        if in_image:
            overlap_chars += chars

    sandwich_ratio = overlap_chars / total_chars
    return image_count, full_page_like_count, sandwich_ratio


def _page_images(page: Any) -> tuple[int, int]:
    """Compatibility wrapper: returns (image_count, full_page_like_count)."""
    img_count, full_like, _ = _analyze_page_images_and_sandwich(page, [])
    return img_count, full_like


def _compute_layout_complexity(positions: list[tuple[float, float, str]]) -> float:
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
    return _compute_layout_complexity(positions)


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
    metadata_adjustment: float = 0.0,
    meta_reasons: list[str] | None = None,
    sandwich_ratio: float = 0.0,
) -> PageProbeResult:
    text_chars = len(text.strip())
    text_layer_score = _clamp(min(text_chars / 900, 1.0))
    text_quality_score = _clamp(_text_quality(text))
    full_page_image = full_page_like > 0
    visual_complexity_score = _clamp(
        (image_count / 3.0) * 0.45 + (0.55 if full_page_image else 0.0)
    )
    # Only apply negative metadata adjustment if there is some text layer
    effective_meta_adj = metadata_adjustment if (metadata_adjustment >= 0.0 or text_layer_score >= 0.10) else 0.0
    scan_likelihood = _clamp((1.0 - text_layer_score) * 0.65 + (0.35 if full_page_image else 0.0) + effective_meta_adj)
    
    if sandwich_ratio >= 0.0:
        sandwich_likelihood = sandwich_ratio
    else:
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
    if meta_reasons and effective_meta_adj != 0.0:
        reasons.extend(meta_reasons)
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


def probe_pdf(
    filepath: str | Path,
    *,
    max_deep_pages: int = 4,
    full_page_probe: bool = False,
) -> PdfProbeResult:
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

    meta_adjustment, meta_reasons = _analyze_metadata(reader)

    # Phase 1: Determine base sample pages
    base_pages = (
        list(range(page_count))
        if full_page_probe
        else _sample_indices(page_count)[:max_deep_pages]
    )
    
    page_text_lengths: list[int] = []
    qualities: list[float] = []
    image_counts: list[int] = []
    full_page_image_pages = 0
    layout_scores: list[float] = []
    page_results: list[PageProbeResult] = []
    
    probed_set = set()
    sampled_pages = []

    def probe_single_page(idx: int) -> None:
        if idx in probed_set:
            return
        probed_set.add(idx)
        sampled_pages.append(idx)
        
        page = reader.pages[idx]
        positions: list[tuple[float, float, str]] = []
        text_positions: list[tuple[float, float, int]] = []

        def visitor_text(text: str, cm: Any, tm: Any, *_args: Any) -> None:
            stripped = text.strip()
            if not stripped:
                return
            try:
                e1, f1 = float(tm[4]), float(tm[5])
                a2, b2, c2, d2, e2, f2 = [float(x) for x in cm]
                x_abs = e1 * a2 + f1 * c2 + e2
                y_abs = e1 * b2 + f1 * d2 + f2
                positions.append((x_abs, y_abs, stripped))
                text_positions.append((x_abs, y_abs, len(stripped)))
            except Exception:
                pass

        try:
            text = page.extract_text(visitor_text=visitor_text) or ""
        except Exception:
            text = ""

        page_text_lengths.append(len(text.strip()))
        qualities.append(_text_quality(text))
        
        image_count, full_page_like, sandwich_ratio = _analyze_page_images_and_sandwich(page, text_positions)
        image_counts.append(image_count)
        
        nonlocal full_page_image_pages
        if full_page_like > 0:
            full_page_image_pages += 1
            
        layout_complexity_score = _compute_layout_complexity(positions)
        layout_scores.append(layout_complexity_score)
        
        page_results.append(_page_probe_result(
            page_number=idx + 1,
            text=text,
            image_count=image_count,
            full_page_like=full_page_like,
            layout_complexity_score=layout_complexity_score,
            metadata_adjustment=meta_adjustment,
            meta_reasons=meta_reasons,
            sandwich_ratio=sandwich_ratio,
        ))

    # Probe Phase 1 pages
    for p in base_pages:
        probe_single_page(p)

    # Phase 2: Dynamic expansion if borderline or mixed
    if not full_page_probe and page_count > len(base_pages):
        recommended_engines = {p.recommended_engine for p in page_results}
        text_layers = [p.text_layer_score for p in page_results]
        mean_text_layer = mean(text_layers) if text_layers else 0.0
        sandwich_likes = [p.sandwich_likelihood for p in page_results]
        mean_sandwich = mean(sandwich_likes) if sandwich_likes else 0.0

        mixed_engines = len(recommended_engines) > 1
        borderline_text = 0.55 <= mean_text_layer <= 0.85
        borderline_sandwich = 0.30 <= mean_sandwich <= 0.55

        if mixed_engines or borderline_text or borderline_sandwich:
            max_limit = min(page_count, max_deep_pages * 2)
            k = max_limit - len(page_results)
            if k > 0:
                unsampled = [i for i in range(page_count) if i not in probed_set]
                additional_pages = [unsampled[int(i * len(unsampled) / k)] for i in range(k)]
                for p in additional_pages:
                    probe_single_page(p)

    sampled = max(1, len(sampled_pages))
    avg_chars = mean(page_text_lengths) if page_text_lengths else 0.0
    pages_with_text = sum(1 for n in page_text_lengths if n >= 80)
    text_layer_score = _clamp((pages_with_text / sampled) * 0.55 + min(avg_chars / 900, 1.0) * 0.45)
    text_quality_score = _clamp(mean(qualities) if qualities else 0.0)
    avg_images = mean(image_counts) if image_counts else 0.0
    full_page_ratio = full_page_image_pages / sampled
    visual_complexity_score = _clamp((avg_images / 3.0) * 0.45 + full_page_ratio * 0.55)
    layout_complexity_score = _clamp(mean(layout_scores) if layout_scores else 0.0)
    
    # Only apply negative metadata adjustment if there is some text layer
    effective_meta_adj = meta_adjustment if (meta_adjustment >= 0.0 or text_layer_score >= 0.10) else 0.0
    scan_likelihood = _clamp((1.0 - text_layer_score) * 0.65 + full_page_ratio * 0.35 + effective_meta_adj)
    
    sandwich_likelihood = _clamp(
        mean([p.sandwich_likelihood for p in page_results])
        if page_results
        else 0.0
    )

    recommended_engine, reasons = _routing_reasons(
        text_layer_score=text_layer_score,
        text_quality_score=text_quality_score,
        scan_likelihood=scan_likelihood,
        sandwich_likelihood=sandwich_likelihood,
        visual_complexity_score=visual_complexity_score,
        layout_complexity_score=layout_complexity_score,
    )
    if meta_reasons and effective_meta_adj != 0.0:
        reasons.extend(meta_reasons)

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
        full_page_coverage=len(page_results) == page_count,
        page_results=page_results,
    )


def probe_has_full_page_coverage(probe: PdfProbeResult) -> bool:
    return _page_numbers_cover_all_pages(
        page_count=probe.page_count,
        page_numbers=[item.page_number for item in probe.page_results],
    )


def missing_probe_pages(probe: PdfProbeResult) -> list[int]:
    present = {item.page_number for item in probe.page_results}
    return [page for page in range(1, max(0, probe.page_count) + 1) if page not in present]


def probe_coverage_label(probe: PdfProbeResult) -> str:
    if probe_has_full_page_coverage(probe):
        return f"full-page probe ({probe.page_count}/{probe.page_count} pages)"
    return f"sampled probe ({len(probe.page_results)}/{probe.page_count} pages)"


def _mapping_has_full_page_coverage(data: dict[str, Any]) -> bool:
    page_results = data.get("page_results") or []
    page_numbers = [
        int(item.get("page_number") or 0)
        for item in page_results
        if isinstance(item, dict)
    ]
    return _page_numbers_cover_all_pages(
        page_count=int(data.get("page_count") or 0),
        page_numbers=page_numbers,
    )


def _page_numbers_cover_all_pages(*, page_count: int, page_numbers: list[int]) -> bool:
    if page_count <= 0:
        return False
    return sorted(set(page_numbers)) == list(range(1, page_count + 1))


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
