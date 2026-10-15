"""One-time converter between a plaintext SQLite DB and a SQLCipher-encrypted one.

Ships as a TOOL for the operator — it is never invoked by the app. Use it once
when turning always-on encryption on (or off):

    # plaintext loc_tms.db  ->  encrypted (writes <out>, leaves the source intact)
    python scripts/convert_db_encryption.py encrypt data/loc_tms.db data/loc_tms.enc.db

    # encrypted            ->  plaintext
    python scripts/convert_db_encryption.py decrypt data/loc_tms.enc.db data/loc_tms.db

The key is derived from ENCRYPTION_KEY exactly like the running app
(config.sqlcipher_hex_key), so an encrypted DB produced here opens under
LOC_TMS_DB_ENCRYPTION=sqlcipher and vice-versa. The SAME ENCRYPTION_KEY the app
uses MUST be in the environment (or its keyfile) when you run this.

Conversion uses SQLCipher's ATTACH + sqlcipher_export() (a whole-DB copy through
the codec), which is the supported way to cross the plaintext/encrypted boundary
(PRAGMA rekey cannot do it). The source file is never modified; the destination
is written fresh. STOP the app first (no live writers) and BACK UP before running.

Safety: refuses to overwrite an existing destination unless --force is given.
"""

import argparse
import os
import sys

# Make `from app import ...` work regardless of CWD.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import config
from app.cli._common import same_file


def _sqlcipher():
    try:
        from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
    except ImportError:
        print("ERROR: the optional 'sqlcipher3' binding is not installed.")
        print("       Run: pip install sqlcipher3")
        sys.exit(2)
    return sqlcipher


def _key_pragma() -> str:
    return f"PRAGMA key = \"x'{config.sqlcipher_hex_key()}'\""


def encrypt(src: str, dst: str) -> None:
    """Read plaintext `src`, write SQLCipher-encrypted `dst`."""
    sqlcipher = _sqlcipher()
    conn = sqlcipher.connect(src)  # source is plaintext -> no key
    try:
        conn.execute(
            f"ATTACH DATABASE ? AS encrypted KEY \"x'{config.sqlcipher_hex_key()}'\"",
            (dst,),
        )
        conn.execute("SELECT sqlcipher_export('encrypted')")
        conn.execute("DETACH DATABASE encrypted")
        conn.commit()
    finally:
        conn.close()


def decrypt(src: str, dst: str) -> None:
    """Read SQLCipher-encrypted `src`, write plaintext `dst`."""
    sqlcipher = _sqlcipher()
    conn = sqlcipher.connect(src)
    try:
        conn.execute(_key_pragma())  # key the source
        conn.execute("ATTACH DATABASE ? AS plaintext KEY ''", (dst,))  # empty key = plaintext
        conn.execute("SELECT sqlcipher_export('plaintext')")
        conn.execute("DETACH DATABASE plaintext")
        conn.commit()
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Convert a SQLite DB between plaintext and SQLCipher-encrypted."
    )
    p.add_argument("direction", choices=("encrypt", "decrypt"))
    p.add_argument("src", help="source DB file")
    p.add_argument("dst", help="destination DB file (written fresh)")
    p.add_argument("--force", action="store_true", help="overwrite dst if it already exists")
    args = p.parse_args(argv)

    if not os.path.isfile(args.src):
        print(f"ERROR: source not found: {args.src}")
        return 2
    if same_file(args.src, args.dst):
        print("ERROR: src and dst must differ (this tool never edits in place).")
        return 2
    if os.path.exists(args.dst) and not args.force:
        print(f"ERROR: destination exists: {args.dst} (pass --force to overwrite)")
        return 2
    if os.path.exists(args.dst) and args.force:
        os.remove(args.dst)

    src_key = "env" if config.ENCRYPTION_KEY_SOURCE == "env" else "keyfile"
    print(f"Using ENCRYPTION_KEY from: {src_key}")
    if args.direction == "encrypt":
        encrypt(args.src, args.dst)
        print(f"OK: wrote encrypted DB -> {args.dst}")
        print("Next: set LOC_TMS_DB_ENCRYPTION=sqlcipher and point the app at this file.")
    else:
        decrypt(args.src, args.dst)
        print(f"OK: wrote plaintext DB -> {args.dst}")
        print("Next: unset LOC_TMS_DB_ENCRYPTION and point the app at this file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
