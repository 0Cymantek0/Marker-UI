"""Markdown renderer that owns ``<img>`` emission for image-understanding blocks.

Why this exists
---------------
marker's stock renderer (``renderers/html.py:extract_html``) appends **exactly
one** ``<img src=...>`` for *every* Picture/Figure block whenever
``extract_images`` is on:

    element = BeautifulSoup(f"<p>{content}<img src='{image_name}'></p>", ...)

``ImageUnderstandingProcessor`` writes its VLM/OCR result into ``block.html``.
Before this renderer, the processor *also* appended its own ``<img>`` whenever
it wanted to keep the original — so the block carried one ``<img>`` and marker
appended a second, yielding the ``![](x)![](x)`` double-embed seen across the
baseline. For replace/decorative types the processor emitted no ``<img>`` but
marker still forced one, so an "omitted" decorative image still rendered.

Fix: the processor stops emitting ``<img>`` entirely and instead writes a
``marker-ui-iu-handled keep=<0|1>`` sentinel comment into ``block.html``. This
renderer detects that sentinel and becomes the single owner of ``<img>`` for the
block:

  * keep=1 (augment: chart/diagram/table/photo/...) -> emit exactly one ``<img>``
  * keep=0 (replace/decorative: equation, decorative) -> emit no ``<img>``

The image file is still registered in the ``images`` dict either way, so ZIP
packaging / audit keeps the source bytes even when the markdown omits the link.
Blocks the processor never touched (no sentinel) fall through to marker's exact
default behaviour, so non-understanding conversions are byte-for-byte unchanged.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from marker.renderers.markdown import MarkdownRenderer
from marker.settings import settings

from app.processors.image_understanding import IU_HANDLED_PREFIX

# Matches the sentinel the processor injects, capturing the keep flag.
# Tolerates HTML-comment form (``<!-- ... -->``) and the raw tag form, since the
# content reaches us pre-markdownify as the raw <marker-comment> tag text.
_HANDLED_RE = re.compile(
    rf"{re.escape(IU_HANDLED_PREFIX)}\s+keep=([01])"
)


def _handled_keep(content: str) -> bool | None:
    """Return True/False (keep/drop) if *content* carries our sentinel, else None."""
    m = _HANDLED_RE.search(content or "")
    if not m:
        return None
    return m.group(1) == "1"


class ImageUnderstandingRenderer(MarkdownRenderer):
    """MarkdownRenderer that suppresses marker's duplicate ``<img>`` for blocks
    handled by ImageUnderstandingProcessor (see module docstring)."""

    def extract_html(self, document, document_output, level=0):
        soup = BeautifulSoup(document_output.html, "html.parser")

        content_refs = soup.find_all("content-ref")
        ref_block_id = None
        images = {}
        for ref in content_refs:
            src = ref.get("src")
            sub_images = {}
            content = ""
            for item in document_output.children:
                if item.id == src:
                    content, sub_images_ = self.extract_html(
                        document, item, level + 1
                    )
                    sub_images.update(sub_images_)
                    ref_block_id = item.id
                    break

            if ref_block_id.block_type in self.image_blocks:
                keep = _handled_keep(content)
                if keep is None:
                    # Not one of our blocks: marker's exact default behaviour.
                    if self.extract_images:
                        image = self.extract_image(document, ref_block_id)
                        image_name = (
                            f"{ref_block_id.to_path()}."
                            f"{settings.OUTPUT_IMAGE_FORMAT.lower()}"
                        )
                        images[image_name] = image
                        element = BeautifulSoup(
                            f"<p>{content}<img src='{image_name}'></p>",
                            "html.parser",
                        )
                        ref.replace_with(
                            self.insert_block_id(element, ref_block_id)
                        )
                    else:
                        element = BeautifulSoup(f"{content}", "html.parser")
                        ref.replace_with(
                            self.insert_block_id(element, ref_block_id)
                        )
                else:
                    # Our block: register the image file (so ZIP/audit keep the
                    # bytes) but emit <img> only when keep=1. We own the tag.
                    if self.extract_images:
                        image = self.extract_image(document, ref_block_id)
                        image_name = (
                            f"{ref_block_id.to_path()}."
                            f"{settings.OUTPUT_IMAGE_FORMAT.lower()}"
                        )
                        images[image_name] = image
                        img_tag = (
                            f"<img src='{image_name}'>" if keep else ""
                        )
                        element = BeautifulSoup(
                            f"<p>{content}{img_tag}</p>", "html.parser"
                        )
                    else:
                        element = BeautifulSoup(f"{content}", "html.parser")
                    ref.replace_with(self.insert_block_id(element, ref_block_id))
            elif ref_block_id.block_type in self.page_blocks:
                images.update(sub_images)
                if self.paginate_output:
                    content = (
                        f"<div class='page' data-page-id="
                        f"'{ref_block_id.page_id}'>{content}</div>"
                    )
                element = BeautifulSoup(f"{content}", "html.parser")
                ref.replace_with(self.insert_block_id(element, ref_block_id))
            else:
                images.update(sub_images)
                element = BeautifulSoup(f"{content}", "html.parser")
                ref.replace_with(self.insert_block_id(element, ref_block_id))

        output = str(soup)
        if level == 0:
            output = self.merge_consecutive_tags(output, "b")
            output = self.merge_consecutive_tags(output, "i")
            output = self.merge_consecutive_math(output)
            import textwrap

            output = textwrap.dedent(
                f"""
            <!DOCTYPE html>
            <html>
                <head>
                    <meta charset="utf-8" />
                </head>
                <body>
                    {output}
                </body>
            </html>
"""
            )

        return output, images
