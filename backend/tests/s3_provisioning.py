"""Shared S3-compatible store provisioning for payload-store conformance.

Contract mirrors ``pg_provisioning``: environment variables point at a
real object-store server, each test gets an isolated throwaway bucket
that is emptied and removed on close, and ``MARKER_TEST_S3_STRICT``
turns a missing server into a failure so an invoked industrial target
can never pass silently through skips.

Environment:

* ``MARKER_TEST_S3_ENDPOINT`` — base URL, e.g. ``http://127.0.0.1:9000``
* ``MARKER_TEST_S3_ACCESS_KEY`` / ``MARKER_TEST_S3_SECRET_KEY`` —
  credentials (injected, never committed)
* ``MARKER_TEST_S3_STRICT`` — ``1`` refuses to skip when unset
"""

from __future__ import annotations

import os
import uuid

import pytest

ENDPOINT_ENV = "MARKER_TEST_S3_ENDPOINT"
ACCESS_KEY_ENV = "MARKER_TEST_S3_ACCESS_KEY"
SECRET_KEY_ENV = "MARKER_TEST_S3_SECRET_KEY"
STRICT_ENV = "MARKER_TEST_S3_STRICT"

#: Store name registered in the shared conformance suite.
S3_STORE_NAME = "s3_minio"

_MISSING = (
    "S3 payload-store conformance needs {endpoint} (object-store base "
    "URL) plus {access} and {secret} credentials; point them at a real "
    "S3-compatible server (MinIO, etc.) — run "
    "backend/scripts/run_kernel_pg_conformance.py or docker to "
    "provision one locally"
)


def strict_mode() -> bool:
    return os.getenv(STRICT_ENV, "").lower() in ("1", "true", "yes")


def require_s3_env() -> tuple[str, str, str]:
    """(endpoint, access_key, secret_key), or skip/fail loudly.

    Strict mode refuses to skip: an invoked industrial target must fail
    when the server is missing rather than report green through skips.
    """
    endpoint = os.getenv(ENDPOINT_ENV, "").strip()
    access_key = os.getenv(ACCESS_KEY_ENV, "").strip()
    secret_key = os.getenv(SECRET_KEY_ENV, "").strip()
    if not (endpoint and access_key and secret_key):
        message = _MISSING.format(
            endpoint=ENDPOINT_ENV, access=ACCESS_KEY_ENV, secret=SECRET_KEY_ENV
        )
        if strict_mode():
            pytest.fail(f"strict mode refuses to skip: {message}")
        pytest.skip(message)
    return endpoint, access_key, secret_key


def unique_bucket() -> str:
    """One throwaway namespace name (3–63 chars, S3-legal)."""
    return f"marker-kps-{uuid.uuid4().hex[:16]}"


def maybe_s3_store_factory():
    """Factory for the shared conformance registry, or ``None``.

    Registers only when the environment provides a real server; strict
    mode fails at collection instead of silently dropping the store
    from the suite.
    """
    endpoint = os.getenv(ENDPOINT_ENV, "").strip()
    access_key = os.getenv(ACCESS_KEY_ENV, "").strip()
    secret_key = os.getenv(SECRET_KEY_ENV, "").strip()
    if not (endpoint and access_key and secret_key):
        if strict_mode():
            pytest.fail(
                "strict mode refuses to skip: " + _MISSING.format(
                    endpoint=ENDPOINT_ENV, access=ACCESS_KEY_ENV, secret=SECRET_KEY_ENV
                )
            )
        return None

    def _factory(root) -> "object":
        from app.kernel.object_store import S3PayloadStore, S3StoreConfig

        return S3PayloadStore(
            S3StoreConfig(
                endpoint_url=endpoint,
                bucket=unique_bucket(),
                access_key_id=access_key,
                secret_access_key=secret_key,
                delete_namespace_on_close=True,
            )
        )

    return _factory
