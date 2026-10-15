"""loc-core translation-kernel facade -- suite for composable-core phase 4, Train K1.

Proves the load-bearing contracts of the ``app/kernel/`` alias-facade package + the K1
extensions to the import-contract scanner (see the SPEC-loc-core-kernel spec sec 3/4/6):

  KERNEL-IMPORT-GRAPH PURITY (fresh subprocesses -- the finder is lazy, so touching an alias
  resolves it):
    * FULL surface -- import app.kernel + touch the whole surface: NONE of fastapi/starlette/
      jinja2 and NO app.web/app.cli/app.backoffice in sys.modules (166 was the spec's RECORDED
      figure at its pin; NOT asserted as equality -- only the framework/shell ABSENCES are).
    * NARROWNESS BOUND -- import app.kernel.pipeline: smtplib/jwt/app.services.mailer/
      app.services.oidc/app.services.databricks_export NOT loaded, AND < 86 module families
      (app.filters.* format modules fold into one family; see test_narrowness_bound)
      (measured baseline ~60 pipeline vs ~277 full surface -- a real, non-vacuous ceiling).
    * BOOT-INTEGRATION -- import app.main (NOT app.kernel directly): after K2 migrated the web +
      backoffice shells onto app.kernel, boot now ROUTES THROUGH the facade, so 'app.kernel' IS
      in sys.modules and EXACTLY ONE _KernelAliasFinder is installed (the name-matched idempotence
      guard holds despite many shell modules importing it); alias identity holds post-boot. (The
      K1-era "dormant on a default install" property ended by design with K2 -- see the test.)

  ALIAS IDENTITY + IMPORT SYSTEM CONTRACTS (fresh subprocesses):
    * is-identity via ATTRIBUTE ACCESS (service/spine/port), the deep-import spelling, and the
      connectors depth-2 shared-package object.
    * DENY arm: app.kernel.connectors.base / app.kernel.ports._selftest_probe /
      app.kernel.nosuchmodule each raise ModuleNotFoundError with .name set; no stray
      app.kernel.connectors./app.kernel.ports. keys leak; and importlib.util.find_spec on an
      unmapped app.kernel.* RAISES (the disclosed F4-2 deviation, pinned so a return-None
      refactor re-opening the silent-duplicate hole goes red).
    * __spec__ preservation + live reload genuinely re-executes (module_from_spec's clobber
      restored -- else reload is a silent no-op and the lqa/orchestrator reload sites break).
    * alias key-sets (services/ports/spine) pairwise disjoint (the scheduler service-vs-port
      collision guard).
    * NO leaked publics (underscore-aliased imports + del'd installer): app.kernel's public
      attrs are exactly the surface (after touching it + importing pipeline).
    * EXACTLY ONE finder after a sys.modules purge + re-import (name-matched idempotence), and
      the finder is at sys.meta_path[0] on first install.

  SURFACE + RELOCATION:
    * SURFACE-DRIFT (spelling-agnostic): the AST union over app/web+app/cli+app/backoffice
      (BOTH app.services.X and app.kernel.X spellings) == _KERNEL_SURFACE union the two
      shell-facing modules -- survives K2/K3 migration.
    * mime relocation: app.services.mime.mime_for_fmt IS app.web.deps.mime_for_fmt (the
      re-export) and MIME_BY_FMT is byte-identical.

  SCANNER EXTENSIONS (in-process guard-of-the-guard):
    * the core-located reverse fixture is DETECTED on its own synthetic-target row when
      re-scanned with allow_override={} (while the real reverse row's allowlist stays EMPTY).
    * generalized parent-leaf detects 'from app.web import deps' AND its RELATIVE spelling
      'from ..web import deps'.
    * a planted un-grandfathered 'from app.services.X import y' shell import is an offender; a
      stale grandfather entry is reported; and for (file, module)-PAIR granularity a planted
      SECOND app.services import in deps.py (beyond its permanent trust_auth pair) IS reported,
      while a permanent pair whose specific import is deleted is reported STALE.

  F1 LIMITATION: this suite proves IMPORT purity only. It CANNOT detect the class-(c)
  duck-typed Request coupling (security/oidc/connectors.oauth) -- a call-contract fact
  recorded in the spec sec 2.3, not an import edge, not asserted here.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR; children inherit it). ASCII-only prints.
Final line: KERNEL FACADE OK.
"""

