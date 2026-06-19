"""Image-understanding processor for marker-pdf Picture blocks.

The VLM extraction result is written into ``Picture.html`` as **HTML**, not
Markdown. marker renders every document by serialising the block tree to HTML
and then running it through ``markdownify`` (for the Markdown renderer) or
returning it verbatim (for the HTML / JSON renderers). markdownify *escapes*
Markdown metacharacters in raw text nodes (``$$`` -> ``\\$\\$``, ``a_1`` ->
``a\\_1``, ``**x**`` -> ``\\*\\*x\\*\\*``), so injecting raw Markdown here would
corrupt LaTeX, Mermaid, and bold in the final output. Emitting HTML lets
marker's own renderers convert it cleanly and uniformly — the same approach
marker uses for every other block type.
"""

from __future__ import annotations

import html as _html
import logging
from io import BytesIO
from typing import Any

from marker.processors import BaseProcessor
from marker.schema import BlockTypes

from app.models.image_understanding import (
    ImageHandlingMode,
    ImageType,
    RouteDecision,
    RouteKind,
)
from app.services.vlm_service import VLMService

logger = logging.getLogger(__name__)

# Sentinel for "local OCR service not yet built" (distinct from "built, None").
_UNSET: Any = object()

# Sentinel comment our processor writes into ``picture.html`` for every block it
# handles. ImageUnderstandingRenderer keys off it to take ownership of <img>
# emission for that block (marker force-appends one <img> per image block; we
# suppress that duplicate). The token also encodes the keep/drop intent:
#   keep=1 -> renderer keeps marker's single <img> (augment: chart/diagram/...)
#   keep=0 -> renderer drops it (replace/decorative: image truly omitted)
# markdownify strips HTML comments, so this never reaches the final markdown.
IU_HANDLED_PREFIX = "marker-ui-iu-handled"


def _handled_marker(keep: bool) -> str:
    return f"<marker-comment>{IU_HANDLED_PREFIX} keep={1 if keep else 0}</marker-comment>"


# ---------------------------------------------------------------------------
# markdownify patches (applied once at import).
#
# marker constructs ``Markdownify`` without a ``code_language_callback`` and has
# no converter for our ``<marker-comment>`` sidecar tag, so we add both here.
# Patching the class (not an instance) is the only injection point, since the
# Markdown renderer builds a fresh ``Markdownify`` per call.
# ---------------------------------------------------------------------------

try:
    from marker.renderers.markdown import Markdownify

    def _convert_marker_comment(self, el, text, parent_tags):
        # Carry per-image metadata into the Markdown as an HTML comment so it
        # survives for downstream LLMs / grep without rendering visibly.
        content = el.get_text() or ""
        return f"\n<!-- {content} -->\n"

    Markdownify.convert_marker_comment = _convert_marker_comment

    _orig_convert_pre = Markdownify.convert_pre

    def _convert_pre(self, el, text, parent_tags):
        # Preserve the ```<lang> info string from <code class="language-xxx">.
        # Without this, our Mermaid fences collapse to a bare ``` fence and no
        # renderer (or react-markdown) can identify them as Mermaid.
        code_el = el.find("code") if hasattr(el, "find") else None
        lang = ""
        if code_el is not None and code_el.has_attr("class"):
            for cls in code_el["class"]:
                if cls.startswith("language-"):
                    lang = cls[len("language-"):]
                    break
        if not lang:
            return _orig_convert_pre(self, el, text, parent_tags)
        if not text:
            return ""
        return f"\n\n```{lang}\n{text}\n```\n\n"

    Markdownify.convert_pre = _convert_pre
except ImportError:
    pass


