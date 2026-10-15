"""Recovery support for the retired legacy Fernet database vault.

New vault writes are refused: copying the database file misses committed WAL
pages, and shutdown cannot safely remove plaintext while writers hold handles.
Use SQLCipher for database encryption. Existing vaults remain recoverable.

Documented in: docs/architecture/secrets-and-crypto.md
"""

from pathlib import Path

from cryptography.fernet import Fernet

from app import config

RETIRED_MESSAGE = (
    "Legacy encrypt_db shutdown encryption is retired because it cannot safely "
    "preserve WAL data and remove plaintext. Use SQLCipher database encryption "
    "(docs/SECRETS.md). Disable encrypt_db with local config set encrypt_db 0 "
    "--yes before continuing with a plaintext database. Existing vaults "
    "remain recoverable; no new vault was written and no plaintext was removed."
)


def _fernet() -> Fernet:
    return Fernet(config.ENCRYPTION_KEY.encode())


def _vault(db_path) -> Path:
    return Path(str(db_path) + ".vault")


def lock(db_path) -> bool:
    """Refuse legacy snapshots, including direct callers outside app shutdown."""
    raise RuntimeError(RETIRED_MESSAGE)


def unlock(db_path) -> bool:
    """Decrypt <db>.vault back to the DB file. Returns True if restored."""
    db_path = Path(db_path)
    vp = _vault(db_path)
    if vp.exists():
        plaintext = _fernet().decrypt(vp.read_bytes())
        config.prepare_private_file(db_path)
        db_path.write_bytes(plaintext)
        return True
    return False


def unlock_if_needed(db_path) -> None:
    """At startup: if the vault exists and no plaintext DB is present, restore it."""
    db_path = Path(db_path)
    if _vault(db_path).exists() and not db_path.exists():
        unlock(db_path)


def lock_and_remove(db_path) -> bool:
    """Fail explicitly without risking either the live DB or a previous vault."""
    raise RuntimeError(RETIRED_MESSAGE)
