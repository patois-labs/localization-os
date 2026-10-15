"""SecretStore port -- the named runtime-secret RESOLUTION seam (composable-core
phase 1, Train 4).

This module NAMES the seam Localization OS uses to RESOLVE its two runtime secrets --
``SESSION_SECRET`` and ``ENCRYPTION_KEY`` -- from the environment / a persisted
keyfile. It is a thin FACADE over ``config._secret`` (the resolver Train S
corrected): WRAP, DON'T MOVE -- ``config._secret`` / ``config._keyfile`` are untouched;
the default adapter delegates to them, so the resolution order, provenance markers, and
Train-S boot warnings are BYTE-IDENTICAL.

WHAT THIS PORT NAMES (and, deliberately, what it does NOT)
---------------------------------------------------------
It names the RESOLUTION seam ONLY. After this train ``config`` obtains the two secrets
via ``ports.secrets.get().resolve(...)`` instead of a bare ``config._secret(...)`` call,
so a future KMS/vault adapter (phase 3+, not built) can supply the same ``(value,
source)`` pair without touching ``config``. The ~10 Fernet consumers that read the
resolved ``config.ENCRYPTION_KEY`` / ``config.SESSION_SECRET`` CONSTANTS
(``storage``, ``vault``, ``engines``, ``settings``, ``connectors/store``,
``connectors/webhooks``, ``vendors``, ``models``, ``dbcrypt`` via ``sqlcipher_hex_key``,
plus the ``SESSION_SECRET`` middleware/HMAC readers) do NOT change: they consume the
already-resolved constant, not a service module.

BOUNDARY IS NAME-LEVEL, NOT IMPORT-GUARDABLE (stated, not faked -- like MTEngine)
--------------------------------------------------------------------------------
Consumers read the ``config`` CONSTANTS, resolved once at import; they do not import a
secrets service module, and the resolver lives INSIDE ``config`` (the bottom of the
import graph), which ~everything imports. So there is no forbid-able import edge:
SecretStore gets NO import-contract row in phase 1 (the same reason RelationalStore --
123 sqlmodel importers -- and MTEngine -- a name-level factory boundary -- get none). The
port names the resolution CALL seam; a call-level guarantee, not an import edge.
Documented in ``tests/_import_contracts.py`` (Train-4 note) and the Train-4 errata.

THE CONFIG-IMPORT-TIME INVERSION (load-bearing -- read before editing)
---------------------------------------------------------------------
``config`` resolves its secrets AT MODULE IMPORT, and ``config`` is the BOTTOM of the
import graph. So this port MUST be resolvable during ``config``'s own import, which
inverts the usual dependency direction. The rule that makes it safe:

  * This module imports ``config`` **LAZILY, inside ``resolve()``** -- NEVER at module
    scope. ``scheduler`` and ``blobstore`` do ``from app import config`` at module scope
    because they are only ever imported LAZILY, AFTER ``config`` is fully loaded
    (``telemetry`` does not import ``config`` at all -- it reaches it only transitively via
    ``prompt_tap``). THIS module is the exception: ``config`` imports IT during ``config``'s
    own import, so a module-scope ``from app import config`` here would re-enter a half-built
    ``config`` and -- worse -- ``config`` calling ``secrets.get()`` before this module's
    LAST-statement self-registration ran would raise ``UnknownPort``. Importing ``config``
    lazily inside ``resolve`` breaks the cycle: this module's body needs only ``ports``
    (which has no ``config`` dependency), self-registers its default, returns; ``config``
    then calls ``resolve``, which reaches the (by-now sufficiently-built: ``_secret`` is
    defined well above the resolution block) ``config`` module. The lazy import uses the
    ``from app import config`` FORM deliberately -- at re-entry the parent attribute
    ``app.config`` is not yet bound, so it resolves via CPython's IMPORT_FROM sys.modules
    fallback (bpo-30024, 3.7+); ``import app.config`` has no such fallback and would fail.
  * IMPORT-SAFETY (spec risk note): the default adapter touches ONLY env + keyfile via
    ``config._secret`` -- no DB, no Settings read -- so importing this module during
    ``config`` import never pulls ``app.db``'s import-time engine construction into the
    config-only import graph (the phase-0c ``--pg-config-child`` property survives).

NO ``ports.get()`` AT MODULE SCOPE (frozen-file docstring rule / spec LOW-2). This module
self-registers its default as its LAST statement and resolves nothing at import.

THE REGISTRY BYPASS (un-closeable this train; mirrors Train 1/2 errata (b))
--------------------------------------------------------------------------
Production reaches the store only through ``ports.secrets.get()`` (which ``config``'s
resolution seam uses). The raw registry -- ``from app import ports;
ports.get("secrets")`` -- returns the SAME adapter object; there is no wrapper (unlike
``telemetry.get()``'s ``_RedactingSink``), so ``secrets.get() is ports.get("secrets")``.
That is un-closeable this train (``app/ports/__init__.py`` is frozen) and harmless here
(no redaction law to bypass), so it is DOCUMENTED, not fixed. ``config``'s secrets are
still resolved through the port either way.

Documented in: docs/architecture/kernel-and-ports.md
"""

