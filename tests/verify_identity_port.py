"""Identity port verification (offline) -- composable-core phase 1, Train 6 (SECURITY).

Train 6 is deliberately MINIMAL and its constraints are security law; this suite is the
reviewer-bait proof of each one. The Identity port NAMES the ONE inbound-auth resolver
(``app.web.deps.api_identity``) with ``get()`` ONLY, migrates ZERO call sites, and is NOT
runtime-swappable. What is proven here:

  (1) IDENTITY, not a wrapper: ``identity.get()`` returns the SAME callable object
      ``deps.api_identity`` -- so the 11 live call sites and any new caller share one
      resolver (``get()`` is a re-export, not a shim that could drift).

  (2) NO RUNTIME ADAPTER SWAP: from request-handling-style code,
      ``ports.register("identity", ...)`` / ``register_default`` / ``unregister`` each HARD-
      refuse (``PortRegistrationRefused``). The auth-swap surface phase 0b closed cannot be
      re-opened via the generic registry. (Also pinned by Train H's ``test_identity_refusal``;
      re-asserted here in the Identity-security context.)

  (3) NO MODULE-LEVEL BACKDOOR: ``app.ports.identity`` exposes NO ``register`` /
      ``register_default`` / ``unregister`` attribute and never touches the generic registry.
      ``__all__ == ["get"]``. (Monkeypatching ``identity.get`` itself is OUT OF SCOPE: a
      caller able to rebind a module attribute already has arbitrary in-process code
      execution -- true of every function in the app; the port defends the REGISTRY swap
      surface, not against in-process monkeypatching.)

  (4) GENERIC REGISTRY PATH IS FAIL-CLOSED: because this module self-registers NO default,
      ``ports.get("identity")`` misses both slots, lazily imports this module (which registers
      nothing), re-checks, and raises ``UnknownPort`` -- the resolver is NOT obtainable through
      the generic registry, only through ``identity.get()``. Documented as fail-closed.

  (5) TRUST DOES NOT LAUNDER A WRONG TOKEN (functional, live embedded boot): under the phase-0b
      double condition (``LOC_TMS_EMBEDDED=1`` + the switch) a TOKENLESS request resolves to the
      trust principal (200) but a WRONG token is still 401 -- and the route enforces this via
      the SAME callable ``identity.get()`` hands back. ``trust_auth`` is an INNER BRANCH of that
      callable, never a registrable peer adapter (proving why (2)'s wholesale swap is refused).
      A targeted assert only -- the full trust matrix is ``verify_trust_host_auth``, not repeated.

  (6) IMPORT-TIMING (justifies the call-time ``deps`` import): ``import app.ports.identity``
      constructs NEITHER the DB engine (``app.db``) NOR the heavy ``app.web.deps`` graph, so the
      fail-closed ``ports.get("identity")`` path and future embedder probes force only a cheap,
      side-effect-free import. Proven in a PRISTINE child process.

No Protocol/isinstance check: the spec defines the Identity adapter as a CALLABLE, not an
object with methods (the deliberate ``get()`` asymmetry vs the other ports), so there is no
Protocol to structurally check -- following the spec.

Boot-simulation pattern (verify_trust_host_auth / verify_embedded_profile): three modes
selected from argv BEFORE any app import (config freezes the mode at import). Hermetic:
throwaway LOC_TMS_DATA_DIR + cleared provider keys, never the real DB. ASCII-only prints.
Final line: IDENTITY PORT OK.
"""

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_IMPORT_TIMING_CHILD = "--import-timing-child" in sys.argv
_EMBEDDED_CHILD = "--embedded-child" in sys.argv

# The two children decide their mode BEFORE importing anything from app (config freezes the
# embedded flag at import; the import-timing child must import app.ports.identity FIRST of
# all, so its env is set here and nothing from app is imported at module scope below).
if _IMPORT_TIMING_CHILD:
    init_env("loctms_identity_it_")
    os.environ.pop("LOC_TMS_EMBEDDED", None)
elif _EMBEDDED_CHILD:
    init_env("loctms_identity_emb_")
    os.environ.pop("LOC_TMS_SERVER", None)
    os.environ["LOC_TMS_EMBEDDED"] = "1"
else:
    init_env("loctms_identity_")
    os.environ.pop("LOC_TMS_EMBEDDED", None)


PRINCIPAL = "host-service@embedder.example"


def _is(r, code, what):
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:300]})"


