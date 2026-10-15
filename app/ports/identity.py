"""Identity port -- names the inbound-auth resolver seam (composable-core phase 1, Train 6).

This is the SECURITY train. It NAMES the ONE inbound-auth resolver Localization OS has --
``app.web.deps.api_identity`` -- and does so in the most deliberately MINIMAL way the
spec allows. Its constraints are security law, not style:

  * ``get()`` ONLY. There is NO ``register`` / ``register_default`` / ``unregister`` on
    this module, and this module NEVER touches the generic ``app.ports`` registry (it does
    not self-register a default). The inbound-auth resolver is NOT runtime-swappable.
  * ZERO call-site migration. ``get()`` RE-EXPORTS the SAME callable the 11 live call sites
    already invoke; nothing is re-routed. The port exists for FUTURE embedders and as the
    documented boundary, not to change how a request is authenticated today.

WHY ``get()``-ONLY / NO ``register`` (BLOCKER-5 / BLOCKER-6)
-----------------------------------------------------------
A generic mutable ``register("identity", <adapter>)`` would be a process-wide auth-swap
surface: any module that can import ``app.ports`` (a future registry module, a ported
connector, a leaked test helper) could replace the inbound-auth resolver silently, with
NONE of the guards phase 0b put on the trust perimeter (a boot-frozen ``LOC_TMS_EMBEDDED``
flag + a ``REMOTE_IMMUTABLE_KEYS`` Setting + boot validation + the two-lever viewer default
+ the admin coupling law). So the general registry HARD-REFUSES ``identity`` on EVERY write
path (``app/ports/__init__.py`` ``_FROZEN_PORTS``: ``register`` / ``register_default`` /
``unregister`` all raise ``PortRegistrationRefused``; pinned by Train H's
``verify_import_contracts`` ``test_identity_refusal``), and THIS module exposes ``get()``
alone. SCOPE of the guarantee (Sec M2): no code path through this package's PUBLIC API --
an override, self-registration, or a module attribute of this module -- swaps the resolver.
Raw writes to ``ports._OVERRIDES``, rebinding ``ports._FROZEN_PORTS``, or rebinding
``deps.api_identity`` all require arbitrary in-process code execution, which already owns the
resolver directly; the port defends the REGISTRY swap surface, NOT against monkeypatching
(see ``verify_identity_port`` item 3). Within that scope there is no public write path that
swaps the inbound-auth resolver at runtime.

Corollary -- the generic ``ports.get("identity")`` path is FAIL-CLOSED. Because this module
does NOT self-register a default, ``ports.get("identity")`` misses ``_OVERRIDES`` (no public
write path can populate it) AND ``_DEFAULTS``, lazily imports THIS module (which registers
nothing), re-checks, and raises ``UnknownPort``. So the ONLY way to reach the resolver
through the port is this module's own ``identity.get()`` -- the generic registry cannot
hand it out. (Asserted in ``verify_identity_port``.) Recorded tradeoff (Sec L4):
``unregister("identity")`` refuses too, so a poisoned RAW-write override (a direct
``ports._OVERRIDES["identity"] = ...`` poke) has no SANCTIONED cleanup path -- but planting
one requires the same arbitrary-code-execution privilege the cleanup would, and
``ports.get("identity")`` has ZERO production consumers (production reaches the resolver only
via ``identity.get()``), so the poison sits on a path nothing reads.

WHY ``trust_auth`` IS NOT A REGISTRABLE ALTERNATE (BLOCKER-5 -- the laundering hole)
-----------------------------------------------------------------------------------
The Identity port has EXACTLY ONE adapter in phase 1: ``deps.api_identity``.
``trust_auth.principal()`` (``app/services/trust_auth.py:325``) is NOT a peer adapter -- it
takes NO ``request`` and is an INNER BRANCH of ``api_identity``, reached ONLY from the
tokenless path (``deps.py:659-660``) AFTER the ``X-Loc-Token`` header has been read and
found empty. That placement is the anti-laundering guarantee: a present-but-WRONG token
never reaches the trust branch (it falls through to ``apikeys.verify_key`` and the terminal
``return None`` -> 401), so trust can never launder a bad token into a trusted request.

Registering ``trust_auth`` as a REGISTRABLE alternate would be exactly the wrong move: a
wholesale adapter swap would replace ``api_identity`` in its entirety, so EVERY request --
INCLUDING one carrying a wrong token -- would resolve to the trust principal. That re-opens
the precise laundering hole phase 0b closed, re-created by the abstraction meant to
formalize its absence. §3 discrepancy 2's "bring-your-own alternate" is therefore satisfied
INSIDE the default adapter (the tokenless branch), never beside it.

THE ELEVEN CALL SITES -- DOCUMENTATION, NOT MIGRATION TARGETS (B6)
-----------------------------------------------------------------
There is no single "the dependency that reads ``api_identity``": there are ELEVEN direct
PRODUCTION call sites (the verify suites also call ``deps.api_identity`` directly -- this
enumeration is the PRODUCTION surface, not every caller), and a PARTIAL migration (some
routes through the port, some not) is the dangerous outcome, so phase 1 migrates NONE. The
11 sites keep calling ``deps.api_identity``
unchanged; ``identity.get()`` returns that same callable for NEW code and for the documented
boundary. Enumerated here (re-located at branch base ``4e1798d`` -- all still exact) so a
future reader sees the full surface, NOT as things to re-route:

  * ``app/main.py:285``       -- the audit middleware (imports ``api_identity`` by name).
  * ``app/web/api.py:122``    -- inbound document API.
  * ``app/web/api.py:174``
  * ``app/web/api.py:350``
  * ``app/web/api.py:1516``
  * ``app/web/api.py:1834``
  * ``app/web/api.py:1932``
  * ``app/web/api.py:1948``
  * ``app/web/deps.py:780``   -- ``_require_doc_scope`` (same-module call).
  * ``app/web/deps.py:802``   -- ``_require_admin_scope`` (same-module call).
  * ``app/web/keys.py:296``   -- the key-management API.

Because NOTHING is re-routed, there is NO Identity import-contract row (there is no
forbidden import edge to enforce -- ``deps`` has a large legitimate import graph, so a row
over ``deps.api_identity`` would be inexpressible/vacuous, exactly the RelationalStore /
MTEngine / Scheduler-module precedent). The enforceable properties for this port live in the
REGISTRY refusals (pinned by Train H) and in ``verify_identity_port``.

IMPORT-TIMING: ``deps`` IS IMPORTED LAZILY, AT CALL TIME, INSIDE ``get()``
--------------------------------------------------------------------------
``app.web.deps`` is HEAVY -- at module scope it imports ``app.services.settings`` (->
``app.db``, which CONSTRUCTS THE SQLAlchemy ENGINE AT IMPORT TIME), ``providers``, the
templating layer, and most of the web service graph. This module therefore does NOT import
``deps`` at module scope; ``get()`` does ``from app.web import deps`` at CALL time.

Justification (this port's import profile differs from every other phase-1 port):
  * There are NO production importers this train -- the 11 sites call ``deps.api_identity``
    directly, so ``get()`` is reached only by NEW code and tests. There is no hot path whose
    latency a call-time import would matter to.
  * ``import app.ports.identity`` MUST stay cheap and SIDE-EFFECT-FREE, because the generic
    registry's lazy fallback FORCES that import: ``ports.get("identity")`` (the fail-closed
    path above, and anything that probes the module) does
    ``importlib.import_module("app.ports.identity")``. A module-scope ``from app.web import
    deps`` would pull ``app.db``'s import-time engine construction and the whole web layer
    into that import -- turning a fail-closed registry miss (and any future embedder's
    "does this module exist?" probe) into a heavyweight, DB-touching side effect. Keeping the
    ``deps`` import inside ``get()`` makes this module import to a bare function definition:
    ``verify_identity_port`` proves ``import app.ports.identity`` does NOT construct the DB
    engine.
  * It mirrors the deferred-resolution discipline the frozen registry itself uses
    (``importlib`` at call time), and honors the frozen-file docstring rule spirit -- module
    scope does the minimum; resolution happens at call time.

``get()`` returns ``deps.api_identity`` via a FRESH attribute lookup each call (not a value
captured at import), so ``identity.get() is deps.api_identity`` holds AND a legitimate
test/hot-swap monkeypatch of ``deps.api_identity`` is honored (rather than shadowed behind a
frozen alias) -- the same call-time-lookup discipline Train 1's re-exports use.

SPLIT-BRAIN CAVEAT -- DO NOT hot-swap ``deps.api_identity`` on a LIVE install (Sec M1). The
fresh-lookup property makes a monkeypatch reach ``identity.get()`` AND every route that calls
``deps.api_identity`` at request time -- but NOT the audit middleware, which binds the name by
``from app.web.deps import ... api_identity`` at ``main.py:26`` (a value captured at
that import). A live monkeypatch would therefore split the brain: the routes would authorize
against the patched resolver while the middleware audited against the ORIGINAL one, so the
audited ACTOR could diverge from the AUTHORIZING identity. The fresh lookup exists for TEST
patchability (and to guarantee ``is`` identity), NOT as a supported live-swap mechanism --
which is the whole point of ``get()``-only: there is no sanctioned runtime swap.

Documented in: docs/architecture/kernel-and-ports.md
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # documentation-only; never imported at runtime (keeps the module cheap)
    from collections.abc import Callable

    from fastapi import Request

# get() is the ONLY public surface. Deliberately NO register / register_default /
# unregister here, and this module never calls the generic app.ports registry -- the
# inbound-auth resolver is not runtime-swappable (see the module docstring).
__all__ = ["get"]


def get() -> Callable[[Request], dict | None]:
    """The ONE Identity adapter: ``app.web.deps.api_identity``, the inbound-auth resolver
    (``X-Loc-Token`` -> per-user key / connector token / tokenless trust branch / None-401).

    Returns the SAME callable the 11 live call sites already invoke -- for NEW code and the
    documented boundary; NOTHING is re-routed this train. Not runtime-swappable: there is no
    ``register`` on this port, and the generic registry HARD-REFUSES ``identity`` on every
    write path (Train H). ``trust_auth`` is an INNER BRANCH of this callable, never a peer
    adapter (the anti-laundering placement -- see the module docstring).

    ``deps`` is imported HERE, at call time (not module scope), so ``import
    app.ports.identity`` stays cheap and DB-free -- the property the fail-closed generic
    ``ports.get('identity')`` path and future embedder probes rely on. The attribute lookup
    is fresh each call, so the returned object is identically ``deps.api_identity``."""
    from app.web import deps

    return deps.api_identity
