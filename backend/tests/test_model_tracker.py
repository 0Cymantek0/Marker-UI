import pytest
import json
from unittest.mock import MagicMock, patch
from app.services.model_tracker import (
    ModelTracker,
    MODEL_EXPECTED_SIZES,
    setup_monkeypatch,
    check_and_clean_if_corrupt,
    tracker as global_tracker,
    initialize_all_model_metadata
)

def test_model_expected_sizes():
    assert len(MODEL_EXPECTED_SIZES) == 5
    assert MODEL_EXPECTED_SIZES["layout"] == 1445594557
    assert MODEL_EXPECTED_SIZES["text_recognition"] == 1439035225
    assert MODEL_EXPECTED_SIZES["table_recognition"] == 211235298
    assert MODEL_EXPECTED_SIZES["text_detection"] == 76939499
    assert MODEL_EXPECTED_SIZES["ocr_error_detection"] == 274583927

def test_tracker_initial_and_reset_state():
    with patch("app.services.model_tracker.is_model_downloaded", return_value=False):
        tracker = ModelTracker()
        status = tracker.get_status_dict()
        assert status["overall"]["status"] == "pending"
        assert status["overall"]["total_bytes"] == sum(MODEL_EXPECTED_SIZES.values())
        assert status["overall"]["progress"] == 0.0

        # Now simulate layout and text_detection already downloaded
        def mock_downloaded(key):
            return key in ("layout", "text_detection")

        def mock_size(key):
            return 1000 if key == "layout" else 500

        with patch("app.services.model_tracker.is_model_downloaded", side_effect=mock_downloaded), \
             patch("app.services.model_tracker.get_local_model_size", side_effect=mock_size):
            tracker.reset()
            status = tracker.get_status_dict()
            
            # Completed models
            assert status["models"]["layout"]["status"] == "completed"
            assert status["models"]["layout"]["total_bytes"] == 1000
            assert status["models"]["layout"]["downloaded_bytes"] == 1000
            assert status["models"]["layout"]["progress"] == 100.0

            assert status["models"]["text_detection"]["status"] == "completed"
            assert status["models"]["text_detection"]["total_bytes"] == 500
            assert status["models"]["text_detection"]["downloaded_bytes"] == 500
            assert status["models"]["text_detection"]["progress"] == 100.0

            # Pending models should use expected sizes
            assert status["models"]["text_recognition"]["status"] == "pending"
            assert status["models"]["text_recognition"]["total_bytes"] == MODEL_EXPECTED_SIZES["text_recognition"]
            assert status["models"]["text_recognition"]["downloaded_bytes"] == 0

            # Overall progress should reflect the completed bytes over overall total
            expected_total = 1000 + 500 + MODEL_EXPECTED_SIZES["text_recognition"] + MODEL_EXPECTED_SIZES["table_recognition"] + MODEL_EXPECTED_SIZES["ocr_error_detection"]
            expected_downloaded = 1500
            assert status["overall"]["total_bytes"] == expected_total
            assert status["overall"]["downloaded_bytes"] == expected_downloaded
            assert status["overall"]["progress"] == round((expected_downloaded / expected_total) * 100, 1)

def test_tracker_initialize_model_files():
    tracker = ModelTracker()
    tracker.initialize_model_files("layout", ["file1.bin", "file2.bin"])
    status = tracker.get_status_dict()
    layout = status["models"]["layout"]
    assert layout["total_bytes"] == 0
    assert layout["downloaded_bytes"] == 0
    assert "file1.bin" in layout["files"]
    assert "file2.bin" in layout["files"]

def test_tracker_error_aborts_progress():
    with patch("app.services.model_tracker.is_model_downloaded", return_value=False):
        tracker = ModelTracker()
        tracker.set_failed("Connection timed out")
        assert tracker.has_error is True
        
        # Try updating progress, it should return early and not update downloaded_bytes
        tracker.update_file_progress("layout", "file1.bin", 5000)
        status = tracker.get_status_dict()
        assert status["models"]["layout"]["downloaded_bytes"] == 0

def test_custom_downloads_abort_on_error():
    global_tracker.set_failed("Simulated error")
    try:
        import surya.common.s3 as surya_s3
        setup_monkeypatch()
        
        # Verify custom_download_file raises ValueError immediately if tracker has error
        with pytest.raises(ValueError, match="Download aborted due to error or cancellation"):
            surya_s3.download_file("s3://layout/2025_09_23/model.safetensors", "/tmp/model.safetensors")

        with pytest.raises(ValueError, match="Download aborted due to error or cancellation"):
            surya_s3.download_directory("s3://layout/2025_09_23", "/tmp/layout")
    finally:
        global_tracker.reset()

