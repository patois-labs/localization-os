"""Encrypted-at-rest file storage for uploads (Fernet / AES-128-CBC + HMAC).

Reads auto-detect the encryption marker, so toggling the setting never corrupts
or orphans existing files. The key comes from config (env var or protected
keyfile in the data dir).

Documented in: docs/architecture/secrets-and-crypto.md
"""

from cryptography.fernet import Fernet

from app import config
from app.services import settings

_MAGIC = b"LSENC1:"


def _fernet() -> Fernet:
    return Fernet(config.ENCRYPTION_KEY.encode())


def enabled() -> bool:
    return settings.get("encrypt_uploads") == "1"


def encode_at_rest(data: bytes) -> bytes:
    """The at-rest envelope, path-free: LSENC1-wrapped ciphertext when encrypt_uploads=1,
    raw bytes otherwise. The single source of the write-path transform, shared byte-for-byte
    by the disk adapter (via write_upload) and the S3 adapter (SPEC-s3-blobstore §4.1)."""
    if enabled():
        return _MAGIC + _fernet().encrypt(data)
    return data


def decode_at_rest(data: bytes) -> bytes:
    """The read-path auto-detect, path-free: decrypt LSENC1-prefixed bytes, pass others
    through. Never orphans a toggled file (the auto-detect invariant). Shared byte-for-byte
    by the disk adapter (via read_upload) and the S3 adapter (SPEC-s3-blobstore §4.1)."""
    if data.startswith(_MAGIC):
        return _fernet().decrypt(data[len(_MAGIC) :])
    return data


def write_upload(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_at_rest(data))  # delegates -- byte-identical to before


def read_upload(path) -> bytes:
    return decode_at_rest(path.read_bytes())  # delegates -- byte-identical to before