from typing import Protocol, runtime_checkable

from app import ports

__all__ = ["EnvKeyfileSecretStore", "SecretStore", "get"]


@runtime_checkable
class SecretStore(Protocol):
    """Resolve one runtime secret to a ``(value, source)`` pair.

    ``resolve(env_key, keyfile_name, generate, *, alt_env=None)`` returns
    ``(value, source)`` where ``source`` is ``"env"`` or ``"keyfile"``. A future
    KMS/vault adapter injects an env-shaped value and returns ``source="env"``. The
    signature mirrors ``config._secret`` exactly so the default adapter is a pure
    delegation."""

    def resolve(
        self, env_key: str, keyfile_name: str, generate, *, alt_env: str | None = None
    ) -> tuple[str, str]: ...


class EnvKeyfileSecretStore:
    """The DEFAULT adapter: today's ``config._secret`` (post Train S) verbatim.

    ``resolve`` delegates to ``config._secret`` so the resolution order
    (prefixed env -> EXISTING keyfile -> non-embedded unprefixed env -> generate keyfile),
    the ``os.chmod(0o600)`` keyfile write, the ``(value, source)`` provenance, and the
    three Train-S boot warnings are BYTE-IDENTICAL -- wrap, don't move.

    ``config`` is imported LAZILY here (see the module docstring's inversion note): this
    adapter is constructed during ``config``'s OWN import, so it must not bind ``config``
    at module scope. By the time ``resolve`` is CALLED (from ``config``'s resolution
    block), ``config._secret`` is defined."""

    def resolve(
        self, env_key: str, keyfile_name: str, generate, *, alt_env: str | None = None
    ) -> tuple[str, str]:
        # LAZY -- never a module-scope import (see the module docstring's inversion note).
        # Do NOT "simplify" this to `import app.config as config` / `import app.config`:
        # the `from app import config` FORM is LOAD-BEARING. At the re-entrant call during
        # config's OWN import, the parent attribute `app.config` is not yet set, so this
        # resolves via CPython's IMPORT_FROM sys.modules fallback (bpo-30024, 3.7+); the
        # `import app.config` forms have NO such fallback and would fail at boot everywhere.
        from app import config

        return config._secret(env_key, keyfile_name, generate, alt_env=alt_env)


def get() -> "SecretStore":
    """The active SecretStore: an override first, else the default
    ``EnvKeyfileSecretStore`` self-registered below, resolved through the frozen
    ``app.ports`` registry. ``config``'s secret-resolution block calls
    ``get().resolve(...)`` for each secret. Returns the raw adapter (no wrapper), so
    ``get() is ports.get('secrets')``.

    An override (``ports.register('secrets', ...)``) takes effect ONLY if registered
    BEFORE ``config``'s first import (the two secrets are CONSTANTS resolved once at
    ``config`` import; nothing reloads ``config``, so a later ``register('secrets', ...)``
    is INERT -- unlike the other ports, this one has no live hot-swap). Test E is the pin.
    NOTE (L3): if this is called while ``config`` has NOT yet been imported, the default
    adapter's first ``resolve`` triggers the FULL ``config`` import as a side effect
    (``load_dotenv``, the Train-S boot warnings, and keyfile GENERATION persisted to
    disk) -- correct and DB-free, but a real filesystem side effect worth knowing."""
    return ports.get("secrets")


# Self-register the default adapter as the module's LAST statement (frozen-file docstring
# rule): a default registered BEFORE an import failure lower in the module would survive
# in _DEFAULTS even though the failed module is dropped from sys.modules. No such import
# here, but the rule is followed uniformly.
ports.register_default("secrets", EnvKeyfileSecretStore())
