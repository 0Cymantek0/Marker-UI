"""PR81A embedding lanes: deterministic fake, CLIP, and SigLIP embedders.

An embedder maps a page image (PNG bytes) or a query text to a fixed-dim
float32 unit vector. Three implementations share one protocol:

* :class:`HashEmbedder` — deterministic cryptographic fake used by tests
  and offline structural verification; never a retrieval-quality claim;
* :class:`ClipEmbedder` — ``openai/clip-vit-base-patch32`` single-vector
  image/text embeddings (classic dense visual baseline);
* :class:`SiglipEmbedder` — ``google/siglip-base-patch16-224``
  (stronger zero-shot image/text encoder used as a model-sensitivity
  lane).

Model weights load lazily on first use and only in live benchmark runs;
unit tests always inject the fake or frozen vectors. Embedder identity
(string) participates in every visual cache/identity key so vectors from
different models can never be mixed.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Embedder(Protocol):
    """Image/text embedder contract for the PR81A visual index."""

    @property
    def identity(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed_image(self, png_bytes: bytes) -> np.ndarray: ...

    def embed_text(self, text: str) -> np.ndarray: ...


class HashEmbedder:
    """Deterministic fake embedder — same input, same unit vector.

    Distinct inputs map to independent random-looking directions; it is
    used to test index mechanics (partitions, budgets, authorization
    filtering) without model downloads. It has zero retrieval skill and
    must never be used to claim retrieval quality.
    """

    def __init__(self, dim: int = 64) -> None:
        self._dim = int(dim)
        self._identity = f"hash:{self._dim}"

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def dim(self) -> int:
        return self._dim

    def _vector(self, payload: bytes) -> np.ndarray:
        digest = hashlib.sha256(payload).digest()
        seed = int.from_bytes(digest[:8], "big")
        rng = np.random.default_rng(seed)
        vec = rng.standard_normal(self._dim).astype(np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm

    def _unit(self, vec: np.ndarray) -> np.ndarray:
        norm = float(np.linalg.norm(vec))
        return (vec / norm).astype(np.float32)

    def embed_image(self, png_bytes: bytes) -> np.ndarray:
        # same content -> near-same vector in both modalities; a small
        # modality jitter keeps image and text embeddings distinct
        base = self._vector(b"content\0" + png_bytes)
        jitter = 0.05 * self._vector(b"img-jitter\0" + png_bytes)
        return self._unit(base + jitter)

    def embed_text(self, text: str) -> np.ndarray:
        payload = text.encode("utf-8")
        base = self._vector(b"content\0" + payload)
        jitter = 0.05 * self._vector(b"txt-jitter\0" + payload)
        return self._unit(base + jitter)


class _TransformerImageTextEmbedder:
    """Shared transformers-based image/text tower wrapper."""

    def __init__(self, model_name: str, model_cls: str, processor_cls: str) -> None:
        self.model_name = model_name
        self._model_cls = model_cls
        self._processor_cls = processor_cls
        self._model = None
        self._processor = None

    def _load(self):
        if self._model is None:
            import torch
            import transformers

            model_cls = getattr(transformers, self._model_cls)
            processor_cls = getattr(transformers, self._processor_cls)
            self._model = model_cls.from_pretrained(self.model_name)
            self._processor = processor_cls.from_pretrained(self.model_name)
            self._model.eval()
            self._torch = torch
        return self._model, self._processor

    @property
    def identity(self) -> str:
        return f"{self.model_name}"

    @property
    def dim(self) -> int:
        model, _ = self._load()
        config = model.config
        for attr in ("projection_dim", "hidden_size", "vision_config.hidden_size"):
            value = config
            for part in attr.split("."):
                value = getattr(value, part, None)
                if value is None:
                    break
            if isinstance(value, int):
                return value
        raise RuntimeError(f"cannot determine embedding dim for {self.model_name}")

    def _unit(self, tensor) -> np.ndarray:
        vec = tensor.detach().cpu().float().numpy().reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            raise RuntimeError("zero-norm embedding")
        return (vec / norm).astype(np.float32)

    def embed_image(self, png_bytes: bytes) -> np.ndarray:
        import io

        from PIL import Image

        model, processor = self._load()
        torch = self._torch
        image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        with torch.no_grad():
            outputs = model.get_image_features(**inputs)
        return self._unit(outputs)

    def embed_text(self, text: str) -> np.ndarray:
        model, processor = self._load()
        torch = self._torch
        inputs = processor(text=[text], return_tensors="pt", padding=True, truncation=True)
        with torch.no_grad():
            outputs = model.get_text_features(**inputs)
        return self._unit(outputs)


class ClipEmbedder(_TransformerImageTextEmbedder):
    """openai/clip-vit-base-patch32 dense single-vector baseline."""

    def __init__(self) -> None:
        super().__init__(
            model_name="openai/clip-vit-base-patch32",
            model_cls="CLIPModel",
            processor_cls="CLIPProcessor",
        )


class SiglipEmbedder(_TransformerImageTextEmbedder):
    """google/siglip-base-patch16-224 model-sensitivity lane."""

    def __init__(self) -> None:
        super().__init__(
            model_name="google/siglip-base-patch16-224",
            model_cls="SiglipModel",
            processor_cls="SiglipProcessor",
        )
