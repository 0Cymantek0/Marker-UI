"""Seed a real kernel workspace from the PR81A corpus.

This is the production-shaped half of the experiment: every document is
committed through the real truth kernel with its **actual PDF bytes**
staged into the content-addressed source store, so a rendered page can
be reproduced from the exact committed ``ContentRevisionRecord`` — the
source-resolution chain the benchmark scores is real, not simulated.

The seeded workspace carries:

* one ``SourceIdentityRecord`` per document (logical identity);
* one ``ContentRevisionRecord`` per revision with the real blob key of
  the staged PDF artifact;
* one ``SecurityDomainRecord`` per document (the PR78 partition
  dimension);
* one ``ViewDocumentRecord`` per revision whose nodes are the oracle
  transcript runs (``p<page>n<index>`` node ids make the page mapping
  explicit);
* one materialized generation + published lexical generation (FTS5) so
  lexical lanes execute through the production query path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.generations import GenerationService
from app.kernel.patches import ViewDocumentRecord
from app.kernel.publications import PublicationService, PublicationSetRef
from app.kernel.reading_order import OrderNode, ReadingOrderGraph
from app.kernel.records import (
    SOURCE_CONSISTENCY_STABLE_HANDLE,
    ContentRevisionRecord,
    SecurityDomainRecord,
    SourceIdentityRecord,
)
from app.kernel.snapshots import resolve_snapshot
from app.kernel.source_store import LocalSourceStore
from app.eval.pr81a.corpus import Corpus, CorpusDoc, RevisionArtifact

DOMAIN_GENERAL = "general"
DOMAIN_RESTRICTED = "restricted"


def _node_id(page_number: int, run_index: int) -> str:
    return f"p{page_number}n{run_index:02d}"


def _page_of_node(node_id: str) -> int:
    return int(node_id[1 : node_id.index("n")])


@dataclass(frozen=True)
class SeededDoc:
    """One committed document with its source-resolvable identity."""

    doc_id: str
    source_ref: str
    revision_record_id: str
    revision: str
    blob_key: str
    suffix: str
    media_type: str
    domain: str
    visual_admitted: bool
    view_id: str
    pdf_path: Path
    node_texts: dict[str, str] = field(default_factory=dict)
    page_texts: dict[int, tuple[str, ...]] = field(default_factory=dict)

    def artifact_path(self, store: LocalSourceStore) -> Path:
        return store.artifact_path(self.blob_key, self.suffix)

    def page_of_node(self, node_id: str) -> int:
        return _page_of_node(node_id)

    def page_text(self, page_number: int) -> str:
        return "\n".join(self.page_texts[page_number])


@dataclass
class SeededWorkspace:
    workspace_id: str
    factory: async_sessionmaker
    service: KernelCommitService
    source_store: LocalSourceStore
    corpus: Corpus
    docs: dict[str, SeededDoc] = field(default_factory=dict)
    superseded: dict[str, SeededDoc] = field(default_factory=dict)
    publication: PublicationSetRef | None = None
    publication_history: tuple[PublicationSetRef, ...] = ()

    def doc(self, doc_id: str) -> SeededDoc:
        return self.docs[doc_id]

    @property
    def pinned_publication(self) -> PublicationSetRef:
        if not self.publication_history:
            raise RuntimeError("no earlier publication to pin")
        return self.publication_history[0]


async def _seed_doc(
    ws: SeededWorkspace,
    doc: CorpusDoc,
    revision: RevisionArtifact,
    previous: SeededDoc | None = None,
) -> SeededDoc:
    staged = await ws.source_store.stage_from_path(revision.pdf_path, suffix=".pdf")
    records = []
    if previous is None:
        source = SourceIdentityRecord(
            record_id=f"src.{doc.doc_id}",
            source_kind="local_path",
            source_key=f"C:/eval/pr81a/{doc.doc_id}.pdf",
        )
        records.append(source)
        source_ref = source.record_id
    else:
        # one logical source commits once; a new content revision mints
        # ContentRevision + View records against the existing source
        source_ref = previous.source_ref
    content = ContentRevisionRecord(
        record_id=f"rev.{doc.doc_id}.{revision.revision}",
        source_ref=source_ref,
        blob_key=staged.blob_key,
        byte_length=revision.byte_length,
        media_type="application/pdf",
        consistency_class=SOURCE_CONSISTENCY_STABLE_HANDLE,
        suffix=".pdf",
    )
    records.append(content)
    if previous is None or previous.domain != doc.domain:
        records.append(
            SecurityDomainRecord(
                record_id=f"assign.{doc.doc_id}.{revision.revision}",
                source_ref=source_ref,
                domain_key=doc.domain,
            )
        )
    node_texts: dict[str, str] = {}
    page_texts: dict[int, tuple[str, ...]] = {}
    for page_number, nodes in revision.pages:
        page_texts[page_number] = nodes
        for run_index, text in enumerate(nodes):
            node_texts[_node_id(page_number, run_index)] = text
    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in node_texts), ()
    )
    view = ViewDocumentRecord(
        record_id=f"view.{doc.doc_id}.{revision.revision}",
        content_revision_ref=content.record_id,
        graph=graph,
        texts=dict(node_texts),
        view_id=f"doc-{doc.doc_id}",
    )
    records.append(view)
    await ws.service.commit(
        KernelCommitBatch(
            workspace_id=ws.workspace_id,
            records=tuple(records),
        )
    )
    seeded = SeededDoc(
        doc_id=doc.doc_id,
        source_ref=source_ref,
        revision_record_id=content.record_id,
        revision=revision.revision,
        blob_key=staged.blob_key,
        suffix=".pdf",
        media_type="application/pdf",
        domain=doc.domain,
        visual_admitted=doc.visual_admitted,
        view_id=f"doc-{doc.doc_id}",
        pdf_path=revision.pdf_path,
        node_texts=node_texts,
        page_texts=page_texts,
    )
    return seeded


async def build_generation_and_publish(ws: SeededWorkspace) -> PublicationSetRef:
    generation = await GenerationService(ws.factory).build_and_activate(
        await resolve_snapshot(ws.factory, ws.workspace_id)
    )
    pubs = PublicationService(ws.factory)
    ref = await pubs.publish(materialized_generation_id=generation.generation_id)
    if ws.publication is not None:
        ws.publication_history = (*ws.publication_history, ws.publication)
    ws.publication = ref
    return ref


async def publish_high_assurance_partition(ws: SeededWorkspace) -> PublicationSetRef:
    """Publish the ``ha.`` partition over the general domain (PR78 shape)."""
    generation = await GenerationService(ws.factory).build_and_activate(
        await resolve_snapshot(ws.factory, ws.workspace_id)
    )
    pubs = PublicationService(ws.factory)
    return await pubs.publish_high_assurance(
        materialized_generation_id=generation.generation_id,
        partition_domains=frozenset({DOMAIN_GENERAL}),
    )


async def seed_workspace(
    *,
    factory: async_sessionmaker,
    service: KernelCommitService,
    corpus: Corpus,
    workspace_id: str,
    source_root: Path,
) -> SeededWorkspace:
    """Commit every corpus document (revision-doc at its superseded cut)."""
    ws = SeededWorkspace(
        workspace_id=workspace_id,
        factory=factory,
        service=service,
        source_store=LocalSourceStore(source_root),
        corpus=corpus,
    )
    for doc in corpus.docs:
        revision = doc.current
        if len(doc.revisions) > 1:
            revision = doc.revision("v3")  # seed the superseded cut first
        ws.docs[doc.doc_id] = await _seed_doc(ws, doc, revision)
    await build_generation_and_publish(ws)
    return ws


async def revise_document(ws: SeededWorkspace, doc_id: str, revision_name: str) -> SeededDoc:
    """Commit a newer content revision + view revision, then republish."""
    doc = ws.corpus.doc(doc_id)
    current = ws.docs[doc_id]
    newer = doc.revision(revision_name)
    if newer.revision == current.revision:
        raise ValueError(f"{doc_id} already at {revision_name}")
    ws.superseded[doc_id] = current
    updated = await _seed_doc(ws, doc, newer, previous=current)
    ws.docs[doc_id] = updated
    await build_generation_and_publish(ws)
    return updated