import ast
import os
import subprocess
import sys
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_kernel_facade_")

from tests import _import_contracts as ic

_APP_DIR = os.path.join(_ROOT, "app")


def _child(body, allow_fail=False):
    """Run a snippet in a FRESH interpreter (repo root importable, hermetic env inherited).
    Returns (rc, stdout, stderr). Asserts rc==0 unless allow_fail."""
    src = "import sys, os\n" + textwrap.dedent(body)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", src], cwd=_ROOT, env=env, capture_output=True, text=True, timeout=300
    )
    if not allow_fail and proc.returncode != 0:
        raise AssertionError(
            "child subprocess failed (rc=%d)\n--- stdout ---\n%s\n--- stderr ---\n%s"  # noqa: UP031
            % (proc.returncode, proc.stdout, proc.stderr[-3000:])
        )
    return proc.returncode, proc.stdout, proc.stderr


# ============================================================================
# 1. purity subprocesses
# ============================================================================
def test_full_surface_purity():
    _child("""
        import app.kernel as K
        for n in K._KERNEL_SURFACE:
            __import__("app.kernel." + n)
        for p in K._KERNEL_PORTS:
            __import__("app.kernel.ports." + p)
        for s in K._KERNEL_SPINE:
            __import__("app.kernel." + s)
        for banned in ("fastapi", "starlette", "jinja2"):
            assert banned not in sys.modules, "web framework leaked: " + banned
        shells = [m for m in sys.modules
                  if m == "app.web" or m.startswith("app.web.")
                  or m == "app.cli" or m.startswith("app.cli.")
                  or m == "app.backoffice" or m.startswith("app.backoffice.")]
        assert not shells, "shell modules leaked into the kernel surface: %r" % shells[:8]
        n_app = len([m for m in sys.modules if m.startswith("app.")])
        print("FULL-SURFACE app.* modules (recorded, not asserted):", n_app)
    """)
    print(
        "  1a. full-surface purity: whole kernel surface imports with NO "
        "fastapi/starlette/jinja2 and NO app.web/cli/backoffice OK"
    )


def test_narrowness_bound():
    _child("""
        import app.kernel.pipeline  # a REAL submodule -> the narrow engine surface
        for banned in ("smtplib", "jwt", "app.services.mailer", "app.services.oidc",
                       "app.services.databricks_export"):
            assert banned not in sys.modules, "pipeline pulled an excluded leaf: " + banned
        for banned in ("fastapi", "starlette", "jinja2"):
            assert banned not in sys.modules, "pipeline pulled a web framework: " + banned
        import re
        app_mods = [m for m in sys.modules if m.startswith("app.")]
        # SRP pure-move pieces (2026-09-14: <mod>_common / _partN / _leafN / _hubN) fold into
        # their parent module: the bound counts responsibilities reached, not files.
        families = {re.sub(r"_(?:common|part\\d+|leaf\\d*|hub\\d*)$", "", m) for m in app_mods}
        # File-format filters (2026-10-10, formats parity batches): app/filters/__init__ imports
        # every format module to fill the registry, so each new file format is one more module
        # but not one more responsibility. They fold into ONE family, and the ceiling dropped by
        # the same amount (98 families with 16 filter modules unfolded -> 83 folded; < 100
        # became < 85), so the bound is exactly as tight for everything else. Open-core S1
        # (2026-10-14) added ONE core family on the pipeline path, app.extensions (the optional
        # add-on registry; app.editions is not loaded by the pipeline): < 85 became < 86.
        families = {"app.filters" if f.startswith("app.filters.") else f for f in families}
        n_app = len(families)
        assert n_app < 86, "pipeline surface is not narrow: %d module families (>= 86)" % n_app
        print("PIPELINE app.* modules:", len(app_mods), "families:", n_app)
    """)
    print(
        "  1b. narrowness bound: app.kernel.pipeline excludes smtplib/jwt/mailer/oidc/"
        "databricks_export and stays < 100 app.* modules OK"
    )


