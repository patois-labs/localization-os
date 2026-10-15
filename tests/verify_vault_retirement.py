"""Legacy vault retirement: refuse unsafe snapshots and keep recovery available."""

import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from tests._bootstrap import init_env

TMP = Path(init_env("loctms_vault_retirement_"))

from app import config
from app.db import init_db
from app.main import _shutdown, _startup
from app.services import dbcrypt, settings, vault
from tests._cli_runner import run_cli


class VaultRetirement(unittest.TestCase):
    def test_live_wal_never_published_or_deleted(self):
        for name in ("lock", "lock_and_remove"):
            with self.subTest(name=name):
                path = TMP / (name + ".db")
                conn = sqlite3.connect(path)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("CREATE TABLE private (value TEXT)")
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("INSERT INTO private VALUES ('private committed record')")
                    conn.commit()
                    vp = Path(str(path) + ".vault")
                    vp.write_bytes(b"previous recoverable artifact")
                    before = path.read_bytes()
                    wal = Path(str(path) + "-wal").read_bytes()
                    with self.assertRaisesRegex(RuntimeError, "SQLCipher"):
                        getattr(vault, name)(path)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(Path(str(path) + "-wal").read_bytes(), wal)
                    self.assertEqual(vp.read_bytes(), b"previous recoverable artifact")
                    self.assertEqual(conn.execute("SELECT count(*) FROM private").fetchone(), (1,))
                finally:
                    conn.close()

    def test_shutdown_refuses_legacy(self):
        with (
            patch.object(dbcrypt, "enabled", return_value=False),
            patch.object(config, "IS_SQLITE", True),
            patch.object(settings, "get", return_value="1"),
            self.assertRaisesRegex(RuntimeError, "SQLCipher"),
        ):
            _shutdown()

    def test_startup_refuses_before_starting_services(self):
        with (
            patch("app.main.init_db"),
            patch.object(dbcrypt, "enabled", return_value=False),
            patch.object(settings, "get", return_value="1"),
            patch.object(config, "IS_SQLITE", True),
            patch.object(
                settings, "apply_to_config", side_effect=AssertionError("startup continued")
            ),
            self.assertRaisesRegex(RuntimeError, "SQLCipher"),
        ):
            _startup()

    def test_shared_write_validation(self):
        with patch.object(dbcrypt, "enabled", return_value=False):
            for value in ("1", 1):
                with (
                    self.subTest(value=value),
                    self.assertRaisesRegex(settings.InvalidSettingValue, "SQLCipher"),
                ):
                    settings.set_many({"encrypt_db": value})
            settings.set_many({"encrypt_db": "0"})
        with patch.object(dbcrypt, "enabled", return_value=True):
            settings.validate_value("encrypt_db", "1")

    def test_cli_refuses_legacy(self):
        code, _, err = run_cli(["config", "set", "encrypt_db", "1", "--yes"])
        self.assertNotEqual(code, 0)
        self.assertIn("SQLCipher", err)
        self.assertEqual(settings.get("encrypt_db"), "0")

    def test_shutdown_sqlcipher_and_disabled_are_inert(self):
        with patch.object(vault, "lock_and_remove", side_effect=AssertionError("unsafe vault")):
            with patch.object(dbcrypt, "enabled", return_value=True):
                _shutdown()
            with patch.object(settings, "get", return_value="0"):
                _shutdown()
            with patch.object(config, "IS_SQLITE", False):
                _shutdown()

    def test_existing_vault_recovery(self):
        path = TMP / "recovery.db"
        payload = b"legacy recovery fixture"
        Path(str(path) + ".vault").write_bytes(vault._fernet().encrypt(payload))
        vault.unlock_if_needed(path)
        self.assertEqual(path.read_bytes(), payload)
        path.write_bytes(b"newer plaintext")
        vault.unlock_if_needed(path)
        self.assertEqual(path.read_bytes(), b"newer plaintext")


if __name__ == "__main__":
    init_db()
    result = unittest.TextTestRunner().run(
        unittest.defaultTestLoader.loadTestsFromTestCase(VaultRetirement)
    )
    if not result.wasSuccessful():
        sys.exit(1)
    print("verify_vault_retirement OK")
