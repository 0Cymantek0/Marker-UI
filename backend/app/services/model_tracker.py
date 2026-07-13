import logging
import os
import time
import threading
import json
import concurrent.futures
from pathlib import Path
from typing import Any, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession
import requests

logger = logging.getLogger(__name__)

MODEL_NAMES = {
    "layout": "Layout Detection",
    "text_recognition": "Text Recognition",
    "text_detection": "Text Detection",
    "table_recognition": "Table Structure Recognition",
    "ocr_error_detection": "OCR Error Correction",
}

MODEL_EXPECTED_SIZES = {
    "layout": 1445594557,
    "text_recognition": 1439035225,
    "table_recognition": 211235298,
    "text_detection": 76939499,
    "ocr_error_detection": 274583927,
}

def get_model_checkpoint(model_key: str) -> Optional[str]:
    try:
        from surya.settings import settings as surya_settings
        checkpoints = {
            "layout": surya_settings.LAYOUT_MODEL_CHECKPOINT,
            "text_recognition": surya_settings.RECOGNITION_MODEL_CHECKPOINT,
            "table_recognition": surya_settings.TABLE_REC_MODEL_CHECKPOINT,
            "text_detection": surya_settings.DETECTOR_MODEL_CHECKPOINT,
            "ocr_error_detection": surya_settings.OCR_ERROR_MODEL_CHECKPOINT,
        }
        return checkpoints.get(model_key)
    except Exception:
        return None

def check_and_clean_if_corrupt(model_key: str) -> bool:
    """Checks if model directory is healthy. If corrupt/incomplete, deletes corrupt files, keeps healthy ones, returns False."""
    try:
        from surya.common.s3 import S3DownloaderMixin
        import shutil
        import json

        cp = get_model_checkpoint(model_key)
        if not cp:
            return False

        local_path = S3DownloaderMixin.get_local_path(cp)
        if not local_path:
            return False

        p = Path(local_path)
        if not p.exists():
            return False

        manifest_path = p / "manifest.json"
        if not manifest_path.exists():
            logger.info(f"Removing corrupt directory for {model_key} (missing manifest.json)")
            shutil.rmtree(p, ignore_errors=True)
            return False

        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            
            files = manifest.get("files", [])
            if not files:
                logger.info(f"Removing corrupt directory for {model_key} (empty files list in manifest)")
                shutil.rmtree(p, ignore_errors=True)
                return False

            is_healthy = True
            for file in files:
                f_path = p / file
                if not f_path.exists():
                    is_healthy = False
                elif f_path.stat().st_size == 0:
                    logger.info(f"Removing empty/corrupt file {file} for {model_key}")
                    f_path.unlink()
                    is_healthy = False

        except Exception as e:
            logger.info(f"Removing corrupt directory for {model_key} (invalid manifest JSON: {e})")
            shutil.rmtree(p, ignore_errors=True)
            return False

        # Clean up any extra/unknown files in the directory to optimize user storage
        try:
            manifest_files = set(files)
            manifest_files.add("manifest.json")
            for item in p.iterdir():
                if item.name not in manifest_files:
                    logger.info(f"Removing extra file/directory {item.name} from {model_key} to optimize storage")
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Error cleaning up extra files for {model_key}: {e}")

        return is_healthy
    except Exception as e:
        logger.warning(f"Error checking health for {model_key}: {e}")
        return False

def is_model_downloaded(model_key: str) -> bool:
    return check_and_clean_if_corrupt(model_key)

def get_local_model_size(model_key: str) -> int:
    try:
        from surya.common.s3 import S3DownloaderMixin
        cp = get_model_checkpoint(model_key)
        if not cp:
            return 0
        local_path = S3DownloaderMixin.get_local_path(cp)
        if not local_path:
            return 0
        p = Path(local_path)
        if not p.exists():
            return 0
        total = 0
        for item in p.rglob("*"):
            if item.is_file() and item.name != "manifest.json":
                total += item.stat().st_size
        return total
    except Exception:
        return 0