def test_boot_purity():
    # K2 (composable-core phase 4) migrated the web + backoffice shells onto app.kernel, so the
    # facade is now on the DEFAULT boot path: importing app.main mounts the web routers whose
    # module-scope imports (e.g. app.web.deps' `from app.kernel import ...`) pull app.kernel and
    # install the finder. The K1-era "dormant on a default install" property ended by design with
    # K2; the surviving invariant is that boot installs EXACTLY ONE finder (idempotence holds even
    # though many shell modules import the facade) and aliasing still resolves to the real module.
    _child("""
        import app.main  # the composition root -- now routes through app.kernel via the shells
        assert "app.kernel" in sys.modules, \\
            "boot did NOT import the facade (K2 shells are expected to route through it)"
        finders = [f for f in sys.meta_path if type(f).__name__ == "_KernelAliasFinder"]
        assert len(finders) == 1, \\
            "boot installed %d kernel finders (expected exactly 1)" % len(finders)
        import app.kernel.tm, app.services.tm
        assert app.kernel.tm is app.services.tm, "post-boot alias identity broken"
        print("BOOT-INTEGRATION ok: app.main imported, app.kernel present, exactly 1 finder, "
              "alias identity holds")
    """)
    print(
        "  1c. boot-integration: import app.main pulls app.kernel (K2 shells route through the "
        "facade) and installs EXACTLY ONE finder; alias identity holds post-boot OK"
    )


# ============================================================================
# 2. alias identity + import-system contracts
# ============================================================================
def test_is_identity():
    _child("""
        import app.kernel
        import app.kernel.tm, app.services.tm
        assert hasattr(app.kernel, "tm"), "alias not bound as a package attribute"
        assert app.kernel.tm is app.services.tm, "service alias is not the real module"
        # spine
        import app.kernel.config, app.config
        assert app.kernel.config is app.config, "spine alias broken"
        # port (namespaced)
        import app.kernel.ports.telemetry, app.ports.telemetry
        assert app.kernel.ports is app.ports
        assert app.kernel.ports.telemetry is app.ports.telemetry, "port alias broken"
        # scheduler service-vs-port collision -> distinct objects, both correct
        import app.kernel.scheduler, app.services.scheduler
        import app.kernel.ports.scheduler, app.ports.scheduler
        assert app.kernel.scheduler is app.services.scheduler
        assert app.kernel.ports.scheduler is app.ports.scheduler
        assert app.kernel.scheduler is not app.kernel.ports.scheduler
        # deep-import spelling binds the SAME symbol (proves the 81-stmt migration is neutral)
        from app.kernel.tm import LocaleMismatchError as km
        from app.services.tm import LocaleMismatchError as rm
        assert km is rm, "deep-import symbol differs across the alias"
        # connectors depth-2 shared-package object (the facade aliases the PACKAGE, not the
        # sub directly): app.kernel.connectors IS app.services.connectors, so after importing
        # the real sub it is reachable by attribute access through the shared package object.
        import app.kernel.connectors, app.services.connectors.base
        assert app.kernel.connectors is app.services.connectors
        assert app.kernel.connectors.base is app.services.connectors.base
        print("identity ok; meta_path[0]=", type(sys.meta_path[0]).__name__)
    """)
    print(
        "  2a. is-identity: service/spine/port aliases ARE the real modules (attribute "
        "access + deep-import symbol + scheduler collision + connectors depth-2) OK"
    )


