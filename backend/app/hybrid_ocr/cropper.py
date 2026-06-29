"""Create local image crops for Hybrid OCR specialist workers."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from app.hybrid_ocr.contracts import HybridTarget

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def materialize_target_crops(filepath: str, targets: list[HybridTarget]) -> None:
    if not targets:
        return
    source = Path(filepath)
    if not source.exists():
        return
    suffix = source.suffix.lower()
    if suffix in _IMAGE_EXTS:
        _crop_from_image(source, targets)
        return
    if suffix == ".pdf":
        _crop_from_pdf(source, targets)


def _crop_from_image(source: Path, targets: list[HybridTarget]) -> None:
    with Image.open(source) as image:
        for target in targets:
            out = Path(target.crop_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            crop = _safe_crop(image, target.bbox)
            if crop is image and source.suffix.lower() == ".png":
                shutil.copyfile(source, out)
            else:
                crop.save(out, format="PNG")


def _crop_from_pdf(source: Path, targets: list[HybridTarget]) -> None:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(source))
    page_images: dict[int, Image.Image] = {}
    try:
        for target in targets:
            page_index = max(0, min(target.page_index, len(pdf) - 1))
            if page_index not in page_images:
                page = pdf[page_index]
                page_images[page_index] = page.render(scale=2.0).to_pil()
            out = Path(target.crop_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            _safe_crop(page_images[page_index], target.bbox).save(out, format="PNG")
    finally:
        for image in page_images.values():
            image.close()
        pdf.close()


def _safe_crop(image: Image.Image, bbox: Any) -> Image.Image:
    if not bbox:
        return image.copy()
    coords = _coerce_bbox(bbox)
    if coords is None:
        return image.copy()
    left, top, right, bottom = coords
    width, height = image.size
    # Marker/Surya bboxes are often already pixels. If values look normalized,
    # scale them. Otherwise clamp. If PDF points arrive, full-page crop remains
    # safer than failing conversion.
    if 0 <= right <= 1 and 0 <= bottom <= 1:
        left, right = left * width, right * width
        top, bottom = top * height, bottom * height
    left = max(0, min(width - 1, int(left)))
    top = max(0, min(height - 1, int(top)))
    right = max(left + 1, min(width, int(right)))
    bottom = max(top + 1, min(height, int(bottom)))
    return image.crop((left, top, right, bottom))


def _coerce_bbox(bbox: Any) -> tuple[float, float, float, float] | None:
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        try:
            return float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
        except (TypeError, ValueError):
            return None
    return None
