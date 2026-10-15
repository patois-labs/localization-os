"""Import-contract harness + frozen named-port registry (composable-core phase 1, Train H).

This suite proves the two things Train H stands up:

  A. THE SHARED IMPORT-CONTRACT SCANNER (``tests/_import_contracts.py``) is non-vacuous
     and drives a per-port CONTRACT TABLE. The anti-vacuity self-test invokes the scanner
     against the permanent planted-violation fixture (``app/ports/_selftest_probe.py``)
     with an allowlist that OMITS the probe and asserts the probe IS reported -- so a
     silently no-op guard (wrong module paths, scanner not walking) CANNOT pass. The
     production table SEPARATELY allowlists the probe as a known fixture, so the real run
     stays green. TWO distinct invocations against TWO distinct allowlists, never
     conflated. The two seed rows (TelemetrySink, BlobStore) are PENDING -- reported, not
     asserted -- until their trains build the ports.

  B. THE FROZEN REGISTRY (``app/ports/__init__.py``). A NEW port module resolves via the
     lazy ``importlib`` fallback WITHOUT any edit to the frozen file; an override set
     BEFORE a port module is imported still wins after the import forces the default in
     (clobber-prevention); a repeat self-registration cannot clobber (setdefault); a
     genuine miss raises ``UnknownPort``; and ``register("identity", ...)`` is a HARD
     refusal (``PortRegistrationRefused``) so the inbound-auth resolver can never be
     runtime-swapped.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR; no app boot, no DB). ASCII-only prints.
Final line: ``IMPORT CONTRACTS OK``.
"""

import ast
import importlib
import importlib.abc
import importlib.machinery
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_import_contracts_")

from app import ports
from tests import _import_contracts as ic

_APP_DIR = os.path.join(_ROOT, "app")
_PROBE_MODULE = "app.ports._selftest_probe"


def _probe_contract():
    """The one ENFORCED row -- the planted-violation fixture."""
    row = next((c for c in ic.PORT_CONTRACTS if c.port == ic.PROBE_PORT), None)
    assert row is not None, "the probe contract row must exist in PORT_CONTRACTS"
    return row


def _reset_probe():
    """Clear all registry state for the probe port AND drop its module from sys.modules,
    so a following test's lazy import genuinely re-runs the module's self-registration.
    The suite runs as ONE process, so this cross-test isolation is load-bearing.

    The override slot is cleared via the SANCTIONED public ``unregister`` (no private-dict
    poke -- the pattern later trains should copy). The default slot must be popped directly
    because there is deliberately NO public way to undo a self-registration; that poke plus
    the sys.modules drop are test-teardown-only."""
    ports.unregister(ic.PROBE_PORT)
    ports._DEFAULTS.pop(ic.PROBE_PORT, None)
    sys.modules.pop(_PROBE_MODULE, None)