def test_deny_arm():
    _child("""
        import app.kernel, importlib.util
        for nm in ("app.kernel.connectors.base", "app.kernel.ports._selftest_probe",
                   "app.kernel.nosuchmodule"):
            try:
                __import__(nm)
            except ModuleNotFoundError as e:
                assert e.name == nm, "wrong .name: %r != %r" % (e.name, nm)
            else:
                raise AssertionError("deny arm did NOT raise for " + nm)
        stray = [k for k in sys.modules
                 if (k.startswith("app.kernel.connectors.")
                     or k.startswith("app.kernel.ports."))]
        # the 6 mapped ports are the ONLY app.kernel.ports.* keys allowed (none imported here)
        assert not stray, "stray phantom alias submodule leaked: %r" % stray
        # F4-2 disclosed deviation: find_spec on an unmapped app.kernel.* RAISES (not None)
        try:
            importlib.util.find_spec("app.kernel.nosuchmodule")
        except ModuleNotFoundError as e:
            assert e.name == "app.kernel.nosuchmodule"
        else:
            raise AssertionError("find_spec must RAISE (F4-2), not return None/spec")
        # control: a normal missing name still returns None
        assert importlib.util.find_spec("app.services.nosuch_zzz") is None
        print("deny arm ok; no stray keys; find_spec raises on unmapped app.kernel.*")
    """)
    print(
        "  2b. deny arm: connectors.base / ports._selftest_probe / nosuch all raise "
        "ModuleNotFoundError, no stray alias keys, find_spec RAISES (F4-2 pinned) OK"
    )


def test_spec_preserved_and_reload():
    _child("""
        import importlib, app.kernel, app.kernel.tm, app.services.tm as t
        assert t.__name__ == "app.services.tm", t.__name__
        assert t.__spec__.name == "app.services.tm", t.__spec__.name
        assert t.__spec__.origin and t.__spec__.origin.endswith("tm.py"), t.__spec__.origin
        assert sys.modules["app.kernel.tm"] is t
        # reload genuinely re-executes: overwrite a name the module BODY assigns, reload,
        # assert it is restored (F4-3: a newly-ADDED external attr would SURVIVE a genuine
        # re-exec, so it cannot serve as the sentinel -- mutate an EXISTING body constant).
        name, orig = None, None
        for k, v in list(vars(t).items()):
            if (not k.startswith("_") and isinstance(v, (str, int, float, bool, tuple))
                    and not callable(v)):
                name, orig = k, v
                break
        assert name is not None, "no simple body constant found to use as the reload sentinel"
        setattr(t, name, "___DIRTY_KERNEL_SENTINEL___")
        importlib.reload(t)
        assert getattr(t, name) == orig, (
            "reload was a SILENT NO-OP: body constant %r not restored (%r)" % (name, getattr(t, name)))
        assert sys.modules["app.services.tm"] is t, "reload replaced the module object"
        # reload via the ALIAS object too (same underlying module)
        importlib.reload(sys.modules["app.kernel.tm"])
        print("reload re-executed via body constant %r; __spec__ preserved" % name)
    """)  # noqa: E501
    print(
        "  2c. __spec__ preserved + live reload genuinely re-executes (module_from_spec "
        "clobber restored -- the lqa/orchestrator reload sites stay live) OK"
    )


def test_alias_keyset_disjoint():
    _child("""
        import app.kernel as K
        services = {f"app.kernel.{n}" for n in K._KERNEL_SURFACE}
        ports = {f"app.kernel.ports.{p}" for p in K._KERNEL_PORTS}
        spine = {f"app.kernel.{s}" for s in K._KERNEL_SPINE}
        assert not (services & ports), services & ports
        assert not (services & spine), services & spine
        assert not (ports & spine), ports & spine
        # every derived key is present in the committed _ALIAS_MAP
        for s in services | ports | spine:
            assert s in K._ALIAS_MAP, "missing alias key: " + s
        print("alias key-sets pairwise disjoint: services=%d ports=%d spine=%d"
              % (len(services), len(ports), len(spine)))
    """)
    print(
        "  2d. alias key-sets (services/ports/spine) pairwise disjoint -- the scheduler "
        "service-vs-port collision (and any future one) cannot silently drop an alias OK"
    )


