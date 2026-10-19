"""Shared env/bootstrap preamble for the offline verify suites.

Every verify_*.py suite runs as its own subprocess (see test_verify_suites.py)
and must be hermetic: a private, disposable LOC_TMS_DATA_DIR so no suite can
collide with another suite's data or touch the real loc_tms.db, and every
cloud-provider API key blanked so a suite can never reach a live provider
(this also matters locally: LOCAL_BASE_URL="" keeps the maintainer's live LM Studio
from leaking into a run). This module centralizes that ~10-line preamble,
previously pasted near-verbatim in ~137 files.

The _ROOT / os.chdir / sys.path.insert lines MUST run before ``tests``
is importable, so those stay inline in each suite. Call init_env() right
after them:

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(_ROOT)
    sys.path.insert(0, _ROOT)
    from tests._bootstrap import init_env  # noqa: E402
    init_env("loctms_myprefix_")

A few suites capture the tempdir path directly (e.g. to build file paths
under it); init_env() returns it for that case:

    _TMP = init_env("loctms_myprefix_")
"""

import atexit
import os
import shutil
import tempfile


def _cleanup(tmp: str) -> None:
    """Best-effort removal of the suite's temp DATA_DIR at process exit.

    WHY (housekeeping, 2026-07-26): mkdtemp() dirs were NEVER removed —
    ~180 per full run — and 45,445 stale ``loctms_*`` dirs accumulated in %TEMP%,
    degrading suites from ~5s to ~60s each (tempfile slows down as the directory
    entry count grows). Registered via atexit so it runs however the suite exits.

    On Windows an open SQLite handle makes its file undeletable, so we first
    dispose the SQLAlchemy engine IF the suite ever imported app.db — via
    sys.modules, never an import, so a suite that never touched the app doesn't
    pull it in at exit. Everything is ``ignore_errors``: cleanup must never turn
    a green suite red, and a partially-removed dir is still a win (the next run
    can't slow down on entries that no longer exist)."""
    import sys

    db = sys.modules.get("app.db")
    if db is not None:
        try:  # noqa: SIM105
            db.engine.dispose()
        except Exception:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def init_env(prefix: str) -> str:
    """Create a fresh temp DATA_DIR, point LOC_TMS_DATA_DIR at it, and blank
    the four cloud-provider env vars. Returns the tempdir path. The dir is
    removed at process exit (see _cleanup)."""
    tmp = tempfile.mkdtemp(prefix=prefix)
    atexit.register(_cleanup, tmp)
    os.environ["LOC_TMS_DATA_DIR"] = tmp
    os.environ["LOC_TMS_TEST_MODE"] = "1"  # #291: explicit test mode
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["TYPESAFE_API_KEY"] = ""
    os.environ["LOCAL_BASE_URL"] = ""
    # Blank the UNPREFIXED secret spellings so a suite resolves its secrets from
    # its own throwaway keyfile, never from a value the host env / repo .env
    # happens to export. config._secret honors SESSION_SECRET / ENCRYPTION_KEY on
    # non-embedded boots (Train S), so without this an operator box carrying them
    # would flip a suite's in-process secret SOURCE from 'keyfile' to 'env' (and
    # feed a possibly-non-Fernet ENCRYPTION_KEY into the encryption suites). The
    # prefixed LOC_TMS_* spellings are intentionally left alone: a suite that
    # needs one sets it itself right after init_env (e.g. verify_sqlcipher).
    os.environ["SESSION_SECRET"] = ""
    os.environ["ENCRYPTION_KEY"] = ""
    # Suites assume the built-in default engine. scripts/bootstrap.py writes a .env with
    # DEFAULT_PROVIDER=mock for newcomers, and config loads .env without overriding a set
    # variable, so pin the built-in value here or a contributor's .env changes results.
    os.environ["DEFAULT_PROVIDER"] = "claude"
    # This suite is OFFLINE BY DESIGN (keys blanked above) yet many fixtures pin
    # default_provider="claude"/"local" and rely on the keyless built-in resolving to
    # MockProvider. That fallback is now REFUSED on a configured install
    # (providers.get_provider raises ConfiguredProviderUnavailable so a keyless prod
    # box can never fake output). This flag is the explicit "offline, mock expected"
    # opt-in that keeps a suite on its historical posture when run directly; it is
    # DEFAULT-OFF and MUST stay unset in production. Mirrors the same line in
    # test_verify_suites.py (which runs each suite as a subprocess).
    os.environ["LOC_TMS_MOCK_FALLBACK"] = "1"
    return tmp
