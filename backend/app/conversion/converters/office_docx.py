"""OfficeDocxConverter — converts DOCX to Markdown using mammoth and markdownify.

Extracts inline embedded images, routing them through EmbeddedImageService,
and resolving them to Markdown comments and original image references/transcriptions.
"""

from __future__ import annotations

import logging
from typing import Any
import mammoth
from bs4 import BeautifulSoup
import markdownify

from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.embedded_image import EmbeddedImageService
from app.conversion.stream_info import StreamInfo

logger = logging.getLogger(__name__)


class OfficeDocxConverter(BaseConverter):
    """Converts Word documents (.docx) to clean Markdown model-free."""

    engine_name = "office_docx"
    priority = 10
    requires_marker_models = False
    requires_gpu = False

    _EXTENSIONS = frozenset({".docx"})

    def __init__(self, marker_service: Any = None) -> None:
        self._marker_service = marker_service

    @property
    def supported_extensions(self) -> frozenset[str]:
        return self._EXTENSIONS

    def accepts(self, stream_info: StreamInfo, config: dict[str, Any]) -> bool:
        return stream_info.extension in self._EXTENSIONS

    def convert(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> UniversalConversionResult:
        """Run mammoth HTML conversion, process embedded images, and markdownify."""
        images_data = {}
        image_counter = 0

        def convert_image(image_element):
            nonlocal image_counter
            image_counter += 1
            try:
                with image_element.open() as f:
                    image_bytes = f.read()
            except Exception as exc:
                logger.warning("Failed to read docx image content: %r", exc)
                image_bytes = b""

            content_type = image_element.content_type
            ext = "png"
            if content_type:
                parts = content_type.split("/")
                if len(parts) == 2:
                    ext = parts[1]

            img_name = f"image_{image_counter}.{ext}"
            images_data[image_counter] = (img_name, image_bytes)

            token = f"￹{image_counter}￺"
            return {"src": token}

        # Run Mammoth to HTML
        try:
            with open(filepath, "rb") as docx_file:
                mammoth_result = mammoth.convert_to_html(
                    docx_file,
                    convert_image=mammoth.images.img_element(convert_image)
                )
                html_content = mammoth_result.value
        except Exception as exc:
            logger.warning("Mammoth HTML conversion failed: %r. Falling back to raw text", exc)
            try:
                with open(filepath, "rb") as docx_file:
                    raw_text_result = mammoth.extract_raw_text(docx_file)
                    text = raw_text_result.value
            except Exception as inner_exc:
                logger.error("Mammoth raw text extraction failed: %r", inner_exc)
                raise
            return UniversalConversionResult(
                text=text,
                extension="md",
                images={},
                metadata={"mammoth_fallback": True, "error": str(exc)},
            )

        # Parse with BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")
        img_tags = soup.find_all("img")

        img_service = EmbeddedImageService(self._marker_service)
        processed_images = {}
        image_metadata = []
        out_images = {}

        for img_tag in img_tags:
            token = img_tag.get("src", "")
            if token.startswith("￹") and token.endswith("￺"):
                try:
                    img_idx = int(token[1:-1])
                except ValueError:
                    continue

                img_info = images_data.get(img_idx)
                if img_info:
                    img_name, image_bytes = img_info
                    # Get surrounding context text
                    context_parts = []
                    prev_p = img_tag.find_previous("p")
                    if prev_p:
                        context_parts.append(prev_p.get_text().strip())
                    next_p = img_tag.find_next("p")
                    if next_p:
                        context_parts.append(next_p.get_text().strip())

                    context_text = "\n".join([p for p in context_parts if p])

                    try:
                        result = img_service.process_image(
                            image_bytes_or_pil=image_bytes,
                            context_text=context_text,
                            options=config,
                            image_name=img_name,
                        )
                        processed_images[img_idx] = result

                        # Record metadata for badge UI
                        meta = {
                            "image_name": img_name,
                            "image_type": result.get("image_type", "other"),
                            "confidence": result.get("confidence", 1.0),
                            "model": result.get("model") or ("local-ocr" if result.get("route") == "ocr" else (config.get("vlm_model") or "unknown")),
                            "omitted": result.get("omitted", False),
                            "cost_usd": result.get("cost_usd", 0.0),
                        }
                        if result.get("route") == "skip_decorative" and not result.get("vlm_decided"):
                            meta["model"] = "router-local"
                        image_metadata.append(meta)

                        # Write to output image payload if kept
                        if not result.get("omitted", False):
                            out_images[img_name] = image_bytes
                    except Exception as img_exc:
                        logger.warning("Failed to process image %s: %r", img_name, img_exc)
                        processed_images[img_idx] = {
                            "markdown": f"![{img_name}]({img_name})",
                            "omitted": False,
                        }
                        out_images[img_name] = image_bytes
                
                # Replace img tag with the token string
                img_tag.replace_with(token)

        # Convert remaining HTML to Markdown
        markdown = markdownify.markdownify(str(soup))

        # Replace placeholders with final processed markdown references/content
        for img_idx, result in processed_images.items():
            token = f"￹{img_idx}￺"
            replacement = result.get("markdown", "")
            markdown = markdown.replace(token, replacement)

        metadata = {}
        if image_metadata:
            metadata["image_understanding"] = image_metadata

        return UniversalConversionResult(
            text=markdown,
            extension="md",
            images=out_images,
            metadata=metadata,
        )
