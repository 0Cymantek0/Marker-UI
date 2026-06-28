"""Service wrapping marker-pdf converters.

Uses marker's ConfigParser API for proper option handling,
renderer selection, and LLM service instantiation.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from app.services.model_tracker import tracker

logger = logging.getLogger(__name__)

_CONVERTERS: dict[str, type] | None = None


def _import_marker() -> None:
    global _CONVERTERS
    if _CONVERTERS is not None:
        return

    from marker.converters.pdf import PdfConverter
    from marker.converters.table import TableConverter
    from marker.converters.ocr import OCRConverter
    from marker.converters.extraction import ExtractionConverter

    _CONVERTERS = {
        "PdfConverter": PdfConverter,
        "TableConverter": TableConverter,
        "OCRConverter": OCRConverter,
        "ExtractionConverter": ExtractionConverter,
    }
    logger.info("marker-pdf converters imported: %s", list(_CONVERTERS.keys()))

    try:
        from marker.services.vertex import GoogleVertexService

        def patched_get_google_client(self, timeout: int):
            from google import genai
            from google.oauth2 import service_account
            import json

            http_options = {"timeout": timeout * 1000}
            if self.vertex_dedicated:
                http_options["headers"] = {"x-vertex-ai-llm-request-type": "dedicated"}

            project_id = self.vertex_project_id
            credentials = None

            # Resolve secret placeholder if present
            if project_id and project_id.startswith("secret:"):
                from app.core.api_manager import get_secret
                project_id = get_secret(project_id.replace("secret:", ""))

            # Handle JSON service account key
            if project_id and project_id.strip().startswith("{"):
                try:
                    info = json.loads(project_id)
                    credentials = service_account.Credentials.from_service_account_info(
                        info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
                    )
                    project_id = info.get("project_id", project_id)
                except Exception as exc:
                    logger.warning("Failed to parse Vertex service account JSON: %r", exc)

            return genai.Client(
                vertexai=True,
                project=project_id,
                location=self.vertex_location,
                credentials=credentials,
                http_options=http_options,
            )

        GoogleVertexService.get_google_client = patched_get_google_client
        logger.info("Successfully patched GoogleVertexService.get_google_client")
    except Exception as e:
        logger.warning("Failed to monkeypatch GoogleVertexService: %r", e)


LLM_SERVICE_MAP: dict[str, str] = {
    "gemini": "marker.services.gemini.GoogleGeminiService",
    "openai": "marker.services.openai.OpenAIService",
    "custom_openai": "marker.services.openai.OpenAIService",
    "claude": "marker.services.claude.ClaudeService",
    # Custom Anthropic routes through our subclass so base_url is set on the
    # client instance instead of mutating the process-global ANTHROPIC_BASE_URL
    # env var (UCM-004.5). That keeps concurrent custom-anthropic jobs isolated.
    "custom_anthropic": "app.services.custom_anthropic_service.CustomAnthropicService",
    "ollama": "marker.services.ollama.OllamaService",
    "azure": "marker.services.azure_openai.AzureOpenAIService",
    "vertex": "marker.services.vertex.GoogleVertexService",
}

IMAGE_UNDERSTANDING_PROCESSOR = (
    "app.processors.image_understanding.ImageUnderstandingProcessor"
)

# Custom Markdown renderer that owns <img> emission for image-understanding
# blocks (kills the double-embed; honours decorative/equation omission). Only
# swapped in for the markdown renderer in understanding/both mode — every other
# output format and extraction mode keeps marker's stock renderer untouched.
IMAGE_UNDERSTANDING_RENDERER = (
    "app.renderers.image_understanding_renderer.ImageUnderstandingRenderer"
)
_MARKER_MARKDOWN_RENDERER = "marker.renderers.markdown.MarkdownRenderer"
_MARKER_JSON_RENDERER = "marker.renderers.json.JSONRenderer"
_MARKER_HTML_RENDERER = "marker.renderers.html.HTMLRenderer"
_MARKER_CHUNK_RENDERER = "marker.renderers.chunk.ChunkRenderer"

# Canonical output formats the UI can request. Order is stable for display.
# These are the only formats the multi-format render path knows how to render
# from one parsed marker Document — anything else falls back to markdown.
_SUPPORTED_FORMATS: tuple[str, ...] = ("markdown", "json", "html", "chunks")


def _renderer_string_for_format(fmt: str, options: dict[str, Any]) -> str:
    """Resolve the marker renderer dotted-path for one output format.

    The image-understanding renderer is swapped in ONLY for the markdown
    format when understanding/both mode is active — every other format keeps
    marker's stock renderer, matching the single-format ``_select_renderer``
    contract so behaviour never silently changes for an existing format.
    """
    mode = options.get("image_handling_mode")
    if fmt == "markdown":
        if mode in ("understanding", "both"):
            return IMAGE_UNDERSTANDING_RENDERER
        return _MARKER_MARKDOWN_RENDERER
    if fmt == "json":
        return _MARKER_JSON_RENDERER
    if fmt == "html":
        return _MARKER_HTML_RENDERER
    if fmt == "chunks":
        return _MARKER_CHUNK_RENDERER
    return _MARKER_MARKDOWN_RENDERER


def _select_renderer(options: dict[str, Any], default_renderer: str) -> str:
    """Swap in our renderer only when it actually applies.

    Conditions: image understanding is active (understanding/both) AND marker
    resolved the stock Markdown renderer. Any explicit non-markdown output
    (json/html/chunks) or extraction mode falls through to ``default_renderer``
    unchanged, so we never alter behaviour we don't own.
    """
    mode = options.get("image_handling_mode")
    if mode in ("understanding", "both") and default_renderer == _MARKER_MARKDOWN_RENDERER:
        return IMAGE_UNDERSTANDING_RENDERER
    return default_renderer

# marker's native image-description processor. It handles the SAME block types
# our processor does (Picture + Figure) and runs EARLIER in the pipeline, so in
# understanding/both modes it makes a paid LLM call on every image that our
# processor then overwrites — pure waste, and double-billing. We drop it from
# the default pipeline whenever our processor is active.
NATIVE_IMAGE_DESCRIPTION_PROCESSOR = (
    "marker.processors.llm.llm_image_description.LLMImageDescriptionProcessor"
)

# Custom config keys consumed by ImageUnderstandingProcessor.__init__.
# marker's ConfigParser.generate_config_dict() silently drops any key not
# present in its crawler.attr_set (verified: none of these are registered),
# so they must be re-injected after the parser runs to reach the processor.
IMAGE_UNDERSTANDING_CONFIG_KEYS: tuple[str, ...] = (
    "image_handling_mode",
    "vlm_model",
    "max_images_per_doc",
    "context_window_size",
    "include_original_ref",
    "router_enabled",
    "smart_router_level",
    "decorative_max_text_density",
    "ocr_min_text_density",
    "ocr_min_lines",
    "allow_cloud_vlm",
    "dedup_enabled",
    "dedup_max_distance",
    "downscale_vlm_crops",
    "vlm_crop_max_px",
    "batch_enabled",
    "vlm_batch_size",
    "max_batch_retries",
    "ocr_engine",
    "hybrid_ocr_profile",
    "hybrid_ocr_require_specialists",
)


def _default_pipeline_dotted_paths() -> list[str]:
    """Resolve marker's full default PdfConverter processor pipeline to dotted
    paths.

    marker's ``PdfConverter.__init__`` REPLACES the entire default pipeline the
    moment a non-None ``processor_list`` is passed (converters/pdf.py:122-125).
    So to add our processor without silently dropping every built-in structural
    and LLM processor (TableProcessor, LLMTableProcessor, LLMEquationProcessor,
    LLMImageDescriptionProcessor, ...), we must materialise the default list
    ourselves and append to it. Returns ``[]`` if marker can't be imported (e.g.
    a partial test env), so the caller falls back to the bare single-element
    list rather than crashing.
    """
    try:
        from marker.converters.pdf import PdfConverter
        from marker.util import classes_to_strings

        return list(classes_to_strings(PdfConverter.default_processors))
    except Exception as exc:  # noqa: BLE001 - degrade to bare list, never crash
        logger.warning(
            "Could not resolve marker default processor pipeline (%r); image "
            "understanding will run WITHOUT the default LLM/structural processors.",
            exc,
        )
        return []


def with_image_understanding_processor(
    options: dict[str, Any],
    processors: str | None,
) -> str | None:
    """Append the image-understanding processor when a non-extraction mode is requested.

    Critically, when the caller has NOT supplied an explicit processor list, we
    expand marker's full default pipeline to dotted paths first and append our
    processor to it. Returning a bare single-element list here would make marker
    replace the entire default pipeline (see ``_default_pipeline_dotted_paths``),
    silently disabling every built-in LLM processor whenever image understanding
    is on — the ISSUE-1 P0 regression. An explicit caller-supplied list is still
    honoured verbatim (intentional overrides are not clobbered).
    """
    mode = options.get("image_handling_mode")
    if mode not in ("understanding", "both"):
        return processors

    explicit = [p.strip() for p in (processors or "").split(",") if p.strip()]
    if explicit:
        # Caller chose an explicit pipeline: respect it, just add ours.
        if IMAGE_UNDERSTANDING_PROCESSOR not in explicit:
            explicit.append(IMAGE_UNDERSTANDING_PROCESSOR)
        return ",".join(explicit)

    # No explicit list: preserve marker's full default pipeline, drop the
    # redundant native image-description processor (ours supersedes it for
    # Picture + Figure blocks), then append ours.
    pipeline = [
        p
        for p in _default_pipeline_dotted_paths()
        if p != NATIVE_IMAGE_DESCRIPTION_PROCESSOR
    ]
    if IMAGE_UNDERSTANDING_PROCESSOR not in pipeline:
        pipeline.append(IMAGE_UNDERSTANDING_PROCESSOR)
    return ",".join(pipeline)


def build_marker_options(
    llm_config: dict[str, Any],
    conversion_config: dict[str, Any],
) -> dict[str, Any]:
    """Build the options dict that ConfigParser expects.

    Resolves selected/overridden LLM provider and model configurations.
    """
    options: dict[str, Any] = {}

    use_llm = conversion_config.get("use_llm", False)
    if use_llm:
        providers = llm_config.get("providers", [])
        active = llm_config.get("active", {})

        provider_id = conversion_config.get("llm_provider") or active.get("provider_id", "none")
        model_id = conversion_config.get("llm_model") or active.get("model_id", "")

        if provider_id == "none" or not provider_id:
            options.update({k: v for k, v in conversion_config.items() if k not in ("llm_provider", "llm_model")})
            return options

        prov = next((p for p in providers if p["id"] == provider_id), None)
        if not prov:
            options.update({k: v for k, v in conversion_config.items() if k not in ("llm_provider", "llm_model")})
            return options

        options["use_llm"] = True
        p_type = prov["type"]

        model_cfg = next((m for m in prov.get("models", []) if m["model_id"] == model_id), None)

        def_timeout = 60
        def_retries = 3
        def_output = 4096

        if p_type in ("gemini", "claude"):
            def_output = 8192
            def_timeout = 30
        elif p_type == "ollama":
            def_timeout = 120
        elif p_type == "openai" and model_id and "mini" in model_id:
            def_output = 4096

        timeout = (model_cfg.get("timeout") if model_cfg else None) or def_timeout
        max_retries = (model_cfg.get("max_retries") if model_cfg else None) or def_retries
        max_output = (model_cfg.get("max_output_tokens") if model_cfg else None) or def_output

        options["timeout"] = timeout
        options["max_retries"] = max_retries
        options["retry_wait_time"] = 3
        options["max_output_tokens"] = max_output

        secret_placeholder = f"secret:provider_{provider_id}_key_0_api_key"

        if p_type == "gemini":
            options["llm_service"] = LLM_SERVICE_MAP[p_type]
            options["gemini_api_key"] = secret_placeholder
            options["gemini_model_name"] = model_id
        elif p_type == "claude":
            options["llm_service"] = LLM_SERVICE_MAP[p_type]
            options["claude_api_key"] = secret_placeholder
            options["claude_model_name"] = model_id
        elif p_type == "custom_anthropic":
            # Pass base_url through marker options so our CustomAnthropicService
            # pins it on the client instance (UCM-004.5). Never mutate
            # os.environ here: that is process-global and would leak across
            # concurrent custom-anthropic jobs using different providers.
            options["llm_service"] = LLM_SERVICE_MAP[p_type]
            options["claude_api_key"] = secret_placeholder
            options["claude_model_name"] = model_id
            options["base_url"] = prov.get("base_url") or "https://api.anthropic.com/v1"
        elif p_type in ("openai", "custom_openai"):
            options["llm_service"] = LLM_SERVICE_MAP[p_type]
            options["openai_api_key"] = secret_placeholder
            options["openai_base_url"] = prov.get("base_url") or "https://api.openai.com/v1"
            options["openai_model"] = model_id
        elif p_type == "ollama":
            options["llm_service"] = LLM_SERVICE_MAP[p_type]
            options["ollama_base_url"] = prov.get("base_url") or "http://localhost:11434"
            options["ollama_model"] = model_id
        elif p_type == "azure":
            options["llm_service"] = LLM_SERVICE_MAP[p_type]
            options["azure_api_key"] = secret_placeholder
            options["azure_endpoint"] = prov.get("base_url") or ""
            options["azure_api_version"] = "2023-05-15"
            options["deployment_name"] = model_id
        elif p_type == "vertex":
            options["llm_service"] = LLM_SERVICE_MAP[p_type]
            options["vertex_project_id"] = secret_placeholder
            options["vertex_location"] = prov.get("base_url") or "us-central1"
            options["gemini_model_name"] = model_id

    options.update({k: v for k, v in conversion_config.items() if k not in ("llm_provider", "llm_model")})
    processors = with_image_understanding_processor(
        options,
        options.get("processors"),
    )
    if processors is not None:
        options["processors"] = processors
    return options


# ---------------------------------------------------------------------------
# OOM safety net (ISSUE-5)
#
# Surya runs at its built-in CUDA defaults (e.g. RECOGNITION_BATCH_SIZE=256).
# On a small card (6 GB) a dense page can blow past VRAM and raise ``torch.cuda.OutOfMemoryError``, which would
# otherwise crash the whole job. Instead we catch it, free the cache, halve the
# batch sizes on the shared surya predictors, and retry. The lowered batch sizes
# persist on the predictor singletons, so once a card has shown its ceiling the
# rest of the run stays under it.
# ---------------------------------------------------------------------------

# Predictors on the marker model_dict whose batch_size we shrink on OOM. The
# Layout/Recognition predictors wrap a FoundationPredictor that carries the
# memory-dominant recognition batch, so we descend into it too.
_OOM_RETRY_LIMIT = 3
_MIN_BATCH_SIZE = 1


def _is_cuda_oom(exc: BaseException) -> bool:
    """True if ``exc`` is a CUDA out-of-memory error.

    ``torch.cuda.OutOfMemoryError`` is the typed case, but some code paths raise
    a plain ``RuntimeError('CUDA out of memory ...')``, so we match the message
    too. Importing torch lazily keeps this usable in a torch-less test env.
    """
    try:
        import torch

        if isinstance(exc, torch.cuda.OutOfMemoryError):
            return True
    except Exception:  # noqa: BLE001 - torch missing / no cuda attr
        pass
    return isinstance(exc, RuntimeError) and "out of memory" in str(exc).lower()


def _iter_batch_size_holders(model_dict: dict[str, Any] | None) -> list[Any]:
    """Collect every object on the model_dict that exposes a ``batch_size``.

    Descends one level into a wrapped ``foundation_predictor`` (Layout /
    Recognition predictors hold the memory-dominant recognition batch there).
    """
    holders: list[Any] = []
    for predictor in (model_dict or {}).values():
        if hasattr(predictor, "batch_size"):
            holders.append(predictor)
        inner = getattr(predictor, "foundation_predictor", None)
        if inner is not None and hasattr(inner, "batch_size"):
            holders.append(inner)
    return holders


def _halve_batch_sizes(model_dict: dict[str, Any] | None) -> bool:
    """Halve the resolved batch size on every surya predictor (floor 1).

    Returns True if at least one batch size actually dropped, False if every
    holder is already at the floor (so the caller can stop retrying).
    """
    lowered = False
    for holder in _iter_batch_size_holders(model_dict):
        current = holder.batch_size
        if current is None:
            # None means "use the device default" — resolve it so we can shrink.
            getter = getattr(holder, "get_batch_size", None)
            if callable(getter):
                try:
                    current = getter()
                except Exception:  # noqa: BLE001 - fall back to leaving as-is
                    current = None
        if not isinstance(current, int) or current <= _MIN_BATCH_SIZE:
            continue
        holder.batch_size = max(_MIN_BATCH_SIZE, current // 2)
        lowered = True
    return lowered


def _empty_cuda_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 - best effort; never block a retry
        pass


def run_with_oom_retry(
    convert: Any,
    model_dict: dict[str, Any] | None,
    *,
    limit: int = _OOM_RETRY_LIMIT,
) -> Any:
    """Call ``convert()``; on CUDA OOM, free cache + halve batches + retry.

    ``convert`` is a zero-arg callable that runs the conversion. Re-raises the
    OOM once batches can't shrink further or ``limit`` attempts are exhausted,
    so a genuinely-too-big job still surfaces instead of looping forever. Any
    non-OOM exception propagates immediately.
    """
    attempt = 0
    while True:
        try:
            return convert()
        except BaseException as exc:  # noqa: BLE001 - inspect, then re-raise
            if not _is_cuda_oom(exc):
                raise
            attempt += 1
            if attempt >= limit:
                logger.error(
                    "CUDA OOM after %d attempt(s); giving up. Last error: %r",
                    attempt,
                    exc,
                )
                raise
            _empty_cuda_cache()
            if not _halve_batch_sizes(model_dict):
                logger.error(
                    "CUDA OOM and batch sizes already at minimum; giving up."
                )
                raise
            logger.warning(
                "CUDA OOM (attempt %d/%d): freed cache, halved surya batch "
                "sizes, retrying.",
                attempt,
                limit,
            )


class MarkerService:
    """Manages marker-pdf model loading and document conversion."""

    def __init__(self) -> None:
        self._model_dict: dict[str, Any] | None = None
        self._initialized = False
        self._lock = threading.Lock()
        self._conversion_lock = threading.Lock()
        self._hybrid_ocr_orchestrator: Any | None = None

    def initialize(self, device: str | None = None) -> None:
        from app.services.gpu_service import gpu_service

        # Wait for background GPU/CUDA installation to finish before importing torch/marker
        first_wait = True
        while gpu_service.status_dict["status"] == "installing":
            if first_wait:
                logger.info("GPU installation in progress, waiting before loading models...")
                first_wait = False
            time.sleep(5)

        if not self._initialized:
            logger.info("Marker engine is starting up. Awaiting initialization...")

        with self._lock:
            if self._initialized:
                return

            import sys
            is_pytest = "pytest" in sys.modules
            from app.services.model_tracker import check_models_downloaded, download_all_models_parallel
            if not check_models_downloaded() and not is_pytest:
                logger.info("Models missing on disk. Triggering parallel download...")
                download_all_models_parallel()
            tracker.set_loading(True)
            t0 = time.perf_counter()

            _import_marker()

            from marker.models import create_model_dict

            # device is None in the single-node parent path (marker resolves the
            # global TORCH_DEVICE_MODEL). A GPU worker passes its pinned device
            # (e.g. "cuda:1") so its models load onto exactly that GPU.
            logger.info("Loading marker model dict (device=%s) ...", device or "auto")
            self._model_dict = create_model_dict(device=device) if device else create_model_dict()
            elapsed = time.perf_counter() - t0
            logger.info("Marker models loaded in %.1f s", elapsed)
            self._initialized = True
            tracker.set_initialized(True)

    def release_models(self) -> None:
        """Release Marker model references for low-VRAM specialist phases."""
        with self._lock:
            self._model_dict = None
            self._initialized = False
            tracker.set_initialized(False)
        _empty_cuda_cache()

    def convert_file(
        self,
        filepath: str | Path,
        options: dict[str, Any],
        device: str | None = None,
    ) -> dict[str, Any]:
        self.initialize(device=device)

        from marker.output import text_from_rendered

        with self._conversion_lock:
            converter, converter_cls_name, chosen_renderer = self._build_converter(options)

            document = self._build_marker_document(converter, filepath)
            document, hybrid_ocr_meta = self._maybe_run_hybrid_ocr(
                document=document,
                filepath=filepath,
                options=options,
                converter=converter,
            )
            rendered = self._render_document_format(
                converter=converter,
                document=document,
                fmt=options.get("output_format", "markdown"),
                renderer_str=chosen_renderer,
                converter_cls_name=converter_cls_name,
            )
            text, ext, images = text_from_rendered(rendered)

            metadata = self._collect_render_metadata(rendered, converter, hybrid_ocr_meta)

        return {
            "text": text,
            "extension": ext,
            "images": images,
            "metadata": metadata,
        }

    def convert_file_formats(
        self,
        filepath: str | Path,
        options: dict[str, Any],
        formats: list[str],
        device: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Render MULTIPLE output formats from a single document parse.

        marker builds a ``Document`` once (the expensive layout + OCR pass) and
        each renderer (markdown/json/html/chunks) consumes that same parsed
        document, so asking for N formats costs one parse — not N. This is the
        "no reconverting" path for multi-format output.

        Returns ``{format: {"text", "extension", "images", "metadata"}}``.
        """
        self.initialize(device=device)

        from marker.output import text_from_rendered

        # Dedupe + drop unknowns so a bad client request never crashes a render.
        formats = [f for f in dict.fromkeys(formats) if f in _SUPPORTED_FORMATS]
        if not formats:
            formats = ["markdown"]

        # ConfigParser.get_renderer() reads cli_options["output_format"], so it
        # must exist even though we render many formats below. The value here
        # only seeds the converter's stock renderer, which the per-format loop
        # overrides for each requested format — markdown is a safe placeholder.
        options.setdefault("output_format", "markdown")

        with self._conversion_lock:
            converter, _converter_cls_name, _chosen_renderer = self._build_converter(options)

            # The document is parsed ONCE here; every format renders from it.
            document = self._build_marker_document(converter, filepath)
            document, hybrid_ocr_meta = self._maybe_run_hybrid_ocr(
                document=document,
                filepath=filepath,
                options=options,
                converter=converter,
            )

            formats_out: dict[str, dict[str, Any]] = {}
            for fmt in formats:
                renderer_str = _renderer_string_for_format(fmt, options)
                rendered = self._render_document_format(
                    converter=converter,
                    document=document,
                    fmt=fmt,
                    renderer_str=renderer_str,
                    converter_cls_name=None,
                )
                text, ext, images = text_from_rendered(rendered)

                metadata = self._collect_render_metadata(rendered, converter, hybrid_ocr_meta)

                formats_out[fmt] = {
                    "text": text,
                    "extension": ext,
                    "images": images,
                    "metadata": metadata,
                }

        return formats_out

    def convert_bytes(
        self,
        data: bytes,
        filename: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        from app.core.config import UPLOAD_DIR
        tmp_dir = UPLOAD_DIR
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / filename
        tmp_path.write_bytes(data)
        try:
            return self.convert_file(tmp_path, dict(options))
        finally:
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def get_defaults() -> dict[str, Any]:
        _import_marker()
        try:
            from marker.config.parser import ConfigParser
            cp = ConfigParser({})
            return {k: v for k, v in cp.dict_config.items()}
        except Exception:
            return {}

    def _build_converter(self, options: dict[str, Any]) -> tuple[Any, str, str]:
        from marker.config.parser import ConfigParser

        converter_cls_name = options.pop("converter_cls", "PdfConverter")
        converter_cls = (_CONVERTERS or {}).get(
            converter_cls_name,
            (_CONVERTERS or {})["PdfConverter"],
        )
        config_parser = ConfigParser(options)
        config_dict = config_parser.generate_config_dict()
        for _k in IMAGE_UNDERSTANDING_CONFIG_KEYS:
            if _k in options:
                config_dict[_k] = options[_k]
        chosen_renderer = _select_renderer(options, config_parser.get_renderer())
        converter = converter_cls(
            config=config_dict,
            artifact_dict=self._model_dict,
            processor_list=config_parser.get_processors(),
            renderer=chosen_renderer,
            llm_service=config_parser.get_llm_service(),
        )
        return converter, converter_cls_name, chosen_renderer

    def _build_marker_document(self, converter: Any, filepath: str | Path) -> Any:
        build_document = getattr(converter, "build_document", None)
        if not callable(build_document):
            raise RuntimeError("Marker converter does not expose build_document; Hybrid OCR seam requires it.")
        return run_with_oom_retry(
            lambda: build_document(str(filepath)),
            self._model_dict,
        )

    def _render_document_format(
        self,
        *,
        converter: Any,
        document: Any,
        fmt: str,
        renderer_str: str,
        converter_cls_name: str | None,
    ) -> Any:
        from marker.util import strings_to_classes

        renderer_cls = strings_to_classes([renderer_str])[0]
        if converter_cls_name == "OCRConverter" and fmt not in ("json", "chunks"):
            converter.renderer = renderer_cls
        renderer = converter.resolve_dependencies(renderer_cls)
        return renderer(document)

    def _maybe_run_hybrid_ocr(
        self,
        *,
        document: Any,
        filepath: str | Path,
        options: dict[str, Any],
        converter: Any,
    ) -> tuple[Any, dict[str, Any]]:
        if options.get("ocr_engine") != "hybrid_ocr":
            return document, {}
        if self._hybrid_ocr_orchestrator is None:
            from app.hybrid_ocr import HybridOcrOrchestrator

            self._hybrid_ocr_orchestrator = HybridOcrOrchestrator()
        return self._hybrid_ocr_orchestrator.refine(
            document=document,
            filepath=str(filepath),
            options=options,
            marker_service=self,
            converter=converter,
        )

    def _collect_render_metadata(
        self,
        rendered: Any,
        converter: Any,
        hybrid_ocr_meta: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = getattr(rendered, "metadata", None) or {}
        if hybrid_ocr_meta:
            metadata = dict(metadata)
            metadata["hybrid_ocr"] = hybrid_ocr_meta
        image_understanding_meta = _collect_image_understanding_meta(converter)
        if image_understanding_meta:
            metadata = dict(metadata)
            metadata["image_understanding"] = image_understanding_meta
        return metadata


def _collect_image_understanding_meta(converter: Any) -> list[dict[str, Any]]:
    """Read the per-image sidecar stash from an ImageUnderstandingProcessor.

    The processor accumulates ``_image_meta`` during ``__call__`` (markdownify
    strips HTML comments, so a comment channel cannot survive to output).
    marker stores processor instances on ``converter.processor_list`` after
    initialization; we locate ours by class name and read the stash.
    """
    processor_list = getattr(converter, "processor_list", None) or []
    for processor in processor_list:
        if type(processor).__name__ == "ImageUnderstandingProcessor":
            return list(getattr(processor, "image_meta", []) or [])
    return []
