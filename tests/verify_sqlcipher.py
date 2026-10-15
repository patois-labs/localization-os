"""SQLCipher always-on DB encryption verification (suite #39, offline).

Opt-in encrypted-at-rest mode: LOC_TMS_DB_ENCRYPTION=sqlcipher turns the local
SQLite file into a transparently-encrypted SQLCipher database (encrypted on disk
at ALL times, not just on shutdown like the legacy vault).

This suite SKIPS CLEANLY (exit 0, printed note, final 'OK' line) when the optional
`sqlcipher3` binding is absent — exactly like verify_mcp when the `mcp` SDK is
missing — so the app's other 38 suites are never coupled to this optional dep.

When the binding IS present it exercises the real path:
  1. env-gated engine: with the var set, app.db builds a SQLCipher engine; init_db
     brings the full schema up on a throwaway encrypted DB; insert + read back.
  2. file-not-plaintext: the on-disk DB has no 'SQLite format 3' magic and does
     not contain a known plaintext marker we wrote.
  3. wrong-key: opening the encrypted file with a different key fails.
  4. convert tool: encrypt (plaintext -> encrypted) then decrypt (back) round-trips
     a tiny DB in BOTH directions with data intact.

CRITICAL: the encryption mode is chosen at IMPORT (the engine is built once), so
LOC_TMS_DB_ENCRYPTION is set BEFORE importing app. Each verify_*.py runs as its
own subprocess (see tests/test_verify_suites.py), so this env var never leaks into
the other suites.
"""

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from tests._bootstrap import init_env

_TMP = init_env("loctms_sqlcipher_")
# Pin a deterministic ENCRYPTION_KEY so the derived SQLCipher key is stable within
# this run (and so the convert tool + engine agree). A valid Fernet key.
os.environ["LOC_TMS_ENCRYPTION_KEY"] = "u7Jq0Xy1Zt3Vb5Nc7Md9Pf1Rh3Tk5Wm7Yp9As1Bd0="

# --- Skip cleanly if the optional binding is not installed --------------------
try:
    from sqlcipher3 import dbapi2 as sqlcipher
except ImportError as exc:  # pragma: no cover - only without the optional dep
    print(
        f"  SKIP: sqlcipher3 binding not installed ({exc}) - "
        "pip install sqlcipher3 to enable always-on DB encryption"
    )
    print("  (skipped: no sqlcipher3 binding)")
    print("SQLCIPHER OK")
    raise SystemExit(0) from exc

# Binding is present: turn always-on encryption ON *before* importing app.config.
os.environ["LOC_TMS_DB_ENCRYPTION"] = "sqlcipher"

from sqlalchemy import text

from app import config
from app.db import engine, init_db
from app.services import dbcrypt

# scripts/ is a sibling of app/; make the convert tool importable.
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import convert_db_encryption as convert

_MARKER = "SUPER-SECRET-MARKER-loc-tms-42"


def _read_file(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def test_engine_roundtrip() -> None:
    assert dbcrypt.enabled(), "dbcrypt.enabled() should be True with the env var set"
    assert config.DB_ENCRYPTION_ON, "config.DB_ENCRYPTION_ON should be True"
    # Full app schema on a throwaway encrypted DB.
    init_db()
    # Insert a known marker via the app engine, then read it back transparently.
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organization (name, created_at) VALUES (:n, CURRENT_TIMESTAMP)"),
            {"n": _MARKER},
        )
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(text("SELECT name FROM organization")).fetchall()]
    assert _MARKER in names, "marker row not read back through the encrypted engine"
    engine.dispose()  # flush WAL so the on-disk file is complete for the checks below
    print(
        f"  engine: init_db built full schema on encrypted DB; insert+read OK (dialect={engine.dialect.name})"  # noqa: E501
    )


def test_file_not_plaintext() -> None:
    raw = _read_file(config.DB_PATH)
    assert not raw.startswith(b"SQLite format 3"), (
        "encrypted DB starts with the plaintext SQLite magic!"
    )
    assert _MARKER.encode() not in raw, "plaintext marker found in the encrypted DB file!"
    print(
        "  at-rest: DB file has no 'SQLite format 3' magic and no plaintext "
        f"marker ({len(raw)} bytes)"
    )