class ImageUnderstandingProcessor(BaseProcessor):
    """Mutate Picture blocks in-place with VLM-derived text.

    The default mode is ``extraction`` so existing marker image extraction is
    unchanged unless a caller explicitly chooses ``understanding`` or ``both``.
    """

    block_types = (
        BlockTypes.Picture,
        BlockTypes.PictureGroup,
        BlockTypes.Figure,
        BlockTypes.FigureGroup,
    )

    def __init__(
        self,
        config: Any | None = None,
        vlm_service: Any | None = None,
        detection_model: Any | None = None,
        recognition_model: Any | None = None,
        ocr_error_model: Any | None = None,
        layout_model: Any | None = None,
    ) -> None:
        super().__init__(config)
        cfg = config if isinstance(config, dict) else {}
        self.image_handling_mode = ImageHandlingMode(
            cfg.get("image_handling_mode", ImageHandlingMode.extraction)
        )
        self.vlm_model = cfg.get("vlm_model")
        self.max_images_per_doc = int(cfg.get("max_images_per_doc", 50))
        self.context_window_size = int(cfg.get("context_window_size", 2))
        self.include_original_ref = bool(cfg.get("include_original_ref", True))
        self.router_enabled = bool(cfg.get("router_enabled", True))
        self.allow_cloud_vlm = bool(cfg.get("allow_cloud_vlm", False))
        self.dedup_enabled = bool(cfg.get("dedup_enabled", True))
        self.dedup_max_distance = int(cfg.get("dedup_max_distance", 0))
        self.downscale_vlm_crops = bool(cfg.get("downscale_vlm_crops", True))
        self.vlm_crop_max_px = int(cfg.get("vlm_crop_max_px", 768))
        self.batch_enabled = bool(cfg.get("batch_enabled", True))
        self.vlm_batch_size = int(cfg.get("vlm_batch_size", 8))
        self.max_batch_retries = int(cfg.get("max_batch_retries", 2))
        self.ocr_engine = str(cfg.get("ocr_engine", "surya"))
        self._vlm_service = vlm_service
        # Surya models injected by marker's resolve_dependencies (param name ==
        # artifact_dict key). Used by the Tier-0 router (detection) and Tier-2
        # local OCR (recognition + detection). Absent in torch-less test envs;
        # the router then degrades to the VLM route.
        self._detection_model = detection_model
        self._recognition_model = recognition_model
        self._ocr_error_model = ocr_error_model
        self._layout_model = layout_model
        self._router = self._build_router(cfg)
        # Tier-2 local OCR service, built lazily on first ocr-routed image.
        self._local_ocr: Any = _UNSET
        # Dedup cache: aHash -> the rendered html the original block received,
        # so a repeated image is fanned back without a second model call.
        self._dedup_cache: list[tuple[str, str]] = []
        # Sidecar metadata collected during __call__ for the badge UI.
        # marker's MarkdownRenderer strips HTML comments, so a <!-- ... -->
        # channel does not survive to output. Instead we collect per-image
        # metadata here and marker_service reads it after the converter runs.
        self._image_meta: list[dict[str, Any]] = []

    def _build_router(self, cfg: dict[str, Any]) -> Any | None:
        """Construct the Tier-0 router, or None when disabled.

        Returns None when ``router_enabled`` is False so ``__call__`` takes the
        legacy per-image classify+extract path (the §7 escape hatch). When
        enabled but no detection model is injected, the router still builds and
        degrades every image to the VLM route.
        """
        if not self.router_enabled:
            return None
        from app.processors.image_router import ImageRouter

        return ImageRouter(
            detection_model=self._detection_model,
            layout_model=self._layout_model,
            config={
                **cfg,
                "allow_cloud_vlm": self.allow_cloud_vlm,
                "smart_router_level": cfg.get("smart_router_level", "smart"),
            },
        )

    @property
    def image_meta(self) -> list[dict[str, Any]]:
        """Per-image classification metadata for the badge UI (sidecar channel)."""
        return self._image_meta

    def __call__(self, document: Any, *args: Any, **kwargs: Any) -> None:
        if self.image_handling_mode == ImageHandlingMode.extraction:
            return

        pictures = list(
            document.contained_blocks([BlockTypes.Picture, BlockTypes.Figure])
        )
        logger.info(
            "ImageUnderstanding start: mode=%s model=%s pictures=%d max=%d",
            self.image_handling_mode.value,
            self._resolved_model_id() or "unknown",
            len(pictures),
            self.max_images_per_doc,
        )
        processed = 0
        skipped_no_image = 0
        skipped_limit = 0
        failed = 0
        deduped = 0
        routed = {"skip_decorative": 0, "ocr": 0, "vlm": 0}
        # VLM-routed images are collected here and drained in batches after the
        # route loop, so one structured call handles many images (plan §3).
        vlm_queue: list[dict[str, Any]] = []
        for picture in pictures:
            if processed >= self.max_images_per_doc:
                skipped_limit += 1
                continue

            image = _picture_to_image(picture, document)
            if image is None:
                skipped_no_image += 1
                continue

            # Dedup: an identical image already extracted this run is fanned
            # back without a second model call (plan §8a).
            image_hash = self._image_hash(image)
            cached = self._lookup_dedup(image_hash)
            if cached is not None:
                self._replay_outcome(picture, cached)
                deduped += 1
                processed += 1
                continue

            heading_chain, surrounding = gather_local_context(
                document,
                picture,
                n=self.context_window_size,
            )

            decision = self._route(image)
            routed[decision.route.value] = routed.get(decision.route.value, 0) + 1

            if decision.route == RouteKind.skip_decorative:
                self._apply_decorative(picture, decision)
                self._store_dedup(
                    image_hash, {"kind": "decorative", "vlm_decided": False}
                )
                processed += 1
                continue

            if decision.route == RouteKind.ocr:
                ocr_outcome = self._apply_local_ocr(picture, image, decision)
                if ocr_outcome is not None:
                    self._store_dedup(image_hash, ocr_outcome)
                    processed += 1
                    continue
                # OCR produced nothing usable: escalate to the VLM rather than
                # emit token soup (plan §5 self-correction / escalation gate).
                logger.debug("OCR empty; escalating image to VLM")
                routed["ocr"] -= 1
                routed["vlm"] = routed.get("vlm", 0) + 1

            # Privacy gate (plan §11a): with cloud disabled, no image leaves the
            # box. The router already degrades visual->ocr when it has a
            # detection model; this guard covers the router-off / no-model paths
            # so a vlm route never silently turns into a cloud send. Try local
            # OCR as a last resort, else skip the image untouched.
            if not self.allow_cloud_vlm:
                routed["vlm"] = max(0, routed.get("vlm", 0) - 1)
                ocr_outcome = self._apply_local_ocr(picture, image, decision)
                if ocr_outcome is not None:
                    routed["ocr"] = routed.get("ocr", 0) + 1
                    self._store_dedup(image_hash, ocr_outcome)
                    processed += 1
                else:
                    logger.debug(
                        "Cloud VLM disabled and local OCR empty; skipping image"
                    )
                    skipped_no_image += 1
                continue

            # Defer the cloud send: queue this image for batched extraction.
            # Downscale the crop here (the cheapest large cost lever, §8) — OCR
            # and dedup already ran on the full-res image.
            vlm_queue.append(
                {
                    "picture": picture,
                    "image": image,
                    "image_bytes": _image_to_png_bytes(self._vlm_crop(image)),
                    "heading_chain": heading_chain,
                    "surrounding": surrounding,
                    "image_hash": image_hash,
                }
            )

        # Drain the VLM queue in batches (or one-by-one when batching is off).
        vlm_ok, vlm_failed = self._drain_vlm_queue(vlm_queue)
        processed += vlm_ok
        failed += vlm_failed

        logger.info(
            "ImageUnderstanding done: processed=%d failed=%d "
            "skipped_no_image=%d skipped_over_limit=%d deduped=%d total=%d "
            "routed_decorative=%d routed_ocr=%d routed_vlm=%d",
            processed,
            failed,
            skipped_no_image,
            skipped_limit,
            deduped,
            len(pictures),
            routed["skip_decorative"],
            routed["ocr"],
            routed["vlm"],
        )

    def _drain_vlm_queue(self, queue: list[dict[str, Any]]) -> tuple[int, int]:
        """Process all VLM-routed images; returns (processed_ok, failed).

        Identical queued images (same aHash) are collapsed to one representative
        sent to the model, then the result is fanned back to every duplicate
        (plan §8a dedup applied across the whole batch). When ``batch_enabled``
        is off, each unique image takes the legacy two-call path.
        """
        if not queue:
            return 0, 0

        # Group by hash so duplicates share one extraction. Items with no hash
        # (dedup disabled / hashing failed) each form their own group.
        groups: list[list[dict[str, Any]]] = []
        by_hash: dict[str, list[dict[str, Any]]] = {}
        for item in queue:
            h = item.get("image_hash")
            if h is None:
                groups.append([item])
            elif h in by_hash:
                by_hash[h].append(item)
            else:
                bucket = [item]
                by_hash[h] = bucket
                groups.append(bucket)

        representatives = [g[0] for g in groups]
        model_id = self._resolved_model_id()

        if self.batch_enabled:
            extractions = self._run_batch(representatives)
        else:
            extractions = self._run_serial(representatives)

        ok = 0
        failed = 0
        for group, extraction in zip(groups, extractions):
            if extraction is not None and getattr(extraction, "route", None) == "decorative":
                for item in group:
                    self._apply_decorative(
                        item["picture"],
                        RouteDecision(
                            route=RouteKind.skip_decorative,
                            reason="vlm decorative",
                        ),
                        vlm_decided=True,
                    )
                    ok += 1
                self._store_dedup(
                    group[0].get("image_hash"),
                    {"kind": "decorative", "vlm_decided": True},
                )
                continue

            if extraction is not None and getattr(extraction, "route", None) == "ocr_sufficient":
                for item in group:
                    outcome = self._apply_local_ocr(
                        item["picture"],
                        item["image"],
                        RouteDecision(route=RouteKind.ocr, reason="vlm ocr_sufficient"),
                    )
                    if outcome is None:
                        failed += 1
                    else:
                        self._store_dedup(item.get("image_hash"), outcome)
                        ok += 1
                continue

            if extraction is None or extraction.error:
                if extraction is not None and extraction.error:
                    logger.warning(
                        "Batch extraction failed: %s", extraction.error
                    )
                failed += len(group)
                continue

            outcome = {
                "kind": "vlm",
                "image_type": extraction.image_type,
                "payload": extraction.payload,
                "confidence": float(extraction.confidence),
                "model": model_id,
                "cost_usd": float(getattr(extraction, "cost_usd", 0.0) or 0.0),
            }
            # Fan the single extraction back to every duplicate in the group.
            # Only the representative carries the cost — the duplicates were
            # served from cache and cost nothing (avoids double-counting spend).
            for n_in_group, item in enumerate(group):
                replay = outcome if n_in_group == 0 else {**outcome, "cost_usd": 0.0}
                self._replay_outcome(item["picture"], replay)
                ok += 1
            self._store_dedup(group[0].get("image_hash"), outcome)
        return ok, failed

    def _run_batch(self, items: list[dict[str, Any]]) -> list[Any | None]:
        """Run batched classify+extract over representative items.

        Splits into ``vlm_batch_size`` chunks. Any provider/transport failure on
        a chunk yields ``None`` for each item in it (counted as failed), never
        raising — one bad batch never aborts the document.

        Capability-detect: a service that does not implement
        ``classify_and_extract_batch`` (e.g. a minimal client, or a provider
        without structured-output support) transparently degrades to the serial
        two-call path (plan §3.3 — capability-detect, don't assume).
        """
        from app.prompts.image_batch import BatchItem

        service = self._get_vlm_service()
        if not hasattr(service, "classify_and_extract_batch"):
            logger.debug("VLM service has no batch method; using serial path")
            return self._run_serial(items)

        results: list[Any | None] = []
        size = max(1, self.vlm_batch_size)
        for start in range(0, len(items), size):
            chunk = items[start : start + size]
            batch_items = [
                BatchItem(
                    image_bytes=it["image_bytes"],
                    mime_type="image/png",
                    heading_chain=it["heading_chain"],
                    surrounding=it["surrounding"],
                )
                for it in chunk
            ]
            try:
                chunk_results = service.classify_and_extract_batch(
                    batch_items, max_retries=self.max_batch_retries
                )
            except Exception as exc:  # noqa: BLE001 - fail-soft
                logger.warning("Batch call raised (%r); marking chunk failed", exc)
                chunk_results = []
            # Align to chunk length: pad short responses with None.
            for i in range(len(chunk)):
                results.append(
                    chunk_results[i] if i < len(chunk_results) else None
                )
        return results

    def _run_serial(self, items: list[dict[str, Any]]) -> list[Any | None]:
        """Legacy two-call path per representative image (batching disabled)."""
        service = self._get_vlm_service()
        results: list[Any | None] = []
        for it in items:
            try:
                classification = service.classify(
                    it["image_bytes"], "image/png",
                    it["heading_chain"], it["surrounding"],
                )
                extraction = service.extract(
                    it["image_bytes"], "image/png", classification.image_type,
                    it["heading_chain"], it["surrounding"],
                )
                # Carry the classifier confidence onto the extraction so the
                # badge shows the routing confidence, matching the old path.
                if not extraction.error:
                    extraction.confidence = float(classification.confidence)
                    extraction.image_type = classification.image_type
                results.append(extraction)
            except Exception as exc:  # noqa: BLE001 - fail-soft
                logger.warning("Serial VLM call raised (%r)", exc)
                results.append(None)
        return results

    def _route(self, image: Any) -> RouteDecision:
        """Tier-0 route for one image; defaults to VLM when the router is off.

        With ``router_enabled=False`` (or no router built) every image takes the
        ``vlm`` route, reproducing the legacy classify+extract behaviour exactly.
        """
        if self._router is None:
            return RouteDecision(route=RouteKind.vlm, reason="router disabled")
        decision = self._router.route(image)
        logger.debug("ImageRouter -> %s (%s)", decision.route.value, decision.reason)
        return decision

    def _apply_decorative(
        self, picture: Any, decision: RouteDecision, *, vlm_decided: bool = False
    ) -> None:
        """Omit a decorative image (plan §2 skip path).

        Bug #4 part 1 (honest origin): a router skip_decorative is a LOCAL
        decision — no VLM call ran — so recording ``model=<gemini...>`` is
        misleading metadata. We record ``router-local`` for the local router and
        the real VLM model id only when the VLM itself returned route=decorative
        (``vlm_decided=True``).

        Bug #4 part 2 (no clutter): a truly-omitted image leaves NO visible
        "Decorative element omitted." stub and NO ``<img>``. We write only the
        keep=0 handled-marker sentinel + the metadata comment, so the renderer
        drops marker's forced image and nothing renders into the markdown. The
        image file is still registered (keep=0 path) so ZIP/audit retains bytes.
        """
        model_id = self._resolved_model_id() if vlm_decided else "router-local"
        meta = {
            "image_name": _picture_image_name(picture),
            "image_type": ImageType.decorative.value,
            "confidence": 1.0,
            "model": model_id,
            "omitted": True,
            "duration_ms": 0,
        }
        meta_str = (
            f"marker-ui image-understanding: "
            f"type={meta['image_type']} model={meta['model']} "
            f"confidence={float(meta['confidence']):.2f} "
            f"cost_usd=0.000000 duration_ms={meta['duration_ms']}"
        )
        picture.html = "\n".join(
            [
                _handled_marker(False),
                f"<marker-comment>{_escape(meta_str)}</marker-comment>",
            ]
        )
        picture.description = None
        self._record_meta(meta)

    def _record_meta(self, meta: dict[str, Any]) -> None:
        """Append the badge-relevant subset of ``meta`` to the sidecar stash."""
        self._image_meta.append(
            {
                "image_name": meta["image_name"],
                "image_type": meta["image_type"],
                "confidence": meta["confidence"],
                "model": meta["model"],
                "omitted": meta["omitted"],
                "cost_usd": float(meta.get("cost_usd", 0.0) or 0.0),
            }
        )

    # -----------------------------------------------------------------
    # Dedup (plan §8a)
    # -----------------------------------------------------------------

    def _vlm_crop(self, image: Any) -> Any:
        """Return the image to send to the cloud VLM, downscaled if enabled.

        Downscaling only affects the cloud send (plan §8); the OCR path and
        dedup hash already ran on the full-resolution crop.
        """
        if not self.downscale_vlm_crops:
            return image
        from app.utils.image_downscale import downscale_to_max

        return downscale_to_max(image, self.vlm_crop_max_px)

    def _image_hash(self, image: Any) -> str | None:
        """aHash for dedup, or None when dedup is off / hashing failed."""
        if not self.dedup_enabled:
            return None
        from app.utils.image_hash import average_hash

        return average_hash(image)

    def _lookup_dedup(self, image_hash: str | None) -> dict[str, Any] | None:
        """Return a cached outcome for an image within the dedup distance."""
        if image_hash is None or not self._dedup_cache:
            return None
        from app.utils.image_hash import hamming_distance

        for cached_hash, outcome in self._dedup_cache:
            if hamming_distance(image_hash, cached_hash) <= self.dedup_max_distance:
                return outcome
        return None

    def _store_dedup(self, image_hash: str | None, outcome: dict[str, Any]) -> None:
        """Remember an outcome so a later identical image can be fanned back."""
        if image_hash is None:
            return
        self._dedup_cache.append((image_hash, outcome))

    def _replay_outcome(self, picture: Any, outcome: dict[str, Any]) -> None:
        """Re-apply a cached extraction outcome onto a duplicate block.

        The cached *outcome* (not the raw HTML) is replayed so the new block's
        own image name regenerates correctly in the kept-original link.
        """
        kind = outcome.get("kind")
        if kind == "decorative":
            self._apply_decorative(
                picture,
                RouteDecision(route=RouteKind.skip_decorative, reason="dedup"),
                vlm_decided=bool(outcome.get("vlm_decided", False)),
            )
            return

        if kind == "ocr":
            meta = {
                "image_name": _picture_image_name(picture),
                "image_type": ImageType.other.value,
                "confidence": 1.0,
                "model": "local-ocr",
                "omitted": False,
                "duration_ms": 0,
            }
            _apply_ocr_html(
                picture,
                ocr_html=outcome.get("ocr_html", ""),
                meta=meta,
            )
            self._record_meta(meta)
            return

        # VLM outcome.
        image_type = outcome.get("image_type", ImageType.other)
        if image_type == ImageType.decorative:
            # A VLM that classified the image as decorative omits it the same way
            # the router does — no stub, no <img> (bug #4). The VLM made this
            # call, so the honest origin is the real model id.
            self._apply_decorative(
                picture,
                RouteDecision(route=RouteKind.skip_decorative, reason="vlm decorative"),
                vlm_decided=True,
            )
            return
        meta = {
            "image_name": _picture_image_name(picture),
            "image_type": image_type.value,
            "confidence": float(outcome.get("confidence", 0.0)),
            "model": outcome.get("model"),
            "omitted": image_type == ImageType.decorative,
            "duration_ms": 0,
            "cost_usd": float(outcome.get("cost_usd", 0.0) or 0.0),
        }
        _mutate_picture(
            picture,
            image_type=image_type,
            payload=outcome.get("payload", {}),
            mode=self.image_handling_mode,
            meta=meta,
            include_original_ref=self.include_original_ref,
        )
        self._record_meta(meta)

    def _apply_local_ocr(
        self, picture: Any, image: Any, decision: RouteDecision
    ) -> dict[str, Any] | None:
        """Transcribe a text-as-image locally and write it to the block.

        Returns a dedup outcome dict when usable text was recovered and applied;
        None when OCR produced nothing (the caller then escalates to the VLM).
        No cloud token is spent here — the deterministic line-227 fix (§5b).
        """
        ocr = self._get_local_ocr()
        if ocr is None or not ocr.available:
            return None
        result = ocr.recognize(image)
        if result.error or not result.html:
            logger.debug(
                "Local OCR no output (%s); lines=%d",
                result.error or "empty",
                result.line_count,
            )
            return None

        meta = {
            "image_name": _picture_image_name(picture),
            # text-as-image transcription is its own outcome; record it as a
            # table_image-free "other" text result. The badge shows model=local.
            "image_type": ImageType.other.value,
            "confidence": 1.0,
            "model": "local-ocr",
            "omitted": False,
            "duration_ms": result.duration_ms,
        }
        _apply_ocr_html(
            picture,
            ocr_html=result.html,
            meta=meta,
        )
        self._record_meta(meta)
        logger.info(
            "Local OCR transcribed %d lines (%dms) for %s",
            result.line_count,
            result.duration_ms,
            meta["image_name"],
        )
        return {"kind": "ocr", "ocr_html": result.html}

    def _get_local_ocr(self) -> Any | None:
        """Lazily build the Tier-2 OCR engine behind the pluggable seam (§5).

        Routes through ``build_ocr_engine`` so a future engine swap is a config
        change, not a processor edit. Returns None when no recognition model is
        injected (torch-less env) or the configured engine cannot be built.
        """
        if self._local_ocr is _UNSET:
            if self._recognition_model is None:
                self._local_ocr = None
            else:
                from app.services.ocr_engine import build_ocr_engine

                try:
                    self._local_ocr = build_ocr_engine(
                        self.ocr_engine,
                        recognition_model=self._recognition_model,
                        detection_model=self._detection_model,
                    )
                except (NotImplementedError, ValueError) as exc:
                    logger.warning(
                        "OCR engine %r unavailable (%s); local OCR disabled",
                        self.ocr_engine,
                        exc,
                    )
                    self._local_ocr = None
        return self._local_ocr

    def _get_vlm_service(self) -> VLMService:
        if self._vlm_service is None:
            self._vlm_service = VLMService(model_id=self.vlm_model)
        return self._vlm_service

    def _resolved_model_id(self) -> str | None:
        """Best-effort model id used for extraction, for the badge modal."""
        if self._vlm_service is None and not self.allow_cloud_vlm:
            return self.vlm_model or "local-only"
        try:
            service = self._get_vlm_service()
            return getattr(service, "model_id", None) or self.vlm_model
        except Exception:  # noqa: BLE001 - metadata is best effort
            return self.vlm_model