# ============================================================================
# A. the scanner + contract table
# ============================================================================
def test_anti_vacuity():
    row = _probe_contract()

    # Invocation 1 -- DETECTION: scan with an allowlist that OMITS the probe. The probe's
    # FIVE REAL planted imports must each surface it as an offender. This is what makes a
    # no-op guard (wrong target path, scanner not walking function bodies, a match form
    # silently broken) impossible to pass silently.
    offenders, _used = ic.scan_contract(_APP_DIR, _ROOT, row, allow_override={})
    hit = [o for o in offenders if o.startswith(ic.PROBE_FILE + ":")]
    assert hit, (
        "ANTI-VACUITY FAILURE: the scanner did NOT flag the planted forbidden import in "
        f"{ic.PROBE_FILE} (target {ic.PROBE_FORBIDDEN_TARGET!r}). The guard is vacuous -- check the module path / that ast.walk "  # noqa: E501
        f"reaches function-body imports.\n  offenders seen: {offenders!r}"
    )
    # Exactly FIVE offender lines, ALL from the probe: one per match form -- stem /
    # full-module / plain import (static) + relative (pkg_aware) + dynamic (match_dynamic).
    # The last two are the GUARD-OF-THE-GUARD (L7): they match ONLY while scan_contract
    # keeps pkg_aware=True and match_dynamic=True, so flipping either off drops the count
    # below five here. Fewer means a form's detection broke; extra-file offenders mean a
    # name collision on the synthetic target.
    assert offenders == hit and len(hit) == 5, (
        "the probe must be the SOLE offender with EXACTLY 5 planted-form hits (stem / "  # noqa: UP031
        "full-module / plain import / relative / dynamic); got %d total, %d from the "
        "probe:\n  %r" % (len(offenders), len(hit), offenders)
    )
    print(
        "  A1. anti-vacuity: scanner DETECTS all 5 planted import forms in the probe "
        "(sole offender, exactly 5 lines incl. relative+dynamic guard-of-the-guard) "
        "when the probe is omitted from the allowlist OK"
    )

    # Invocation 2 -- PRODUCTION allowlist (the row's own): the probe is a known fixture,
    # so ZERO offenders (green). Because the planted import is REAL, the allowlist entry is
    # USED -> not stale. Note the coupling: if the planted import were ever removed, BOTH
    # invocation 1's detection AND this stale check would fail loudly -- the fixture cannot
    # silently rot (check 11's stale-entry property, inherited).
    offenders2, used2 = ic.scan_contract(_APP_DIR, _ROOT, row)
    assert not offenders2, (
        f"the production probe row must be GREEN (probe allowlisted); offenders: {offenders2!r}"
    )
    assert used2 == {ic.PROBE_FILE}, (
        f"the probe allowlist entry must be USED (the planted import is real); used={used2!r}"
    )
    assert not ic.stale_entries(row, used2), "the probe row must have no stale allowlist entry"
    print(
        "  A2. anti-vacuity: production row is GREEN (probe allowlisted) AND its "
        "allowlist entry is USED, so removing the planted import fails loudly OK"
    )


def test_contract_table():
    probe_row = _probe_contract()
    assert not probe_row.pending, "the probe row must be ENFORCED (pending=False)"

    enforced = 0
    for c in ic.PORT_CONTRACTS:
        offenders, used = ic.scan_contract(_APP_DIR, _ROOT, c)
        stale = ic.stale_entries(c, used)
        # ALLOWLIST files must exist for EVERY row (a typo'd path silently never matches ->
        # a vacuous allowlist). Owner-file existence is asserted only for ENFORCED rows: a
        # PENDING row's owning port module is built by a LATER train and legitimately does
        # not exist yet.
        for k in set(c.allowlist):
            # K1 (H1): an allowlist key may be a bare rel path (whole-file grandfather) OR a
            # (rel, dotted) PAIR. Check the file element so os.path.join never sees a tuple.
            rel = k[0] if isinstance(k, tuple) else k
            assert os.path.exists(os.path.join(_ROOT, rel)), (
                f"contract {c.port!r} allowlists a missing file: {rel}"
            )
        if c.pending:
            # PENDING rows are REPORTED, never asserted -- they land green in the train
            # that builds the port. Show the still-open edges + any stale allowlist entry.
            print(
                "  B-pending [%s]: %d open edge(s), %d stale allowlist entry(ies), "  # noqa: UP031
                "enables in %s -- %s"
                % (c.port, len(offenders), len(stale), c.enabled_in or "?", c.note)
            )
            continue
        # ENFORCED: owner files must also exist; no offenders; no stale entry.
        for rel in c.owner:
            assert os.path.exists(os.path.join(_ROOT, rel)), (
                f"ENFORCED contract {c.port!r} names a missing owner file: {rel}"
            )
        assert not offenders, (
            "ENFORCED contract {!r} has UNCLASSIFIED importer(s) of {} -- add the port owner "
            "or an allowlist reason:\n  {}".format(c.port, c.target, "\n  ".join(sorted(offenders)))
        )
        assert not stale, (
            "ENFORCED contract {!r} allowlists file(s) that no longer import {} (stale): {}".format(
                c.port, c.target, ", ".join(sorted(stale))
            )
        )
        enforced += 1
    # At least one enforced row (the probe today). A later train flipping a pending row to
    # enforced must NOT break this -- so >=, not ==.
    assert enforced >= 1, "at least one contract row (the probe) must be ENFORCED; got 0"
    # List pending ports DYNAMICALLY so this line cannot go stale as trains flip rows
    # (Train 1 enabled TelemetrySink, Train 3 enabled BlobStore -- both now ENFORCED).
    _pending = [c.port for c in ic.PORT_CONTRACTS if c.pending]
    print(
        "  A3. contract table: %d ENFORCED row(s) green (no offenders, no stale) + "  # noqa: UP031
        "%d PENDING row(s) reported%s OK"
        % (enforced, len(_pending), (" (" + ", ".join(_pending) + ")") if _pending else "")
    )


