"""Tests for the tqdm-tap progress tracker stage mapping."""

from app.services import progress_tracker


class TestMatchStage:
    def test_ocr_is_the_widest_band(self):
        label, start, end = progress_tracker._match_stage("Recognizing Text")
        assert label == "Recognizing text (OCR)"
        # OCR dominates wall-clock, so its band must be the largest.
        assert (end - start) >= 40

    def test_layout_detection_maps_to_early_band(self):
        label, start, end = progress_tracker._match_stage("Detecting bboxes")
        assert start < 30

    def test_generic_running_falls_through_to_llm(self):
        label, start, end = progress_tracker._match_stage("SomeProcessor running")
        assert label == "Refining with LLM"

    def test_unknown_desc_returns_none(self):
        assert progress_tracker._match_stage("totally unrelated bar") is None

    def test_case_insensitive(self):
        assert progress_tracker._match_stage("RECOGNIZING TABLES") is not None


class TestEmit:
    def test_interpolates_within_band(self, monkeypatch):
        captured = {}

        def fake_reporter(job_id, percent, label):
            captured["job_id"] = job_id
            captured["percent"] = percent
            captured["label"] = label

        monkeypatch.setattr(progress_tracker, "_reporter", fake_reporter)
        monkeypatch.setattr(progress_tracker, "_resolve_job_id", lambda: "job-1")

        # Recognizing Text band is 33..80. Halfway through 101 items => ~56-57%.
        progress_tracker._emit("Recognizing Text", 50, 101)

        assert captured["job_id"] == "job-1"
        assert 53 <= captured["percent"] <= 59
        assert "50/101" in captured["label"]

    def test_no_job_no_emit(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            progress_tracker, "_reporter", lambda *a: calls.append(a)
        )
        monkeypatch.setattr(progress_tracker, "_resolve_job_id", lambda: None)

        progress_tracker._emit("Recognizing Text", 50, 101)
        assert calls == []

    def test_zero_total_is_ignored(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            progress_tracker, "_reporter", lambda *a: calls.append(a)
        )
        monkeypatch.setattr(progress_tracker, "_resolve_job_id", lambda: "job-1")

        progress_tracker._emit("Recognizing Text", 0, 0)
        assert calls == []
