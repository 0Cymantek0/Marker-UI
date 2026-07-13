"""Tests for the MarkerError taxonomy (UCM-002)."""

from __future__ import annotations

import json
from pathlib import Path


from app import errors


def test_each_concrete_error_has_stable_code_and_exit_code():
    expected_codes = {
        "USAGE_ERROR",
        "INPUT_NOT_FOUND",
        "INPUT_NOT_ALLOWED",
        "UNSUPPORTED_FORMAT",
        "OUTPUT_EXISTS",
        "OUTPUT_WRITE_FAILED",
        "CONFIG_ERROR",
        "AUTH_REQUIRED",
        "AUTH_FORBIDDEN",
        "NETWORK_BLOCKED",
        "NETWORK_FETCH_FAILED",
        "URL_UNSAFE",
        "CONVERSION_FAILED",
        "MODEL_NOT_READY",
        "MODEL_DOWNLOAD_FAILED",
        "CLOUD_NOT_ALLOWED",
        "TIMEOUT",
        "CANCELLED",
        "PARTIAL_FAILURE",
        "NATIVE_DEPENDENCY_MISSING",
        "INTERNAL_ERROR",
    }
    assert set(errors.ERROR_CLASSES) == expected_codes
    for code, cls in errors.ERROR_CLASSES.items():
        assert cls.code == code
        assert cls().exit_code == errors.EXIT_CODE_BY_CODE[code]


def test_input_not_found_payload_shape_matches_contract():
    err = errors.InputNotFoundError(
        "Input file not found: /x/missing.pdf",
        hint="Check the path or pass --source-url for remote files.",
        details={"path": "/x/missing.pdf"},
    )
    payload = err.to_payload()
    assert payload == {
        "ok": False,
        "schema_version": "marker.error.v1",
        "error": {
            "code": "INPUT_NOT_FOUND",
            "message": "Input file not found: /x/missing.pdf",
            "hint": "Check the path or pass --source-url for remote files.",
            "details": {"path": "/x/missing.pdf"},
            "retryable": False,
        },
    }
    assert err.exit_code == 3


def test_from_exception_preserves_existing_marker_error():
    original = errors.UnsupportedFormatError("bad suffix", details={"suffix": ".xyz"})
    mapped = errors.from_exception(original)
    assert mapped is original
    assert mapped.code == "UNSUPPORTED_FORMAT"


def test_from_exception_maps_stdlib_shapes():
    assert errors.from_exception(FileNotFoundError("gone")).code == "INPUT_NOT_FOUND"
    assert errors.from_exception(FileExistsError("there")).code == "OUTPUT_EXISTS"
    assert errors.from_exception(PermissionError("nope")).code == "INPUT_NOT_ALLOWED"
    assert errors.from_exception(ValueError("bad arg")).code == "USAGE_ERROR"
    assert errors.from_exception(TimeoutError()).code == "TIMEOUT"
    assert errors.from_exception(RuntimeError("boom")).code == "INTERNAL_ERROR"


def test_to_payload_drops_non_json_safe_details():
    err = errors.InternalError(
        "boom",
        details={"path": Path("/tmp/x"), "ok": [1, 2]},
    )
    payload = err.to_payload()
    # Path coerced to str; list preserved
    assert payload["error"]["details"]["path"] == str(Path("/tmp/x"))
    assert payload["error"]["details"]["ok"] == [1, 2]
    # payload must round-trip through json
    json.dumps(payload)


def test_retryable_override():
    err = errors.InternalError("boom", retryable=True)
    assert err.retryable is True
    assert errors.InternalError("boom").retryable is False


def test_output_exists_exit_code():
    assert errors.OutputExistsError("exists").exit_code == 11


def test_url_unsafe_exit_code():
    assert errors.UrlUnsafeError("private ip").exit_code == 7