def test_dynamic_and_relative_forms_detected():
    """Non-vacuity proof for the port scanner's two hardening arms (M2 dynamic, M3
    relative), and that BOTH stay OFF in check-11 mode (defaults) so check 11 is
    byte-identical."""
    mods, stems = {"app.services.storage"}, {"storage"}

    # M2 -- dynamic imports (the providers.py house style). Detected WITH match_dynamic,
    # ignored without it.
    src_dyn = (
        "def f():\n"
        "    __import__('app.services.storage')\n"
        "    importlib.import_module('app.services.storage')\n"
    )
    assert len(ic.import_hits(ast.parse(src_dyn), mods, stems, match_dynamic=True)) == 2, (
        "M2: both __import__ and import_module('app.services.storage') must be detected"
    )
    assert ic.import_hits(ast.parse(src_dyn), mods, stems) == [], (
        "M2: dynamic imports must be IGNORED in check-11 mode (flag off) -- byte-identity"
    )

    # M3 -- relative imports resolved via package context. Detected WITH pkg, ignored
    # without it. `from .storage import x` -> app.services.storage (mods branch);
    # `from . import storage` -> app.services + name storage (stem branch).
    pkg = ["app", "services"]
    src_rel = "def f():\n    from .storage import x\n    from . import storage\n"
    assert len(ic.import_hits(ast.parse(src_rel), mods, stems, pkg=pkg)) == 2, (
        "M3: both relative forms must resolve to app.services.storage and match"
    )
    assert ic.import_hits(ast.parse(src_rel), mods, stems) == [], (
        "M3: relative imports must be IGNORED without package context -- byte-identity"
    )
    print(
        "  A4. scanner hardening: dynamic (__import__/import_module) + relative "
        "(from .storage / from . import storage) imports detected under the port "
        "scanner's flags, IGNORED in check-11 mode OK"
    )


# ============================================================================
# B. the frozen registry
# ============================================================================
def test_freeze_lazy_resolution():
    # The frozen file must have NO knowledge of the probe port -- resolution is purely by
    # convention (importlib of app.ports.<port>). Prove it statically first.
    with open(os.path.join(_APP_DIR, "ports", "__init__.py"), encoding="utf-8") as fh:
        init_src = fh.read()
    assert ic.PROBE_PORT not in init_src, (
        "the frozen __init__.py must NOT name the probe port -- a NEW port must resolve "
        "without editing it"
    )

    _reset_probe()
    assert _PROBE_MODULE not in sys.modules, "precondition: probe module not yet imported"
    adapter = ports.get(ic.PROBE_PORT)  # forces the lazy importlib fallback + self-register
    assert getattr(adapter, "name", None) == "_selftest_probe_default", (
        f"get() must resolve the probe's self-registered DEFAULT via lazy import; got {adapter!r}"
    )
    # It landed in _DEFAULTS (self-registration), not _OVERRIDES.
    assert ic.PROBE_PORT in ports._DEFAULTS and ic.PROBE_PORT not in ports._OVERRIDES, (
        "a self-registered default must land in _DEFAULTS only"
    )
    print(
        "  B1. freeze: a NEW port module resolves via lazy importlib WITHOUT any edit to "
        "the frozen __init__.py (self-registers into _DEFAULTS) OK"
    )


