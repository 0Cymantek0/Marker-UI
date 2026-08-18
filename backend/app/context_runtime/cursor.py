"""Opaque, tamper-evident continuation cursor tokens (PR79A).

Cursor tokens are deliberately only a signed reference to server-side state.
The token carries a protocol version, signing-key id, random state handle, and
random replay nonce.  Query text, publication identity, authorization state,
budgets, and pin metadata stay in the durable cursor row and never enter the
token.

The keyring is injected by the caller.  One current key signs new tokens and
the verification set may retain older keys during rotation.  No secret is
loaded from source code or generated implicitly by this module.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from app.utils.canonical import canonical_json_bytes

__all__ = [
    "CURSOR_HANDLE_BYTES",
    "CURSOR_NONCE_BYTES",
    "CURSOR_SIGNATURE_BYTES",
    "CURSOR_TOKEN_MAX_BYTES",
    "CURSOR_TOKEN_VERSION",
    "CursorCodec",
    "CursorCodecError",
    "CursorEnvelope",
    "CursorExpiredError",
    "CursorIntegrityError",
    "CursorKeyError",
    "CursorKeyring",
    "CursorMalformedError",
    "CursorVersionError",
    "ExpiredCursorError",
    "MalformedCursorError",
    "UnsupportedCursorVersionError",
    "new_cursor_handle",
    "new_cursor_nonce",
    "validate_cursor_expiry",
]

# Protocol version is an integer inside the signed envelope.  Changing any
# field meaning requires a new version and an explicit decoder branch.
CURSOR_TOKEN_VERSION = 1
CURSOR_HANDLE_BYTES = 32
CURSOR_NONCE_BYTES = 32
CURSOR_SIGNATURE_BYTES = 32  # HMAC-SHA256 output
CURSOR_TOKEN_MAX_BYTES = 1024
CURSOR_KEY_MIN_BYTES = 32

_KEY_ID_MAX_LENGTH = 64
_OPAQUE_PART_MAX_LENGTH = 128
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_OPAQUE_PART_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class CursorCodecError(ValueError):
    """Base error for fail-closed cursor token validation."""


class CursorMalformedError(CursorCodecError):
    """Token is not a well-formed cursor envelope."""


class CursorVersionError(CursorCodecError):
    """Token names a cursor protocol version this decoder does not support."""


class CursorKeyError(CursorCodecError):
    """Token names a key that is not in the injected verification keyring."""


class CursorIntegrityError(CursorCodecError):
    """Token HMAC does not match the selected verification key."""


class CursorExpiredError(CursorCodecError):
    """Durable cursor state is no longer within its expiry lease."""


# Friendly names for callers that use adjective-first exception names.
MalformedCursorError = CursorMalformedError
UnsupportedCursorVersionError = CursorVersionError
ExpiredCursorError = CursorExpiredError


def _secret_bytes(value: bytes | bytearray | memoryview | str, *, label: str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    elif isinstance(value, (bytearray, memoryview)):
        value = bytes(value)
    if not isinstance(value, bytes) or len(value) < CURSOR_KEY_MIN_BYTES:
        raise ValueError(
            f"{label} must contain at least {CURSOR_KEY_MIN_BYTES} bytes"
        )
    return value


def _validate_key_id(value: str) -> str:
    if not isinstance(value, str) or not _KEY_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "cursor key id must match "
            f"{_KEY_ID_PATTERN.pattern}; got {value!r}"
        )
    return value


def _validate_opaque_part(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_PART_PATTERN.fullmatch(value):
        raise ValueError(
            f"cursor {label} must be URL-safe opaque text of at most "
            f"{_OPAQUE_PART_MAX_LENGTH} characters"
        )
    return value


def new_cursor_handle() -> str:
    """Return fresh random state identity for one durable cursor row."""

    return secrets.token_urlsafe(CURSOR_HANDLE_BYTES)


def new_cursor_nonce() -> str:
    """Return fresh random replay nonce for one cursor issuance."""

    return secrets.token_urlsafe(CURSOR_NONCE_BYTES)


class CursorKeyring:
    """Injected signing and verification material for :class:`CursorCodec`.

    ``current_key_id``/``current_key`` sign new tokens.  ``verification_keys``
    contains old keys retained through a rotation window.  The current key is
    always included in the verification set.  Constructor accepts either the
    explicit current-key form or a complete ``keys`` mapping for convenient
    dependency injection in tests and application wiring.
    """

    current_key_id: str
    current_key: bytes
    verification_keys: Mapping[str, bytes]

    def __init__(
        self,
        keys: Mapping[str, bytes | bytearray | memoryview | str] | None = None,
        *,
        current_key_id: str | None = None,
        current_key: bytes | bytearray | memoryview | str | None = None,
        verification_keys: Mapping[
            str, bytes | bytearray | memoryview | str
        ] | None = None,
    ) -> None:
        if keys is not None and (current_key is not None or verification_keys is not None):
            raise ValueError("pass either keys or current/verification key arguments")

        if keys is not None:
            normalized = {
                _validate_key_id(key_id): _secret_bytes(value, label="cursor key")
                for key_id, value in keys.items()
            }
            if current_key_id is None:
                raise ValueError("current_key_id is required when keys is supplied")
            if current_key_id not in normalized:
                raise ValueError("current_key_id must name one supplied key")
            selected_id = _validate_key_id(current_key_id)
            selected_key = normalized[selected_id]
        else:
            if current_key_id is None or current_key is None:
                raise ValueError("current_key_id and current_key are required")
            selected_id = _validate_key_id(current_key_id)
            selected_key = _secret_bytes(current_key, label="current cursor key")
            normalized = {}
            for key_id, value in (verification_keys or {}).items():
                normalized[_validate_key_id(key_id)] = _secret_bytes(
                    value, label="cursor verification key"
                )
            if selected_id in normalized and not hmac.compare_digest(
                normalized[selected_id], selected_key
            ):
                raise ValueError("current key conflicts with verification key")

        normalized[selected_id] = selected_key
        self.current_key_id = selected_id
        self.current_key = selected_key
        self.verification_keys = MappingProxyType(dict(normalized))

    @property
    def keys(self) -> Mapping[str, bytes]:
        """Complete immutable verification set, including current key."""

        return self.verification_keys

    def verification_key(self, key_id: str) -> bytes:
        """Resolve one key id or fail closed for a retired/unknown key."""

        try:
            return self.verification_keys[key_id]
        except KeyError as exc:
            raise CursorKeyError(f"unknown cursor signing key id {key_id!r}") from exc


@dataclass(frozen=True)
class CursorEnvelope:
    """Validated token claims; all values are opaque references."""

    version: int
    key_id: str
    handle: str
    nonce: str

    @property
    def kid(self) -> str:
        """Short alias used by token-oriented integrations."""

        return self.key_id

    @property
    def cursor_handle(self) -> str:
        """Explicit state-handle alias."""

        return self.handle


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise CursorMalformedError("cursor expiry must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_cursor_expiry(
    expires_at: datetime,
    *,
    now: datetime | None = None,
) -> None:
    """Reject an expired durable cursor lease.

    Expiry is intentionally checked against server-side row state, not put in
    the token.  Naive database timestamps are interpreted as UTC, matching
    SQLite's existing timestamp behavior.
    """

    current = _utc(now) if now is not None else datetime.now(timezone.utc)
    if _utc(expires_at) <= current:
        raise CursorExpiredError("cursor state has expired")


class CursorCodec:
    """Issue and verify opaque HMAC-SHA256 cursor tokens."""

    def __init__(
        self,
        keyring: CursorKeyring | None = None,
        *,
        current_key_id: str | None = None,
        current_key: bytes | bytearray | memoryview | str | None = None,
        verification_keys: Mapping[
            str, bytes | bytearray | memoryview | str
        ] | None = None,
    ) -> None:
        if keyring is not None and any(
            value is not None
            for value in (current_key_id, current_key, verification_keys)
        ):
            raise ValueError("pass either keyring or key material arguments")
        self.keyring = keyring or CursorKeyring(
            current_key_id=current_key_id,
            current_key=current_key,
            verification_keys=verification_keys,
        )

    @staticmethod
    def _payload(*, key_id: str, handle: str, nonce: str) -> bytes:
        return canonical_json_bytes(
            {
                "handle": _validate_opaque_part(handle, label="handle"),
                "key_id": _validate_key_id(key_id),
                "nonce": _validate_opaque_part(nonce, label="nonce"),
                "version": CURSOR_TOKEN_VERSION,
            }
        )

    @staticmethod
    def _wire(payload: bytes, signature: bytes) -> str:
        raw = payload + signature
        if len(raw) > CURSOR_TOKEN_MAX_BYTES:
            raise CursorMalformedError("cursor token exceeds maximum size")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def encode(self, handle: str, nonce: str | None = None) -> str:
        """Sign one handle/nonce pair with current key."""

        nonce_value = nonce if nonce is not None else new_cursor_nonce()
        payload = self._payload(
            key_id=self.keyring.current_key_id,
            handle=handle,
            nonce=nonce_value,
        )
        signature = hmac.new(
            self.keyring.current_key,
            payload,
            digestmod="sha256",
        ).digest()
        return self._wire(payload, signature)

    issue = encode

    @staticmethod
    def _decode_base64(token: str) -> bytes:
        if not isinstance(token, str) or not token or len(token) > CURSOR_TOKEN_MAX_BYTES * 2:
            raise CursorMalformedError("cursor token must be bounded non-empty text")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise CursorMalformedError("cursor token is not URL-safe base64")
        padded = token + "=" * (-len(token) % 4)
        try:
            raw = base64.b64decode(
                padded.encode("ascii"), altchars=b"-_", validate=True
            )
        except (binascii.Error, ValueError, UnicodeError) as exc:
            raise CursorMalformedError("cursor token is not valid base64") from exc
        if len(raw) > CURSOR_TOKEN_MAX_BYTES:
            raise CursorMalformedError("cursor token exceeds maximum size")
        if len(raw) <= CURSOR_SIGNATURE_BYTES:
            raise CursorMalformedError("cursor token is shorter than its signature")
        return raw

    @staticmethod
    def _claims(payload: bytes) -> CursorEnvelope:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CursorMalformedError("cursor payload is not valid JSON") from exc
        if not isinstance(decoded, dict) or set(decoded) != {
            "handle",
            "key_id",
            "nonce",
            "version",
        }:
            raise CursorMalformedError("cursor payload has unexpected fields")
        try:
            canonical = canonical_json_bytes(decoded)
        except Exception as exc:  # noqa: BLE001 - classify all malformed input
            raise CursorMalformedError("cursor payload is not canonical JSON") from exc
        if canonical != payload:
            raise CursorMalformedError("cursor payload is not canonical JSON")
        version = decoded["version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise CursorMalformedError("cursor version must be an integer")
        if version != CURSOR_TOKEN_VERSION:
            raise CursorVersionError(f"unsupported cursor token version {version!r}")
        try:
            key_id = _validate_key_id(decoded["key_id"])
            handle = _validate_opaque_part(decoded["handle"], label="handle")
            nonce = _validate_opaque_part(decoded["nonce"], label="nonce")
        except ValueError as exc:
            raise CursorMalformedError(str(exc)) from exc
        return CursorEnvelope(
            version=version,
            key_id=key_id,
            handle=handle,
            nonce=nonce,
        )

    def decode(self, token: str) -> CursorEnvelope:
        """Verify token shape, protocol version, key, and signature.

        Expiry is deliberately not a token concern: cursors expire through
        their durable server-side row state (``validate_cursor_expiry``).
        """

        raw = self._decode_base64(token)
        payload, signature = raw[:-CURSOR_SIGNATURE_BYTES], raw[-CURSOR_SIGNATURE_BYTES:]
        claims = self._claims(payload)
        key = self.keyring.verification_key(claims.key_id)
        expected = hmac.new(key, payload, digestmod="sha256").digest()
        if not hmac.compare_digest(signature, expected):
            raise CursorIntegrityError("cursor token signature mismatch")
        return claims

    verify = decode
    parse = decode