def _safe_prev_block(document: Any, block: Any) -> Any | None:
    """document.get_prev_block that degrades to None on a detached block.

    marker's page.get_prev_block does ``structure.index(block.id)``, which raises
    ValueError when the block exists in the tree but was dropped from its page's
    structure list (e.g. a Figure filtered post-layout). Local context is
    best-effort metadata, so a detached block yields no context instead of
    crashing the whole conversion.
    """
    try:
        return document.get_prev_block(block)
    except ValueError:
        return None


def _safe_next_block(document: Any, block: Any) -> Any | None:
    """document.get_next_block that degrades to None on a detached block (see above)."""
    try:
        return document.get_next_block(block)
    except ValueError:
        return None


def gather_local_context(document: Any, picture_block: Any, n: int = 2) -> tuple[str, str]:
    """Return heading chain and +/-N text-block context around a Picture."""
    headings: list[str] = []
    before: list[str] = []
    after: list[str] = []

    prev = _safe_prev_block(document, picture_block)
    while prev is not None:
        if getattr(prev, "block_type", None) == BlockTypes.SectionHeader:
            text = _block_text(prev, document)
            if text:
                headings.append(text)
            if (getattr(prev, "heading_level", None) or 0) <= 1:
                break
        prev = _safe_prev_block(document, prev)
    headings.reverse()

    prev = _safe_prev_block(document, picture_block)
    while prev is not None and len(before) < n:
        if getattr(prev, "block_type", None) == BlockTypes.Text:
            text = _block_text(prev, document)
            if text:
                before.append(text)
        prev = _safe_prev_block(document, prev)
    before.reverse()

    nxt = _safe_next_block(document, picture_block)
    while nxt is not None and len(after) < n:
        if getattr(nxt, "block_type", None) == BlockTypes.Text:
            text = _block_text(nxt, document)
            if text:
                after.append(text)
        nxt = _safe_next_block(document, nxt)

    return "\n".join(headings), "\n".join([*before, *after])