# ============================================================================
# Mode 1: import-timing child (PRISTINE process) -- proves the call-time deps import.
# ============================================================================
def import_timing_child_main() -> None:
    from app import ports

    # (4-COLD, Corr L3) The GENUINELY cold lazy-import order: app.ports.identity has NOT been
    # imported yet in this pristine process, so this is the fail-closed path a real
    # ports.get('identity') takes -- it forces the registry's importlib fallback (a real fresh
    # import of the module) and must still raise UnknownPort because the module self-registers
    # nothing. (default_main's item (4) exercises the WARM order, where the module was already
    # imported; only here is the cold registry-driven import actually triggered.)
    assert "app.ports.identity" not in sys.modules, "precondition: module not yet imported"
    try:
        ports.get("identity")
    except ports.UnknownPort:
        pass
    else:
        raise AssertionError("cold ports.get('identity') must raise UnknownPort (fail-closed)")
    assert "app.ports.identity" in sys.modules, (
        "the cold ports.get('identity') must have forced the lazy importlib import"
    )
    assert "identity" not in ports._DEFAULTS and "identity" not in ports._OVERRIDES, (
        "the cold registry-driven import must self-register NOTHING"
    )

    # (6) That cold registry-driven import -- and a direct import below -- must NOT drag in
    # app.db (the engine), the heavy app.web.deps graph, app.config, or even fastapi: the
    # whole justification for importing deps lazily inside get() rather than at module scope
    # (a module-scope deps import would make the fail-closed ports.get('identity') path above
    # and any embedder probe pull the DB engine).
    import app.ports.identity as idport

    db_mods = sorted(k for k in sys.modules if k == "app.db" or k.startswith("app.db."))
    assert not db_mods, (
        f"importing app.ports.identity constructed the DB layer {db_mods!r} -- the deps import leaked "  # noqa: E501
        "to module scope; it MUST stay inside get()"
    )
    assert "app.web.deps" not in sys.modules, (
        "importing app.ports.identity pulled app.web.deps at module scope -- it MUST be a "
        "call-time import inside get()"
    )
    assert "app.config" not in sys.modules, (
        "importing app.ports.identity pulled app.config -- the module must be inert at import"
    )
    # (Corr L4) fastapi is the ONE leak a botched TYPE_CHECKING edit would cause (the Request
    # type ref must stay under `if TYPE_CHECKING:`). Importable WITHOUT fastapi is the module's
    # best embedder property, so pin it.
    assert "fastapi" not in sys.modules, (
        "importing app.ports.identity pulled fastapi -- the Request type ref must stay under "
        "TYPE_CHECKING; the module must be importable without fastapi"
    )
    assert callable(idport.get), "identity.get must be callable"
    print(
        "  [it-child] cold ports.get('identity') -> UnknownPort (fail-closed, registers "
        "nothing); the forced import pulled NEITHER app.db (no engine) NOR app.web.deps NOR "
        "app.config NOR fastapi -- the module is inert at import; deps is call-time"
    )
    print("IMPORT TIMING CHILD OK")


# ============================================================================
# Mode 2: embedded child -- trust does NOT launder a wrong token (functional).
# ============================================================================
def embedded_child_main() -> None:
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app
    from app.ports import identity
    from app.services import settings, trust_auth
    from app.web import deps

    assert config.IS_EMBEDDED is True, "LOC_TMS_EMBEDDED=1 must set IS_EMBEDDED"
    # The port hands back the SAME live resolver in embedded mode too (no per-mode drift).
    assert identity.get() is deps.api_identity, (
        "identity.get() must be deps.api_identity even on an embedded boot"
    )

    with TestClient(app) as c:
        # Arm the phase-0b double condition: embedded (this boot) AND the switch, with a
        # provisioned trust principal (default scopes api/docs/keys, default role viewer).
        settings.set_many({"embedded_trust_host_auth": "1"})
        settings.invalidate_cache()
        os.environ["LOC_TMS_TRUST_PRINCIPAL"] = PRINCIPAL
        for k in (
            "LOC_TMS_TRUST_SCOPES",
            "LOC_TMS_TRUST_ROLE",
            "LOC_TMS_TRUST_ORG_ID",
            "LOC_TMS_TRUST_HEADER_SECRET",
        ):
            os.environ.pop(k, None)
        trust_auth.configure_at_boot()
        assert trust_auth.active() is True, "trust must be active under the double condition"

        # TOKENLESS -> the trust principal (200): the trust branch is live.
        _is(c.get("/api/projects"), 200, "tokenless under trust mode -> trust principal read")
        # WRONG TOKEN -> still 401: trust NEVER launders a bad token. This is the whole
        # anti-laundering guarantee, and the route enforces it via the SAME callable
        # identity.get() returns -- so had trust been a registrable wholesale adapter (BLOCKER-5),
        # this bad-token request would have resolved to the trust principal (200) instead.
        bad = c.get("/api/projects", headers={"X-Loc-Token": "lsu_totally-bogus-token"})
        _is(bad, 401, "a WRONG token under trust mode must still be 401 (trust launders nothing)")
        # (Sec L3) Assert the 401 BODY too (mirror verify_trust_host_auth's unauthorized body),
        # so a future 401 arising from ANOTHER cause cannot silently satisfy this arm.
        assert bad.json() == {"ok": False, "error": "unauthorized"}, bad.text

        # Leave global state clean for any sibling suite sharing the interpreter.
        settings.set_many({"embedded_trust_host_auth": "0"})
        settings.invalidate_cache()

    print(
        "  [emb-child] embedded+trust: tokenless -> 200 (trust principal), a WRONG token "
        "-> 401 (trust launders nothing); the route enforces this via identity.get()'s "
        "callable, so a wholesale trust adapter swap (refused) would have leaked here"
    )
    print("EMBEDDED CHILD OK")


