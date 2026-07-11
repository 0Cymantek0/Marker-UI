"""Marker error taxonomy.

One source of truth for typed errors raised by the agent seam (CLI/MCP/REST).
Each concrete error carries a stable string code, a human-readable message,
optional hint, optional structured details, a retryable flag, and a CLI exit
code. The CLI serializes ``--json`` errors to stderr using
``MarkerError.to_payload``; MCP maps known errors to structured tool errors.

Design notes:

* Codes are SCREAMING_SNAKE_CASE strings kept stable across releases (section
  9.2 of the enterprise plan). They are the contract for scripts and agents.
* Exit codes (section 9.3) are mapped through ``EXIT_CODE_BY_CODE``.
* Safe serialization: ``to_payload`` never embeds arbitrary object reprs; only
  whitelisted ``details`` keys (already primitives/lists/dicts) flow out.
* Secret values must never be placed in ``message`` or ``details``; callers are
  responsible for redaction before construction.
"""

from __future__ import annotations

from typing import Any


# --- Exit codes (section 9.3) -------------------------------------------------
EXIT_SUCCESS = 0
EXIT_INTERNAL_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_INPUT_NOT_FOUND = 3
EXIT_UNSUPPORTED_FORMAT = 4
EXIT_CONFIG_ERROR = 5
EXIT_AUTH = 6
EXIT_NETWORK = 7
EXIT_CONVERSION_FAILED = 8
EXIT_TIMEOUT_OR_CANCELLED = 9
EXIT_PARTIAL_FAILURE = 10
EXIT_OUTPUT_EXISTS = 11


# --- Error codes (section 9.2) ------------------------------------------------
CODE_USAGE_ERROR = "USAGE_ERROR"
CODE_INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
CODE_INPUT_NOT_ALLOWED = "INPUT_NOT_ALLOWED"
CODE_UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
CODE_OUTPUT_EXISTS = "OUTPUT_EXISTS"
CODE_OUTPUT_WRITE_FAILED = "OUTPUT_WRITE_FAILED"
CODE_CONFIG_ERROR = "CONFIG_ERROR"
CODE_AUTH_REQUIRED = "AUTH_REQUIRED"
CODE_AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
CODE_NETWORK_BLOCKED = "NETWORK_BLOCKED"
CODE_NETWORK_FETCH_FAILED = "NETWORK_FETCH_FAILED"
CODE_URL_UNSAFE = "URL_UNSAFE"
CODE_CONVERSION_FAILED = "CONVERSION_FAILED"
CODE_MODEL_NOT_READY = "MODEL_NOT_READY"
CODE_MODEL_DOWNLOAD_FAILED = "MODEL_DOWNLOAD_FAILED"
CODE_CLOUD_NOT_ALLOWED = "CLOUD_NOT_ALLOWED"
CODE_TIMEOUT = "TIMEOUT"
CODE_CANCELLED = "CANCELLED"
CODE_PARTIAL_FAILURE = "PARTIAL_FAILURE"
CODE_NATIVE_DEPENDENCY_MISSING = "NATIVE_DEPENDENCY_MISSING"
CODE_INTERNAL_ERROR = "INTERNAL_ERROR"


# Code -> CLI exit code mapping. Keep exhaustive and explicit.
EXIT_CODE_BY_CODE: dict[str, int] = {
    CODE_USAGE_ERROR: EXIT_USAGE_ERROR,
    CODE_INPUT_NOT_FOUND: EXIT_INPUT_NOT_FOUND,
    CODE_INPUT_NOT_ALLOWED: EXIT_UNSUPPORTED_FORMAT,
    CODE_UNSUPPORTED_FORMAT: EXIT_UNSUPPORTED_FORMAT,
    CODE_OUTPUT_EXISTS: EXIT_OUTPUT_EXISTS,
    CODE_OUTPUT_WRITE_FAILED: EXIT_OUTPUT_EXISTS,
    CODE_CONFIG_ERROR: EXIT_CONFIG_ERROR,
    CODE_AUTH_REQUIRED: EXIT_AUTH,
    CODE_AUTH_FORBIDDEN: EXIT_AUTH,
    CODE_NETWORK_BLOCKED: EXIT_NETWORK,
    CODE_NETWORK_FETCH_FAILED: EXIT_NETWORK,
    CODE_URL_UNSAFE: EXIT_NETWORK,
    CODE_CONVERSION_FAILED: EXIT_CONVERSION_FAILED,
    CODE_MODEL_NOT_READY: EXIT_CONVERSION_FAILED,
    CODE_MODEL_DOWNLOAD_FAILED: EXIT_CONVERSION_FAILED,
    CODE_CLOUD_NOT_ALLOWED: EXIT_AUTH,
    CODE_TIMEOUT: EXIT_TIMEOUT_OR_CANCELLED,
    CODE_CANCELLED: EXIT_TIMEOUT_OR_CANCELLED,
    CODE_PARTIAL_FAILURE: EXIT_PARTIAL_FAILURE,
    CODE_NATIVE_DEPENDENCY_MISSING: EXIT_CONFIG_ERROR,
    CODE_INTERNAL_ERROR: EXIT_INTERNAL_ERROR,
}