def test_check_and_clean_if_corrupt(tmp_path):
    # Setup mock checkpoint returning our tmp_path
    checkpoint_path = tmp_path / "layout_model"
    checkpoint_path.mkdir()
    
    with patch("app.services.model_tracker.get_model_checkpoint", return_value="s3://layout/2025_09_23"), \
         patch("surya.common.s3.S3DownloaderMixin.get_local_path", return_value=str(checkpoint_path)):
        
        # 1. Healthy case: manifest exists and contains files with size > 0
        manifest = {"files": ["model.bin", "config.json"]}
        with open(checkpoint_path / "manifest.json", "w") as f:
            json.dump(manifest, f)
            
        with open(checkpoint_path / "model.bin", "wb") as f:
            f.write(b"some content")
        with open(checkpoint_path / "config.json", "wb") as f:
            f.write(b"some config")
            
        # Write an extra garbage file and folder to test storage optimization cleanup
        garbage_file = checkpoint_path / "garbage.txt"
        garbage_file.write_text("should be deleted")
        garbage_dir = checkpoint_path / "weird_dir"
        garbage_dir.mkdir()
        (garbage_dir / "file.txt").write_text("nested")

        assert check_and_clean_if_corrupt("layout") is True
        assert checkpoint_path.exists()  # Kept healthy
        assert not garbage_file.exists()  # Cleaned up!
        assert not garbage_dir.exists()   # Cleaned up!
        
        # 2. Corrupt case: one file is 0 bytes
        with open(checkpoint_path / "model.bin", "wb") as f:
            pass  # Truncate to 0 bytes
            
        assert check_and_clean_if_corrupt("layout") is False
        assert checkpoint_path.exists()  # Kept!
        assert not (checkpoint_path / "model.bin").exists()  # Only corrupt file unlinked!


def test_initialize_all_model_metadata():
    with patch("app.services.model_tracker.is_model_downloaded", return_value=False):
        mock_settings = MagicMock()
        mock_settings.LAYOUT_MODEL_CHECKPOINT = "s3://layout/2025_09_23"
        mock_settings.RECOGNITION_MODEL_CHECKPOINT = "s3://recognition/2025_09_23"
        mock_settings.TABLE_REC_MODEL_CHECKPOINT = "s3://table/2025_09_23"
        mock_settings.DETECTOR_MODEL_CHECKPOINT = "s3://detector/2025_09_23"
        mock_settings.OCR_ERROR_MODEL_CHECKPOINT = "s3://ocr_error/2025_09_23"
        mock_settings.S3_BASE_URL = "https://models.datalab.to"

        manifest_mock = {
            "files": ["model.safetensors", "config.json"]
        }

        def mock_get(url, *args, **kwargs):
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value=manifest_mock)
            return r

        def mock_head(url, *args, **kwargs):
            r = MagicMock()
            r.headers = {"content-length": "100"}
            return r

        with patch("surya.settings.settings", mock_settings), \
             patch("surya.common.s3.S3DownloaderMixin.get_local_path", return_value=None), \
             patch("requests.get", side_effect=mock_get), \
             patch("requests.head", side_effect=mock_head):
            
            global_tracker.reset()
            initialize_all_model_metadata()
            
            status = global_tracker.get_status_dict()
            layout_files = status["models"]["layout"]["files"]
            assert "model.safetensors" in layout_files
            assert layout_files["model.safetensors"]["total_bytes"] == 100
            assert layout_files["model.safetensors"]["status"] == "pending"
            assert status["models"]["layout"]["total_bytes"] == 200


def test_tracker_resume_and_summing():
    tracker = ModelTracker()
    file_sizes = {"file1.bin": 1000, "file2.bin": 2000}
    local_sizes = {"file1.bin": 1000, "file2.bin": 500}
    
    tracker.initialize_model_files_with_sizes("layout", file_sizes, local_sizes)
    status = tracker.get_status_dict()["models"]["layout"]
    assert status["files"]["file1.bin"]["status"] == "completed"
    assert status["files"]["file2.bin"]["status"] == "pending"
    assert status["downloaded_bytes"] == 1500
    assert status["total_bytes"] == 3000
    assert status["progress"] == 50.0

    # Resume downloading file2.bin
    tracker.resume_file_download("layout", "file2.bin", 500)
    tracker.update_file_progress("layout", "file2.bin", 100)
    
    status = tracker.get_status_dict()["models"]["layout"]
    assert status["files"]["file2.bin"]["downloaded_bytes"] == 600
    assert status["downloaded_bytes"] == 1600


def test_custom_download_file_resume(tmp_path):
    local_file = tmp_path / "model.safetensors"
    local_file.write_bytes(b"a" * 100)
    
    mock_settings = MagicMock()
    mock_settings.S3_BASE_URL = "https://models.datalab.to"
    
    mock_response = MagicMock()
    mock_response.status_code = 206
    mock_response.headers = {"content-length": "400"}
    mock_response.iter_content = MagicMock(return_value=[b"b" * 400])
    
    with patch("surya.settings.settings", mock_settings), \
         patch("requests.get", return_value=mock_response) as mock_get_call:
        
        global_tracker.reset()
        global_tracker._models["layout"]["files"] = {
            "model.safetensors": {
                "status": "pending",
                "downloaded_bytes": 0,
                "total_bytes": 500
            }
        }
        global_tracker._models["layout"]["total_bytes"] = 500
        
        import surya.common.s3 as surya_s3
        setup_monkeypatch()
        surya_s3.download_file("https://models.datalab.to/layout/2025_09_23/model.safetensors", str(local_file))
        
        mock_get_call.assert_called_once()
        args, kwargs = mock_get_call.call_args
        assert kwargs["headers"] == {"Range": "bytes=100-"}
        
        content = local_file.read_bytes()
        assert len(content) == 500
        assert content[:100] == b"a" * 100
        assert content[100:] == b"b" * 400
        
        status = global_tracker.get_status_dict()["models"]["layout"]
        assert status["files"]["model.safetensors"]["status"] == "completed"
        assert status["files"]["model.safetensors"]["downloaded_bytes"] == 500
        assert status["downloaded_bytes"] == 500