def _picture_to_image(picture: Any, document: Any) -> Any | None:
    """Return the cropped PIL image for a Picture/Figure block, or None."""
    return picture.get_image(document)


def _image_to_png_bytes(image: Any) -> bytes:
    """Serialise a PIL image to PNG bytes."""
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _picture_to_png_bytes(picture: Any, document: Any) -> bytes | None:
    image = _picture_to_image(picture, document)
    if image is None:
        return None
    return _image_to_png_bytes(image)


def _mutate_picture(
    picture: Any,
    *,
    image_type: ImageType,
    payload: dict[str, Any],
    mode: ImageHandlingMode,
    meta: dict[str, Any],
    include_original_ref: bool = True,
) -> None:
    """Replace a Picture block's output with the VLM textual representation.

    Writes **HTML** into ``picture.html`` (see module docstring for why HTML and
    not Markdown). Emits two channels from the single ``meta`` dict:
      * a ``<marker-comment>`` tag that markdownify (patched at import) renders
        as an HTML comment carrying per-image metadata for downstream LLMs/grep.
      * the rendered representation (table / mermaid / latex / description) as
        HTML the renderers convert uniformly.

    ``both`` mode keeps an ``<img>`` reference to the original image so the file
    stays linked for audit / ZIP packaging. In ``understanding`` mode the
    original is kept too for any type that is NOT safe to replace outright (the
    §6 augment gate): 2026 chart->data / diagram->mermaid extraction is not
    reliable enough to delete the source, so only ``equation`` (->LaTeX, robust)
    and ``decorative`` (->omitted) replace destructively. ``include_original_ref``
    can disable the kept link entirely.
    """
    rendered = render_extraction(image_type, payload)
    if not rendered:
        return

    image_name = meta["image_name"]
    keep_original = (
        include_original_ref
        and image_type != ImageType.decorative
        and (mode == ImageHandlingMode.both or not _safe_to_replace(image_type))
    )

    meta_str = (
        f"marker-ui image-understanding: "
        f"type={meta['image_type']} model={meta['model'] or 'unknown'} "
        f"confidence={float(meta['confidence']):.2f} "
        f"cost_usd={float(meta.get('cost_usd', 0.0) or 0.0):.6f} "
        f"duration_ms={meta['duration_ms']}"
    )
    html_parts = [
        _handled_marker(keep_original),
        f"<marker-comment>{_escape(meta_str)}</marker-comment>",
    ]
    if keep_original:
        html_parts.append(
            f"<marker-comment>original_image: {_escape(image_name)}</marker-comment>"
        )
    html_parts.append(rendered)

    # NOTE: we do NOT append our own <img> here. marker's renderer
    # unconditionally appends exactly one <img> per image block
    # (renderers/html.py: `<p>{content}<img src='name'></p>`). Emitting one here
    # too produced the double-embed (![](x)![](x)). The _handled_marker token
    # tells ImageUnderstandingRenderer to own <img> emission for this block:
    # keep marker's single <img> when keep_original, drop it otherwise (so a
    # replace/decorative image is truly omitted). markdownify strips the comment.
    picture.html = "\n".join(html_parts)
    picture.description = None