def test_override_survives_forced_import():
    _reset_probe()
    assert _PROBE_MODULE not in sys.modules, "precondition: probe module not yet imported"
    sentinel = object()
    ports.register(ic.PROBE_PORT, sentinel)  # override set BEFORE the module is imported
    assert ic.PROBE_PORT not in ports._DEFAULTS, "no default yet -- module not imported"
    # Force the module import: its self-registration lands the DEFAULT in _DEFAULTS.
    importlib.import_module(_PROBE_MODULE)
    assert ic.PROBE_PORT in ports._DEFAULTS, "forced import must self-register the default"
    # Overrides-first still holds: the pre-set override wins over the later-landed default.
    assert ports.get(ic.PROBE_PORT) is sentinel, (
        "an override set BEFORE the port module imported must still win after the import "
        "forces the default in (clobber-prevention -- the suite-order determinism the "
        "TelemetrySink security tests depend on)"
    )
    _reset_probe()
    print(
        "  B2. clobber-prevention: an override set before a port module's import still "
        "wins after the import lands the default OK"
    )


def test_setdefault_no_clobber():
    _reset_probe()
    importlib.import_module(_PROBE_MODULE)  # default A self-registered
    first = ports._DEFAULTS[ic.PROBE_PORT]
    other = object()
    ports.register_default(ic.PROBE_PORT, other)  # a SECOND self-registration attempt
    assert ports._DEFAULTS[ic.PROBE_PORT] is first, (
        "setdefault semantics: a repeat self-registration must NOT clobber the first"
    )
    assert ports.get(ic.PROBE_PORT) is first
    # unregister removes an OVERRIDE and can NEVER touch _DEFAULTS: after register+
    # unregister, get() falls back to the untouched default (still `first`).
    ov = object()
    ports.register(ic.PROBE_PORT, ov)
    assert ports.get(ic.PROBE_PORT) is ov, "override must win while registered"
    ports.unregister(ic.PROBE_PORT)
    assert ports.get(ic.PROBE_PORT) is first, (
        "after unregister, get() must fall back to the default"
    )
    assert ports._DEFAULTS[ic.PROBE_PORT] is first, (
        "unregister must NEVER touch _DEFAULTS -- the default is still the first object"
    )
    _reset_probe()
    print(
        "  B3. setdefault: a second self-registration does not clobber the first; "
        "register+unregister falls back to the untouched default OK"
    )


def test_unknown_port_raises():
    try:
        ports.get("no_such_port_xyz")
    except ports.UnknownPort:
        pass
    else:
        raise AssertionError("get() on an unknown port must raise UnknownPort, never None")
    # A genuinely-broken port module (its OWN import chain fails) must NOT be masked as
    # UnknownPort -- that would hide real bugs. (No such module exists in-tree to exercise
    # live; the get() code distinguishes exc.name; documented here as the intended
    # contract.)
    print("  B4. miss: get() on an unknown port raises UnknownPort (never a silent None) OK")


def test_identity_refusal():
    # HARD refusal on the public override -- the security property phase 0b rests on.
    try:
        ports.register("identity", object())
    except ports.PortRegistrationRefused:
        pass
    else:
        raise AssertionError("register('identity', ...) must RAISE PortRegistrationRefused")
    # Defense in depth: the self-registration path refuses identity too, so NO write path
    # can install a swappable inbound-auth resolver.
    try:
        ports.register_default("identity", object())
    except ports.PortRegistrationRefused:
        pass
    else:
        raise AssertionError("register_default('identity', ...) must RAISE too")
    # unregister is a write path too: it must refuse identity (so it can never become a
    # backdoor to disturb the frozen port's slot).
    try:
        ports.unregister("identity")
    except ports.PortRegistrationRefused:
        pass
    else:
        raise AssertionError("unregister('identity') must RAISE too")
    assert "identity" not in ports._OVERRIDES and "identity" not in ports._DEFAULTS, (
        "a refused identity write must leave the registry untouched"
    )
    print(
        "  B5. identity: register()/register_default()/unregister('identity') all "
        "HARD-refuse (PortRegistrationRefused) -- the resolver is not runtime-swappable OK"
    )


