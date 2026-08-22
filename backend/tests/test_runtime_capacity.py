"""PR69 layer 1: deterministic admission logic (invariant 30).

Fake capacity and geometry facts only — these tests prove the arithmetic
and classification invariants: stable demand for equal facts, profile
invalidation on material change, safe paths for unknown/OOD inputs, no
overcommit or negative capacity, idempotent release.
"""

from __future__ import annotations

import math

import pytest

from app.services.runtime_capacity import (
    DEFAULT_PREPROCESSOR_FACTS,
    AdmissionError,
    CapacityEnvelope,
    CapacityLedger,
    DemandClass,
    DemandEstimator,
    PageGeometry,
    PinnedPreprocessorFacts,
    ResourceProfile,
    ResidencyState,
    RuntimeCapacityCoordinator,
    scaled_size,
    visual_token_count,
)


def _estimator(**overrides) -> DemandEstimator:
    kwargs = dict(
        crops_per_megapixel=250.0,
        bytes_weights_resident=3 << 30,
        bytes_per_layout_slice=100 << 20,
        bytes_per_detection_chunk_mp=32 << 20,
        bytes_per_recognition_token=24 << 10,
        max_page_lowres_pixels=30_000_000,
    )
    kwargs.update(overrides)
    return DemandEstimator(**kwargs)


def _envelope(usable: int = 8 << 30, base: int = 3 << 30) -> CapacityEnvelope:
    return CapacityEnvelope(
        usable_bytes=usable,
        safety_reserve_bytes=0,
        base_resident_bytes=base,
        device_total_bytes=None,
        coefficients={},
    )


class TestPinnedPreprocessorMath:
    """The estimator's math must equal the pinned surya/marker arithmetic."""

    def test_scaled_size_matches_surya_scale_to_fit_downscale(self):
        # 2000x3000 into (1024, 512): scale = sqrt(524288/6e6)
        assert scaled_size(2000, 3000, (1024, 512), (168, 168)) == (591, 886)

    def test_scaled_size_upscales_to_minimum(self):
        # 100x100 into min (168,168): scale = sqrt(28224/10000) = 1.68,
        # ceil arithmetic lands exactly on the minimum box.
        assert scaled_size(100, 100, (1024, 512), (168, 168)) == (168, 168)

    def test_scaled_size_noop_inside_bounds(self):
        assert scaled_size(800, 600, (1024, 512), (168, 168)) == (800, 600)

    def test_visual_tokens_match_foundation_grid(self):
        # OCR crop cap (1024, 512), patch 14, merge 2: round up to 28-multiples
        # -> (1036, 532) -> 74x38 patches -> 2812/4 tokens.
        assert visual_token_count(1024, 512, DEFAULT_PREPROCESSOR_FACTS) == 703

    def test_max_tokens_per_crop_uses_ocr_task_cap(self):
        assert _estimator().max_tokens_per_crop() == 703

    def test_a4_page_geometry_dpi_math(self):
        geo = PageGeometry(0, 595, 842)  # A4 points
        low = geo.lowres_px
        high = geo.highres_px
        assert low == (math.ceil(595 * 96 / 72), math.ceil(842 * 96 / 72))
        assert high == (math.ceil(595 * 192 / 72), math.ceil(842 * 192 / 72))

    def test_layout_slices_below_threshold_is_one(self):
        est = _estimator()
        assert est.layout_slices((793, 1123)) == 1

    def test_layout_slices_above_threshold_tiles(self):
        est = _estimator()
        # 3000x4000 lowres -> ceil(3000/1200)=3 x ceil(4000/1200)=4 = 12 slices
        assert est.layout_slices((3000, 4000)) == 12

    def test_detection_chunks_slice_by_height(self):
        est = _estimator()
        assert est.detection_chunks((793, 1123)) == 1
        assert est.detection_chunks((793, 2801)) == 3

    def test_crop_bound_scales_with_highres_megapixels(self):
        est = _estimator()
        crops = est.recognition_crop_bound((1587, 2245))  # A4 at 192 dpi
        assert crops == math.ceil(1587 * 2245 / 1e6 * 250)


