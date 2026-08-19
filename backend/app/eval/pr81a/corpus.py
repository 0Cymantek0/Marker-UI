"""Fail-closed loader for the committed PR81A corpus.

The corpus is benchmark evidence, never production truth. Everything here
validates the committed manifest, PDFs, oracle transcripts, and judged
queries, and derives a content fingerprint over the exact bytes a benchmark
run consumed — mirroring the PR80B corpus conventions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from app.eval.pr81a.normalize import NormalizeError, normalize_answer

MANIFEST_VERSION = "marker.pr81a_corpus.v1"
QUERIES_FILENAME = "queries.json"

#: Slice taxonomy for documents (what kind of visual material they carry).
DOC_SLICES = frozenset(
    {
        "chart.bar",
        "chart.line",
        "chart.pie",
        "chart.org",
        "chart.dashboard",
        "table.grid",
        "form.columns",
        "layout.two_column",
        "text.plain",
        "text.summary",
        "revision.checklist",
        "authz.restricted",
        "authz.public",
    }
)

#: Slice taxonomy for queries (the failure mode each one targets).
QUERY_SLICES = frozenset(
    {
        "text.easy_control",
        "chart.appearance",
        "chart.value_read",
        "table.cell_grid",
        "form.label_placement",
        "layout.column_bind",
        "near_duplicate.decoy",
        "revision.change",
        "authz.revocation",
    }
)

DOMAINS = frozenset({"general", "restricted"})
ANSWER_KINDS = frozenset({"string", "decimal", "count", "percent", "date", "money_million"})
PHASES = frozenset({"baseline", "pre_revision", "post_revision", "pinned_pre_revision"})
PROFILES = frozenset({"default", "allowed", "denied"})
EXPECTATIONS = frozenset({"answer", "no_delivery"})

REVISION_DOC_ID = "doc-rev-01"


class CorpusError(ValueError):
    """Raised for any inconsistency in the committed corpus."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CorpusError(message)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class RevisionArtifact:
    revision: str
    pdf_path: Path
    oracle_path: Path
    pages: tuple[tuple[int, tuple[str, ...]], ...]
    pdf_sha256: str
    byte_length: int
    superseded_by: str | None

    def page(self, page_number: int) -> tuple[str, ...]:
        for number, nodes in self.pages:
            if number == page_number:
                return nodes
        raise CorpusError(f"page {page_number} not present in revision")

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True)
class CorpusDoc:
    doc_id: str
    domain: str
    slices: tuple[str, ...]
    visual_admitted: bool
    revisions: tuple[RevisionArtifact, ...]

    @property
    def current(self) -> RevisionArtifact:
        live = [r for r in self.revisions if r.superseded_by is None]
        _require(len(live) == 1, f"doc {self.doc_id} must have exactly one live revision")
        return live[0]

    def revision(self, name: str) -> RevisionArtifact:
        for rev in self.revisions:
            if rev.revision == name:
                return rev
        raise CorpusError(f"doc {self.doc_id} has no revision {name!r}")

    def blob_key(self, revision: str | None = None) -> str:
        rev = self.current if revision is None else self.revision(revision)
        return f"sha256:{rev.pdf_sha256}"


@dataclass(frozen=True)
class CorpusQuery:
    query_id: str
    text: str
    slice_tag: str
    doc_id: str
    page_number: int
    answer: str
    answer_kind: str
    phase: str
    profile: str
    expectation: str
    normalized_answer: str


@dataclass(frozen=True)
class Corpus:
    root: Path
    manifest_version: str
    generator_version: str
    provenance: str
    docs: tuple[CorpusDoc, ...]
    queries: tuple[CorpusQuery, ...]
    fingerprint: str
    slice_counts: Mapping[str, int]

    def doc(self, doc_id: str) -> CorpusDoc:
        for doc in self.docs:
            if doc.doc_id == doc_id:
                return doc
        raise CorpusError(f"unknown doc_id: {doc_id}")

    def query(self, query_id: str) -> CorpusQuery:
        for query in self.queries:
            if query.query_id == query_id:
                return query
        raise CorpusError(f"unknown query_id: {query_id}")