ERROR_SCHEMA_VERSION = "marker.error.v1"


class MarkerError(Exception):
    """Base typed Marker error.

    Attributes:
        code: stable string code (see ``CODE_*`` constants).
        message: human-readable summary, safe to surface.
        hint: optional suggested remediation.
        details: optional structured primitives (never secrets).
        retryable: whether a retry of the same operation could plausibly help.
    """

    code: str = CODE_INTERNAL_ERROR
    default_exit_code: int = EXIT_INTERNAL_ERROR
    default_retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = dict(details or {})
        self.retryable = self.default_retryable if retryable is None else bool(retryable)

    @property
    def exit_code(self) -> int:
        return EXIT_CODE_BY_CODE.get(self.code, self.default_exit_code)

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the ``marker.error.v1`` JSON object (section 9.1)."""

        return {
            "ok": False,
            "schema_version": ERROR_SCHEMA_VERSION,
            "error": {
                "code": self.code,
                "message": self.message,
                "hint": self.hint,
                "details": _safe_details(self.details),
                "retryable": self.retryable,
            },
        }


def _safe_details(details: dict[str, Any]) -> dict[str, Any]:
    """Best-effort JSON-safe copy of details.

    Drops values that cannot round-trip through ``json.dumps`` (e.g. Path,
    Exception). Never mutates caller's dict.
    """

    import json

    safe: dict[str, Any] = {}
    for key, value in details.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            safe[str(key)] = str(value)
        else:
            safe[str(key)] = value
    return safe


# --- Concrete error classes ---------------------------------------------------

class UsageError(MarkerError):
    code = CODE_USAGE_ERROR
    default_exit_code = EXIT_USAGE_ERROR
    default_retryable = False


class InputNotFoundError(MarkerError):
    code = CODE_INPUT_NOT_FOUND
    default_exit_code = EXIT_INPUT_NOT_FOUND
    default_retryable = False


class InputNotAllowedError(MarkerError):
    code = CODE_INPUT_NOT_ALLOWED
    default_exit_code = EXIT_UNSUPPORTED_FORMAT
    default_retryable = False


class UnsupportedFormatError(MarkerError):
    code = CODE_UNSUPPORTED_FORMAT
    default_exit_code = EXIT_UNSUPPORTED_FORMAT
    default_retryable = False


class OutputExistsError(MarkerError):
    code = CODE_OUTPUT_EXISTS
    default_exit_code = EXIT_OUTPUT_EXISTS
    default_retryable = False


class OutputWriteFailedError(MarkerError):
    code = CODE_OUTPUT_WRITE_FAILED
    default_exit_code = EXIT_OUTPUT_EXISTS
    default_retryable = False


class ConfigError(MarkerError):
    code = CODE_CONFIG_ERROR
    default_exit_code = EXIT_CONFIG_ERROR
    default_retryable = False


class AuthRequiredError(MarkerError):
    code = CODE_AUTH_REQUIRED
    default_exit_code = EXIT_AUTH
    default_retryable = False


class AuthForbiddenError(MarkerError):
    code = CODE_AUTH_FORBIDDEN
    default_exit_code = EXIT_AUTH
    default_retryable = False


class NetworkBlockedError(MarkerError):
    code = CODE_NETWORK_BLOCKED
    default_exit_code = EXIT_NETWORK
    default_retryable = False


class NetworkFetchFailedError(MarkerError):
    code = CODE_NETWORK_FETCH_FAILED
    default_exit_code = EXIT_NETWORK
    default_retryable = True


class UrlUnsafeError(MarkerError):
    code = CODE_URL_UNSAFE
    default_exit_code = EXIT_NETWORK
    default_retryable = False


class ConversionFailedError(MarkerError):
    code = CODE_CONVERSION_FAILED
    default_exit_code = EXIT_CONVERSION_FAILED
    default_retryable = False


class ModelNotReadyError(MarkerError):
    code = CODE_MODEL_NOT_READY
    default_exit_code = EXIT_CONVERSION_FAILED
    default_retryable = True


class ModelDownloadFailedError(MarkerError):
    code = CODE_MODEL_DOWNLOAD_FAILED
    default_exit_code = EXIT_CONVERSION_FAILED
    default_retryable = True


class CloudNotAllowedError(MarkerError):
    code = CODE_CLOUD_NOT_ALLOWED
    default_exit_code = EXIT_AUTH
    default_retryable = False


class OperationTimeoutError(MarkerError):
    code = CODE_TIMEOUT
    default_exit_code = EXIT_TIMEOUT_OR_CANCELLED
    default_retryable = True


class OperationCancelledError(MarkerError):
    code = CODE_CANCELLED
    default_exit_code = EXIT_TIMEOUT_OR_CANCELLED
    default_retryable = False


class PartialFailureError(MarkerError):
    code = CODE_PARTIAL_FAILURE
    default_exit_code = EXIT_PARTIAL_FAILURE
    default_retryable = False


class NativeDependencyMissingError(MarkerError):
    code = CODE_NATIVE_DEPENDENCY_MISSING
    default_exit_code = EXIT_CONFIG_ERROR
    default_retryable = False


class InternalError(MarkerError):
    code = CODE_INTERNAL_ERROR
    default_exit_code = EXIT_INTERNAL_ERROR
    default_retryable = False


# Registry of concrete classes for introspection/tests.
ERROR_CLASSES: dict[str, type[MarkerError]] = {
    cls.code: cls
    for cls in (
        UsageError,
        InputNotFoundError,
        InputNotAllowedError,
        UnsupportedFormatError,
        OutputExistsError,
        OutputWriteFailedError,
        ConfigError,
        AuthRequiredError,
        AuthForbiddenError,
        NetworkBlockedError,
        NetworkFetchFailedError,
        UrlUnsafeError,
        ConversionFailedError,
        ModelNotReadyError,
        ModelDownloadFailedError,
        CloudNotAllowedError,
        OperationTimeoutError,
        OperationCancelledError,
        PartialFailureError,
        NativeDependencyMissingError,
        InternalError,
    )
}


def from_exception(exc: BaseException) -> MarkerError:
    """Map arbitrary exception to a typed MarkerError.

    Preserves an existing ``MarkerError`` unchanged. Translates a few well-known
    stdlib exception shapes to specific codes so the agent seam can keep raising
    native exceptions (FileNotFoundError, ValueError, etc.) while still getting
    stable codes at the boundary.
    """

    if isinstance(exc, MarkerError):
        return exc
    if isinstance(exc, FileNotFoundError):
        return InputNotFoundError(str(exc) or "Input not found", hint="Check the path or pass --source-url for remote files.")
    if isinstance(exc, PermissionError):
        return InputNotAllowedError(str(exc) or "Permission denied", hint="Check file permissions and workspace roots.")
    if isinstance(exc, FileExistsError):
        return OutputExistsError(str(exc) or "Output file already exists", hint="Pass --overwrite to replace it, or choose a different output path.")
    if isinstance(exc, TimeoutError):
        return OperationTimeoutError(str(exc) or "Operation timed out", retryable=True)
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return UsageError(str(exc) or "Invalid argument")
    # Defensive: un-migrated converters may still raise bare RuntimeError for
    # missing native binaries (e.g. ffmpeg). Sniff the message so callers still
    # get a typed, actionable error instead of generic INTERNAL_ERROR.
    if isinstance(exc, RuntimeError) and "ffmpeg" in str(exc).lower():
        return NativeDependencyMissingError(
            str(exc),
            hint="Install ffmpeg and ffprobe on the host or in the container.",
            details={"original_type": type(exc).__name__},
        )
    return InternalError(str(exc) or "Internal error", retryable=False)