class TestDemandClassification:
    def test_equal_facts_produce_equal_estimates(self):
        est = _estimator()
        geometries = [PageGeometry(i, 595, 842) for i in range(4)]
        a = est.estimate_for_geometries(geometries, profile_id="p")
        b = est.estimate_for_geometries(list(geometries), profile_id="p")
        assert a == b
        assert a.demand_class is DemandClass.NORMAL

    def test_ocr_disabled_removes_recognition_demand(self):
        est = _estimator()
        geometries = [PageGeometry(0, 595, 842)]
        with_ocr = est.estimate_for_geometries(geometries, profile_id="p")
        without = est.estimate_for_geometries(
            geometries, profile_id="p", ocr_enabled=False
        )
        assert without.peak_recognition_batch == 0
        assert without.envelope_bytes < with_ocr.envelope_bytes

    def test_out_of_distribution_page_takes_safe_class(self):
        est = _estimator(max_page_lowres_pixels=100_000)
        # 595x595 pt -> ~476k lowres pixels > 100k bound
        estimate = est.estimate_for_geometries(
            [PageGeometry(0, 595, 595)], profile_id="p"
        )
        assert estimate.demand_class is DemandClass.OUT_OF_DISTRIBUTION
        assert any("exceed characterized bound" in note for note in estimate.notes)

    def test_page_count_is_reported_not_used_as_pressure_proxy(self):
        est = _estimator()
        one_big = est.estimate_for_geometries([PageGeometry(0, 595, 842)], profile_id="p")
        many_small = est.estimate_for_geometries(
            [PageGeometry(i, 100, 100) for i in range(50)], profile_id="p"
        )
        # The envelope is driven by per-page multipliers, not page count:
        # 50 tiny pages must not out-pressure 1 dense A4.
        assert many_small.envelope_bytes < one_big.envelope_bytes

    def test_missing_file_skips_admission_entirely(self, tmp_path):
        est = _estimator()
        assert est.estimate(tmp_path / "nope.pdf", profile_id="p") is None

    def test_non_marker_suffix_skips_admission(self, tmp_path):
        est = _estimator()
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"pk")
        assert est.estimate(docx, profile_id="p") is None

    def test_unreadable_pdf_skips_admission(self, tmp_path):
        est = _estimator()
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"definitely not a pdf")
        assert est.estimate(bad, profile_id="p") is None


class TestProfileIdentity:
    def _profile(self, batches=(("recognition", 256), ("layout", 32))):
        return ResourceProfile(
            family="marker-gpu",
            device_label="cuda:0",
            dtype_label="auto",
            batch_vector=tuple(sorted(batches)),
        )

    def test_material_batch_change_changes_fingerprint(self):
        a = self._profile()
        b = self._profile(batches=(("recognition", 128), ("layout", 32)))
        assert a.fingerprint() != b.fingerprint()

    def test_device_change_changes_fingerprint(self):
        a = self._profile()
        b = ResourceProfile(
            family="marker-gpu",
            device_label="cuda:1",
            dtype_label="auto",
            batch_vector=a.batch_vector,
        )
        assert a.fingerprint() != b.fingerprint()

    def test_preprocessor_facts_change_changes_fingerprint(self):
        a = self._profile()
        b = ResourceProfile(
            family="marker-gpu",
            device_label="cuda:0",
            dtype_label="auto",
            batch_vector=a.batch_vector,
            facts=PinnedPreprocessorFacts(patch_size=28),
        )
        assert a.fingerprint() != b.fingerprint()

    def test_fingerprint_stable_across_construction_order(self):
        a = self._profile(batches=(("layout", 32), ("recognition", 256)))
        b = self._profile(batches=(("recognition", 256), ("layout", 32)))
        assert a.fingerprint() == b.fingerprint()

    def test_with_batches_returns_new_identity(self):
        a = self._profile()
        b = a.with_batches((("recognition", 64),))
        assert a.batch_vector != b.batch_vector
        assert a.fingerprint() != b.fingerprint()