# ============================================================================
# Mode 0 (default, NON-embedded): shape, registry, fail-closed + a byte-identity 401.
# ============================================================================
def _run_child(flag: str, prefix: str) -> None:
    """Spawn a sibling process in a child mode, mint it a fresh DATA_DIR, and surface its
    output on failure. Mirrors verify_trust_host_auth's child-boot pattern."""
    child_env = dict(os.environ)
    child_env.pop("LOC_TMS_DATA_DIR", None)  # the child's init_env mints its own
    child_env.pop("LOC_TMS_EMBEDDED", None)
    for k in (
        "LOC_TMS_TRUST_PRINCIPAL",
        "LOC_TMS_TRUST_SCOPES",
        "LOC_TMS_TRUST_ROLE",
        "LOC_TMS_TRUST_ORG_ID",
        "LOC_TMS_TRUST_HEADER_SECRET",
    ):
        child_env.pop(k, None)
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__), flag],
        env=child_env,
        capture_output=True,
        text=True,
    )
    if child.returncode != 0:
        sys.stdout.write(child.stdout)
        sys.stderr.write(child.stderr)
        raise AssertionError(f"the {prefix} boot FAILED (see output above)")
    for line in child.stdout.splitlines():
        if line.strip():
            print("  " + line.strip())


def default_main() -> None:
    from fastapi.testclient import TestClient

    from app import ports
    from app.main import app
    from app.ports import identity
    from app.web import deps

    # ---- (1) identity, not a wrapper ------------------------------------------------ #
    real = deps.api_identity
    got = identity.get()
    assert got is real, (
        "identity.get() must return the IDENTICAL deps.api_identity callable (a re-export, "
        f"not a wrapper that could drift); got {got!r}"
    )
    # (Corr M2) The re-export is a FRESH attribute lookup each call, NOT a value captured at
    # import -- prove it STRUCTURALLY: a value captured at import (a frozen alias) would pass
    # the `is real` check above identically, so it cannot catch that regression. Rebind
    # deps.api_identity to a sentinel and assert get() tracks it, then restore. (This is a
    # HERMETIC test-only rebind on a throwaway process; the module docstring's split-brain
    # caveat forbids doing this on a LIVE install.)
    sentinel = lambda request: None  # noqa: E731
    deps.api_identity = sentinel
    try:
        assert identity.get() is sentinel, (
            "identity.get() must track a rebind of deps.api_identity (fresh call-time lookup, "
            "not a frozen import-time alias)"
        )
    finally:
        deps.api_identity = real
    assert identity.get() is real, "restore failed: get() must return the real resolver again"
    print(
        "  [1] identity.get() IS deps.api_identity (same callable object, a re-export via a "
        "fresh call-time lookup -- proven by tracking a rebind) -- the 11 call sites and any "
        "new caller share ONE resolver"
    )

    # ---- (3) no module-level backdoor ----------------------------------------------- #
    for name in ("register", "register_default", "unregister"):
        assert not hasattr(identity, name), (
            f"app.ports.identity must expose NO {name!r} attribute -- the resolver is not "
            "runtime-swappable via this module"
        )
    assert identity.__all__ == ["get"], (
        f"app.ports.identity.__all__ must be exactly ['get']; got {identity.__all__!r}"
    )
    publics = [
        n
        for n in dir(identity)
        if not n.startswith("_")
        and callable(getattr(identity, n))
        and getattr(getattr(identity, n), "__module__", None) == identity.__name__
    ]
    assert publics == ["get"], (
        f"the ONLY public callable defined in app.ports.identity must be get(); got {publics!r}"
    )
    print(
        "  [3] no backdoor: app.ports.identity exposes no register/register_default/"
        "unregister and never touches the generic registry; __all__ == ['get'] "
        "(monkeypatching get() itself is out of scope -- arbitrary code execution already)"
    )

    # ---- (2) no runtime adapter swap ------------------------------------------------ #
    for fn, label in (
        (lambda: ports.register("identity", object()), "register"),
        (lambda: ports.register_default("identity", object()), "register_default"),
        (lambda: ports.unregister("identity"), "unregister"),
    ):
        try:
            fn()
        except ports.PortRegistrationRefused:
            pass
        else:
            raise AssertionError(
                f"ports.{label}('identity', ...) must RAISE PortRegistrationRefused"
            )
    assert "identity" not in ports._OVERRIDES and "identity" not in ports._DEFAULTS, (
        "a refused identity write must leave the registry untouched"
    )
    print(
        "  [2] no runtime swap: ports.register/register_default/unregister('identity') "
        "each HARD-refuse (PortRegistrationRefused) from request-handling-style code"
    )

    # ---- (4) generic registry path is fail-closed ----------------------------------- #
    # NOTE: this is the WARM order -- app.ports.identity was already imported at the top of
    # default_main, so ports.get() here re-checks both slots against the cached module rather
    # than forcing a fresh import. The property holds identically (get() raises UnknownPort
    # whether the import is warm or cold), but the GENUINELY COLD registry-driven import is
    # exercised in import_timing_child_main (Corr L3), a pristine process where the module has
    # never been imported.
    try:
        ports.get("identity")
    except ports.UnknownPort:
        pass
    else:
        raise AssertionError(
            "ports.get('identity') must raise UnknownPort: the module self-registers no "
            "default, so the generic registry cannot hand out the resolver (fail-closed)"
        )
    # app.ports.identity self-registered NOTHING (warm-order re-assert of registry inertness).
    assert "identity" not in ports._DEFAULTS and "identity" not in ports._OVERRIDES, (
        "importing app.ports.identity must NOT self-register anything"
    )
    print(
        "  [4] fail-closed: ports.get('identity') raises UnknownPort (warm order here; the "
        "cold registry-driven import is proven in the import-timing child) -- the resolver "
        "is reachable ONLY via identity.get()"
    )

    # ---- (4b) anti-laundering pin: trust_auth.principal() still takes NO request ----- #
    # The phase-3 trust-hardening train (source_permitted / annotate_forwarded_actor) must
    # NOT have migrated the anti-laundering property onto a request-bearing resolver: the
    # Train-6 "principal takes no request" law is what keeps trust from re-resolving off a
    # forwarded header. Pin the signature so a future refactor cannot quietly widen it.
    import inspect

    from app.services import trust_auth

    params = list(inspect.signature(trust_auth.principal).parameters)
    assert params == [], (
        f"trust_auth.principal() must take NO parameters (the 'no request' law); got {params!r}"
    )
    print(
        "  [4b] anti-laundering signature pin: trust_auth.principal() still takes no "
        "request parameter (phase-3 hardening did not migrate it onto a request-bearing "
        "resolver)"
    )

    # ---- (5-nonembedded) byte-identity of the auth path: 401 AND 200 both intact ----- #
    # The get() indirection must not turn a rejection into an acceptance (no 401->200) NOR a
    # valid identity into a rejection (no 200->401) on an ordinary (non-embedded, no-trust)
    # install. All three requests reach the resolver identity.get() returns.
    from app.models import Role
    from app.services import apikeys, security

    with TestClient(app) as c:
        _is(c.get("/api/projects"), 401, "non-embedded tokenless must be 401 (no trust)")
        assert c.get("/api/projects").json() == {"ok": False, "error": "unauthorized"}
        _is(
            c.get("/api/projects", headers={"X-Loc-Token": "lsu_bogus"}),
            401,
            "non-embedded bad token must be 401",
        )
        # (Corr M1) POSITIVE control (house pattern verify_trust_host_auth:143-144): a REAL
        # per-user key resolves to 200 through the same callable get() returns -- proving the
        # indirection did not break the accept path (a resolver that 401'd everything would
        # pass the two negative checks above but fail here).
        user = security.create_user("real@x.com", "Real", Role.ADMIN, "real-password-1234")
        _row, real_key = apikeys.create_key(user.id, "real", scopes="api docs")
        _is(
            c.get("/api/projects", headers={"X-Loc-Token": real_key}),
            200,
            "a VALID per-user key must resolve to 200 (accept path intact)",
        )
    print(
        "  [5a] non-embedded auth path unchanged: tokenless -> 401 (byte-identical body), a "
        "bad token -> 401, AND a VALID key -> 200; the get() indirection turns no 401 into a "
        "200 and no 200 into a 401"
    )

    # ---- (6) + (5-embedded) via child processes ------------------------------------- #
    _run_child("--import-timing-child", "import-timing child")  # (6)
    _run_child("--embedded-child", "embedded child")  # (5) functional trust proof

    print("IDENTITY PORT OK")


def main() -> None:
    if _IMPORT_TIMING_CHILD:
        import_timing_child_main()
    elif _EMBEDDED_CHILD:
        embedded_child_main()
    else:
        default_main()


if __name__ == "__main__":
    main()