def test_no_leaked_publics():
    _child("""
        import app.kernel as K
        for n in K._KERNEL_SURFACE:
            __import__("app.kernel." + n)
        for s in K._KERNEL_SPINE:
            __import__("app.kernel." + s)
        for p in K._KERNEL_PORTS:
            __import__("app.kernel.ports." + p)
        import app.kernel.pipeline  # F4-5: pipeline becomes a public attr only once imported
        pub = {a for a in vars(K) if not a.startswith("_")}
        expected = set(K._KERNEL_SURFACE) | set(K._KERNEL_SPINE) | {"ports", "pipeline"}
        assert pub == expected, ("public-attr drift; extra=%r missing=%r"
                                 % (sorted(pub - expected), sorted(expected - pub)))
        # no leaked module-globals (underscore-aliased imports + del'd installer)
        assert not (pub & {"sys", "os", "importlib", "abc", "util"}), pub
        print("public attrs == surface (%d): no leaked sys/importlib/os" % len(pub))
    """)
    print(
        "  2e. no leaked publics: app.kernel's public attrs are exactly the surface leaf "
        "names + {ports, pipeline}; underscore-aliased imports do not leak OK"
    )


def test_one_finder_after_purge():
    _child("""
        import app.kernel
        assert type(sys.meta_path[0]).__name__ == "_KernelAliasFinder", "finder not at front"
        n1 = sum(type(f).__name__ == "_KernelAliasFinder" for f in sys.meta_path)
        for k in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
            del sys.modules[k]
        import app.kernel as k2  # noqa: F401 -- re-execute the module, rebinding the class
        n2 = sum(type(f).__name__ == "_KernelAliasFinder" for f in sys.meta_path)
        assert (n1, n2) == (1, 1), (n1, n2)
        print("finders before/after purge+reimport:", n1, n2)
    """)
    print(
        "  2f. exactly ONE finder after a sys.modules purge + re-import (name-matched "
        "idempotence -- an isinstance guard would have stacked a second) + front-inserted OK"
    )


# ============================================================================
# 3. surface drift + mime relocation
# ============================================================================
def _shell_union_both_spellings():
    """AST union over app/web+app/cli+app/backoffice of service leaf names, counting BOTH the
    app.services.X and app.kernel.X spellings (spelling-agnostic -- survives K2/K3)."""
    union = set()
    for sub in ("app/web", "app/cli", "app/backoffice"):
        base = os.path.join(_ROOT, sub)
        for root, _d, fns in os.walk(base):
            for fn in sorted(fns):
                if not fn.endswith(".py"):
                    continue
                tree = ast.parse(open(os.path.join(root, fn), encoding="utf-8").read())  # noqa: SIM115
                for n in ast.walk(tree):
                    if isinstance(n, ast.ImportFrom):
                        m = n.module or ""
                        for parent in ("app.services", "app.kernel"):
                            if m == parent:
                                union |= {a.name for a in n.names}
                            elif m.startswith(parent + "."):
                                union.add(m.split(".")[2])
                    elif isinstance(n, ast.Import):
                        for a in n.names:
                            for parent in ("app.services", "app.kernel"):
                                if a.name.startswith(parent + "."):
                                    union.add(a.name.split(".")[2])
    return union


def test_surface_drift():
    import app.kernel as K

    union = _shell_union_both_spellings()
    expected = set(K._KERNEL_SURFACE) | {"trust_auth", "vendor_studio_sso"}
    assert union == expected, (
        "SURFACE DRIFT -- regenerate _KERNEL_SURFACE in app/kernel/__init__.py\n"
        f"  in shells but not surface(+shell-facing): {sorted(union - expected)!r}\n"
        f"  in surface(+shell-facing) but not shells: {sorted(expected - union)!r}"
    )
    print(
        "  3a. surface-drift (spelling-agnostic): shell service union (%d) == "  # noqa: UP031
        "_KERNEL_SURFACE + {trust_auth, vendor_studio_sso} OK" % len(union)
    )


def test_mime_relocation():
    _child("""
        import app.services.mime as core_mime
        import app.web.deps as deps
        assert deps.mime_for_fmt is core_mime.mime_for_fmt, "web/deps re-export is not identity"
        assert deps.MIME_BY_FMT is core_mime.MIME_BY_FMT, "MIME_BY_FMT re-export differs"
        assert deps.DEFAULT_MIME == core_mime.DEFAULT_MIME
        # a couple of concrete values (byte-identity of the delivery bundle Content-Type)
        assert core_mime.mime_for_fmt("docx").startswith("application/vnd.openxml")
        assert core_mime.mime_for_fmt("nope") == core_mime.DEFAULT_MIME
        # delivery.py now sources it from core, not from the shell
        import app.services.delivery  # must import with no app.web edge
        print("mime relocation ok; web/deps re-exports the core object")
    """)
    print(
        "  3b. mime relocation: app.services.mime.mime_for_fmt IS app.web.deps.mime_for_fmt "
        "(wrap-don't-move re-export), MIME_BY_FMT byte-identical OK"
    )


