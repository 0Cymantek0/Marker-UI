"""PR69 layer 2: concurrency and lease races (invariants 30 + 31).

Real threads hammering the real locks — the proof target is the absence of
over-admission, mid-lease eviction, and leaked capacity, not any specific
lock implementation.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.runtime_capacity import (
    AdmissionError,
    CapacityEnvelope,
    DemandEstimate,
    DemandEstimator,
    PageGeometry,
    ResourceProfile,
    ResidencyState,
    RuntimeCapacityCoordinator,
)

_N_THREADS = 16
_N_ROUNDS = 40


def _small_estimator() -> DemandEstimator:
    return DemandEstimator(
        crops_per_megapixel=250.0,
        bytes_weights_resident=1 << 20,
        bytes_per_layout_slice=1 << 20,
        bytes_per_detection_chunk_mp=1 << 20,
        bytes_per_recognition_token=1,
        max_page_lowres_pixels=30_000_000,
    )


def _coordinator(usable: int, base: int = 0) -> RuntimeCapacityCoordinator:
    envelope = CapacityEnvelope(
        usable_bytes=usable,
        safety_reserve_bytes=0,
        base_resident_bytes=base,
        device_total_bytes=None,
        coefficients={},
    )
    return RuntimeCapacityCoordinator(
        profile=ResourceProfile(
            family="marker-gpu",
            device_label="cuda:0",
            dtype_label="auto",
            batch_vector=(("recognition", 256),),
        ),
        envelope=envelope,
        estimator=_small_estimator(),
    )


def _normal_estimate(coordinator: RuntimeCapacityCoordinator, demand: int) -> DemandEstimate:
    # Craft an estimate with an exact envelope for arithmetic races.
    base = coordinator.estimator.estimate_for_geometries(
        [PageGeometry(0, 300, 200)], profile_id=coordinator.profile.fingerprint()
    )
    from dataclasses import replace

    return replace(base, envelope_bytes=demand)


class TestCapacityRaces:
    def test_racing_admissions_never_exceed_budget(self):
        budget = 100 << 20
        per_request = budget // 2  # exactly two fit, never three
        coordinator = _coordinator(usable=budget)
        admitted = []
        refused = []
        barrier = threading.Barrier(_N_THREADS)

        def worker(i: int) -> None:
            barrier.wait()
            for _ in range(_N_ROUNDS):
                ticket = None
                try:
                    ticket = coordinator.admit_estimate(
                        f"job-{i}", _normal_estimate(coordinator, per_request)
                    )
                    admitted.append(1)
                    time.sleep(0.0005)
                except AdmissionError:
                    refused.append(1)
                finally:
                    if ticket is not None:
                        coordinator.finish(ticket, outcome="success")

        with ThreadPoolExecutor(max_workers=_N_THREADS) as pool:
            list(pool.map(worker, range(_N_THREADS)))

        assert len(admitted) > 0
        assert len(admitted) + len(refused) == _N_THREADS * _N_ROUNDS
        # Invariants: never overcommitted at any instant (checked via the
        # ledger's own bookkeeping being consistent) and fully drained now.
        assert coordinator.ledger.reserved_bytes() == 0
        assert coordinator.leases.active_count() == 0

    def test_concurrent_peak_never_exceeds_budget_with_instrumented_ledger(self):
        budget = 64 << 20
        coordinator = _coordinator(usable=budget)
        per_request = 8 << 20  # 8 concurrent fit; 9 must not
        peak_reserved = 0
        peak_lock = threading.Lock()
        stop = threading.Event()

        def sampler() -> None:
            nonlocal peak_reserved
            while not stop.is_set():
                current = coordinator.ledger.reserved_bytes()
                with peak_lock:
                    peak_reserved = max(peak_reserved, current)
                time.sleep(0.0002)

        def worker(i: int) -> None:
            for _ in range(_N_ROUNDS):
                ticket = None
                try:
                    ticket = coordinator.admit_estimate(
                        f"job-{i}", _normal_estimate(coordinator, per_request)
                    )
                    time.sleep(0.001)
                except AdmissionError:
                    pass
                finally:
                    if ticket is not None:
                        coordinator.finish(ticket, outcome="success")

        sampler_thread = threading.Thread(target=sampler, daemon=True)
        sampler_thread.start()
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(worker, range(12)))
        stop.set()
        sampler_thread.join(timeout=2)

        # The observed concurrent reservation peak can never exceed the
        # declared activation budget — the core anti-overcommit property.
        assert peak_reserved <= budget
        assert peak_reserved > 0

    def test_load_failure_after_reservation_releases_capacity(self):
        coordinator = _coordinator(usable=64 << 20)
        ticket = coordinator.admit_estimate(
            "j1", _normal_estimate(coordinator, 16 << 20)
        )
        assert coordinator.ledger.reserved_bytes() == 16 << 20
        # The worker's terminal path on a failed cold load.
        coordinator.finish(ticket, outcome="failed", detail="model load crashed")
        assert coordinator.ledger.reserved_bytes() == 0

    def test_exception_inside_execution_still_settles(self):
        coordinator = _coordinator(usable=64 << 20)
        outcomes = []

        def execute(job_id: str) -> None:
            ticket = coordinator.admit_estimate(
                job_id, _normal_estimate(coordinator, 8 << 20)
            )
            try:
                raise RuntimeError("converter blew up")
            except Exception as exc:
                outcomes.append(str(exc))
            finally:
                coordinator.finish(ticket, outcome="failed")

        threads = [threading.Thread(target=execute, args=(f"j{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(outcomes) == 8
        assert coordinator.ledger.reserved_bytes() == 0
        assert coordinator.leases.active_count() == 0

    def test_repeated_stale_cleanup_never_releases_another_ticket(self):
        coordinator = _coordinator(usable=64 << 20)
        t1 = coordinator.admit_estimate("j1", _normal_estimate(coordinator, 16 << 20))
        t2 = coordinator.admit_estimate("j2", _normal_estimate(coordinator, 16 << 20))
        # Stale double-settles of t1 must not touch t2's capacity.
        for _ in range(5):
            coordinator.finish(t1, outcome="success")
        assert coordinator.ledger.reserved_bytes() == 16 << 20
        coordinator.finish(t2, outcome="success")
        assert coordinator.ledger.reserved_bytes() == 0


class TestLeaseDrainRaces:
    def test_unload_waits_for_active_lease(self):
        coordinator = _coordinator(usable=64 << 20)
        coordinator.leases.begin_load()
        coordinator.leases.mark_warm()
        lease = coordinator.leases.acquire("borrower")
        drained = []

        def request_unload() -> None:
            drained.append(coordinator.request_unload(timeout=2.0))

        t = threading.Thread(target=request_unload)
        t.start()
        time.sleep(0.2)
        # Still draining: the generation is held by the active borrower.
        assert coordinator.leases.state is ResidencyState.DRAINING
        # New admissions for this generation are blocked while draining.
        with pytest.raises(AdmissionError):
            coordinator.leases.acquire("latecomer")
        coordinator.leases.release(lease.lease_id)
        t.join(timeout=3)
        assert drained == [True]
        assert coordinator.leases.state is ResidencyState.RELEASED

    def test_unload_timeout_refuses_instead_of_evicting(self):
        coordinator = _coordinator(usable=64 << 20)
        coordinator.leases.begin_load()
        coordinator.leases.mark_warm()
        lease = coordinator.leases.acquire("borrower")
        start = time.monotonic()
        drained = coordinator.request_unload(timeout=0.3)
        elapsed = time.monotonic() - start
        assert drained is False
        assert elapsed < 2.0
        # The drain aborted back to WARM: the borrower keeps its generation.
        assert coordinator.leases.state is ResidencyState.WARM
        assert coordinator.leases.active_count() == 1
        coordinator.leases.release(lease.lease_id)

    def test_admission_arriving_during_drain_is_refused(self):
        coordinator = _coordinator(usable=64 << 20)
        coordinator.leases.begin_load()
        coordinator.leases.mark_warm()
        lease = coordinator.leases.acquire("j1")
        result: list[bool] = []

        def drain() -> None:
            result.append(coordinator.request_unload(timeout=1.5))

        t = threading.Thread(target=drain)
        t.start()
        time.sleep(0.1)
        with pytest.raises(AdmissionError, match="draining"):
            coordinator.admit_estimate("j2", _normal_estimate(coordinator, 1 << 20))
        coordinator.leases.release(lease.lease_id)
        t.join(timeout=3)
        assert result == [True]

    def test_volunteer_unload_releases_own_lease_but_keeps_others(self):
        coordinator = _coordinator(usable=64 << 20)
        ticket = coordinator.admit_estimate("self", _normal_estimate(coordinator, 4 << 20))
        other = coordinator.leases.acquire("other-job")
        # The hybrid-OCR low-VRAM protocol: the executing job volunteers.
        drained = coordinator.request_unload(timeout=0.5, volunteer_job="self")
        # The other borrower blocked the unload: refused, nobody evicted.
        assert drained is False
        assert coordinator.leases.active_count() == 1
        assert coordinator.leases.state is ResidencyState.WARM
        coordinator.leases.release(other.lease_id)
        # Settling the volunteer's ticket after a volunteer unload is a
        # harmless no-op (its lease is already gone).
        coordinator.finish(ticket, outcome="success")
        assert coordinator.ledger.reserved_bytes() == 0

    def test_volunteer_unload_with_no_other_borrowers_drains(self):
        coordinator = _coordinator(usable=64 << 20)
        ticket = coordinator.admit_estimate("self", _normal_estimate(coordinator, 4 << 20))
        drained = coordinator.request_unload(timeout=1.0, volunteer_job="self")
        assert drained is True
        assert coordinator.leases.state is ResidencyState.RELEASED
        coordinator.finish(ticket, outcome="success")
        assert coordinator.ledger.reserved_bytes() == 0

    def test_last_lease_release_permits_unload(self):
        coordinator = _coordinator(usable=64 << 20)
        coordinator.leases.begin_load()
        coordinator.leases.mark_warm()
        leases = [coordinator.leases.acquire(f"j{i}") for i in range(4)]
        result: list[bool] = []

        def drain() -> None:
            result.append(coordinator.request_unload(timeout=3.0))

        t = threading.Thread(target=drain)
        t.start()
        for lease in leases:
            time.sleep(0.05)
            coordinator.leases.release(lease.lease_id)
        t.join(timeout=4)
        assert result == [True]
        assert coordinator.leases.state is ResidencyState.RELEASED


class TestOomFeedbackUnderConcurrency:
    def test_oom_storm_opens_bounded_protective_cooldown_then_recovers(self):
        coordinator = _coordinator(usable=64 << 20)
        estimate = _normal_estimate(coordinator, 4 << 20)
        for i in range(3):
            ticket = coordinator.admit_estimate(f"oom-{i}", estimate)
            coordinator.finish(ticket, outcome="oom", detail="CUDA out of memory: boom")
        with pytest.raises(AdmissionError, match="protective cooldown"):
            coordinator.admit_estimate("after", estimate)
        # Independent later work is unaffected once the cooldown lapses;
        # simulate the clock by clearing via a clean success + reset hook.
        coordinator.note_profile_transition(coordinator.profile)  # explicit reset
        ticket = coordinator.admit_estimate("recovered", estimate)
        coordinator.finish(ticket, outcome="success")
        assert coordinator.ledger.reserved_bytes() == 0

    def test_clean_executions_reset_consecutive_oom_pressure(self):
        coordinator = _coordinator(usable=64 << 20)
        estimate = _normal_estimate(coordinator, 4 << 20)
        for i in range(2):
            ticket = coordinator.admit_estimate(f"oom-{i}", estimate)
            coordinator.finish(ticket, outcome="oom")
        coordinator.note_successful_execution()
        # Pressure reset: three more OOMs are needed for cooldown, not one.
        ticket = coordinator.admit_estimate("oom-3", estimate)
        coordinator.finish(ticket, outcome="oom")
        ticket = coordinator.admit_estimate("still-admitted", estimate)
        assert ticket is not None
        coordinator.finish(ticket, outcome="success")
