"""Round-trip tests for the worker secrets snapshot (export -> seed)."""

import pytest

from app.core import api_manager


def _clear_all():
    api_manager.seed_secrets_snapshot({
        "secrets_cache": {},
        "provider_keys": {},
        "active_key_index": {},
        "provider_concurrency": {},
        "provider_hosts": {},
    })


@pytest.fixture(autouse=True)
def _restore_api_manager_state():
    """Snapshot/export mutate process-global api_manager caches; restore on teardown
    so a later test in the same session sees a clean slate."""
    saved = api_manager.export_secrets_snapshot()
    yield
    api_manager.seed_secrets_snapshot(saved)


def test_export_is_plain_picklable_data():
    import pickle

    _clear_all()
    api_manager._secrets_cache["provider_p1_key_0_api_key"] = "sk-abc"
    api_manager._provider_keys["p1"] = ["sk-abc", "sk-fallback"]
    api_manager._active_key_index["p1"] = 1
    api_manager._provider_concurrency["p1"] = 4
    api_manager._provider_hosts["api.example.com"] = "p1"

    snap = api_manager.export_secrets_snapshot()

    # Must survive a pickle round-trip (it crosses the spawn boundary).
    restored = pickle.loads(pickle.dumps(snap))
    assert restored == snap
    assert restored["secrets_cache"]["provider_p1_key_0_api_key"] == "sk-abc"
    assert restored["provider_keys"]["p1"] == ["sk-abc", "sk-fallback"]
    assert restored["active_key_index"]["p1"] == 1
    assert restored["provider_concurrency"]["p1"] == 4
    assert restored["provider_hosts"]["api.example.com"] == "p1"


def test_export_is_a_copy_not_a_reference():
    _clear_all()
    api_manager._provider_keys["p1"] = ["sk-abc"]
    snap = api_manager.export_secrets_snapshot()
    # Mutating live state after export must not change the snapshot.
    api_manager._provider_keys["p1"].append("sk-new")
    assert snap["provider_keys"]["p1"] == ["sk-abc"]


def test_seed_replaces_state_in_a_fresh_process():
    _clear_all()
    # Simulate a worker that starts with empty caches, then seeds.
    snap = {
        "secrets_cache": {"provider_p2_key_0_api_key": "sk-xyz"},
        "provider_keys": {"p2": ["sk-xyz"]},
        "active_key_index": {"p2": 0},
        "provider_concurrency": {"p2": 2},
        "provider_hosts": {"host2": "p2"},
    }
    api_manager.seed_secrets_snapshot(snap)

    assert api_manager.get_secret("provider_p2_key_0_api_key") == "sk-xyz"
    assert api_manager._provider_keys["p2"] == ["sk-xyz"]
    assert api_manager._provider_concurrency["p2"] == 2
    assert api_manager._provider_hosts["host2"] == "p2"
    # Semaphores are never carried across; they rebuild lazily at the new limit.
    assert api_manager._sync_semaphores == {}


def test_seed_with_empty_snapshot_is_a_noop():
    _clear_all()
    api_manager._secrets_cache["k"] = "v"
    api_manager.seed_secrets_snapshot({})
    # Empty/None snapshot must not wipe existing state.
    assert api_manager._secrets_cache.get("k") == "v"