class TestLedgerArithmetic:
    def test_reservation_cannot_exceed_activation_budget(self):
        ledger = CapacityLedger(_envelope(usable=8 << 30, base=3 << 30))
        with pytest.raises(AdmissionError, match="capacity refused"):
            ledger.admit("j1", (8 << 30) + 1)

    def test_concurrent_reservations_cannot_overcommit(self):
        ledger = CapacityLedger(_envelope(usable=8 << 30, base=3 << 30))
        budget = (8 << 30) - (3 << 30)
        first = ledger.admit("j1", budget // 2)
        with pytest.raises(AdmissionError):
            ledger.admit("j2", budget - first.bytes + 1)
        # Exactly the remainder still fits: never negative, never over.
        second = ledger.admit("j3", budget - first.bytes)
        assert ledger.available_bytes() == 0

    def test_release_is_idempotent(self):
        ledger = CapacityLedger(_envelope())
        r = ledger.admit("j1", 100 << 20)
        assert ledger.release(r.reservation_id) is True
        assert ledger.release(r.reservation_id) is False
        assert ledger.release("never-existed") is False
        assert ledger.reserved_bytes() == 0

    def test_negative_demand_rejected(self):
        ledger = CapacityLedger(_envelope())
        with pytest.raises(AdmissionError, match="cannot be negative"):
            ledger.admit("j1", -1)

    def test_exclusive_reservation_consumes_whole_budget_and_blocks(self):
        ledger = CapacityLedger(_envelope(usable=8 << 30, base=3 << 30))
        r = ledger.admit("j1", 1, exclusive=True)
        assert r.bytes == (8 << 30) - (3 << 30)
        with pytest.raises(AdmissionError, match="exclusive safe-path"):
            ledger.admit("j2", 1)
        ledger.release(r.reservation_id)
        assert ledger.admit("j3", 1) is not None

    def test_reservation_blocked_while_exclusive_active(self):
        ledger = CapacityLedger(_envelope())
        ledger.admit("j1", 1, exclusive=True)
        with pytest.raises(AdmissionError, match="exclusive safe-path"):
            ledger.admit("j2", 1, exclusive=True)

    def test_base_residency_is_charged_once_not_per_reservation(self):
        ledger = CapacityLedger(_envelope(usable=8 << 30, base=3 << 30))
        budget = (8 << 30) - (3 << 30)
        half = budget // 2
        ledger.admit("j1", half)
        # The weights bound must not be charged again per reservation.
        r2 = ledger.admit("j2", half)
        assert r2.bytes == half


class TestCoordinatorAdmission:
    def _coordinator(self, **overrides) -> RuntimeCapacityCoordinator:
        estimator = _estimator(**{k: v for k, v in overrides.items() if k in {
            "crops_per_megapixel", "bytes_weights_resident",
            "bytes_per_layout_slice", "bytes_per_detection_chunk_mp",
            "bytes_per_recognition_token", "max_page_lowres_pixels",
        }})
        coordinator = RuntimeCapacityCoordinator(
            profile=ResourceProfile(
                family="marker-gpu",
                device_label="cuda:0",
                dtype_label="auto",
                batch_vector=(("recognition", 256),),
            ),
            envelope=_envelope(
                usable=overrides.get("usable", 8 << 30),
                base=overrides.get("base", 3 << 30),
            ),
            estimator=estimator,
            clock=lambda: 0.0,
        )
        return coordinator

    def test_admit_then_finish_restores_capacity(self):
        c = self._coordinator()
        estimate = c.estimator.estimate_for_geometries(
            [PageGeometry(0, 595, 842)], profile_id=c.profile.fingerprint()
        )
        ticket = c.admit_estimate("j1", estimate)
        assert c.ledger.reserved_bytes() == estimate.envelope_bytes
        c.finish(ticket, outcome="success")
        assert c.ledger.reserved_bytes() == 0
        assert c.leases.active_count() == 0

    def test_finish_is_idempotent_for_stale_double_settle(self):
        c = self._coordinator()
        estimate = c.estimator.estimate_for_geometries(
            [PageGeometry(0, 300, 200)], profile_id=c.profile.fingerprint()
        )
        ticket = c.admit_estimate("j1", estimate)
        c.finish(ticket, outcome="success")
        c.finish(ticket, outcome="failed")  # stale settle must be a no-op
        assert c.ledger.reserved_bytes() == 0
        assert c.snapshot()["oom_events"] == []

    def test_foreign_profile_estimate_rejected(self):
        c = self._coordinator()
        estimate = c.estimator.estimate_for_geometries(
            [PageGeometry(0, 595, 842)], profile_id="someotherprofile"
        )
        with pytest.raises(AdmissionError, match="different runtime profile"):
            c.admit_estimate("j1", estimate)

    def test_unknown_class_takes_exclusive_safe_path(self):
        c = self._coordinator()
        estimate = c.estimator.estimate_for_geometries([], profile_id=c.profile.fingerprint())
        assert estimate.demand_class is DemandClass.UNKNOWN
        ticket = c.admit_estimate("j1", estimate)
        assert ticket.reservation.exclusive is True
        # A second admission — even NORMAL — must wait.
        normal = c.estimator.estimate_for_geometries(
            [PageGeometry(0, 300, 200)], profile_id=c.profile.fingerprint()
        )
        with pytest.raises(AdmissionError):
            c.admit_estimate("j2", normal)

    def test_unknown_policy_reject_refuses_unknown(self):
        c = self._coordinator()
        coordinator2 = RuntimeCapacityCoordinator(
            profile=c.profile,
            envelope=c.ledger.envelope,
            estimator=c.estimator,
            unknown_policy="reject",
            clock=lambda: 0.0,
        )
        estimate = coordinator2.estimator.estimate_for_geometries(
            [], profile_id=coordinator2.profile.fingerprint()
        )
        with pytest.raises(AdmissionError, match="refused by policy"):
            coordinator2.admit_estimate("j1", estimate)

    def test_admission_while_draining_refused(self):
        c = self._coordinator()
        c.leases.set_state(ResidencyState.DRAINING)
        estimate = c.estimator.estimate_for_geometries(
            [PageGeometry(0, 300, 200)], profile_id=c.profile.fingerprint()
        )
        with pytest.raises(AdmissionError, match="draining"):
            c.admit_estimate("j1", estimate)

    def test_oom_outcome_releases_reservation_and_lease(self):
        c = self._coordinator()
        estimate = c.estimator.estimate_for_geometries(
            [PageGeometry(0, 300, 200)], profile_id=c.profile.fingerprint()
        )
        ticket = c.admit_estimate("j1", estimate)
        c.finish(ticket, outcome="oom", detail="boom")
        assert c.ledger.reserved_bytes() == 0
        assert c.leases.active_count() == 0

    def test_snapshot_is_plain_data(self):
        c = self._coordinator()
        estimate = c.estimator.estimate_for_geometries(
            [PageGeometry(0, 300, 200)], profile_id=c.profile.fingerprint()
        )
        c.admit_estimate("j1", estimate)
        c.observe_cold_load(3.5)
        snap = c.snapshot()
        import json

        json.dumps(snap)  # must stay picklable/serializable for events
        assert snap["profile"]["family"] == "marker-gpu"
        assert snap["observations"][-1]["transition"] == "cold_load"
