from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
START_PS1 = REPO_ROOT / "start.ps1"


def _launcher_script() -> str:
    return START_PS1.read_text(encoding="utf-8")


def _launcher_timeout_default(script: str, env_name: str) -> tuple[int, int]:
    match = re.search(
        rf'Get-LauncherIntEnv\s+-Name "{re.escape(env_name)}"\s+-Default (?P<default>\d+)\s+-Minimum (?P<minimum>\d+)',
        script,
    )
    assert match, f"Missing {env_name} launcher timeout setting"
    return int(match.group("default")), int(match.group("minimum"))


def test_windows_backend_readiness_uses_long_bounded_timeout() -> None:
    script = _launcher_script()

    soft_default, soft_minimum = _launcher_timeout_default(
        script, "MARKER_BACKEND_READY_TIMEOUT_SECONDS"
    )
    hard_default, hard_minimum = _launcher_timeout_default(
        script, "MARKER_BACKEND_READY_HARD_TIMEOUT_SECONDS"
    )

    assert soft_minimum == 1
    assert hard_minimum == 0
    assert soft_default >= 120
    assert hard_default >= 300
    assert hard_default > soft_default
    assert "within 30 seconds" not in script
    assert "Backend failed to start" not in script


def test_windows_backend_soft_timeout_warns_without_failing() -> None:
    script = _launcher_script()

    soft_block_start = script.index("if (-not $warnedAfterSoftTimeout")
    hard_block_start = script.index("if ($HardTimeoutSeconds -gt 0")
    soft_block = script[soft_block_start:hard_block_start]

    assert "WARNING: $Name is still starting after $SoftTimeoutSeconds seconds." in soft_block
    assert "Continuing to wait because the process is still running." in soft_block
    assert "return $false" not in soft_block
    assert "ERROR: $Name did not become ready within hard timeout $HardTimeoutSeconds seconds." in script
