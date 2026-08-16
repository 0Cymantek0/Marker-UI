"""Source artifact store tests (PR70/71 local slice).

Adversarial TOCTOU matrix from the plan (§13.2): every mutation injected
at a deterministic acquisition boundary must end in one of the two valid
outcomes — a coherent acquisition of exactly one revision, or an
IncoherentSourceError with nothing published — never mixed evidence.
"""

from __future__ import annotations

import hashlib
import os
import tracemalloc
from pathlib import Path

import pytest

from app.kernel.errors import InjectedFaultError, KernelError
from app.kernel.source_store import (
    SOURCE_FAULT_PHASES,
    IncoherentSourceError,
    LocalSourceStore,
    SourceStoreError,
)

pytestmark = pytest.mark.asyncio


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def test_stage_publishes_content_addressed_artifact(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    src = _write(tmp_path / "src" / "doc.pdf", b"%PDF-1.4 exact bytes")

    staged = await store.stage_from_path(src, suffix=".pdf")

    expected = hashlib.sha256(b"%PDF-1.4 exact bytes").hexdigest()
    assert staged.blob_key == f"sha256:{expected}"
    assert staged.byte_length == len(b"%PDF-1.4 exact bytes")
    assert staged.already_present is False
    assert staged.artifact_path == store.artifact_path(staged.blob_key, ".pdf")
    assert staged.artifact_path.is_file()
    assert staged.artifact_path.read_bytes() == b"%PDF-1.4 exact bytes"
    assert staged.pre_stat == staged.post_stat
    assert staged.pre_stat["size"] == staged.byte_length
    assert await store.verify_artifact(staged.blob_key, ".pdf", staged.byte_length)


async def test_stage_is_idempotent_and_dedups_identical_bytes(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    src = _write(tmp_path / "a.pdf", b"same bytes")
    src2 = _write(tmp_path / "b.pdf", b"same bytes")

    first = await store.stage_from_path(src, suffix=".pdf")
    second = await store.stage_from_path(src2, suffix=".pdf")

    assert first.already_present is False
    assert second.already_present is True
    assert second.blob_key == first.blob_key
    assert second.artifact_path == first.artifact_path
    assert store.dedup_hits == 1
    keys = await store.list_artifacts()
    assert keys == [first.blob_key]


async def test_truncation_during_read_is_incoherent(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    payload = b"A" * (3 * 1024 * 1024)  # spans multiple 1 MiB chunks
    src = _write(tmp_path / "mut.pdf", payload)

    def truncate_mid_read() -> None:
        with open(src, "r+b") as handle:
            handle.truncate(1024)

    with pytest.raises(IncoherentSourceError, match="changed size"):
        await store.stage_from_path(src, suffix=".pdf", hooks={"during-read": truncate_mid_read})

    assert await store.list_artifacts() == []
    assert list((tmp_path / "store" / "tmp").glob("*.tmp")) == []


async def test_append_during_read_is_incoherent(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    payload = b"B" * (2 * 1024 * 1024)
    src = _write(tmp_path / "mut.pdf", payload)

    def append_mid_read() -> None:
        with open(src, "ab") as handle:
            handle.write(b"appended")

    with pytest.raises(IncoherentSourceError):
        await store.stage_from_path(src, suffix=".pdf", hooks={"during-read": append_mid_read})

    assert await store.list_artifacts() == []


async def test_inplace_mutation_during_read_is_incoherent(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    payload = b"C" * (2 * 1024 * 1024)
    src = _write(tmp_path / "mut.pdf", payload)

    def rewrite_mid_read() -> None:
        with open(src, "r+b") as handle:
            handle.seek(0)
            handle.write(b"X" * 4096)  # same size, different bytes

    with pytest.raises(IncoherentSourceError, match="identity changed"):
        await store.stage_from_path(src, suffix=".pdf", hooks={"during-read": rewrite_mid_read})

    assert await store.list_artifacts() == []


async def test_replacement_after_resolve_acquires_current_content_coherently(tmp_path):
    store = LocalSourceStore(tmp_path / "store")
    original = _write(tmp_path / "orig.pdf", b"original content A")
    target = _write(tmp_path / "target.pdf", b"replacement content B")
    src = tmp_path / "src" / "swap.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"original content A")

    def replace_after_resolve() -> None:
        # Path substitution between policy resolution and open: the
        # acquisition opens whatever the path currently resolves to and
        # takes all evidence from that single open — no mixed splice.
        os.replace(target, src)
        assert original.read_bytes() == b"original content A"  # untouched

    staged = await store.stage_from_path(
        src, suffix=".pdf", hooks={"after-resolve": replace_after_resolve}
    )

    expected = hashlib.sha256(b"replacement content B").hexdigest()
    assert staged.blob_key == f"sha256:{expected}"
    assert staged.artifact_path.read_bytes() == b"replacement content B"
    # pre/post evidence describes the file actually read
    assert staged.pre_stat == staged.post_stat


async def test_mutation_after_read_completes_does_not_affect_acquired_bytes(tmp_path):
    """External change after a coherent acquisition cannot rewrite it."""
    store = LocalSourceStore(tmp_path / "store")
    src = _write(tmp_path / "doc.pdf", b"acquired revision A")

    staged = await store.stage_from_path(src, suffix=".pdf")

    # The external source mutates afterwards; the artifact is immutable.
    src.write_bytes(b"mutated revision B")
    assert staged.artifact_path.read_bytes() == b"acquired revision A"
    assert await store.verify_artifact(staged.blob_key, ".pdf", staged.byte_length)


@pytest.mark.parametrize(
    "phase",
    sorted(SOURCE_FAULT_PHASES - {"after-publish", "after-verify"}),
)
async def test_fault_before_publish_leaves_no_artifact(tmp_path: Path, phase: str):
    store = LocalSourceStore(tmp_path / "store", fault_phases={phase})
    src = _write(tmp_path / "doc.pdf", b"faulted acquisition")

    with pytest.raises(InjectedFaultError):
        await store.stage_from_path(src, suffix=".pdf")

    assert await store.list_artifacts() == []
    assert list((tmp_path / "store" / "tmp").glob("*.tmp")) == []


@pytest.mark.parametrize("phase", ["after-publish", "after-verify"])
async def test_fault_after_publish_leaves_unreferenced_residue_only(tmp_path: Path, phase: str):
    """Crash after the atomic publish: bytes exist but no kernel record
    can reference them yet — pre-commit residue, never accepted truth."""
    store = LocalSourceStore(tmp_path / "store", fault_phases={phase})
    src = _write(tmp_path / "doc.pdf", b"residue window")

    with pytest.raises(InjectedFaultError):
        await store.stage_from_path(src, suffix=".pdf")

    assert len(await store.list_artifacts()) == 1  # unreachable, harmless
    assert list((tmp_path / "store" / "tmp").glob("*.tmp")) == []


async def test_large_source_streams_with_bounded_memory(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    size = 32 * 1024 * 1024
    src = tmp_path / "big.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with open(src, "wb") as handle:
        block = b"\x00Z" * (512 * 1024)  # 1 MiB deterministic block
        for _ in range(size // len(block)):
            handle.write(block)
            digest.update(block)

    tracemalloc.start()
    try:
        staged = await store.stage_from_path(src, suffix=".pdf")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert staged.blob_key == f"sha256:{digest.hexdigest()}"
    # 1 MiB chunks: peak must stay far below the 32 MiB source size.
    assert peak < 16 * 1024 * 1024, f"peak traced memory {peak} exceeds bound"


async def test_verify_artifact_detects_corruption(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    src = _write(tmp_path / "doc.pdf", b"verbatim bytes")
    staged = await store.stage_from_path(src, suffix=".pdf")

    staged.artifact_path.chmod(0o644)
    with open(staged.artifact_path, "r+b") as handle:
        handle.seek(0)
        handle.write(b"tampered")

    assert not await store.verify_artifact(staged.blob_key, ".pdf", staged.byte_length)
    assert await store.artifact_exists(staged.blob_key, ".pdf")


async def test_hostile_inputs_rejected(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    with pytest.raises(SourceStoreError):
        store.artifact_path("sha256:" + "../" * 8 + "evil", ".pdf")
    with pytest.raises(SourceStoreError):
        store.artifact_path("sha256:" + "0" * 64, ".EXE")
    with pytest.raises(SourceStoreError):
        store.artifact_path("sha256:" + "0" * 64, ".pdf.exe")
    with pytest.raises(SourceStoreError):
        store.artifact_path("not-a-key", ".pdf")
    with pytest.raises(KernelError):
        await store.stage_from_path(tmp_path / "missing.pdf", suffix="../evil")


async def test_corrupted_existing_artifact_is_healed_on_reacquisition(tmp_path: Path):
    store = LocalSourceStore(tmp_path / "store")
    src = _write(tmp_path / "doc.pdf", b"heal me")
    first = await store.stage_from_path(src, suffix=".pdf")

    first.artifact_path.chmod(0o644)
    with open(first.artifact_path, "r+b") as handle:
        handle.write(b"corrupt!")

    healed = await store.stage_from_path(src, suffix=".pdf")
    assert healed.already_present is False  # corrupt occupant replaced
    assert healed.artifact_path.read_bytes() == b"heal me"
    assert await store.verify_artifact(healed.blob_key, ".pdf", healed.byte_length)