def _apply_ocr_html(
    picture: Any,
    *,
    ocr_html: str,
    meta: dict[str, Any],
) -> None:
    """Write locally-transcribed OCR text into a Picture block as HTML.

    Mirrors :func:`_mutate_picture` but takes pre-rendered HTML (the OCR
    transcription) rather than a typed payload.

    Bug #3: an ``ocr`` route means layout sent a *text-as-image* here — a body
    paragraph (or caption) that surya misdetected as a Picture/Figure. The
    transcription reproduces that text verbatim, so keeping the original ``<img>``
    of the very same text emits the content twice (the transcribed paragraph,
    then an image of it). The transcription fully supersedes the crop, so we emit
    the keep=0 sentinel and never re-embed the source. The image file is still
    registered by the renderer (keep=0 path) so ZIP / audit retains the bytes;
    only the redundant visible embed is removed.
    """
    meta_str = (
        f"marker-ui image-understanding: "
        f"type={meta['image_type']} model={meta['model'] or 'local-ocr'} "
        f"confidence={float(meta['confidence']):.2f} "
        f"cost_usd=0 duration_ms={meta['duration_ms']} route=ocr"
    )
    html_parts = [
        _handled_marker(False),
        f"<marker-comment>{_escape(meta_str)}</marker-comment>",
        ocr_html,
    ]

    # See _mutate_picture: the renderer owns <img> emission via the handled-marker.
    picture.html = "\n".join(html_parts)
    picture.description = None