def test_wrong_key_fails() -> None:
    # Correct key opens it; a different key does not.
    c = sqlcipher.connect(str(config.DB_PATH))
    c.execute(f"PRAGMA key = \"x'{config.sqlcipher_hex_key()}'\"")
    ok = c.execute("SELECT COUNT(*) FROM organization").fetchone()[0]
    c.close()
    assert ok >= 1

    c = sqlcipher.connect(str(config.DB_PATH))
    c.execute("PRAGMA key = \"x'{}'\"".format("ff" * 32))
    try:
        c.execute("SELECT COUNT(*) FROM organization").fetchone()
        raise AssertionError("wrong key opened the encrypted DB!")
    except sqlcipher.DatabaseError:
        pass
    finally:
        c.close()
    print("  wrong-key: correct key reads; a different key is rejected")


def test_convert_roundtrip() -> None:
    d = tempfile.mkdtemp(prefix="loctms_conv_")
    plain = os.path.join(d, "plain.db")
    enc = os.path.join(d, "enc.db")
    back = os.path.join(d, "back.db")

    # Build a tiny plaintext DB.
    c = sqlcipher.connect(plain)
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    c.execute("INSERT INTO t(v) VALUES('a'),('b')")
    c.commit()
    c.close()
    assert _read_file(plain).startswith(b"SQLite format 3")

    # encrypt -> encrypted file is not plaintext, opens with the app key.
    convert.encrypt(plain, enc)
    raw = _read_file(enc)
    assert not raw.startswith(b"SQLite format 3") and b"'a'" not in raw
    c = sqlcipher.connect(enc)
    c.execute(f"PRAGMA key = \"x'{config.sqlcipher_hex_key()}'\"")
    assert c.execute("SELECT v FROM t ORDER BY id").fetchall() == [("a",), ("b",)]
    c.close()

    # decrypt -> plaintext again, data intact.
    convert.decrypt(enc, back)
    assert _read_file(back).startswith(b"SQLite format 3")
    c = sqlcipher.connect(back)
    assert c.execute("SELECT v FROM t ORDER BY id").fetchall() == [("a",), ("b",)]
    c.close()
    print("  convert: encrypt then decrypt round-trips a tiny DB both directions")


def test_cli_decrypt_plaintext_clean_error() -> None:
    """#27: `db decrypt <plaintext.db> <dst>` used to dump a raw
    sqlcipher3.DatabaseError traceback ('file is not a database'). cmd_db._convert now
    wraps the conversion and raises a clean CliError (exit 1, no traceback), and cleans
    up the partial destination the aborted export left behind."""
    from types import SimpleNamespace

    from app.cli import cmd_db
    from app.cli._common import CliError

    d = tempfile.mkdtemp(prefix="loctms_dec_")
    plain = os.path.join(d, "plain.db")
    dst = os.path.join(d, "out.db")
    c = sqlcipher.connect(plain)
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
    c.commit()
    c.close()

    args = SimpleNamespace(src=plain, dst=dst, force=False, remote=None)

    class _Emit:
        as_json = False

        def result(self, *a, **k):  # pragma: no cover - not reached on failure
            raise AssertionError("decrypt of a plaintext source must not succeed")

    raised = None
    try:
        cmd_db._convert(args, _Emit(), "decrypt")
    except CliError as e:
        raised = e
    assert raised is not None, "decrypting a plaintext DB must raise CliError, not crash"
    assert raised.code == 1, raised.code
    msg = str(raised)
    assert "could not decrypt" in msg and "Traceback" not in msg, msg
    assert "SQLCipher-encrypted" in msg, ("hint should guide the operator", msg)
    assert not os.path.exists(dst), "the partial/corrupt destination must be removed"
    print("  #27: db decrypt of a plaintext source -> clean CliError, dst cleaned up")


def main() -> None:
    print("  sqlcipher3 binding present: exercising the real encrypted path")
    print(f"  (sqlcipher lib {sqlcipher.sqlite_version})")
    test_engine_roundtrip()
    test_file_not_plaintext()
    test_wrong_key_fails()
    test_convert_roundtrip()
    test_cli_decrypt_plaintext_clean_error()
    print("SQLCIPHER OK")


if __name__ == "__main__":
    main()
