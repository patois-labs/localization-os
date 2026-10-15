"""PERMANENT inert self-test fixture for the import-contract harness (Train H).

This module exists SOLELY to back the harness's two proofs; NOTHING in production imports
it. The lazy registry (``app.ports.get``) imports it only when ``get("_selftest_probe")``
is called, which only ``tests/verify_import_contracts.py`` does -- so it is INERT by
construction (no production code path reaches it). It is a single PERMANENT module (not a
throwaway created/deleted per run, which invited a Windows-unlink flake history).

Two proofs it backs
--------------------
1. LAZY-RESOLUTION / FREEZE -- ``get("_selftest_probe")`` resolves this module's default
   via ``app.ports``'s ``importlib`` fallback WITHOUT any edit to the frozen
   ``app/ports/__init__.py``, proving a later port train can add a port by adding only a
   module. Its default is a harmless no-op stub.

2. ANTI-VACUITY planted violation -- this module deliberately contains a FORBIDDEN import
   (see ``_planted_forbidden_import_never_executed`` below) that the shared scanner
   (``tests/_import_contracts.py``) MUST flag. The anti-vacuity test invokes the scanner
   with an allowlist that OMITS this file and asserts it IS reported as an offender, so a
   silently no-op guard cannot pass. The production contract table SEPARATELY allowlists
   this file as a known fixture, keeping the real suite green.

The planted import is REAL (the statement genuinely appears in this file's AST, so the
scanner really detects it) but lives INSIDE a never-called function, so importing this
module never executes it -- the target module need not exist and is never loaded. Because
the import is real, the production allowlist entry for this file stays "used"; if the
planted import were ever removed, the scanner's stale-allowlist property would fail loudly
(the fixture cannot silently rot).

Module scope self-registers ONLY (it never calls ``ports.get()`` -- the frozen-file
rule against partially-initialized-module recursion).

Documented in: docs/architecture/kernel-and-ports.md
"""

from app.ports import register_default


class _NoopProbeSink:
    """The default adapter for the ``_selftest_probe`` port: does nothing. Its only job
    is to be a distinct object the freeze/override tests can identify."""

    name = "_selftest_probe_default"

    def emit(self, record):  # pragma: no cover - inert stub, never invoked in production
        return None


def _planted_forbidden_import_never_executed():  # pragma: no cover - never called
    """DO NOT CALL and DO NOT REMOVE.

    The body's FIVE imports are the anti-vacuity fixture the shared import-contract scanner
    must detect (see this module's docstring). Each exercises one of the scanner's match
    forms, so EVERY form has a non-vacuity proof (not just the stem branch). The
    anti-vacuity test asserts EXACTLY these five offender lines, all from this file:
      1. ``from app.services import <leaf>``          -- the ``app.services`` stem branch
      2. ``from app.services.<leaf> import <name>``   -- the full-module ``mods`` branch
      3. ``import app.services.<leaf>``               -- the ``ast.Import`` branch
      4. ``from ..services import <leaf>``            -- the RELATIVE branch (pkg_aware)
      5. ``__import__("app.services.<leaf>")``        -- the DYNAMIC branch (match_dynamic)
    Forms 4 and 5 are the GUARD-OF-THE-GUARD (L7): they match ONLY when scan_contract's
    ``pkg_aware`` / ``match_dynamic`` arms are engaged, so flipping either off (silently
    stripping relative+dynamic coverage from enforced rows) drops the offender count below
    five and fails the anti-vacuity test. Both are invisible in check-11 default mode, so
    check 11's byte-identity is unaffected.

    They live in a never-called function so importing ``_selftest_probe`` never runs them;
    the target is a synthetic name that need not exist. The forbidden target MUST stay in
    sync with ``PROBE_FORBIDDEN_TARGET`` in ``tests/_import_contracts.py``. Removing any of
    them weakens the harness AND (removing all) trips the scanner's stale-allowlist guard
    (the allowlist entry for this file would go unused)."""
    import app.services._selftest_probe_forbidden  # noqa: F401
    from app.services import _selftest_probe_forbidden  # noqa: F401
    from app.services._selftest_probe_forbidden import _x  # noqa: F401

    from ..services import (
        _selftest_probe_forbidden as _r,  # noqa: F401
    )

    __import__("app.services._selftest_probe_forbidden")


register_default("_selftest_probe", _NoopProbeSink())
