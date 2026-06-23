"""Probe optional and required dependency capabilities at runtime.

This module determines whether optional libraries (like mammoth, openpyxl, etc.)
are installed, allowing the router to decide routes and the capability API
to report engine status dynamically.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil

logger = logging.getLogger(__name__)

# Map of logical dependency name to actual Python import package name
_DEP_IMPORTS = {
    "mammoth": "mammoth",
    "python-pptx": "pptx",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "lxml": "lxml",
    "beautifulsoup4": "bs4",
    "markdownify": "markdownify",
    "charset-normalizer": "charset_normalizer",
    "defusedxml": "defusedxml",
    "liteparse": "liteparse",
    "pypdf": "pypdf",
}

# In-memory cache for fast lookups
_CACHE: dict[str, bool] = {}


def is_dependency_available(name: str) -> bool:
    """Check if a dependency is importable, with caching."""
    if name not in _DEP_IMPORTS:
        return False
    if name in _CACHE:
        return _CACHE[name]

    import_name = _DEP_IMPORTS[name]
    try:
        spec = importlib.util.find_spec(import_name)
        available = spec is not None
    except Exception:
        available = False

    _CACHE[name] = available
    return available


def get_dependency_status() -> dict[str, bool]:
    """Get status of all registered optional dependencies."""
    return {name: is_dependency_available(name) for name in _DEP_IMPORTS}


def get_engine_status() -> dict[str, str]:
    """Report readiness status of each conversion engine.

    Returns a dict mapping engine name -> status string.
    Status can be:
    - "ready": Engine and all dependencies are fully available.
    - "models_downloading": Needs Marker models, which are downloading.
    - "models_missing": Needs Marker models, which are not downloaded.
    - "missing_optional_dependency": Missing required Python libraries.
    """
    from app.services.model_tracker import check_models_downloaded, tracker

    # 1. marker_pdf engine
    t_status = tracker.get_status_dict()
    overall_status = (t_status.get("overall") or {}).get("status")
    if t_status.get("initialized") or check_models_downloaded():
        marker_pdf_status = "ready"
    elif t_status.get("loading") or overall_status in {"downloading", "loading"}:
        marker_pdf_status = "models_downloading"
    else:
        marker_pdf_status = "models_missing"

    # 2. office_docx engine
    if is_dependency_available("mammoth") and is_dependency_available("markdownify"):
        office_docx_status = "ready"
    else:
        office_docx_status = "missing_optional_dependency"

    # 3. office_pptx engine
    if is_dependency_available("python-pptx"):
        office_pptx_status = "ready"
    else:
        office_pptx_status = "missing_optional_dependency"

    # 4. spreadsheet engine
    if is_dependency_available("openpyxl"):
        spreadsheet_status = "ready"
    else:
        spreadsheet_status = "missing_optional_dependency"

    # 5. xml_rss engine
    if is_dependency_available("defusedxml") and is_dependency_available("beautifulsoup4"):
        xml_rss_status = "ready"
    else:
        xml_rss_status = "missing_optional_dependency"

    # 6. html engine
    if is_dependency_available("beautifulsoup4") and is_dependency_available("markdownify"):
        html_status = "ready"
    else:
        html_status = "missing_optional_dependency"

    # 7. text_data, notebook, archive are built-in/standard lib based,
    # with charset-normalizer improving non-UTF-8 decoding when present.
    liteparse_status = "ready" if (
        is_dependency_available("liteparse") or shutil.which("lit")
    ) else "missing_optional_dependency"

    text_data_status = "ready"
    notebook_status = "ready"
    archive_status = "ready"

    return {
        "marker_pdf": marker_pdf_status,
        "liteparse_pdf": liteparse_status,
        "office_docx": office_docx_status,
        "office_pptx": office_pptx_status,
        "spreadsheet": spreadsheet_status,
        "text_data": text_data_status,
        "xml_rss": xml_rss_status,
        "html": html_status,
        "notebook": notebook_status,
        "archive": archive_status,
    }
