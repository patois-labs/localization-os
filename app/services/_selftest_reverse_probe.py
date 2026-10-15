"""PERMANENT inert self-test fixture for the REVERSE import-contract row (composable-core
phase 4, K1). Nothing in production imports it.

The reverse row forbids core (``app/services/**`` + ``app/filters/**`` + the spine) from
importing a shell (``app.web*``/``app.cli*``/``app.backoffice*``/``integrations*``) and holds
an EMPTY allowlist that must stay green -- so a live core->shell offender cannot live on it
without breaking its promise (round-3 F-3). This fixture is the reverse row's
guard-of-the-guard: it is a CORE file (``app/services``) that plants a forbidden shell-spelled
import, so the subject-scoped reverse scanner can be proven to FIRE on a core->shell edge.

To keep the real reverse row green, the planted import targets a SYNTHETIC module
``app.web._selftest_reverse_forbidden`` that does NOT exist and nothing real imports (it is
only ever parsed, never executed -- it lives inside a never-called function). The fixture sits
on its OWN Contract row (target = that synthetic name, allowlist = ONLY this file); the real
reverse row ``owner``-skips this file so its allowlist stays EMPTY. ``verify_kernel_facade.py``
re-scans the fixture's own row with ``allow_override={}`` and asserts this file IS reported --
proving the core-subject-scoped reverse scanner genuinely detects a core->``app.web``-spelled
edge (the ``app/ports/_selftest_probe`` fixture cannot serve here: ``app/ports`` is OUT of the
reverse row's subject scope).

The planted import is REAL (it genuinely appears in this file's AST), so the fixture's own-row
allowlist entry stays "used"; removing it would trip the scanner's stale-allowlist guard (the
fixture cannot silently rot). ``REVERSE_PROBE_FORBIDDEN_TARGET`` in
``tests/_import_contracts.py`` MUST stay in sync with the planted target below.

Documented in: docs/architecture/kernel-and-ports.md
"""


def _planted_reverse_forbidden_import_never_executed():  # pragma: no cover - never called
    """DO NOT CALL and DO NOT REMOVE. The body plants the forbidden core->shell import the
    reverse scanner must detect. It targets a SYNTHETIC ``app.web`` submodule that does not
    exist, so importing this fixture never runs it and the target is never loaded."""
    from app.web._selftest_reverse_forbidden import _reverse_probe_marker  # noqa: F401