class ModelTracker:
    """Thread-safe tracker for monitoring AI model download progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._loading = False
        self._cancel_requested = False
        self._error: Optional[str] = None
        self._start_time: Optional[float] = None
        self._session_downloaded_bytes = 0

        # Initialize models dict
        self._models: Dict[str, Dict[str, Any]] = {}
        for key, name in MODEL_NAMES.items():
            self._models[key] = {
                "name": name,
                "status": "pending",
                "downloaded_bytes": 0,
                "total_bytes": MODEL_EXPECTED_SIZES.get(key, 0),
                "progress": 0.0,
                "files": {}
            }
        
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._initialized = False
            self._loading = False
            self._cancel_requested = False
            self._error = None
            self._start_time = None
            self._session_downloaded_bytes = 0
            for key in self._models:
                if is_model_downloaded(key):
                    local_size = get_local_model_size(key)
                    self._models[key] = {
                        "name": MODEL_NAMES[key],
                        "status": "completed",
                        "downloaded_bytes": local_size,
                        "total_bytes": local_size,
                        "progress": 100.0,
                        "files": {}
                    }
                else:
                    self._models[key] = {
                        "name": MODEL_NAMES[key],
                        "status": "pending",
                        "downloaded_bytes": 0,
                        "total_bytes": MODEL_EXPECTED_SIZES.get(key, 0),
                        "progress": 0.0,
                        "files": {}
                    }

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    @property
    def has_error(self) -> bool:
        with self._lock:
            return self._error is not None

    def request_cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            self._loading = False
            # Mark all downloading models as failed
            for key in self._models:
                if self._models[key]["status"] in ("pending", "downloading"):
                    self._models[key]["status"] = "failed"

    def set_initialized(self, val: bool) -> None:
        with self._lock:
            self._initialized = val
            self._loading = False
            if val:
                self._error = None
                self._cancel_requested = False
                # Ensure all models are completed
                for key in self._models:
                    self._models[key]["status"] = "completed"
                    self._models[key]["progress"] = 100.0

    def set_loading(self, val: bool) -> None:
        with self._lock:
            self._loading = val

    def set_failed(self, error_msg: str) -> None:
        with self._lock:
            self._error = error_msg
            self._loading = False
            for key in self._models:
                if self._models[key]["status"] in ("pending", "downloading"):
                    self._models[key]["status"] = "failed"

    def set_cancelled(self) -> None:
        with self._lock:
            self._error = "Download cancelled by user"
            self._loading = False

    def start_model_download(self, model_key: str) -> None:
        with self._lock:
            if model_key in self._models:
                self._models[model_key]["status"] = "downloading"
                self._error = None
                if self._start_time is None:
                    self._start_time = time.time()

    def complete_model_download(self, model_key: str) -> None:
        with self._lock:
            if model_key in self._models:
                self._models[model_key]["status"] = "completed"
                self._models[model_key]["progress"] = 100.0
                # If total bytes wasn't set, set it to downloaded bytes
                if self._models[model_key]["total_bytes"] == 0:
                    self._models[model_key]["total_bytes"] = self._models[model_key]["downloaded_bytes"]

    def fail_model_download(self, model_key: str, error_msg: str) -> None:
        with self._lock:
            if model_key in self._models:
                self._models[model_key]["status"] = "failed"
                self._error = error_msg

    def initialize_model_files(self, model_key: str, filenames: list[str]) -> None:
        with self._lock:
            if model_key in self._models:
                # If already populated with files, don't blow it away
                if self._models[model_key]["files"]:
                    return
                # Reset total_bytes and downloaded_bytes because we will sum file sizes as they start downloading
                self._models[model_key]["total_bytes"] = 0
                self._models[model_key]["downloaded_bytes"] = 0
                self._models[model_key]["files"] = {}
                for filename in filenames:
                    self._models[model_key]["files"][filename] = {
                        "status": "pending",
                        "downloaded_bytes": 0,
                        "total_bytes": 0
                    }

    def initialize_model_files_with_sizes(self, model_key: str, file_sizes: Dict[str, int], local_sizes: Dict[str, int]) -> None:
        with self._lock:
            if model_key in self._models:
                self._models[model_key]["files"] = {}
                total_bytes = 0
                downloaded_bytes = 0
                for filename, size in file_sizes.items():
                    local_size = local_sizes.get(filename, 0)
                    status = "completed" if local_size >= size and size > 0 else "pending"
                    self._models[model_key]["files"][filename] = {
                        "status": status,
                        "downloaded_bytes": local_size,
                        "total_bytes": size
                    }
                    total_bytes += size
                    downloaded_bytes += local_size
                
                self._models[model_key]["total_bytes"] = total_bytes
                self._models[model_key]["downloaded_bytes"] = downloaded_bytes
                if total_bytes > 0:
                    self._models[model_key]["progress"] = min(100.0, round((downloaded_bytes / total_bytes) * 100.0, 1))

    def start_file_download(self, model_key: str, filename: str) -> None:
        with self._lock:
            if model_key in self._models:
                if filename not in self._models[model_key]["files"]:
                    self._models[model_key]["files"][filename] = {
                        "status": "downloading",
                        "downloaded_bytes": 0,
                        "total_bytes": 0
                    }
                else:
                    self._models[model_key]["files"][filename]["status"] = "downloading"

    def resume_file_download(self, model_key: str, filename: str, offset: int) -> None:
        with self._lock:
            if model_key in self._models:
                self._models[model_key]["status"] = "downloading"
                if filename not in self._models[model_key]["files"]:
                    self._models[model_key]["files"][filename] = {
                        "status": "downloading",
                        "downloaded_bytes": offset,
                        "total_bytes": 0
                    }
                else:
                    self._models[model_key]["files"][filename]["status"] = "downloading"
                    self._models[model_key]["files"][filename]["downloaded_bytes"] = offset
                
                # Update model downloaded_bytes by summing files
                self._models[model_key]["downloaded_bytes"] = sum(
                    f["downloaded_bytes"] for f in self._models[model_key]["files"].values()
                )

    def set_file_total_size(self, model_key: str, filename: str, total_size: int) -> None:
        with self._lock:
            if model_key in self._models and filename in self._models[model_key]["files"]:
                file_info = self._models[model_key]["files"][filename]
                old_total = file_info["total_bytes"]
                file_info["total_bytes"] = total_size
                # Update model total size
                self._models[model_key]["total_bytes"] += (total_size - old_total)

    def update_file_progress(self, model_key: str, filename: str, chunk_len: int) -> None:
        with self._lock:
            if self._error or self._cancel_requested:
                return
            if self._start_time is None:
                self._start_time = time.time()
            self._session_downloaded_bytes += chunk_len

            if model_key in self._models:
                if self._models[model_key]["status"] in ("failed", "completed"):
                    return
                if filename in self._models[model_key]["files"]:
                    self._models[model_key]["files"][filename]["downloaded_bytes"] += chunk_len

                # Update model downloaded_bytes by summing files
                self._models[model_key]["downloaded_bytes"] = sum(
                    f["downloaded_bytes"] for f in self._models[model_key]["files"].values()
                )

                # Update progress percentage
                total = self._models[model_key]["total_bytes"]
                downloaded = self._models[model_key]["downloaded_bytes"]
                if total > 0:
                    self._models[model_key]["progress"] = min(100.0, round((downloaded / total) * 100.0, 1))

    def complete_file_download(self, model_key: str, filename: str) -> None:
        with self._lock:
            if model_key in self._models and filename in self._models[model_key]["files"]:
                file_info = self._models[model_key]["files"][filename]
                file_info["status"] = "completed"
                # Align bytes
                diff = file_info["total_bytes"] - file_info["downloaded_bytes"]
                if diff > 0:
                    file_info["downloaded_bytes"] = file_info["total_bytes"]
                    self._session_downloaded_bytes += diff

                # Update model downloaded_bytes by summing files
                self._models[model_key]["downloaded_bytes"] = sum(
                    f["downloaded_bytes"] for f in self._models[model_key]["files"].values()
                )

                total = self._models[model_key]["total_bytes"]
                downloaded = self._models[model_key]["downloaded_bytes"]
                if total > 0:
                    self._models[model_key]["progress"] = min(100.0, round((downloaded / total) * 100.0, 1))

    def fail_file_download(self, model_key: str, filename: str, error_msg: str) -> None:
        with self._lock:
            if model_key in self._models and filename in self._models[model_key]["files"]:
                self._models[model_key]["files"][filename]["status"] = "failed"
                self._models[model_key]["status"] = "failed"
                self._error = error_msg

    def get_status_dict(self) -> Dict[str, Any]:
        with self._lock:
            # Calculate overall metrics
            total_bytes = 0
            downloaded_bytes = 0
            downloading_models = 0
            completed_models = 0
            any_downloading = False

            for key, m in self._models.items():
                total_bytes += m["total_bytes"]
                downloaded_bytes += m["downloaded_bytes"]
                if m["status"] == "downloading":
                    any_downloading = True
                    downloading_models += 1
                elif m["status"] == "completed":
                    completed_models += 1

            progress = 0.0
            if total_bytes > 0:
                progress = min(100.0, round((downloaded_bytes / total_bytes) * 100.0, 1))
            elif completed_models == len(self._models):
                progress = 100.0

            # Calculate speed and ETA
            speed = 0.0
            eta = 0.0
            if any_downloading and self._start_time is not None:
                elapsed = time.time() - self._start_time
                if elapsed > 0.1 and self._session_downloaded_bytes > 0:
                    speed = self._session_downloaded_bytes / elapsed  # bytes / sec
                    remaining_bytes = max(0, total_bytes - downloaded_bytes)
                    eta = remaining_bytes / speed

            # Overall status string
            if self._initialized:
                overall_status = "completed"
            elif self._error:
                overall_status = "failed"
            elif any_downloading:
                overall_status = "downloading"
            elif self._loading:
                overall_status = "loading"
            else:
                overall_status = "pending"

            return {
                "initialized": self._initialized,
                "loading": self._loading,
                "cancel_requested": self._cancel_requested,
                "error": self._error,
                "models": self._models,
                "overall": {
                    "status": overall_status,
                    "progress": progress,
                    "downloaded_bytes": downloaded_bytes,
                    "total_bytes": total_bytes,
                    "speed": round(speed / (1024 * 1024), 2),  # Convert to MB/s
                    "eta": int(eta)
                }
            }

tracker = ModelTracker()

def get_model_key_from_url(url: str) -> str:
    parsed = url
    for prefix in ["https://models.datalab.to/", "http://models.datalab.to/", "s3://"]:
        if parsed.startswith(prefix):
            parsed = parsed[len(prefix):]
            break
    parts = parsed.split("/")
    if len(parts) > 0:
        model_key = parts[0]
        # Normalize table_rec to table_recognition if needed
        if model_key == "table_rec" or model_key == "table_recognition":
            return "table_recognition"
        if model_key in MODEL_NAMES:
            return model_key
    return "unknown"

def setup_monkeypatch() -> None:
    """Applies monkeypatches to surya.common.s3 download helpers."""
    try:
        import surya.common.s3 as surya_s3
    except ImportError:
        logger.warning("surya package not installed yet, skipping download monkeypatch.")
        return

    def custom_check_manifest(local_dir: str) -> bool:
        local_dir_path = Path(local_dir)
        manifest_path = local_dir_path / "manifest.json"
        if not manifest_path.exists():
            return False
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            for file in manifest.get("files", []):
                file_path = local_dir_path / file
                if not file_path.exists() or file_path.stat().st_size == 0:
                    return False
        except Exception:
            return False
        return True

    def custom_download_file(remote_path: str, local_path: str, chunk_size: int = 1024 * 1024):
        if tracker.cancel_requested or tracker.has_error:
            raise ValueError("Download aborted due to error or cancellation")

        local_path_obj = Path(local_path)
        filename = local_path_obj.name
        model_key = get_model_key_from_url(remote_path)

        if filename == "manifest.json":
            try:
                response = requests.get(remote_path, stream=True, allow_redirects=True)
                response.raise_for_status()
                with open(local_path_obj, "wb") as f:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if tracker.cancel_requested:
                            raise ValueError("Download aborted due to cancellation")
                        if chunk:
                            f.write(chunk)
                return local_path_obj
            except Exception as e:
                if local_path_obj.exists():
                    local_path_obj.unlink()
                raise e

        # Get expected size
        expected_size = 0
        if model_key in tracker._models and filename in tracker._models[model_key]["files"]:
            expected_size = tracker._models[model_key]["files"][filename]["total_bytes"]
        
        if expected_size == 0:
            try:
                hr = requests.head(remote_path, timeout=5, allow_redirects=True)
                expected_size = int(hr.headers.get("content-length", 0))
            except Exception:
                pass

        # Check if already fully downloaded
        if local_path_obj.exists():
            local_size = local_path_obj.stat().st_size
            if expected_size > 0 and local_size == expected_size:
                logger.info(f"Skipping download for {filename} (already fully downloaded: {local_size} bytes)")
                tracker.start_file_download(model_key, filename)
                if expected_size > 0:
                    tracker.set_file_total_size(model_key, filename, expected_size)
                tracker.complete_file_download(model_key, filename)
                return local_path_obj

        # Range-based resume setup
        headers = {}
        write_mode = "wb"
        offset = 0
        if local_path_obj.exists():
            local_size = local_path_obj.stat().st_size
            if expected_size > 0 and local_size < expected_size:
                headers["Range"] = f"bytes={local_size}-"
                write_mode = "ab"
                offset = local_size
                logger.info(f"Attempting to resume download for {filename} from byte {offset}")

        # Start download tracking
        if offset > 0:
            tracker.resume_file_download(model_key, filename, offset)
        else:
            tracker.start_file_download(model_key, filename)

        try:
            response = requests.get(remote_path, headers=headers, stream=True, allow_redirects=True)
            
            if response.status_code == 200 and write_mode == "ab":
                logger.info(f"Server did not return 206 for range request of {filename}, falling back to full download")
                write_mode = "wb"
                offset = 0
                tracker.start_file_download(model_key, filename)
            elif response.status_code == 416:
                logger.warning(f"Range not satisfiable for {filename}, falling back to full download")
                write_mode = "wb"
                offset = 0
                tracker.start_file_download(model_key, filename)
                response = requests.get(remote_path, stream=True, allow_redirects=True)

            response.raise_for_status()

            # Set/Update expected size in tracker
            content_length = int(response.headers.get('content-length', 0))
            total_size = content_length + offset
            tracker.set_file_total_size(model_key, filename, total_size)

            with open(local_path_obj, write_mode) as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if tracker.cancel_requested:
                        raise ValueError("Download aborted due to cancellation")
                    if chunk:
                        f.write(chunk)
                        tracker.update_file_progress(model_key, filename, len(chunk))

            tracker.complete_file_download(model_key, filename)
            return local_path_obj
        except Exception as e:
            tracker.fail_file_download(model_key, filename, str(e))
            logger.error(f"Download error for file {remote_path}: {str(e)}")
            # Remove partial weight file so it cannot masquerade as a complete
            # download on the next health check (check_and_clean_if_corrupt only
            # rejects zero-byte files). A fresh retry starts clean.
            if local_path_obj.exists():
                try:
                    local_path_obj.unlink()
                except OSError:
                    pass
            raise e

    def custom_download_directory(remote_path: str, local_dir: str):
        if tracker.cancel_requested or tracker.has_error:
            raise ValueError("Download aborted due to error or cancellation")

        model_key = get_model_key_from_url(remote_path)
        clean_remote_path = remote_path.replace("s3://", "")

        # Check manifest first
        if surya_s3.check_manifest(local_dir):
            tracker.complete_model_download(model_key)
            return

        tracker.start_model_download(model_key)

        s3_url = surya_s3.join_urls(surya_s3.settings.S3_BASE_URL, clean_remote_path)
        manifest_url = surya_s3.join_urls(s3_url, "manifest.json")
        manifest_path = os.path.join(local_dir, "manifest.json")

        try:
            # Create local_dir if it doesn't exist
            os.makedirs(local_dir, exist_ok=True)
            
            # Download manifest directly to local_dir
            surya_s3.download_file(manifest_url, manifest_path)
            
            with open(manifest_path, "r") as f:
                manifest = json.load(f)

            files = manifest.get("files", [])
            tracker.initialize_model_files(model_key, files)

            # Download files directly in parallel to local_dir
            workers = getattr(surya_s3.settings, "PARALLEL_DOWNLOAD_WORKERS", 4)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = []
                for file in files:
                    remote_file = surya_s3.join_urls(s3_url, file)
                    local_file = os.path.join(local_dir, file)
                    # Create parent directories if nested
                    os.makedirs(os.path.dirname(local_file), exist_ok=True)
                    futures.append(executor.submit(surya_s3.download_file, remote_file, local_file))

                for future in futures:
                    future.result()

            tracker.complete_model_download(model_key)
        except Exception as e:
            tracker.fail_model_download(model_key, str(e))
            raise e

    surya_s3.download_file = custom_download_file
    surya_s3.download_directory = custom_download_directory
    surya_s3.check_manifest = custom_check_manifest
    logger.info("Monkeypatched surya.common.s3 download routines successfully.")

def download_all_models_parallel() -> None:
    """Downloads all missing surya models in parallel using a thread pool."""
    try:
        from surya.settings import settings as surya_settings
        from surya.common.s3 import S3DownloaderMixin, download_directory
    except ImportError:
        logger.warning("surya package not installed, cannot download models.")
        return

    import concurrent.futures

    checkpoints = {
        "layout": surya_settings.LAYOUT_MODEL_CHECKPOINT,
        "text_recognition": surya_settings.RECOGNITION_MODEL_CHECKPOINT,
        "table_recognition": surya_settings.TABLE_REC_MODEL_CHECKPOINT,
        "text_detection": surya_settings.DETECTOR_MODEL_CHECKPOINT,
        "ocr_error_detection": surya_settings.OCR_ERROR_MODEL_CHECKPOINT,
    }

    def download_one(checkpoint: str):
        if tracker.cancel_requested or tracker.has_error:
            return
        local_path = S3DownloaderMixin.get_local_path(checkpoint)
        # Strip s3:// prefix to avoid 404
        clean_checkpoint = checkpoint.replace("s3://", "")
        download_directory(clean_checkpoint, local_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(checkpoints)) as executor:
        futures = [executor.submit(download_one, cp) for cp in checkpoints.values()]
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
            except Exception as e:
                tracker.set_failed(str(e))
                raise e

def initialize_all_model_metadata() -> None:
    """Fetches remote manifests and queries file sizes for all pending models in parallel at startup."""
    try:
        from surya.settings import settings as surya_settings
        from surya.common.s3 import S3DownloaderMixin
    except ImportError:
        return

    import concurrent.futures
    checkpoints = {
        "layout": surya_settings.LAYOUT_MODEL_CHECKPOINT,
        "text_recognition": surya_settings.RECOGNITION_MODEL_CHECKPOINT,
        "table_recognition": surya_settings.TABLE_REC_MODEL_CHECKPOINT,
        "text_detection": surya_settings.DETECTOR_MODEL_CHECKPOINT,
        "ocr_error_detection": surya_settings.OCR_ERROR_MODEL_CHECKPOINT,
    }

    base_url = surya_settings.S3_BASE_URL.rstrip('/')

    def process_model(model_key: str, checkpoint: str):
        if is_model_downloaded(model_key):
            return

        clean_checkpoint = checkpoint.replace("s3://", "")
        s3_url = base_url + '/' + clean_checkpoint
        manifest_url = s3_url + '/manifest.json'
        try:
            response = requests.get(manifest_url, timeout=5)
            response.raise_for_status()
            manifest = response.json()
            
            files = manifest.get("files", [])
            file_sizes = {}
            local_sizes = {}

            # Resolve local path to check existing file sizes
            local_dir = S3DownloaderMixin.get_local_path(checkpoint)
            local_dir_path = Path(local_dir) if local_dir else None
            
            def get_file_size(f):
                try:
                    f_url = s3_url + '/' + f
                    hr = requests.head(f_url, timeout=3, allow_redirects=True)
                    return f, int(hr.headers.get("content-length", 0))
                except Exception:
                    return f, 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(files)) as executor:
                results = executor.map(get_file_size, files)
                for f, size in results:
                    file_sizes[f] = size
                    if local_dir_path:
                        f_path = local_dir_path / f
                        if f_path.exists():
                            local_sizes[f] = f_path.stat().st_size
            
            tracker.initialize_model_files_with_sizes(model_key, file_sizes, local_sizes)
        except Exception as e:
            logger.warning(f"Failed to fetch metadata for {model_key} at startup: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(checkpoints)) as executor:
        # We wrap in list to trigger execution
        list(executor.map(lambda item: process_model(*item), checkpoints.items()))


def check_models_downloaded() -> bool:
    """Check if all 5 required models are already downloaded on disk."""
    try:
        from surya.common.s3 import S3DownloaderMixin, check_manifest
        from surya.settings import settings as surya_settings
    except ImportError:
        return False

    checkpoints = [
        surya_settings.LAYOUT_MODEL_CHECKPOINT,
        surya_settings.RECOGNITION_MODEL_CHECKPOINT,
        surya_settings.TABLE_REC_MODEL_CHECKPOINT,
        surya_settings.DETECTOR_MODEL_CHECKPOINT,
        surya_settings.OCR_ERROR_MODEL_CHECKPOINT,
    ]

    for cp in checkpoints:
        local_path = S3DownloaderMixin.get_local_path(cp)
        if not local_path or not check_manifest(local_path):
            return False
    return True

_retry_callback = None

def register_retry_callback(cb) -> None:
    global _retry_callback
    _retry_callback = cb

def trigger_retry() -> None:
    if _retry_callback:
        _retry_callback()


def self_heal_check() -> Dict[str, Any]:
    """Verify integrity of all model checkpoints, clean corrupt files, and re-download."""
    try:
        from surya.common.s3 import S3DownloaderMixin
        from surya.settings import settings as surya_settings
        import shutil
        import json
    except ImportError:
        return {"success": False, "message": "Surya package not installed"}

    checkpoints = {
        "layout": surya_settings.LAYOUT_MODEL_CHECKPOINT,
        "text_recognition": surya_settings.RECOGNITION_MODEL_CHECKPOINT,
        "table_recognition": surya_settings.TABLE_REC_MODEL_CHECKPOINT,
        "text_detection": surya_settings.DETECTOR_MODEL_CHECKPOINT,
        "ocr_error_detection": surya_settings.OCR_ERROR_MODEL_CHECKPOINT,
    }

    healed_count = 0
    issues = []

    # Clear in-memory model dictionary first if any models exist
    try:
        from app.main import _app_state
        with _app_state.marker_service._lock:
            _app_state.marker_service._model_dict = None
            _app_state.marker_service._initialized = False
    except Exception:
        pass

    for key, cp in checkpoints.items():
        local_path = S3DownloaderMixin.get_local_path(cp)
        if not local_path:
            issues.append(f"No path found for {key}")
            continue

        local_path_obj = Path(local_path)
        manifest_path = local_path_obj / "manifest.json"
        
        is_corrupt = False
        reason = ""
        
        if not local_path_obj.exists() or not manifest_path.exists():
            is_corrupt = True
            reason = "Missing manifest or directory"
        else:
            try:
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)
                for file in manifest.get("files", []):
                    f_path = local_path_obj / file
                    if not f_path.exists():
                        is_corrupt = True
                        reason = f"Missing file: {file}"
                        break
                    if f_path.stat().st_size == 0:
                        is_corrupt = True
                        reason = f"Empty file: {file}"
                        break
            except Exception as e:
                is_corrupt = True
                reason = f"Corrupt manifest: {str(e)}"

        if is_corrupt:
            issues.append(f"{key} is corrupt/missing: {reason}")
            if manifest_path.exists():
                try:
                    manifest_path.unlink()
                except Exception:
                    pass
            if local_path_obj.exists():
                try:
                    shutil.rmtree(local_path_obj, ignore_errors=True)
                except Exception:
                    pass
            healed_count += 1

    # Reset tracker status to trigger fresh download/load
    tracker.reset()

    # Restart background loading/downloading
    trigger_retry()

    return {
        "success": True,
        "healed_count": healed_count,
        "issues": issues,
        "message": f"Self-healing complete. Healed {healed_count} model(s)."
    }


async def reset_models_and_data(delete_user_data: bool = False, db_session: Optional[AsyncSession] = None) -> Dict[str, Any]:
    """Delete all downloaded models on disk, and optionally reset settings/history."""
    try:
        from surya.common.s3 import S3DownloaderMixin
        from surya.settings import settings as surya_settings
        import shutil
        import gc
        from sqlalchemy.ext.asyncio import AsyncSession
    except ImportError:
        return {"success": False, "message": "Surya package not installed"}

    # Stop background loading thread if alive to release files on Windows
    try:
        from app.main import _bg_load_thread, _bg_load_thread_lock
        with _bg_load_thread_lock:
            if _bg_load_thread and _bg_load_thread.is_alive():
                tracker.request_cancel()
                _bg_load_thread.join(timeout=5.0)
    except Exception as e:
        logger.warning(f"Failed to stop background load thread: {e}")

    # Cancel all running task manager jobs
    try:
        from app.main import _app_state
        job_ids = list(_app_state.task_manager._tasks.keys())
        for job_id in job_ids:
            await _app_state.task_manager.cancel_job(job_id)
    except Exception as e:
        logger.warning(f"Failed to cancel running jobs during reset: {e}")

    checkpoints = [
        surya_settings.LAYOUT_MODEL_CHECKPOINT,
        surya_settings.RECOGNITION_MODEL_CHECKPOINT,
        surya_settings.TABLE_REC_MODEL_CHECKPOINT,
        surya_settings.DETECTOR_MODEL_CHECKPOINT,
        surya_settings.OCR_ERROR_MODEL_CHECKPOINT,
    ]

    # Clear in-memory model cache
    try:
        from app.main import _app_state
        with _app_state.marker_service._lock:
            _app_state.marker_service._model_dict = None
            _app_state.marker_service._initialized = False
    except Exception:
        pass

    # Run garbage collection and free PyTorch cache to unlock files
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    deleted_paths = []
    # 1. Delete model directories
    for cp in checkpoints:
        local_path = S3DownloaderMixin.get_local_path(cp)
        if local_path:
            local_path_obj = Path(local_path)
            if local_path_obj.exists():
                # Unlink manifest first (less likely to be locked) to mark it as uninstalled/not present
                manifest_path = local_path_obj / "manifest.json"
                if manifest_path.exists():
                    try:
                        manifest_path.unlink()
                    except Exception as e:
                        logger.error(f"Failed to delete manifest for {cp}: {e}")
                
                try:
                    shutil.rmtree(local_path_obj, ignore_errors=True)
                    deleted_paths.append(str(local_path_obj))
                except Exception as e:
                    logger.warning(f"Failed to delete model path {local_path}: {e}")

    # Reset tracker status
    tracker.reset()

    db_reset = False
    if delete_user_data:
        try:
            from app.database import engine, Base
            from app.core.config import UPLOAD_DIR, OUTPUT_DIR
            from app.core.api_manager import load_secrets_from_db
            from app.routes.settings import init_llm_providers_if_missing
            from app.database import async_session_factory
            from sqlalchemy.ext.asyncio import async_sessionmaker
            
            # Clear uploaded & output files
            if UPLOAD_DIR.exists():
                for item in UPLOAD_DIR.iterdir():
                    try:
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                    except Exception as e:
                        logger.warning(f"Failed to delete upload item {item}: {e}")
            if OUTPUT_DIR.exists():
                for item in OUTPUT_DIR.iterdir():
                    try:
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                    except Exception as e:
                        logger.warning(f"Failed to delete output item {item}: {e}")

            # Re-create DB tables asynchronously
            bind_engine = db_session.bind if db_session is not None else engine
            async with bind_engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
                
            # Initialize default settings and providers
            if db_session is not None:
                local_session_factory = async_sessionmaker(bind_engine, class_=AsyncSession, expire_on_commit=False)
            else:
                local_session_factory = async_session_factory

            async with local_session_factory() as session:
                await init_llm_providers_if_missing(session)
            await load_secrets_from_db()
            
            db_reset = True
        except Exception as e:
            logger.error(f"Error resetting database: {e}", exc_info=True)
            db_reset = False

    # Restart background loading/downloading automatically
    trigger_retry()

    return {
        "success": True,
        "deleted_models": deleted_paths,
        "user_data_reset": db_reset,
        "message": "System reset completed successfully."
    }