class _RaisingLoader(importlib.abc.Loader):
    """A loader whose exec_module raises a chosen exception -- stands in for a port module
    whose OWN import body blows up."""

    def __init__(self, exc):
        self._exc = exc

    def create_module(self, spec):
        return None  # default module object

    def exec_module(self, module):
        raise self._exc


class _RaisingFinder(importlib.abc.MetaPathFinder):
    """A meta-path finder that claims ONE fullname and hands back a raising loader."""

    def __init__(self, fullname, exc):
        self._fullname = fullname
        self._exc = exc

    def find_spec(self, name, path=None, target=None):
        if name == self._fullname:
            return importlib.machinery.ModuleSpec(name, _RaisingLoader(self._exc))
        return None


def _get_via_raising_module(port, exc):
    """Resolve get(port) against a synthetic app.ports.<port> whose exec_module raises
    ``exc``; return whatever get() raised (or None)."""
    full = f"app.ports.{port}"
    finder = _RaisingFinder(full, exc)
    sys.modules.pop(full, None)
    sys.meta_path.insert(0, finder)
    try:
        try:
            ports.get(port)
        except BaseException as e:
            return e
        return None
    finally:
        try:  # noqa: SIM105
            sys.meta_path.remove(finder)
        except ValueError:
            pass
        sys.modules.pop(full, None)


def test_broken_port_module_not_masked():
    # (a) a port module whose OWN body imports a DIFFERENT missing dep -> that error must
    # PROPAGATE, never be masked as UnknownPort (it is a real bug in the port module).
    other = ModuleNotFoundError("No module named 'some_other_dep'", name="some_other_dep")
    got = _get_via_raising_module("brokenprobe_a", other)
    assert isinstance(got, ModuleNotFoundError) and got.name == "some_other_dep", (
        "a broken port module (missing OTHER dep) must propagate its error, not be masked; "
        f"got {got!r}"
    )
    assert not isinstance(got, ports.UnknownPort), "must NOT be masked as UnknownPort"

    # (b) POST-H1: even a ModuleNotFoundError naming the port's OWN BARE name (the
    # app/ports/redis.py -> `import redis` case) must PROPAGATE. Only name ==
    # "app.ports.<port>" (the port module itself genuinely absent) becomes UnknownPort --
    # the bare-name arm that used to mask this is gone.
    bare = ModuleNotFoundError("No module named 'redis'", name="redis")
    got_b = _get_via_raising_module("redis", bare)
    assert isinstance(got_b, ModuleNotFoundError) and got_b.name == "redis", (
        "post-H1: a module error naming the port's BARE name must propagate, not be masked; "
        f"got {got_b!r}"
    )
    assert not isinstance(got_b, ports.UnknownPort), (
        "post-H1: the bare-name arm must NOT mask a real bug as UnknownPort"
    )
    print(
        "  B6. broken-port-module: get() propagates a ModuleNotFoundError raised by a "
        "port module's OWN body (a different dep AND the port's bare name) -- only a "
        "genuinely absent app.ports.<port> becomes UnknownPort OK"
    )


def main():
    test_anti_vacuity()
    test_contract_table()
    test_dynamic_and_relative_forms_detected()
    test_freeze_lazy_resolution()
    test_override_survives_forced_import()
    test_setdefault_no_clobber()
    test_unknown_port_raises()
    test_identity_refusal()
    test_broken_port_module_not_masked()
    print("IMPORT CONTRACTS OK")


if __name__ == "__main__":
    main()