def render_extraction(image_type: ImageType, payload: dict[str, Any]) -> str:
    """Render an extraction payload as HTML for marker's renderers.

    The Markdown renderer pipes this through markdownify (-> a Markdown table /
    fenced Mermaid block / ``$$`` math / prose); the HTML and JSON renderers
    keep it as-is. Returning Markdown here would be escaped by markdownify and
    corrupt LaTeX / Mermaid / bold — see the module docstring.
    """
    if image_type in _CHART_TYPES:
        return _render_chart(payload)
    if image_type == ImageType.table_image:
        return _render_table(payload)
    if image_type in _DIAGRAM_TYPES:
        return _render_diagram(payload)
    if image_type == ImageType.equation:
        return _render_equation(payload)
    if image_type == ImageType.screenshot_ui:
        return _render_screenshot(payload)
    return _render_description(payload)


def _render_chart(payload: dict[str, Any]) -> str:
    series = payload.get("series") or []
    if not series:
        return _render_description(payload)

    columns = ["x", *[str(s.get("name") or f"series_{i + 1}") for i, s in enumerate(series)]]
    x_values: list[Any] = []
    rows_by_x: dict[Any, dict[str, Any]] = {}
    for col, s in zip(columns[1:], series):
        for point in s.get("points") or []:
            x = point.get("x", "")
            if x not in rows_by_x:
                rows_by_x[x] = {}
                x_values.append(x)
            rows_by_x[x][col] = point.get("y", "")

    rows = [[x, *[rows_by_x[x].get(col, "") for col in columns[1:]]] for x in x_values]
    table = _table_html(columns, rows)

    title = str(payload.get("title", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    parts = []
    if title:
        parts.append(f"<p><strong>{_escape(title)}</strong></p>")
    parts.append(table)
    if notes:
        parts.append(f"<p>{_escape(notes)}</p>")
    return "\n".join(parts)


def _render_table(payload: dict[str, Any]) -> str:
    headers = [str(h) for h in payload.get("headers") or []]
    rows = payload.get("rows") or []
    if not headers and rows:
        headers = [f"Column {i + 1}" for i in range(len(rows[0]))]
    if not headers:
        return _render_description(payload)

    norm_rows = [
        list(row)[: len(headers)] + [""] * max(0, len(headers) - len(row))
        for row in rows
    ]
    table = _table_html(headers, norm_rows)
    caption = str(payload.get("caption", "")).strip()
    parts = []
    if caption:
        parts.append(f"<p>{_escape(caption)}</p>")
    parts.append(table)
    return "\n".join(parts)


def _render_diagram(payload: dict[str, Any]) -> str:
    mermaid = str(payload.get("mermaid", "")).strip()
    caption = str(payload.get("caption", "")).strip()
    # A diagram payload can arrive WITHOUT usable mermaid: the VLM may have
    # demoted a fabrication-prone figure to a {alt_text, details} description
    # (vlm_service._demote_to_description), or returned a description shape
    # directly. Emitting an empty ```mermaid fence in that case silently drops
    # the extracted content (and renders as a blank code block). Fall back to
    # the description renderer so the salvaged text survives for ANY diagram
    # type on any document.
    if not mermaid:
        desc = _render_description(payload)
        if desc:
            return desc
        if caption:
            return f"<p>{_escape(caption)}</p>"
        return ""
    parts = []
    if caption:
        parts.append(f"<p>{_escape(caption)}</p>")
    parts.append(
        f'<pre><code class="language-mermaid">{_escape(mermaid)}</code></pre>'
    )
    return "\n".join(parts)


def _render_equation(payload: dict[str, Any]) -> str:
    latex = str(payload.get("latex", "")).strip()
    caption = str(payload.get("caption", "")).strip()
    parts = []
    if caption:
        parts.append(f"<p>{_escape(caption)}</p>")
    parts.append(f'<math display="block">{_escape(latex)}</math>')
    return "\n".join(parts)


def _render_screenshot(payload: dict[str, Any]) -> str:
    app = str(payload.get("application", "")).strip()
    area = str(payload.get("area", "")).strip()
    if app or area:
        heading = f"Screenshot of {app or 'application'}: {area or 'screen'}"
    else:
        heading = "Screenshot"
    parts = [f"<h1>{_escape(heading)}</h1>"]
    summary = str(payload.get("summary", "")).strip()
    if summary:
        parts.append(f"<p>{_escape(summary)}</p>")

    items = []
    for region in payload.get("regions") or []:
        name = str(region.get("name", "Region")).strip() or "Region"
        desc = str(region.get("description", "")).strip()
        ocr = str(region.get("ocr_text", "")).strip()
        line = f"{name}: {desc}" + (f" Text: {ocr}" if ocr else "")
        items.append(f"<li>{_escape(line)}</li>")
    if items:
        parts.append("<ul>" + "".join(items) + "</ul>")
    return "\n".join(parts)


def _render_description(payload: dict[str, Any]) -> str:
    alt = str(payload.get("alt_text") or payload.get("summary") or "").strip()
    details = [str(d).strip() for d in payload.get("details") or [] if str(d).strip()]
    parts = []
    if alt:
        parts.append(f"<p>{_escape(alt)}</p>")
    if details:
        parts.append("<ul>" + "".join(f"<li>{_escape(d)}</li>" for d in details) + "</ul>")
    return "\n".join(parts)


def _block_text(block: Any, document: Any) -> str:
    raw_text = getattr(block, "raw_text", None)
    if callable(raw_text):
        try:
            return str(raw_text(document)).strip()
        except Exception:  # noqa: BLE001 - context is best effort
            pass
    return str(getattr(block, "html", "") or getattr(block, "text", "") or "").strip()


def _table_html(headers: list[Any], rows: list[list[Any]]) -> str:
    """Render an HTML table; markdownify converts it to a Markdown table and
    the HTML/JSON renderers keep it as-is. Cell content is HTML-escaped, so
    Markdown metacharacters in the data survive the round-trip intact."""
    head = "<tr>" + "".join(f"<th>{_escape(h)}</th>" for h in headers) + "</tr>"
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table>{head}{body}</table>"


def _escape(value: Any) -> str:
    return _html.escape(str(value), quote=True)


def _picture_image_name(picture: Any) -> str:
    """Predict the image filename marker's renderer will emit for this picture.

    MarkdownRenderer.extract_html builds image names as
    ``f"{block_id.to_path()}.{OUTPUT_IMAGE_FORMAT.lower()}"``. We mirror that
    here so the badge UI can pair sidecar metadata to rendered ``![](name)``
    tokens. Falls back to a stable string when the block id shape is unexpected.
    """
    from marker.settings import settings

    ext = str(settings.OUTPUT_IMAGE_FORMAT).lower()
    block_id = getattr(picture, "id", None)
    to_path = getattr(block_id, "to_path", None)
    if callable(to_path):
        return f"{to_path()}.{ext}"
    return f"{_picture_ref(picture)}.{ext}"


def _picture_ref(picture: Any) -> str:
    return str(getattr(picture, "id", None) or getattr(picture, "block_id", "unknown"))


_CHART_TYPES = {
    ImageType.chart_bar,
    ImageType.chart_line,
    ImageType.chart_pie,
    ImageType.chart_scatter,
    ImageType.chart_other,
}

_DIAGRAM_TYPES = {
    ImageType.diagram_flow,
    ImageType.diagram_sequence,
    ImageType.diagram_state,
    ImageType.diagram_class,
    ImageType.diagram_architecture,
}

# Types whose extraction is reliable enough to REPLACE the source image even in
# ``understanding`` mode (plan §6 / decision #4). Everything else (charts,
# diagrams, photos, screenshots, technical figures) is *augmented*: the original
# image is kept alongside the extraction because 2026 chart->data /
# diagram->mermaid is not reliable enough to delete the source. ``decorative``
# is handled separately (it is omitted, never augmented).
_REPLACE_SAFE_TYPES = {
    ImageType.equation,
    ImageType.decorative,
}


def _safe_to_replace(image_type: ImageType) -> bool:
    """True when ``image_type`` may replace the source image destructively."""
    return image_type in _REPLACE_SAFE_TYPES