# ============================================================================
# 4. scanner extensions -- guard-of-the-guard (in-process)
# ============================================================================
def _row(port):
    row = next((c for c in ic.PORT_CONTRACTS if c.port == port), None)
    assert row is not None, "missing contract row: " + port
    return row


def test_reverse_fixture_detected():
    # the fixture's OWN row is green normally (fixture allowlisted, entry used).
    frow = _row("reverse_selftest_probe")
    offenders, used = ic.scan_contract(_APP_DIR, _ROOT, frow)
    assert not offenders, f"reverse fixture row must be green normally: {offenders!r}"
    assert used == {ic.REVERSE_PROBE_FILE}, used
    assert not ic.stale_entries(frow, used), "reverse fixture entry must be used (not stale)"
    # re-scanned with an EMPTY allowlist, the core-located fixture IS reported (the scanner
    # genuinely fires on a core->app.web-spelled edge).
    off2, _ = ic.scan_contract(_APP_DIR, _ROOT, frow, allow_override={})
    hit = [o for o in off2 if o.startswith(ic.REVERSE_PROBE_FILE + ":")]
    assert hit and off2 == hit, (
        f"the reverse fixture must be the SOLE offender when un-allowlisted; got {off2!r}"
    )
    # meanwhile the REAL reverse row stays green with an EMPTY allowlist.
    rrow = _row("reverse_core_no_shell")
    assert rrow.allowlist == {}, "the real reverse row must keep an EMPTY allowlist"
    roff, _ = ic.scan_contract(_APP_DIR, _ROOT, rrow)
    assert not roff, f"the real reverse row must be green (no core->shell edge): {roff!r}"
    print(
        "  4a. reverse fixture: detected on its own synthetic-target row under "
        "allow_override={} while the real reverse row's EMPTY allowlist stays green OK"
    )


def test_generalized_parent_leaf():
    prefixes = ("app.web",)
    # 'from app.web import deps' -- the parent-leaf spelling for a NON-app.services parent.
    src_abs = "from app.web import deps\n"
    hits = ic.import_hits(ast.parse(src_abs), set(), set(), prefixes=prefixes)
    assert any(dotted == "app.web.deps" for (_l, _leaf, dotted) in hits), (
        f"generalized parent-leaf missed 'from app.web import deps': {hits!r}"
    )
    # RELATIVE spelling 'from ..web import deps' from a core package (pkg_aware path).
    src_rel = "def f():\n    from ..web import deps\n"
    hits_rel = ic.import_hits(
        ast.parse(src_rel), set(), set(), pkg=["app", "services"], prefixes=prefixes
    )
    assert any(dotted == "app.web.deps" for (_l, _leaf, dotted) in hits_rel), (
        f"generalized parent-leaf missed relative 'from ..web import deps': {hits_rel!r}"
    )
    # ...and BOTH are invisible without the prefix arm (byte-identity for check-11 mode)
    assert ic.import_hits(ast.parse(src_abs), set(), set()) == []
    assert ic.import_hits(ast.parse(src_rel), set(), set(), pkg=["app", "services"]) == []
    print(
        "  4b. generalized parent-leaf: 'from app.web import deps' AND relative "
        "'from ..web import deps' detected (invisible in check-11 mode) OK"
    )