def _load_oracle(path: Path, doc_id: str, revision: str) -> tuple[tuple[int, tuple[str, ...]], ...]:
    _require(path.is_file(), f"missing oracle file: {path.name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    _require(data.get("doc_id") == doc_id, f"oracle doc_id mismatch for {doc_id}")
    _require(data.get("revision") == revision, f"oracle revision mismatch for {doc_id}/{revision}")
    pages = data.get("pages")
    _require(isinstance(pages, list) and pages, f"oracle pages missing for {doc_id}/{revision}")
    out: list[tuple[int, tuple[str, ...]]] = []
    seen: set[int] = set()
    for page in pages:
        number = page.get("page_number")
        nodes = page.get("nodes")
        _require(
            isinstance(number, int) and not isinstance(number, bool) and number >= 1,
            f"bad page_number in {path.name}",
        )
        _require(number not in seen, f"duplicate page_number {number} in {path.name}")
        seen.add(number)
        _require(
            isinstance(nodes, list) and all(isinstance(n, str) and n.strip() for n in nodes),
            f"bad nodes on page {number} of {path.name}",
        )
        out.append((number, tuple(nodes)))
    out.sort(key=lambda item: item[0])
    expected = list(range(1, len(out) + 1))
    _require([n for n, _ in out] == expected, f"page numbers must be 1..N in {path.name}")
    return tuple(out)


def _validate_query(entry: Mapping, index: int, docs_by_id: Mapping[str, CorpusDoc]) -> CorpusQuery:
    prefix = f"queries[{index}]"
    expectation_raw = entry.get("expectation", "answer")
    for field in ("query_id", "text", "slice_tag", "doc_id", "answer", "answer_kind"):
        value = entry.get(field)
        ok = isinstance(value, str) and (value.strip() or expectation_raw == "no_delivery")
        _require(ok, f"{prefix}: missing {field}")
    query_id = entry["query_id"]
    _require(entry["slice_tag"] in QUERY_SLICES, f"{query_id}: unknown query slice {entry['slice_tag']!r}")
    _require(entry["answer_kind"] in ANSWER_KINDS, f"{query_id}: unknown answer kind {entry['answer_kind']!r}")
    phase = entry.get("phase", "baseline")
    profile = entry.get("profile", "default")
    expectation = entry.get("expectation", "answer")
    _require(phase in PHASES, f"{query_id}: unknown phase {phase!r}")
    _require(profile in PROFILES, f"{query_id}: unknown profile {profile!r}")
    _require(expectation in EXPECTATIONS, f"{query_id}: unknown expectation {expectation!r}")
    doc = docs_by_id.get(entry["doc_id"])
    _require(doc is not None, f"{query_id}: unknown doc_id {entry['doc_id']!r}")
    page_number = entry.get("page_number")
    _require(
        isinstance(page_number, int) and not isinstance(page_number, bool) and 1 <= page_number <= doc.current.page_count,
        f"{query_id}: page_number {page_number!r} outside current revision",
    )
    _require(
        expectation != "no_delivery" or (profile == "denied" and entry["answer"] == ""),
        f"{query_id}: no_delivery requires denied profile and empty answer",
    )
    if phase != "baseline":
        _require(entry["doc_id"] == REVISION_DOC_ID, f"{query_id}: phases are only valid on {REVISION_DOC_ID}")
    if profile != "default":
        _require(entry["slice_tag"] == "authz.revocation", f"{query_id}: profiles are only valid on the authz slice")
    try:
        normalized = normalize_answer(entry["answer"], entry["answer_kind"]) if expectation == "answer" else ""
    except NormalizeError as exc:
        raise CorpusError(f"{query_id}: gold answer does not normalize: {exc}") from exc
    return CorpusQuery(
        query_id=query_id,
        text=entry["text"],
        slice_tag=entry["slice_tag"],
        doc_id=entry["doc_id"],
        page_number=page_number,
        answer=entry["answer"],
        answer_kind=entry["answer_kind"],
        phase=phase,
        profile=profile,
        expectation=expectation,
        normalized_answer=normalized,
    )


def load_corpus(root: Path) -> Corpus:
    """Load and fully validate the committed PR81A corpus."""
    root = Path(root)
    manifest_path = root / "manifest.json"
    _require(manifest_path.is_file(), "missing manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    _require(
        manifest.get("manifest_version") == MANIFEST_VERSION,
        f"unsupported manifest_version: {manifest.get('manifest_version')!r}",
    )
    provenance = manifest.get("provenance")
    _require(isinstance(provenance, str) and provenance.strip(), "missing provenance")
    generator_version = manifest.get("generator_version")
    _require(isinstance(generator_version, str) and generator_version.strip(), "missing generator_version")

    docs: list[CorpusDoc] = []
    docs_by_id: dict[str, CorpusDoc] = {}
    fingerprint = hashlib.sha256()
    fingerprint.update(b"manifest\0" + _sha256_bytes(manifest_bytes).encode() + b"\0")

    entries = manifest.get("documents")
    _require(isinstance(entries, list) and entries, "manifest documents missing")
    for index, entry in enumerate(entries):
        prefix = f"documents[{index}]"
        _require(isinstance(entry, Mapping), f"{prefix}: not an object")
        doc_id = entry.get("doc_id")
        _require(isinstance(doc_id, str) and doc_id.strip(), f"{prefix}: missing doc_id")
        _require(doc_id not in docs_by_id, f"duplicate doc_id: {doc_id}")
        domain = entry.get("domain")
        _require(domain in DOMAINS, f"{doc_id}: unknown domain {domain!r}")
        slices = entry.get("slices")
        _require(
            isinstance(slices, list) and slices and all(s in DOC_SLICES for s in slices),
            f"{doc_id}: bad slices",
        )
        admitted = entry.get("visual_admitted")
        _require(isinstance(admitted, bool), f"{doc_id}: visual_admitted must be boolean")
        revisions_data = entry.get("revisions")
        _require(
            isinstance(revisions_data, list) and revisions_data,
            f"{doc_id}: revisions missing",
        )
        revisions: list[RevisionArtifact] = []
        names: set[str] = set()
        for rev_data in revisions_data:
            revision = rev_data.get("revision")
            _require(isinstance(revision, str) and revision.strip(), f"{doc_id}: bad revision name")
            _require(revision not in names, f"{doc_id}: duplicate revision {revision!r}")
            names.add(revision)
            pdf_path = root / rev_data.get("pdf", "")
            oracle_path = root / rev_data.get("oracle", "")
            _require(pdf_path.is_file(), f"{doc_id}/{revision}: missing pdf file")
            pdf_bytes = pdf_path.read_bytes()
            fingerprint.update(
                doc_id.encode("utf-8")
                + b"\0"
                + revision.encode("utf-8")
                + b"\0"
                + _sha256_bytes(pdf_bytes).encode()
                + b"\0"
            )
            pages = _load_oracle(oracle_path, doc_id, revision)
            oracle_bytes = oracle_path.read_bytes()
            fingerprint.update(b"oracle\0" + _sha256_bytes(oracle_bytes).encode() + b"\0")
            superseded_by = rev_data.get("superseded_by")
            _require(
                superseded_by is None or isinstance(superseded_by, str),
                f"{doc_id}/{revision}: bad superseded_by",
            )
            revisions.append(
                RevisionArtifact(
                    revision=revision,
                    pdf_path=pdf_path,
                    oracle_path=oracle_path,
                    pages=pages,
                    pdf_sha256=_sha256_bytes(pdf_bytes),
                    byte_length=len(pdf_bytes),
                    superseded_by=superseded_by,
                )
            )
        live = [r for r in revisions if r.superseded_by is None]
        _require(len(live) == 1, f"{doc_id}: exactly one revision must be live")
        for rev in revisions:
            if rev.superseded_by is not None:
                _require(
                    rev.superseded_by in names,
                    f"{doc_id}/{rev.revision}: superseded_by target missing",
                )
        doc = CorpusDoc(
            doc_id=doc_id,
            domain=domain,
            slices=tuple(slices),
            visual_admitted=admitted,
            revisions=tuple(revisions),
        )
        docs.append(doc)
        docs_by_id[doc_id] = doc

    queries_path = root / QUERIES_FILENAME
    _require(queries_path.is_file(), "missing queries.json")
    queries_bytes = queries_path.read_bytes()
    fingerprint.update(b"queries\0" + _sha256_bytes(queries_bytes).encode() + b"\0")
    queries_data = json.loads(queries_bytes.decode("utf-8"))
    _require(isinstance(queries_data, list) and queries_data, "queries.json empty")
    queries: list[CorpusQuery] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(queries_data):
        _require(isinstance(entry, Mapping), f"queries[{index}]: not an object")
        query = _validate_query(entry, index, docs_by_id)
        _require(query.query_id not in seen_ids, f"duplicate query_id: {query.query_id}")
        seen_ids.add(query.query_id)
        queries.append(query)

    multi_revision_docs = [d for d in docs if len(d.revisions) > 1]
    if multi_revision_docs:
        phases_present = {q.phase for q in queries}
        _require(
            {"pre_revision", "post_revision"} <= phases_present,
            "a multi-revision document must be exercised by pre_revision and post_revision queries",
        )

    slice_counts: dict[str, int] = {}
    for query in queries:
        slice_counts[query.slice_tag] = slice_counts.get(query.slice_tag, 0) + 1
    declared = manifest.get("slice_counts")
    _require(isinstance(declared, Mapping), "manifest slice_counts missing")
    _require(
        {k: v for k, v in declared.items()} == slice_counts,
        "slice_counts do not match recomputed query counts",
    )

    return Corpus(
        root=root,
        manifest_version=MANIFEST_VERSION,
        generator_version=generator_version,
        provenance=provenance,
        docs=tuple(docs),
        queries=tuple(queries),
        fingerprint=fingerprint.hexdigest(),
        slice_counts=slice_counts,
    )
