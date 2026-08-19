"""PR81A on-demand page-image generation and cache.

Production-shaped but evaluation-owned: renders page images from the
immutable committed source artifact addressed by ``(blob_key,
page_index)`` and caches the PNG bytes content-addressed under a store
root. The cache is *derived serving state*:

* identity binds to the exact content revision (blob key), the page
  index, and the full render state (renderer identity + scale), so a new
  content revision can never silently reuse an older revision's pixels;
* generation is on demand and admission-gated — non-admitted documents
  are never rendered, and that skip is counted, not hidden;
* a failed render leaves nothing queryable behind (atomic tmp+replace,
  per-key single-flight, explicit error counters);
* nothing here is a second truth authority: deleting the whole root
  only costs regeneration time.

Authorization is deliberately NOT enforced here — pixels are derived
from already-committed source bytes. Disclosure is controlled by the
visual index and the lanes; this store never delivers content to a
caller by itself.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

RENDER_SCHEMA = "marker.pr81a.page_render.v1"

DEFAULT_SCALE = 2.0


def _renderer_identity() -> str:
    try:
        import pypdfium2 as pdfium

        version = getattr(pdfium, "V_PYPDFIUM2", None) or getattr(
            pdfium, "PYPDFIUM2_VERSION", "unknown"
        )
        return f"pypdfium2:{version}"
    except Exception:  # pragma: no cover - environment probe only
        return "pypdfium2:unknown"


@dataclass(frozen=True)
class RenderState:
    """Everything that changes pixel bytes; all of it keys the cache."""

    scale: float = DEFAULT_SCALE
    renderer: str = field(default_factory=_renderer_identity)

    def identity_payload(self) -> dict:
        return {"scale": self.scale, "renderer": self.renderer}


@dataclass(frozen=True)
class RenderedPage:
    render_key: str
    blob_key: str
    page_index: int
    path: Path
    byte_length: int
    from_cache: bool
    elapsed_ms: float


class VisualRenderError(RuntimeError):
    """Render pipeline failed; nothing was cached."""


class NotAdmittedError(VisualRenderError):
    """Document is not admitted to the visual route; generation refused."""


def render_key_for(blob_key: str, page_index: int, state: RenderState) -> str:
    """Stable cache key binding revision + page + render configuration."""
    payload = json.dumps(
        {
            "schema": RENDER_SCHEMA,
            "blob_key": blob_key,
            "page_index": page_index,
            "render_state": state.identity_payload(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pypdfium_render(pdf_path: Path, page_index: int, scale: float) -> bytes:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_index < 0 or page_index >= len(document):
            raise VisualRenderError(
                f"page index {page_index} out of range for {pdf_path.name}"
            )
        image = document[page_index].render(scale=scale).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        document.close()


class PageRenderStore:
    """Content-addressed PNG cache with per-key single-flight generation."""

    def __init__(
        self,
        root: Path,
        *,
        render_state: RenderState | None = None,
        renderer: Callable[[Path, int, float], bytes] | None = None,
    ) -> None:
        self.root = Path(root)
        self.render_state = render_state or RenderState()
        self._renderer = renderer or _pypdfium_render
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._counters = {
            "rendered": 0,
            "cache_hits": 0,
            "not_admitted": 0,
            "failures": 0,
            "bytes_written": 0,
            "cold_ms_total": 0.0,
            "warm_ms_total": 0.0,
        }
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    # -- identity -------------------------------------------------------

    def key(self, blob_key: str, page_index: int) -> str:
        return render_key_for(blob_key, page_index, self.render_state)

    def path_for(self, render_key: str) -> Path:
        return self.root / "objects" / render_key[:2] / f"{render_key}.png"

    # -- generation -----------------------------------------------------

    def _key_lock(self, render_key: str) -> threading.Lock:
        with self._lock:
            lock = self._key_locks.get(render_key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[render_key] = lock
            return lock

    def peek(self, blob_key: str, page_index: int) -> RenderedPage | None:
        """Return the cached page without generating anything."""
        render_key = self.key(blob_key, page_index)
        path = self.path_for(render_key)
        if not path.is_file():
            return None
        return RenderedPage(
            render_key=render_key,
            blob_key=blob_key,
            page_index=page_index,
            path=path,
            byte_length=path.stat().st_size,
            from_cache=True,
            elapsed_ms=0.0,
        )

    def render(
        self,
        blob_key: str,
        page_index: int,
        pdf_path: Path,
        *,
        admitted: bool,
    ) -> RenderedPage:
        """Generate on demand, or reuse the exact compatible cached render.

        Raises :class:`NotAdmittedError` for non-admitted documents and
        :class:`VisualRenderError` when the render pipeline fails; in
        both cases no cache entry is created or reused.
        """
        if not admitted:
            with self._lock:
                self._counters["not_admitted"] += 1
            raise NotAdmittedError(
                f"source {blob_key} is not admitted to the visual route"
            )
        render_key = self.key(blob_key, page_index)
        with self._key_lock(render_key):
            started = time.perf_counter()
            cached = self.peek(blob_key, page_index)
            if cached is not None:
                elapsed = (time.perf_counter() - started) * 1000
                with self._lock:
                    self._counters["cache_hits"] += 1
                    self._counters["warm_ms_total"] += elapsed
                return RenderedPage(
                    render_key=cached.render_key,
                    blob_key=blob_key,
                    page_index=page_index,
                    path=cached.path,
                    byte_length=cached.byte_length,
                    from_cache=True,
                    elapsed_ms=elapsed,
                )
            try:
                png_bytes = self._renderer(Path(pdf_path), page_index, self.render_state.scale)
            except VisualRenderError:
                with self._lock:
                    self._counters["failures"] += 1
                raise
            except Exception as exc:
                with self._lock:
                    self._counters["failures"] += 1
                raise VisualRenderError(f"render failed: {exc}") from exc
            target = self.path_for(render_key)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.root / "tmp" / f"{render_key}.{os.getpid()}.{threading.get_ident()}.png"
            tmp_path.write_bytes(png_bytes)
            os.replace(tmp_path, target)
            elapsed = (time.perf_counter() - started) * 1000
            with self._lock:
                self._counters["rendered"] += 1
                self._counters["bytes_written"] += len(png_bytes)
                self._counters["cold_ms_total"] += elapsed
            return RenderedPage(
                render_key=render_key,
                blob_key=blob_key,
                page_index=page_index,
                path=target,
                byte_length=len(png_bytes),
                from_cache=False,
                elapsed_ms=elapsed,
            )

    # -- retention / economics -------------------------------------------

    def known_keys(self) -> tuple[str, ...]:
        objects = self.root / "objects"
        if not objects.is_dir():
            return ()
        keys: list[str] = []
        for shard in objects.iterdir():
            if shard.is_dir():
                for entry in shard.iterdir():
                    if entry.suffix == ".png":
                        keys.append(entry.stem)
        return tuple(sorted(keys))

    def prune(self, live_keys: Iterable[str]) -> dict:
        """Remove cached renders not in ``live_keys``; report reclaimed bytes."""
        live = set(live_keys)
        removed = 0
        reclaimed = 0
        for key in self.known_keys():
            if key in live:
                continue
            path = self.path_for(key)
            size = path.stat().st_size
            path.unlink()
            shard = path.parent
            if shard.is_dir() and not any(shard.iterdir()):
                shard.rmdir()
            removed += 1
            reclaimed += size
        return {"removed_entries": removed, "reclaimed_bytes": reclaimed}

    def stats(self) -> dict:
        with self._lock:
            counters = dict(self._counters)
        keys = self.known_keys()
        return {
            **counters,
            "cached_entries": len(keys),
            "cached_bytes": sum(self.path_for(k).stat().st_size for k in keys),
        }