def test_forward_ratchet_and_pairs():
    web = _row("forward_web")
    # the row is green as committed (all shell importers grandfathered, none stale).
    offenders, used = ic.scan_contract(_APP_DIR, _ROOT, web)
    assert not offenders, f"forward_web has unlisted offenders: {offenders[:8]!r}"
    assert not ic.stale_entries(web, used), (
        f"forward_web has stale entries: {sorted(ic.stale_entries(web, used))!r}"
    )

    # (i) a NEW unlisted shell import of app.services.X is an OFFENDER (ratchet forward). Post-K2
    #     the web row has NO bare/temp grandfathers left (all migrated to app.kernel), so we poke a
    #     surviving PERMANENT pair: drop deps.py's trust_auth pair and confirm its still-present
    #     app.services import surfaces -- proving an unlisted shell app.services import is caught.
    victim = ("app/web/deps.py", "app.services.trust_auth")
    assert victim in web.allowlist, f"precondition: {victim!r} is a permanent grandfather pair"
    reduced = {k: v for k, v in web.allowlist.items() if k != victim}
    off_new, _ = ic.scan_contract(_APP_DIR, _ROOT, web, allow_override=reduced)
    assert any(o.startswith("app/web/deps.py:") for o in off_new), (
        f"dropping deps.py's trust_auth pair must surface it as an offender; got {off_new[:8]!r}"
    )

    # (ii) a STALE entry (a grandfathered file that no longer imports app.services) is
    #      reported: add a bogus file that imports nothing forbidden.
    stale_probe = "app/web/deps.py"  # exists, but we key a NONSENSE pair that is never imported
    plus = dict(web.allowlist)
    plus[(stale_probe, "app.services.__nonexistent_never_imported__")] = "bogus"
    _off, used2 = ic.scan_contract(_APP_DIR, _ROOT, web, allow_override=plus)
    stale = set(plus) - used2
    assert (stale_probe, "app.services.__nonexistent_never_imported__") in stale, (
        f"a grandfather PAIR whose import does not exist must be reported STALE; got {stale!r}"
    )

    # (iii) PAIR granularity: a file carrying MULTIPLE permanent pairs is enforced PER-IMPORT, not
    #       wholesale. connectors.py holds two permanent connectors sub-depth pairs
    #       (connectors.base + connectors.wordpress); remove ONLY the base pair and confirm the
    #       (still-present) connectors.base import surfaces while the wordpress pair stays exempt --
    #       proving the file is NOT wholesale-in or wholesale-out.
    minus_one = {
        k: v
        for k, v in web.allowlist.items()
        if k != ("app/web/connectors.py", "app.services.connectors.base")
    }
    off_pair, _ = ic.scan_contract(_APP_DIR, _ROOT, web, allow_override=minus_one)
    assert any(
        o.startswith("app/web/connectors.py:") and o.endswith("imports base") for o in off_pair
    ), (
        "removing ONE connectors.py pair must surface that import (pairs are per-import, not "
        f"whole-file); got {off_pair[:8]!r}"
    )
    assert not any("wordpress" in o for o in off_pair), (
        f"the permanent connectors.wordpress pair must remain exempt: {off_pair!r}"
    )

    # (iv) a permanent PAIR whose specific import is DELETED goes STALE (migrated pair cannot
    #      linger). Simulate: key a permanent-style pair for a module deps.py does NOT import.
    perm_deleted = dict(web.allowlist)
    perm_deleted[("app/web/deps.py", "app.services.__migrated_away__")] = "was permanent"
    _o, used4 = ic.scan_contract(_APP_DIR, _ROOT, web, allow_override=perm_deleted)
    assert ("app/web/deps.py", "app.services.__migrated_away__") in (set(perm_deleted) - used4), (
        "a deleted permanent pair must be STALE"
    )
    print(
        "  4c. forward ratchet + pairs: green as committed; an unlisted app.services import "
        "offends (dropped deps.py trust_auth pair); a stale entry reports; connectors.py pairs "
        "are PER-IMPORT (wordpress stays exempt while base surfaces); a deleted pair goes stale OK"
    )


def main():
    test_full_surface_purity()
    test_narrowness_bound()
    test_boot_purity()
    test_is_identity()
    test_deny_arm()
    test_spec_preserved_and_reload()
    test_alias_keyset_disjoint()
    test_no_leaked_publics()
    test_one_finder_after_purge()
    test_surface_drift()
    test_mime_relocation()
    test_reverse_fixture_detected()
    test_generalized_parent_leaf()
    test_forward_ratchet_and_pairs()
    print("KERNEL FACADE OK")


if __name__ == "__main__":
    main()
