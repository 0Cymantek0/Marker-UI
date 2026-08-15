"""Durable payload staging contract tests (V3.2 PR64, plan 7.2).

Verifies the content-addressed store in isolation: exact-byte identity,
crash-safe publication, dedup, healing, hostile-path rejection, stale
scratch handling, tamper detection, and deterministic fault phases.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from app.kernel.errors import InjectedFaultError, PayloadStageError
from app.kernel.payloads import (
    BLOB_KEY_PATTERN,
    LOCAL_STORE_PROFILE,
    PHASE_AFTER_FSYNC,
    PHASE_AFTER_PUBLISH,
    PHASE_AFTER_VERIFY,
    PHASE_AFTER_WRITE,
    PHASE_BEFORE_WRITE,
    PHASE_MID_WRITE,
    LocalPayloadStore,
)
from app.utils.canonical import payload_byte_hash

pytestmark = pytest.mark.asyncio


def _binary_payload() -> bytes:
    # All 256 byte values (incl. zero bytes) twice, in order.
    return bytes(range(256)) * 2


async def test_blob_key_is_exact_byte_hash(tmp_path: Path) -> None:
    store = LocalPayloadStore(tmp_path / "store")
    data = b"exact bytes"
    staged = await store.stage(data)
    assert staged.blob_key == payload_byte_hash(data)
    assert BLOB_KEY_PATTERN.match(staged.blob_key)
    assert staged.payload_length == len(data)
    assert staged.already_present is False
    assert staged.locator == f"objects/{staged.blob_key[7:9]}/{staged.blob_key[7:]}"


async def test_stage_small_text_binary_and_large_payloads(tmp_path: Path) -> None:
    store = LocalPayloadStore(tmp_path / "store")
    cases = {
        "empty": b"",
        "text": "hello wörld — ünïcode\n".encode("utf-8"),
        "binary": _binary_payload(),
        "large": (b"0123456789abcdef" * 8192),  # 128 KiB
    }
    for name, data in cases.items():
        staged = await store.stage(data)
        assert staged.payload_length == len(data), name
        check = await store.check_object(staged.blob_key, expected_length=len(data))
        assert check.available, name
        assert await store.read(staged.blob_key) == data, name


async def test_restage_identical_bytes_dedups_without_rewrite(tmp_path: Path) -> None:
    store = LocalPayloadStore(tmp_path / "store")
    data = b"duplicate me"
    first = await store.stage(data)
    obj_path = store.object_path(first.blob_key)
    inode_marker = obj_path.read_bytes()

    second = await store.stage(data)
    assert second.blob_key == first.blob_key
    assert second.already_present is True
    assert store.dedup_hits == 1
    assert store.bytes_written == len(data)  # written exactly once
    assert obj_path.read_bytes() == inode_marker


async def test_tampered_object_is_detected_and_healed(tmp_path: Path) -> None:
    store = LocalPayloadStore(tmp_path / "store")
    data = b"original bytes"
    staged = await store.stage(data)
    obj_path = store.object_path(staged.blob_key)

    # Truncation is detected...
    obj_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    obj_path.write_bytes(b"tampered")
    check = await store.check_object(staged.blob_key, expected_length=len(data))
    assert check.exists and not check.hash_ok and not check.length_ok
    # ...but never silently accepted as the payload.
    with pytest.raises(PayloadStageError):
        await store.read(staged.blob_key)

    # Re-supplying exact bytes heals: quarantine evidence kept, fresh
    # verified object published under the same content identity.
    healed = await store.stage(data)
    assert healed.already_present is False
    assert store.heal_replacements == 1
    assert await store.read(staged.blob_key) == data
    quarantine = list((tmp_path / "store" / "quarantine").iterdir())
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == b"tampered"


async def test_stale_tmp_residue_reported_and_cleaned_by_age(tmp_path: Path) -> None:
    store = LocalPayloadStore(tmp_path / "store")
    await store.stage(b"real object")

    residue = tmp_path / "store" / "tmp" / "deadbeef.tmp"
    residue.write_bytes(b"partial write residue")
    # Recent scratch is never touched (a live publisher may own it).
    fresh = tmp_path / "store" / "tmp" / "cafebabe.tmp"
    fresh.write_bytes(b"live writer")

    listed = await store.list_tmp()
    assert {p.name for p in listed} == {"deadbeef.tmp", "cafebabe.tmp"}

    removed = await store.cleanup_tmp(older_than_seconds=3600)
    assert removed == []  # both files are brand new

    old = residue.stat().st_mtime - 7200
    os.utime(residue, (old, old))
    removed = await store.cleanup_tmp(older_than_seconds=3600)
    assert [p.name for p in removed] == ["deadbeef.tmp"]
    assert not residue.exists()
    assert fresh.exists()  # live scratch untouched


async def test_hostile_identifiers_cannot_reach_paths(tmp_path: Path) -> None:
    store = LocalPayloadStore(tmp_path / "store")
    hostile = [
        "sha256:../../etc/passwd",
        "sha256:" + "z" * 64,
        "",
        "../../objects/aa/aaaa",
        "SHA256:" + "a" * 64,
        "sha256:" + "a" * 63,
    ]
    for key in hostile:
        with pytest.raises(PayloadStageError):
            store.object_path(key)
    hostile_locators = [
        "../../etc/passwd",
        "objects/aa/../../../../escape",
        "objects/ZZ/" + "a" * 64,
        "tmp/whatever",
        "objects/aa/" + "g" * 64,
    ]
    for locator in hostile_locators:
        with pytest.raises(PayloadStageError):
            store.path_for_locator(locator)


async def test_fault_phases_are_deterministic_and_leave_no_final_object(
    tmp_path: Path,
) -> None:
    for phase in (PHASE_BEFORE_WRITE, PHASE_MID_WRITE, PHASE_AFTER_WRITE, PHASE_AFTER_FSYNC):
        store = LocalPayloadStore(tmp_path / f"store-{phase}", fault_phases={phase})
        with pytest.raises(InjectedFaultError) as excinfo:
            await store.stage(b"deterministic fault")
        assert excinfo.value.phase == phase
        # No final object may exist: publication never happened.
        assert await store.list_objects() == []
        # Retry after the interruption succeeds and publishes once.
        store._faults = frozenset()
        staged = await store.stage(b"deterministic fault")
        assert staged.already_present is False


async def test_post_publication_faults_leave_orphan_not_reference(tmp_path: Path) -> None:
    # After publish/verify, an injected fault models a crash before the
    # database transaction: complete immutable bytes exist on disk but
    # nothing may reference them yet (the commit layer must not have run).
    for phase in (PHASE_AFTER_PUBLISH, PHASE_AFTER_VERIFY):
        store = LocalPayloadStore(tmp_path / f"store-{phase}", fault_phases={phase})
        with pytest.raises(InjectedFaultError):
            await store.stage(b"orphaned bytes")
        keys = await store.list_objects()
        assert len(keys) == 1  # complete object, unreachable by truth
        check = await store.check_object(keys[0], expected_length=len(b"orphaned bytes"))
        assert check.available


async def test_retry_after_ambiguous_interruption_is_safe(tmp_path: Path) -> None:
    store = LocalPayloadStore(tmp_path / "store", fault_phases={PHASE_AFTER_FSYNC})
    data = b"ambiguous first attempt"
    with pytest.raises(InjectedFaultError):
        await store.stage(data)
    store._faults = frozenset()
    staged = await store.stage(data)
    assert staged.already_present is False
    again = await store.stage(data)
    assert again.already_present is True
    assert await store.read(staged.blob_key) == data


# ---------------------------------------------------------------------------
# storage failure classes (plan 7.6) — deterministic simulation
# ---------------------------------------------------------------------------


async def test_unavailable_storage_root_surfaces_honestly(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory")  # mkdir must fail
    store = LocalPayloadStore(blocked / "payloads")
    with pytest.raises(PayloadStageError):
        await store.stage(b"no space equivalent")


async def test_write_failure_surfaces_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalPayloadStore(tmp_path / "store")
    real_open = open
    calls = {"n": 0}

    def flaky_open(*args, **kwargs):
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else kwargs.get("mode", "")
        if mode == "xb":  # our exclusive tmp creation only
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "No space left on device")
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    with pytest.raises(PayloadStageError):
        await store.stage(b"first try fails on write")
    monkeypatch.setattr("builtins.open", real_open)
    staged = await store.stage(b"first try fails on write")
    assert staged.already_present is False


async def test_fsync_failure_never_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalPayloadStore(tmp_path / "store")

    def failing_fsync(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr("app.kernel.payloads.os.fsync", failing_fsync)
    with pytest.raises(PayloadStageError):
        await store.stage(b"must not appear")
    assert await store.list_objects() == []


async def test_rename_failure_never_publishes_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalPayloadStore(tmp_path / "store")

    def failing_replace(src, dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("app.kernel.payloads.os.replace", failing_replace)
    with pytest.raises(PayloadStageError):
        await store.stage(b"never renamed")
    assert await store.list_objects() == []


async def test_readback_mismatch_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate torn storage: publish succeeds but read-back differs. The
    # stage call must fail closed (never claim availability on an
    # unverifiable read), while the correctly-written object itself
    # remains valid content — a retry with healthy reads dedups onto it.
    store = LocalPayloadStore(tmp_path / "store")
    real_read = Path.read_bytes
    target = b"what should be there"

    def torn_read(self: Path):
        data = real_read(self)
        if data == target:
            return b"torn sector" + data[12:]
        return data

    monkeypatch.setattr(Path, "read_bytes", torn_read)
    with pytest.raises(PayloadStageError):
        await store.stage(target)
    monkeypatch.setattr(Path, "read_bytes", real_read)

    keys = await store.list_objects()
    assert keys == [payload_byte_hash(target)]
    check = await store.check_object(keys[0], expected_length=len(target))
    assert check.available
    staged = await store.stage(target)
    assert staged.already_present is True


async def test_published_objects_are_readonly_hint(tmp_path: Path) -> None:
    store = LocalPayloadStore(tmp_path / "store")
    staged = await store.stage(b"immutable hint")
    mode = stat.S_IMODE(store.object_path(staged.blob_key).stat().st_mode)
    assert mode & stat.S_IWUSR == 0 or os.name == "nt"
