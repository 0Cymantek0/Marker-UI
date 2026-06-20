"""Tests for GPU detection and worker-count resolution (no GPU needed)."""

from unittest.mock import MagicMock, patch

from app.core import gpu


class TestDetectGpus:
    def test_torch_reports_devices(self):
        fake_torch = MagicMock()
        fake_torch.cuda.is_available.return_value = True
        fake_torch.cuda.device_count.return_value = 3
        with patch.dict("sys.modules", {"torch": fake_torch}):
            assert gpu.detect_gpus() == 3

    def test_torch_cpu_only_falls_back_to_nvidia_smi(self):
        fake_torch = MagicMock()
        fake_torch.cuda.is_available.return_value = False
        smi_out = "GPU 0: NVIDIA A100 (UUID: GPU-aaa)\nGPU 1: NVIDIA A100 (UUID: GPU-bbb)\n"
        with patch.dict("sys.modules", {"torch": fake_torch}), \
             patch("subprocess.run", return_value=MagicMock(stdout=smi_out)):
            assert gpu.detect_gpus() == 2

    def test_no_torch_no_smi_returns_zero(self):
        fake_torch = MagicMock()
        fake_torch.cuda.is_available.return_value = False
        with patch.dict("sys.modules", {"torch": fake_torch}), \
             patch("subprocess.run", side_effect=FileNotFoundError):
            assert gpu.detect_gpus() == 0

    def test_torch_raises_then_smi_succeeds(self):
        fake_torch = MagicMock()
        fake_torch.cuda.is_available.side_effect = RuntimeError("boom")
        smi_out = "GPU 0: NVIDIA RTX 4090 (UUID: GPU-zzz)\n"
        with patch.dict("sys.modules", {"torch": fake_torch}), \
             patch("subprocess.run", return_value=MagicMock(stdout=smi_out)):
            assert gpu.detect_gpus() == 1


class TestResolveWorkerCount:
    def test_cpu_only_always_one(self):
        assert gpu.resolve_worker_count("auto", None, 0) == 1
        assert gpu.resolve_worker_count("manual", 8, 0) == 1

    def test_auto_uses_all_detected(self):
        assert gpu.resolve_worker_count("auto", None, 4) == 4

    def test_manual_clamps_to_detected(self):
        assert gpu.resolve_worker_count("manual", 8, 4) == 4
        assert gpu.resolve_worker_count("manual", 2, 4) == 2

    def test_manual_floor_is_one(self):
        assert gpu.resolve_worker_count("manual", 0, 4) == 1
        assert gpu.resolve_worker_count("manual", -5, 4) == 1

    def test_manual_without_count_falls_back_to_detected(self):
        assert gpu.resolve_worker_count("manual", None, 4) == 4

    def test_unknown_mode_uses_detected(self):
        assert gpu.resolve_worker_count("garbage", None, 3) == 3


class TestDeviceForWorker:
    def test_pins_to_cuda_index(self):
        assert gpu.device_for_worker(0, 2) == "cuda:0"
        assert gpu.device_for_worker(1, 2) == "cuda:1"

    def test_cpu_when_no_gpu(self):
        assert gpu.device_for_worker(0, 0) == "cpu"
