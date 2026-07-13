"""Secret encryption/decryption for API keys stored at rest.

Uses Fernet symmetric encryption. The key is derived from a machine-specific
salt stored in data/.secret_key. For a local-first app, this provides
at-rest encryption without requiring user-managed keys.
"""

import os
import base64

from app.crypto import decrypt_value, encrypt_value  # noqa: F401 - compatibility re-export

try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# Keys that contain sensitive API credentials
SENSITIVE_KEY_PATTERNS = ("api_key", "secret", "token", "password")


def _get_or_create_key() -> bytes:
    """Get or create the encryption key file."""
    from app.core.config import DATA_DIR
    key_path = DATA_DIR / ".secret_key"
    key_path.parent.mkdir(parents=True, exist_ok=True)

    if key_path.exists():
        return base64.urlsafe_b64decode(key_path.read_bytes())

    key = Fernet.generate_key()
    key_path.write_bytes(key)
    # Restrict permissions (POSIX only, Windows ignores)
    try:
        os.chmod(key_path, 0o600)
    except (OSError, AttributeError):
        pass
    return base64.urlsafe_b64decode(key)




def is_sensitive_key(key: str) -> bool:
    """Check if a setting key contains sensitive data."""
    key_lower = key.lower()
    return any(pattern in key_lower for pattern in SENSITIVE_KEY_PATTERNS)


def mask_value(value: str) -> str:
    """Mask a sensitive value for display. Shows first 4 and last 4 chars."""
    if not value or len(value) <= 8:
        return "****" if value else ""
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def is_masked(value: str) -> bool:
    """Check if a value has been masked (middle section is all asterisks)."""
    if not value or len(value) <= 8:
        return value == "****"
    # Check if the middle part (between first 4 and last 4 chars) is all *
    middle = value[4:-4]
    return all(c == "*" for c in middle) and len(middle) > 0
