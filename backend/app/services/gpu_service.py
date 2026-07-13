import sys
import subprocess
import threading
import re
import logging
import time
from typing import Any, Dict, List, Optional
from app.database import async_session_factory
from app.models.settings import Setting
from sqlalchemy import update

logger = logging.getLogger(__name__)

class GPUService:
    """Manages background installation and status of GPU acceleration (CUDA PyTorch)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._status = "not_installed"  # not_installed, installing, ready, failed
        self._progress = 0
        self._logs: List[str] = []
        self._error_message: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

        # Check if already installed on startup in a separate thread to not block the main thread
        threading.Thread(target=self.verify_installation_sync, daemon=True).start()

    @property
    def status_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "progress": self._progress,
                "logs": list(self._logs),
                "error_message": self._error_message,
                "cuda_available": self._check_cuda_available(),
            }

    def _check_cuda_available(self) -> bool:
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def verify_installation_sync(self) -> None:
        """Run a quick test to see if CUDA is currently available and set status accordingly."""
        if self._check_cuda_available():
            self._status = "ready"
            self._progress = 100
            self._error_message = None
        else:
            # If we're not currently installing, set to not_installed
            if self._status not in ("installing", "failed"):
                self._status = "not_installed"
                self._progress = 0

    def add_log(self, message: str) -> None:
        with self._lock:
            # Format message with timestamp
            timestamp = time.strftime("%H:%M:%S")
            self._logs.append(f"[{timestamp}] {message}")
            if len(self._logs) > 500:
                self._logs.pop(0)

    def start_install(self) -> bool:
        """Spawn the background installation thread if not already running."""
        with self._lock:
            if self._status == "installing":
                return False

            self._status = "installing"
            self._progress = 0
            self._logs = []
            self._error_message = None
            self.add_log("Starting CUDA-enabled PyTorch installation...")

            self._thread = threading.Thread(target=self._run_install, daemon=True)
            self._thread.start()
            return True

    def _run_install(self) -> None:
        try:
            # Determine correct Python executable
            python_exe = sys.executable
            self.add_log(f"Using Python environment: {python_exe}")

            # Reinstall command targeting CUDA 12.6.
            # cu126 is the only current index that carries torch>=2.7.0 (required
            # by marker-pdf 1.10.0) through the latest (2.13.0) for win_amd64.
            # cu121 tops out at torch 2.5.1 and breaks marker-pdf.
            cmd = [
                python_exe,
                "-m",
                "pip",
                "install",
                "torch",
                "torchvision",
                "--index-url",
                "https://download.pytorch.org/whl/cu126",
                "--force-reinstall",
                "--no-warn-script-location",
                "--no-cache-dir"
            ]

            self.add_log("Executing command: " + " ".join(cmd))
            self.add_log("Please wait, downloading PyTorch with CUDA support (~2.5 GB)...")

            # Start process
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            # Pattern to parse progress bar percentage
            progress_pat = re.compile(r"(\d+)%")

            if process.stdout:
                for line in process.stdout:
                    line_str = line.strip()
                    if not line_str:
                        continue

                    # Extract progress percentage
                    match = progress_pat.search(line_str)
                    if match:
                        pct = int(match.group(1))
                        with self._lock:
                            # Clamp progress during downloading (max 85% to save room for install phase)
                            self._progress = min(85, pct)

                    # Filter out progress bar raw spam from logs
                    is_spam = "|" in line_str and ("█" in line_str or "░" in line_str or "=" in line_str)
                    if not is_spam:
                        # Translate installing/collected info to cleaner status
                        if "Installing collected packages" in line_str:
                            self.add_log("Packages downloaded. Unpacking and installing into virtual environment...")
                            with self._lock:
                                self._progress = 90
                        elif "Successfully installed" in line_str:
                            self.add_log(line_str)
                        else:
                            self.add_log(line_str)

            # Wait for process to complete
            return_code = process.wait()

            if return_code == 0:
                self.add_log("Pip installation completed successfully.")
                self.add_log("Running quick verification test...")
                with self._lock:
                    self._progress = 95

                # Verify installation
                # We need to run this in a separate python process to ensure fresh module imports
                verify_cmd = [
                    python_exe,
                    "-c",
                    "import torch; print(torch.cuda.is_available())"
                ]
                verify_proc = subprocess.run(verify_cmd, capture_output=True, text=True)
                verify_out = verify_proc.stdout.strip()

                if verify_out == "True":
                    self.add_log("Verification SUCCESS: GPU acceleration (CUDA) is now active and ready!")
                    with self._lock:
                        self._status = "ready"
                        self._progress = 100
                else:
                    err_msg = verify_proc.stderr.strip() or "CUDA not available in verify script"
                    self.add_log(f"Verification FAILED: {err_msg}")
                    with self._lock:
                        self._status = "failed"
                        self._progress = 0
                        self._error_message = "CUDA verification failed. Check system GPU drivers."
                    self._disable_gpu_setting_in_db()
            else:
                self.add_log(f"Pip installation failed with exit code {return_code}.")
                with self._lock:
                    self._status = "failed"
                    self._progress = 0
                    self._error_message = f"Pip returned error code {return_code}."
                self._disable_gpu_setting_in_db()

        except Exception as e:
            logger.exception("Error running GPU installation")
            self.add_log(f"Unexpected error: {str(e)}")
            with self._lock:
                self._status = "failed"
                self._progress = 0
                self._error_message = str(e)
            self._disable_gpu_setting_in_db()

    def _disable_gpu_setting_in_db(self) -> None:
        """Set gpu_acceleration_enabled to 'false' in settings DB so it won't auto-retry on next run."""
        self.add_log("Disabling GPU acceleration setting in database to prevent future reinstall loops...")
        
        def _db_work() -> None:
            try:
                import asyncio
                async def _async_update() -> None:
                    async with async_session_factory() as session:
                        stmt = update(Setting).where(Setting.key == "gpu_acceleration_enabled").values(value="false")
                        await session.execute(stmt)
                        await session.commit()
                        logger.info("Successfully disabled gpu_acceleration_enabled in settings DB.")
                
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(_async_update(), loop)
                else:
                    loop.run_until_complete(_async_update())
            except Exception as e:
                logger.error("Failed to disable GPU setting in DB: %s", e)
                
        threading.Thread(target=_db_work, daemon=True).start()

gpu_service = GPUService()

