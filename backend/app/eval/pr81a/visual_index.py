"""PR81A partitioned visual index over page vectors.

One :class:`VisualIndex` instance is one immutable *visual generation*:
a matrix of unit page vectors plus the source-resolvable identity of
every row. Semantics mirror the PR76/PR78 publication model on purpose:

* ``visual_generation_identity`` binds the generation to the workspace,
  the embedder identity, the exact (revision, page) member set, and —
  when high assurance is requested — the partition key. A partitioned
  build never collides with (or borrows rows from) the shared build,
  mirroring ``compute_lexical_identity``;
* high assurance means a *physically separate matrix* containing only
  pages whose source lineage resolves into the allowed domains. A
  forbidden page cannot influence scores, ranks, counts, or ties because
  its vector is not in the array at all;
* standard assurance keeps one shared matrix and restricts the scored
  candidate universe through an authorization filter *before* any
  scoring happens. Forbidden vectors remain co-resident in the shared
  matrix — that residual is exactly the difference between the two
  profiles and is measured, not hidden;
* searches are bounded (``top_k``, ``max_pages_scored``) and
  deterministic: ties break by ``(doc_id, page_number)`` ascending.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

from app.eval.pr81a.embeddings import Embedder

VISUAL_INDEX_SCHEMA = "marker.pr81a.visual_index.v1"

MAX_TOP_K = 64
MAX_PAGES_SCORED = 2048


class VisualIndexError(RuntimeError):
    """Index construction or search contract violation."""


@dataclass(frozen=True)
class VisualPageEntry:
    """Source-resolvable identity of one indexed page."""

    doc_id: str
    page_number: int
    page_index: int
    blob_key: str
    revision: str
    domain: str
    source_ref: str

    def identity_payload(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "page_number": self.page_number,
            "blob_key": self.blob_key,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class VisualHit:
    doc_id: str
    page_number: int
    blob_key: str
    revision: str
    source_ref: str
    score: float
    rank: int


@dataclass(frozen=True)
class VisualSearchResult:
    hits: tuple[VisualHit, ...]
    pages_scored: int
    elapsed_ms: float
    budget: "VisualQueryBudget"


@dataclass(frozen=True)
class VisualQueryBudget:
    top_k: int = 10
    max_pages_scored: int = MAX_PAGES_SCORED

    def __post_init__(self) -> None:
        if not (1 <= self.top_k <= MAX_TOP_K):
            raise VisualIndexError(f"top_k out of range: {self.top_k}")
        if not (1 <= self.max_pages_scored <= MAX_PAGES_SCORED):
            raise VisualIndexError(f"max_pages_scored out of range: {self.max_pages_scored}")


def visual_generation_identity(
    *,
    workspace_id: str,
    embedder_identity: str,
    entries: Sequence[VisualPageEntry],
    partition_key: str = "",
) -> str:
    """Generation id over the exact member set; partitioning splits identity."""
    member_digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: (e.doc_id, e.page_number)):
        member_digest.update(
            json.dumps(entry.identity_payload(), sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\0"
        )
    payload = json.dumps(
        {
            "schema": VISUAL_INDEX_SCHEMA,
            "workspace_id": workspace_id,
            "embedder": embedder_identity,
            "members": member_digest.hexdigest(),
            **({"partition_key": partition_key} if partition_key else {}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class VisualIndex:
    """Immutable page-vector matrix with source-resolvable rows."""

    def __init__(
        self,
        *,
        workspace_id: str,
        embedder_identity: str,
        entries: Sequence[VisualPageEntry],
        matrix: np.ndarray,
        partition_key: str = "",
    ) -> None:
        if matrix.dtype != np.float32 or matrix.ndim != 2:
            raise VisualIndexError("matrix must be float32 [n, d]")
        if matrix.shape[0] != len(entries):
            raise VisualIndexError("matrix rows must match entries")
        if len(entries) and matrix.shape[1] == 0:
            raise VisualIndexError("empty embedding dimension")
        self.workspace_id = workspace_id
        self.embedder_identity = embedder_identity
        self.entries: tuple[VisualPageEntry, ...] = tuple(entries)
        self.matrix = matrix
        self.partition_key = partition_key
        self.generation_id = visual_generation_identity(
            workspace_id=workspace_id,
            embedder_identity=embedder_identity,
            entries=self.entries,
            partition_key=partition_key,
        )

    # -- construction -----------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        workspace_id: str,
        embedder: Embedder,
        pages: Sequence[tuple[VisualPageEntry, bytes]],
        partition_key: str = "",
    ) -> "VisualIndex":
        """Embed PNG bytes for every admitted page into one matrix."""
        if not pages:
            return cls(
                workspace_id=workspace_id,
                embedder_identity=embedder.identity,
                entries=(),
                matrix=np.zeros((0, 1), dtype=np.float32),
                partition_key=partition_key,
            )
        seen: set[tuple[str, int]] = set()
        ordered = sorted(pages, key=lambda pair: (pair[0].doc_id, pair[0].page_number))
        vectors: list[np.ndarray] = []
        entries: list[VisualPageEntry] = []
        for entry, png_bytes in ordered:
            key = (entry.doc_id, entry.page_number)
            if key in seen:
                raise VisualIndexError(f"duplicate page entry: {key}")
            seen.add(key)
            vectors.append(embedder.embed_image(png_bytes))
            entries.append(entry)
        matrix = np.stack(vectors).astype(np.float32)
        return cls(
            workspace_id=workspace_id,
            embedder_identity=embedder.identity,
            entries=entries,
            matrix=matrix,
            partition_key=partition_key,
        )

    @classmethod
    def build_high_assurance(
        cls,
        *,
        workspace_id: str,
        embedder: Embedder,
        pages: Sequence[tuple[VisualPageEntry, bytes]],
        allowed_domains: Iterable[str],
    ) -> "VisualIndex":
        """Physically separate matrix: only allowed-domain lineage enters."""
        allowed = frozenset(allowed_domains)
        admitted = [(entry, png) for entry, png in pages if entry.domain in allowed]
        return cls.build(
            workspace_id=workspace_id,
            embedder=embedder,
            pages=admitted,
            partition_key=hashlib.sha256(
                json.dumps(sorted(allowed), separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )

    # -- search -----------------------------------------------------------

    def search(
        self,
        query_vector: np.ndarray,
        *,
        budget: VisualQueryBudget | None = None,
        candidate_filter: Callable[[VisualPageEntry], bool] | None = None,
    ) -> VisualSearchResult:
        """Rank pages by cosine similarity under a bounded, authorized universe.

        ``candidate_filter`` restricts the scored universe *before* any
        scoring: excluded pages cannot affect scores, ranks, or counts of
        the returned competition.
        """
        budget = budget or VisualQueryBudget()
        started = time.perf_counter()
        if not self.entries:
            return VisualSearchResult((), 0, 0.0, budget)
        allowed_indices = [
            i for i, entry in enumerate(self.entries)
            if candidate_filter is None or candidate_filter(entry)
        ][: budget.max_pages_scored]
        if not allowed_indices:
            return VisualSearchResult((), 0, 0.0, budget)
        submatrix = self.matrix[allowed_indices]
        scores = submatrix @ query_vector.astype(np.float32)
        order = sorted(
            range(len(allowed_indices)),
            key=lambda i: (
                -float(scores[i]),
                self.entries[allowed_indices[i]].doc_id,
                self.entries[allowed_indices[i]].page_number,
            ),
        )[: budget.top_k]
        hits = tuple(
            VisualHit(
                doc_id=self.entries[allowed_indices[i]].doc_id,
                page_number=self.entries[allowed_indices[i]].page_number,
                blob_key=self.entries[allowed_indices[i]].blob_key,
                revision=self.entries[allowed_indices[i]].revision,
                source_ref=self.entries[allowed_indices[i]].source_ref,
                score=round(float(scores[i]), 6),
                rank=position + 1,
            )
            for position, i in enumerate(order)
        )
        elapsed = (time.perf_counter() - started) * 1000
        return VisualSearchResult(hits, len(allowed_indices), elapsed, budget)

    # -- persistence / replay ----------------------------------------------

    def save(self, path: Path) -> None:
        """Save matrix + row identity for offline replay (no pickle)."""
        meta = {
            "schema": VISUAL_INDEX_SCHEMA,
            "workspace_id": self.workspace_id,
            "embedder_identity": self.embedder_identity,
            "partition_key": self.partition_key,
            "generation_id": self.generation_id,
            "entries": [entry.__dict__ for entry in self.entries],
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            path,
            matrix=self.matrix,
            meta=np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8),
        )

    @classmethod
    def load(cls, path: Path) -> "VisualIndex":
        path = Path(path)
        with np.load(path, allow_pickle=False) as data:
            matrix = data["matrix"].astype(np.float32, copy=True)
            meta = json.loads(data["meta"].tobytes().decode("utf-8"))
        if meta.get("schema") != VISUAL_INDEX_SCHEMA:
            raise VisualIndexError(f"unsupported visual index schema: {meta.get('schema')!r}")
        entries = tuple(VisualPageEntry(**item) for item in meta["entries"])
        index = cls(
            workspace_id=meta["workspace_id"],
            embedder_identity=meta["embedder_identity"],
            entries=entries,
            matrix=matrix,
            partition_key=meta.get("partition_key", ""),
        )
        if index.generation_id != meta.get("generation_id"):
            raise VisualIndexError("generation identity mismatch after reload")
        return index
