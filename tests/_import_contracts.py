"""Shared import-contract scanner for the port harness (composable-core phase 1).

NOT a ``verify_*.py`` suite -- the leading underscore keeps it out of
``tests/test_verify_suites.py``'s ``verify_*.py`` glob (precedent: ``tests/_bootstrap.py``,
``tests/_csrf.py``). It is imported BY suites: ``tests/verify_import_contracts.py`` (the
port contract table) and ``tests/verify_features.py`` check 11 (cross-feature reach),
which share ONE AST scanner so a fix to the walker benefits both.

WHAT IT DOES
------------
``import_hits(tree, mods, stems)`` walks a parsed module with ``ast.walk`` (so it catches
lazy / function-body imports -- exactly the form the port call sites use) and reports
every import of a forbidden target in THREE forms:

  * ``from app.services.X import ...``   (module in ``mods``)
  * ``from app.services import X``        (module == ``"app.services"``, leaf in ``stems``)
    -- the form ALL six storage call sites and all ``prompt_tap`` importers use
  * ``import app.services.X``             (dotted name in ``mods``)

The ``from <parent> import <leaf>`` form is matched only for the ``app.services`` parent,
which covers every phase-1 port target (all are ``app.services.*``) and is EXACTLY what
``verify_features.py`` check 11 has always done -- this module is that scanner, extracted
verbatim so check 11's behavior is unchanged.

``import_hits`` additionally carries TWO opt-in hardening arms, both DEFAULT-OFF:
``pkg_aware`` resolves RELATIVE imports (``from .storage import x``) to absolute names, and
``match_dynamic`` catches DYNAMIC imports (``__import__("app.services.X")`` /
``importlib.import_module(...)``). ``scan_contract`` engages BOTH for every contract row,
so an enforced row cannot be evaded by a relative or dynamic import; ``verify_features.py``
check 11 calls the scanner at DEFAULTS (both arms off) BY DESIGN, which is what keeps it
byte-identical. (The ``_selftest_probe`` fixture plants a relative and a dynamic form as a
guard-of-the-guard so neither arm can be silently disabled.)

``Contract`` + ``scan_contract(...)`` build the per-port mechanism on top: each row
forbids one target module to every importer except its owning port module(s) and a listed
allowlist-with-reasons; a listed file that no longer imports the target is a STALE entry
(anti-vacuity, inherited from check 11). The SUITE
(``tests/verify_import_contracts.py``) asserts ENFORCED rows (no offenders, no stale
entry) and merely reports PENDING rows -- a row seeded before its port is built / its call
sites migrated, so it can be seeded in this train and enabled -- landing green -- in the
train that creates its port.

APPEND-ONLY: each later port train adds its ``Contract(...)`` row to ``PORT_CONTRACTS``
below (BlobStore enabled in Train 3, TelemetrySink enabled in Train 1). It never edits the
frozen ``app/ports/__init__.py``.
"""

import ast
import os

# --------------------------------------------------------------------------------------
# Low-level scanner -- extracted verbatim from verify_features.py check 11 so the two
# guards share ONE walker with identical semantics.
# --------------------------------------------------------------------------------------


def _record_from(
    module_str, names, lineno, mods, stems, hits, prefixes=(), stem_parents=("app.services",)
):
    """Apply the ``from`` match to a (possibly resolved) module string. Used for BOTH
    absolute and relative ImportFrom, so the two share one matcher.

    Each hit is a 3-tuple ``(lineno, leaf, dotted)`` (K1 -- composable-core phase 4). The
    THIRD element is the RESOLVED DOTTED TARGET, needed for ``(file, module)``-pair allowlist
    granularity (H1). Element ``[1]`` stays the leaf so every existing 2-tuple reader keeps
    working after ``_vendor_import_hits`` drops the third (check 11 is byte-identical).

    ``prefixes`` (K1 extension 1 -- PREFIX/glob targets, e.g. ``("app.services",)`` for the
    forward rows, ``("app.web", "app.cli", ...)`` for the reverse row) is DEFAULT-EMPTY, so
    with ``prefixes=()`` the two original branches below are the only ones that fire and the
    matcher is byte-for-byte the check-11 matcher (plus the inert third tuple element)."""
    if module_str in mods:
        hits.append((lineno, module_str.rsplit(".", 1)[-1], module_str))
    elif module_str in stem_parents and stems:
        # ``from <parent> import <leaf>`` for a stem-parent. DEFAULT ``("app.services",)`` keeps
        # every existing caller (port rows, check-11's app.services scan) byte-identical. K2
        # opts the feature cross-reach scan into ``("app.services", "app.kernel")`` so a shell
        # that reaches a feature module via the ``from app.kernel import X`` facade spelling is
        # still seen -- the migration must not blind the guard (mirrors the F-5 drift test).
        for nm in names:
            if nm in stems:
                hits.append((lineno, nm, module_str + "." + nm))
    elif prefixes:
        # K1 ext-1: a PREFIX target subtree. F-10 branch-order rule -- a from-parent EXACTLY
        # equal to a prefix root (``from app.services import tm`` / ``from app.web import
        # deps``) routes to the STEMS arm (ANY leaf under the forbidden subtree), reporting
        # the leaf; only a parent STRICTLY UNDER the root (``from app.services.connectors.base
        # import X``) routes to the mods/under arm. (The exact-root case must NOT collapse to
        # the bare package leaf.)
        if module_str in prefixes:
            for nm in names:
                hits.append((lineno, nm, module_str + "." + nm))
        elif any(module_str.startswith(p + ".") for p in prefixes):
            hits.append((lineno, module_str.rsplit(".", 1)[-1], module_str))


def _abs_relative_module(pkg, level, module):
    """Resolve a relative ImportFrom (``level`` > 0) to its absolute dotted module string.

    ``pkg`` = the importing file's package as parts (e.g. ``["app", "services"]``).
    ``from . import x`` (level 1, module None) -> ``"app.services"``; ``from .storage
    import x`` -> ``"app.services.storage"``; ``from ..web import y`` (level 2) ->
    ``"app.web"``."""
    up = level - 1
    base = pkg[: len(pkg) - up] if len(pkg) >= up else []
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _prefix_hit(name, prefixes):
    """True iff dotted ``name`` is a prefix root or strictly under one (K1 ext-1)."""
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def import_hits(
    tree, mods, stems, pkg=None, match_dynamic=False, prefixes=(), stem_parents=("app.services",)
):
    """Every import of a forbidden target in ``tree``, as ``(lineno, leaf, dotted)`` triples.

    ``mods`` = full dotted module paths (e.g. ``{"app.services.prompt_tap"}``); ``stems``
    = their leaf names (for the ``from app.services import <leaf>`` form); ``prefixes`` =
    PREFIX-target roots (K1 ext-1, e.g. ``("app.services",)`` / the reverse shells).

    THIRD tuple element (K1): the RESOLVED DOTTED TARGET, for ``(file, module)``-pair
    allowlisting (H1). Element ``[1]`` stays the leaf so 2-tuple readers are unaffected once
    ``_vendor_import_hits`` drops the third -- check 11 stays byte-identical.

    With ALL optional args at their defaults (``pkg=None``, ``match_dynamic=False``,
    ``prefixes=()``) the MATCHING semantics are byte-for-byte check 11's original
    ``_vendor_import_hits`` (only the inert third tuple element is added) -- check 11 calls it
    exactly that way. The port contract scanner opts INTO the hardening arms:
      * ``pkg`` set (M3): RELATIVE imports (``from .storage import x``) are resolved to
        their absolute dotted name before matching -- app/ has ~85 relative-import sites
        today, so an ``app/services`` module adopting the style cannot evade the guard.
      * ``match_dynamic`` (M2): dynamic imports ``__import__("app.services.X", ...)`` and
        ``importlib.import_module("app.services.X")`` (the house style already in
        providers.py) are matched on a constant-string first arg.
      * ``prefixes`` (K1 ext-1): a whole subtree is forbidden (``app.services.*`` forward;
        ``app.web*``/``app.cli*``/... reverse), so the ``import``/dynamic/``from`` arms also
        match any name at-or-under a prefix root."""
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            names = [a.name for a in n.names]
            if n.level and pkg is not None:
                module_str = _abs_relative_module(pkg, n.level, n.module)
            else:
                # Absolute import, OR relative with no package context (check-11 mode):
                # a relative module name matches nothing -- byte-identical to the original.
                module_str = n.module or ""
            _record_from(
                module_str, names, n.lineno, mods, stems, hits, prefixes, stem_parents=stem_parents
            )
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name in mods or (prefixes and _prefix_hit(a.name, prefixes)):
                    hits.append((n.lineno, a.name.rsplit(".", 1)[-1], a.name))
        elif match_dynamic and isinstance(n, ast.Call):
            f = n.func
            dyn = (isinstance(f, ast.Name) and f.id == "__import__") or (
                isinstance(f, ast.Attribute) and f.attr == "import_module"
            )
            if dyn and n.args:
                a0 = n.args[0]
                if (
                    isinstance(a0, ast.Constant)
                    and isinstance(a0.value, str)
                    and (a0.value in mods or (prefixes and _prefix_hit(a0.value, prefixes)))
                ):
                    hits.append((n.lineno, a0.value.rsplit(".", 1)[-1], a0.value))
    return hits


def _in_subjects(rel, subject_prefixes):
    """K1 ext-2: does ``rel`` fall under a row's ``subject_prefixes``? A prefix ending in
    ``/`` matches a directory subtree; any other prefix matches that exact file. ``None``
    means the row governs the whole ``app_dir`` (the pre-K1 behavior of every existing row)."""
    for p in subject_prefixes:
        if p.endswith("/"):
            if rel.startswith(p):
                return True
        elif rel == p:
            return True
    return False


def cross_reach_scan(
    app_dir,
    root_dir,
    mods,
    stems,
    own,
    allow,
    pkg_aware=False,
    match_dynamic=False,
    prefixes=(),
    subject_prefixes=None,
    stem_parents=("app.services",),
):
    """Walk ``app_dir``; return ``(offenders, used)``.

    A file (path relative to ``root_dir``) that imports a forbidden target is an OFFENDER
    unless it is an ``own`` file (skipped) or grandfathered in ``allow``; a matched ``allow``
    KEY is recorded in ``used`` (so the caller can flag the un-used = STALE ones). Extracted
    from check 11's ``_cross_reach_scan`` (``root_dir`` parametrized); ``pkg_aware`` +
    ``match_dynamic`` thread the M2/M3 hardening and default OFF so check 11 stays
    byte-identical. K1 adds:
      * ``prefixes`` (ext-1) -- PREFIX/glob targets threaded into ``import_hits``.
      * ``subject_prefixes`` (ext-2) -- restricts which files the row governs (a row's
        subjects are filtered inside this single-root walk; ``None`` = whole ``app_dir``).
      * ``(file, module)``-PAIR allowlisting (ext-4/H1) -- ``allow`` keys may be a bare
        ``rel`` (whole-file grandfather, existing behavior) OR a ``(rel, dotted)`` pair
        (grandfathers only that file's import of that one module). A hit is skipped iff
        ``rel in allow`` OR ``(rel, dotted) in allow``; ``used`` is a MIXED-key set of the
        matched keys, so ``stale_entries`` covers pair keys too."""
    offenders, used = [], set()
    for root, _dirs, files in os.walk(app_dir):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), root_dir).replace(os.sep, "/")
            if subject_prefixes is not None and not _in_subjects(rel, subject_prefixes):
                continue
            if rel in own:
                continue
            with open(os.path.join(root, fn), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            pkg = rel.split("/")[:-1] if pkg_aware else None
            hits = import_hits(
                tree,
                mods,
                stems,
                pkg=pkg,
                match_dynamic=match_dynamic,
                prefixes=prefixes,
                stem_parents=stem_parents,
            )
            if not hits:
                continue
            if rel in allow:  # bare-path whole-file grandfather
                used.add(rel)
                continue
            for lineno, leaf, dotted in hits:
                if (rel, dotted) in allow:  # (file, module)-pair grandfather (H1)
                    used.add((rel, dotted))
                    continue
                offenders.append("%s:%d imports %s" % (rel, lineno, leaf))  # noqa: UP031
    return offenders, used


# --------------------------------------------------------------------------------------
# Per-port contract mechanism (the port harness builds on the primitives above).
# --------------------------------------------------------------------------------------


class Contract:
    """One import-boundary rule.

    ``target``     -- the forbidden ``app.services.*`` module (exact-target rows). For a
                      PREFIX row (K1) this is a display-only label; ``prefixes`` does the work.
    ``owner``      -- rel path(s) of file(s) SKIPPED by the scanner (the owning port
                      module(s) for the phase-1 rows; the reverse fixture for the reverse row).
    ``allowlist``  -- reasons for the OTHER legitimate importers that may not (yet) be
                      migrated. A KEY is a bare ``rel_path`` (whole-file grandfather) OR a
                      ``(rel_path, dotted_target)`` PAIR (K1/H1 -- grandfathers only that
                      file's import of that one module); each must actually import or it is a
                      STALE entry.
    ``pending``    -- ``True`` marks a row seeded before its port exists / its call sites
                      migrate: scanned and REPORTED but NOT asserted, so it can be seeded
                      here and enabled -- landing green -- in the train that builds its
                      port.
    ``enabled_in`` -- doc string naming the train that flips ``pending`` off.
    ``note``       -- one-line human description for the report.
    ``prefixes``   -- K1 ext-1: PREFIX-target roots (``("app.services",)`` forward;
                      ``("app.web", ...)`` reverse). When set, ``mods``/``stems`` are empty
                      and the prefix arm matches the whole subtree.
    ``subject_prefixes`` -- K1 ext-2: which files the row governs (``None`` = whole app).
    """

    def __init__(
        self,
        port,
        target=None,
        owner=(),
        allowlist=None,
        pending=False,
        enabled_in="",
        note="",
        prefixes=(),
        subject_prefixes=None,
    ):
        self.port = port
        self.target = target
        self.owner = set(owner)
        self.allowlist = dict(allowlist or {})
        self.pending = pending
        self.enabled_in = enabled_in
        self.note = note
        self.prefixes = tuple(prefixes)
        self.subject_prefixes = subject_prefixes

    @property
    def mods(self):
        # A PREFIX row matches purely through ``prefixes``; its ``target`` is a display-only
        # label, so mods/stems MUST be empty (else the hardcoded app.services stems branch
        # fires with a bogus leaf set and shadows the prefix arm).
        if self.prefixes:
            return set()
        if not self.target:
            return set()
        # K2 (composable-core phase 4): an exact-target ``app.services.X`` row ALSO forbids the
        # ``app.kernel.X`` facade spelling (deep + plain-import forms). Without this, after the
        # web/backoffice shells migrate to ``app.kernel`` a port row is BLIND to a shell reaching
        # its target through the facade -- and the forward row FORCES shells onto exactly that
        # spelling. The parent-leaf ``from app.kernel import X`` form is caught via the
        # ``("app.services","app.kernel")`` stem_parents ``scan_contract`` passes. Mirrors F-5.
        m = {self.target}
        if self.target.startswith("app.services."):
            m.add("app.kernel." + self.target[len("app.services.") :])
        return m

    @property
    def stems(self):
        if self.prefixes:
            return set()
        return {self.target.rsplit(".", 1)[-1]} if self.target else set()


def scan_contract(app_dir, root_dir, contract, allow_override=None):
    """Run one ``contract`` over ``app_dir``. Returns ``(offenders, used)``.

    ``allow_override`` (a set/dict of rel paths) REPLACES the row's own allowlist for this
    scan -- used by the anti-vacuity test to scan the probe row with an allowlist that
    OMITS the probe file (so the probe IS reported as an offender)."""
    allow = contract.allowlist if allow_override is None else allow_override
    return cross_reach_scan(
        app_dir,
        root_dir,
        contract.mods,
        contract.stems,
        contract.owner,
        set(allow),
        pkg_aware=True,
        match_dynamic=True,
        prefixes=contract.prefixes,
        subject_prefixes=contract.subject_prefixes,
        # K2: exact-target rows also forbid the app.kernel.X facade spelling
        # (the parent-leaf `from app.kernel import X` form). PREFIX rows
        # (forward/reverse) have empty stems so this is inert for them -- the
        # forward row stays app.services-only, forcing migration TO app.kernel.
        stem_parents=("app.services", "app.kernel"),
    )


def stale_entries(contract, used):
    """Allowlist entries that did NOT import the target (anti-vacuity: a stale row must
    fail loudly). Empty for a healthy contract."""
    return set(contract.allowlist) - used


# --------------------------------------------------------------------------------------
# The self-test probe fixture -- one source of truth shared by the probe module, the
# contract table, and the suite. ``PROBE_FORBIDDEN_TARGET`` MUST stay in sync with the
# planted import in ``app/ports/_selftest_probe.py`` (a static AST literal there).
# --------------------------------------------------------------------------------------

PROBE_PORT = "_selftest_probe"
PROBE_FILE = "app/ports/_selftest_probe.py"
PROBE_FORBIDDEN_TARGET = "app.services._selftest_probe_forbidden"

# K1 (composable-core phase 4): the REVERSE row's core-located guard-of-the-guard. A CORE file
# (app/services -- unlike the app/ports probe above, which is OUT of the reverse row's subject
# scope) plants a forbidden SHELL-spelled import at this SYNTHETIC target. It sits on its own
# row (below) so the REAL reverse row's allowlist stays EMPTY; verify_kernel_facade.py re-scans
# that own row with allow_override={} and asserts the fixture IS reported. MUST stay in sync
# with the planted import in app/services/_selftest_reverse_probe.py.
REVERSE_PROBE_FILE = "app/services/_selftest_reverse_probe.py"
REVERSE_PROBE_FORBIDDEN_TARGET = "app.web._selftest_reverse_forbidden"


# --------------------------------------------------------------------------------------
# THE PORT CONTRACT TABLE. Append one row per port train; flip ``pending`` off in the
# train that builds the port (its call sites move onto the port in the same diff).
# --------------------------------------------------------------------------------------

PORT_CONTRACTS = [
    # ENFORCED (Train H). The anti-vacuity fixture: the ONLY file allowed to import the
    # synthetic forbidden target is the probe itself, allowlisted as a KNOWN FIXTURE. The
    # probe really imports it, so this entry stays "used" -- and check 11's stale-entry
    # property therefore ALSO fails loudly if the planted import is ever removed.
    Contract(
        port=PROBE_PORT,
        target=PROBE_FORBIDDEN_TARGET,
        owner=(),
        allowlist={
            PROBE_FILE: "permanent inert anti-vacuity fixture (see the module docstring); "
            "its planted forbidden import is what proves the scanner bites",
        },
        pending=False,
        note="self-test probe planted-violation fixture (the one ENFORCED row this train)",
    ),
    # ENFORCED (Train 1, TelemetrySink). providers.py no longer imports prompt_tap: its
    # _tap_capture now routes enabled()/build_record()/the redact-then-dispatch write path
    # through app.ports.telemetry (which re-exports them and owns the dispatcher), so the
    # providers -> prompt_tap import edge is GONE and this row is GREEN (owner
    # app/ports/telemetry.py is the sole legitimate importer of the WRITE seam).
    #
    # ALLOWLIST = the legitimate remaining (VIEWER/EXPORTER-side) prompt_tap importers,
    # NOT providers. NOTE (Train-H finding): the spec named a 5-file allowlist, but running
    # THIS scanner over main (ccf03f0) shows a SIXTH legitimate importer -- web/admin.py:13
    # module-scope-imports prompt_tap for the Prompt-Tap ADMIN page (count_legacy_records,
    # page_snapshot, export_bundle, set_enabled, enabled, clear -- all reads/admin, no
    # capture/build_record write). It is a viewer, not a migration target, so it is
    # allowlisted here. Train 1 MUST keep it (or the row will not go green). Flagged in the
    # Train-H report so the 5-vs-6 discrepancy is not silently absorbed.
    Contract(
        port="telemetry",
        target="app.services.prompt_tap",
        owner=("app/ports/telemetry.py",),
        allowlist={
            # K3: cmd_prompt_tap.py migrated its tap import to app.kernel.prompt_tap. The row is
            # spelling-agnostic (K2: Contract.mods + stem_parents forbid app.kernel.X too), so it
            # is a legitimate classified importer again -- entry KEPT, non-stale (via app.kernel).
            "app/cli/cmd_prompt_tap.py": "the CLI tap viewer/exporter (via app.kernel.prompt_tap)",
            "app/services/scheduler.py": "the hourly TTL-sweep piggyback (scheduler.tick)",
            "app/services/tap_enrich.py": "tap record enrichment (viewer side)",
            "app/services/tap_enrich_part1.py": "tap record enrichment; SRP split 2026-09-14",
            "app/services/tap_view.py": "the web tap viewer",
            "app/services/ledger_controls.py": (
                "guarded admin purge and retained capture-file status; not a telemetry producer"
            ),
            # K2: these two web viewers migrated their tap import to app.kernel.prompt_tap. The
            # row is now spelling-agnostic (Contract.mods + stem_parents forbid app.kernel.X too),
            # so they are legitimate classified importers again -- entries RESTORED, non-stale.
            # SRP split 2026-09-14: the badge reader moved to the workflows_common piece
            "app/web/workflows_common.py": "the builder-link tap badge (via app.kernel.prompt_tap)",
            "app/web/admin.py": "admin/viewer surface (page_snapshot, export_bundle, plus "
            "set_enabled/clear admin controls); no capture/build_record "
            "write, so not a Train-1 migration target (via app.kernel)",
            # H (fix-train, tap_runs.py arrived with the prompt-tap-by-run merge 6168bc01
            # and was never added here): the run-grouped READ MODEL over the same capture
            # files as tap_view.py -- is_action_id/record_key/_dir/_path/_actions_dir/
            # _iter_records/_iter_file/_action_path/ACTIONS_SUBDIR only, never
            # capture/build_record/enabled/dispatch, so it is a viewer sibling of
            # tap_view.py/tap_enrich.py, not a Train-1 migration target.
            "app/services/tap_runs.py": "the run-grouped Prompt Tap read model (viewer "
            "sibling of tap_view.py/tap_enrich.py; read-only capture-file enumeration, "
            "no capture/build_record write)",
        },
        pending=False,
        enabled_in="Train 1 (TelemetrySink)",
        note="providers.py -> prompt_tap WRITE edge, now routed via app.ports.telemetry (GREEN)",
    ),
    # ENFORCED (Train 3, BlobStore). The six upload call sites (ingest write + ingest/
    # export/image-loc/web-images reads + retention delete) now speak KEYS through
    # app.ports.blobstore (DiskBlobStore), so app.services.storage is imported by the
    # owning port module. Phase-3 (SPEC-s3-blobstore §4.1 option A) adds ONE adapter-layer
    # importer -- the S3 adapter shares the Fernet at-rest envelope seam
    # (storage.encode_at_rest/decode_at_rest), a peer of DiskBlobStore.
    Contract(
        port="blobstore",
        target="app.services.storage",
        owner=("app/ports/blobstore.py",),
        # The S3 adapter (a paid add-on) is no longer in the core tree, so the allowlist is empty.
        allowlist={},
        pending=False,
        enabled_in="Train 3 (BlobStore); S3 adapter allowlisted phase 3 (SPEC-s3-blobstore)",
        note="upload call sites -> storage WRITE/READ/DELETE edges routed via "
        "app.ports.blobstore (GREEN)",
    ),
    # ENFORCED (Train 2, Scheduler / TimeDriver). The TimeDriver port owns the retention
    # DAEMON-LIFECYCLE seam: app.main._startup no longer calls retention.start() inline --
    # it routes through ports.scheduler.get().start() -- so main.py no longer imports
    # app.services.retention at all, and app/ports/scheduler.py (owner) is the only file
    # that imports retention for the lifecycle (start + the sweep() -> retention.purge wrap).
    #
    # ALLOWLIST = the legitimate remaining importers of retention's DIRECT purge/erase API,
    # which the spec deliberately keeps direct (Train 2: "POST /api/admin/retention/purge"
    # stays a direct route; the GDPR Art. 17 erase path is not a port). Each is a classified
    # consumer, not migration debt. Discovered by RUNNING this scanner over the branch (not
    # taken from the spec, which names no Train-2 row): 3 files, 6 import lines.
    #
    # NOTE (Train-2 finding): there is deliberately NO row for the app.services.SCHEDULER
    # module. Unlike retention (whose only non-port importers use the purge/erase API), the
    # scheduler module is a BROAD connector-scheduling public API -- set_schedule /
    # interval_minutes / PRESETS / tick_now -- imported by SIX non-port consumers
    # (web/connectors.py, web/api.py, web/workflows.py, web/api_cli.py's tick route,
    # cli/cmd_connector.py, cli/cmd_admin.py's tick verb). The TimeDriver port names only the
    # daemon-LIFECYCLE (start/tick/sweep), a CALL-LEVEL property, not an import edge; an
    # import-edge contract there would allowlist nearly every importer and be VACUOUS -- the
    # same reason the spec gives no row to RelationalStore (123 sqlmodel importers) or
    # MTEngine (name-level factory boundary). Stated, not faked.
    Contract(
        port="scheduler",
        target="app.services.retention",
        owner=("app/ports/scheduler.py",),
        allowlist={
            # K2: app/web/api_cli_admin_1.py (was api_cli.py) migrated its retention import to
            # app.kernel.retention. The
            # row is now spelling-agnostic (Contract.mods + stem_parents forbid app.kernel.X too),
            # so it is a legitimate classified importer again -- entry RESTORED, non-stale.
            "app/web/api_cli_admin_1.py": "the retention status + POST /api/admin/retention/purge "
            "HTTP routes (direct purge, sanctioned by the spec; the "
            "config route uses settings, not retention; via app.kernel)",
            # K3: cmd_admin.py migrated its retention import to app.kernel.retention; the
            # spelling-agnostic row keeps seeing it -- entry KEPT, non-stale (via app.kernel).
            "app/cli/cmd_admin.py": "the retention admin CLI (config + purge verbs; "
            "via app.kernel.retention)",
            "app/cli/cmd_admin_helpers.py": "the retention admin CLI helpers "
            "(SRP split 2026-09-14; via app.kernel.retention)",
            # SRP split 2026-09-14: the erase path moved from content.py into the content pieces
            "app/services/content_projects.py": "the GDPR Art. 17 document-erase path "
            "(retention.delete_document / delete_project_tm)",
            "app/services/content_project_rename_delete_sp.py": "the GDPR Art. 17 "
            "document-erase path (retention.delete_document / delete_project_tm)",
            # #137 (2026-09-15): delete_workflow reaps its own WorkflowRun rows, so it must
            # first un-scope MemoryEvent.workflow_run_id or the delete raises IntegrityError.
            # That un-scope helper is HOMED in retention next to purge_vendor_dispatch_legs
            # (retention.py's own docstring says so) and is already shared by the document /
            # project deletes, so this is the SAME classified FK-hygiene consumer as the two
            # content pieces above -- reuse of one helper, never a second copy, and nothing to
            # do with the retention DAEMON LIFECYCLE this port owns. Lazy, function-scoped.
            "app/services/workflows_part3.py": "delete_workflow's FK-hygiene un-scope of "
            "MemoryEvent.workflow_run_id before it reaps its runs "
            "(retention.unscope_memory_event_runs; shared with the document/project deletes)",
        },
        pending=False,
        enabled_in="Train 2 (Scheduler/TimeDriver)",
        note="retention DAEMON-lifecycle edge, now owned by app.ports.scheduler (GREEN); "
        "main.py's inline retention.start() moved onto the port this train",
    ),
]

# --------------------------------------------------------------------------------------
# Train 4 (SecretStore) -- DELIBERATELY NO import-contract row (documented, not faked).
# --------------------------------------------------------------------------------------
# The SecretStore port (app/ports/secrets.py) names the runtime-secret RESOLUTION seam:
# config obtains SESSION_SECRET / ENCRYPTION_KEY via ports.secrets.get().resolve(...)
# instead of a bare config._secret(...) call. That boundary is NAME-LEVEL, not an import
# edge, so it has NO forbid-able row -- for two reinforcing reasons:
#   1. The resolver (`config._secret`) lives INSIDE config, the BOTTOM of the import graph,
#      which ~every module imports. An "only app.ports.secrets may import config" edge
#      would allowlist essentially the whole app and be VACUOUS.
#   2. The ~10 Fernet/session consumers read the RESOLVED config.ENCRYPTION_KEY /
#      config.SESSION_SECRET CONSTANTS (resolved once at import); they do not import a
#      secrets service module, so there is no consumer->service import to guard.
# This is the same rationale the spec gives for RelationalStore (123 sqlmodel importers)
# and MTEngine (name-level factory boundary), and mirrors Train 2's retention-vs-scheduler
# omission precedent. The port's load-bearing property -- "config resolves the two secrets
# THROUGH the port" -- is a CALL-LEVEL guarantee, pinned by tests/verify_secretstore_port.py
# (the subprocess seam test: a fake override registered before config's first import is
# picked up by config.SESSION_SECRET / ENCRYPTION_KEY), not by an import edge. Stated here,
# not faked. See the SPEC-ports-phase1 spec "Errata (post-gate, Train 4)".
# --------------------------------------------------------------------------------------
# Train 5 (RelationalStore) -- DELIBERATELY NO ROW (append-only note, no Contract).
#
# The RelationalStore port (app/ports/relational.py) names the relational-datastore seam
# over app.db. Its aspirational boundary has the same SHAPE as the others ("only
# app.ports.relational may construct the engine"), but it is NOT enforceable in phase 1 by
# an import edge, so Train 5 adds NO Contract row -- following the Train 2 vacuous-row
# precedent (the scheduler MODULE got no row for the same reason), the spec's explicit
# Train-5 instruction ("adds NO import-contract row; states the boundary as convention +
# records the deferred call-level check"), and the MTEngine name-level-boundary precedent.
#
# WHY no row:
#   * 123 modules under app/ import sqlmodel/sqlalchemy LEGITIMATELY (Session, select, event,
#     text). An import-edge contract on app.db (or on sqlmodel) would allowlist nearly every
#     module -> VACUOUS. A row that allowlists 123 importers proves nothing.
#   * create_engine is imported from sqlmodel (NOT sqlalchemy) at db.py:12, and the real
#     guarantee -- "the engine is CONSTRUCTED only in db.py" -- is a CALL-LEVEL property
#     (create_engine(config.DB_URL, ...) appears only at db.py:30), which needs a call-level
#     check. That call-level check is DEFERRED (deliberately not built in phase 1).
#
# DEFERRED call-level check (scope note for whoever builds it): a create_engine-call scanner
# must EXCLUDE by path both alembic/env.py (uses engine_from_config, not create_engine) and
# tools/vendor_studio/vstudio/db.py (a SEPARATE app with its OWN create_engine) -- both are
# outside this port's contract scope. Documented in app/ports/relational.py's docstring.
# --------------------------------------------------------------------------------------
# Batch 13 (data-export) ExportStore port -- DELIBERATELY NO ROW (documented, not faked).
#
# The ExportStore port (app/ports/exportstore.py) names the staged full-dump export-artifact
# seam. Unlike BlobStore -- which CENTRALIZES a pre-existing shared service (app.services.storage,
# six migrated call sites + the S3 adapter that shares its Fernet-at-rest envelope) and therefore
# earns a forbid-``app.services.storage`` row -- ExportStore centralizes NO app.services module:
#   * DiskExportStore is self-contained PLAINTEXT file I/O (os + app.config.EXPORTS_DIR); it does
#     NOT reach app.services.storage (a Fernet blob is not seekable, so open_range could not serve
#     a 206 -- the whole reason this is a separate port, SPEC-data-export §2.3/§8.3, DX-205).
#   * The Train-C S3 adapter (app/services/s3_exportstore.py) self-registers from its own
#     configure_at_boot and shares NO service seam with the disk adapter (exports are plaintext,
#     so there is no encode_at_rest/decode_at_rest peer to allowlist, unlike s3_blobstore).
# An import-edge contract needs a forbid-able target module imported by non-owners; there is none
# here, so a row would be VACUOUS. This follows the Train-4 (SecretStore) and Train-5
# (RelationalStore) no-row precedent above: a NAME-LEVEL port boundary ("call sites speak keys
# through exportstore.get(), the class is fixed at registration") is a CALL-LEVEL guarantee,
# pinned by tests/verify_exportstore_port.py (conformance + the put_stream->open_range(206)->
# presign round-trip), not by an import edge. The frozen app/ports/_selftest_probe.py and
# tests/verify_blobstore_port.py stay UNTOUCHED (DX-204) -- BlobStore is unchanged.
# --------------------------------------------------------------------------------------


# ==========================================================================================
# K1 (composable-core phase 4) -- the loc-core boundary rows, appended to PORT_CONTRACTS.
#
# FORWARD (three rows, one per shell): a shell may not import ``app.services.*`` directly;
# it goes through ``app.kernel`` (+ its own glue). ENFORCED from K1 via the GRANDFATHER
# RATCHET -- every existing shell file that imports ``app.services`` is grandfathered under
# one shared reason, so a NEW unlisted shell import fails immediately (boundary LIVE) and a
# MIGRATED file left in the list goes stale (forcing the K2/K3 deletion). Self-cleaning in
# BOTH directions. The ratchet-breaking whole-file exemption is avoided for the four files
# that carry a PERMANENT import (deps/vendors shell-facing services; connectors + __main__
# denied-spelling connectors sub-depth): those are fully (file, module)-PAIR granular (H1),
# so their ~30 OTHER imports stay individually enforced and a new import cannot hide.
#
# REVERSE (two rows): core may not import a shell/framework. The REAL row holds an EMPTY
# allowlist and stays green (after K1 relocated the one edge, delivery.py -> web.deps.mime);
# the FIXTURE row (synthetic target) is its core-located guard-of-the-guard.
#
# The K1 forward-row grandfather allowlists are COMMITTED LITERALS generated at build time by
# the K1 builder from the extended scanner (regenerable from the grep); the stale-entry law
# keeps them honest.
# ==========================================================================================

_FWD_TEMP_REASON = "pre-phase-4 shell; migrate to app.kernel then delete this entry"
_FWD_SHELL_REASON = "shell-facing service, not in kernel surface"
_FWD_CONN_REASON = "connectors sub-depth excluded from facade; permanent app.services spelling"

# Reverse-row subjects: the core dirs + the framework-free spine .py files. app/resources +
# app/capabilities are data-only (zero scanned .py) -- listed for forward-compat. EXCLUDED:
# app/ports (adapters bridge by design), app/i18n (shell-adjacent), app/main.py (the
# composition root -- it wires shells + kernel + the S3 adapter).
_REVERSE_SUBJECTS = (
    "app/services/",
    "app/filters/",
    "app/resources/",
    "app/capabilities/",
    "app/models.py",
    "app/db.py",
    "app/config.py",
    "app/features.py",
    "app/logging_config.py",
)
# Forbidden reverse targets: the three shells + integrations + the web frameworks.
_REVERSE_SHELLS = (
    "app.web",
    "app.cli",
    "app.backoffice",
    "integrations",
    "fastapi",
    "starlette",
    "jinja2",
)

_WEB_SUBJECTS = ("app/web/",)
_CLI_SUBJECTS = ("app/cli/",)
_BACKOFFICE_SUBJECTS = ("app/backoffice/",)

# --- WEB forward row ---------------------------------------------------------------------
# K2 (composable-core phase 4) MIGRATED every migratable web app.services import to app.kernel
# and DELETED the temporary grandfather entries below (the ratchet: a migrated file left listed
# would go stale and FAIL). Only the PERMANENT pairs remain -- the two shell-facing services
# (trust_auth, vendor_studio_sso) and the two connectors sub-depth spellings
# (connectors.base/.wordpress), which stay on the app.services spelling by design.
_WEB_BARE = ()
_WEB_TEMP_PAIRS = ()
_WEB_PERM_SHELL = (
    ("app/web/deps.py", "app.services.trust_auth"),
    ("app/web/vendors.py", "app.services.vendor_studio_sso"),
)
_WEB_PERM_CONN = (
    ("app/web/connectors.py", "app.services.connectors.base"),
    ("app/web/connectors_common.py", "app.services.connectors.wordpress"),  # SRP split 2026-09-14
)

# --- CLI forward row ---------------------------------------------------------------------
# K3 (composable-core phase 4): every migratable cli file flipped its ``app.services.*``
# imports to ``app.kernel.*`` (411 import-statement lines across 34 files), so the temporary
# grandfather entries -- the 32-file whole-file ``_CLI_BARE`` list and the
# ``(__main__.py, s3_blobstore)`` temp pair -- are DELETED (the stale-entry ratchet forced it).
# Only the PERMANENT connectors sub-depth pair survives: ``__main__.py`` keeps
# ``from app.services.connectors.base import ...`` (the facade does not alias
# ``app.kernel.connectors.base`` -- B4). cmd_workflow's vendor_dispatch cross-feature reach is
# now migrated to app.kernel too (K2 made verify_features check 11 spelling-agnostic, so the
# frozen vendors manifest classification stays USED under the app.kernel spelling -- no carve-out
# needed; the K3-first-pass _CLI_PERM_REACH pair was dissolved on the K2 rebase).
_CLI_BARE = ()
_CLI_TEMP_PAIRS = ()
_CLI_PERM_CONN = (("app/cli/__main__.py", "app.services.connectors.base"),)

# --- BACKOFFICE forward row --------------------------------------------------------------
# K2 MIGRATED both backoffice files to app.kernel and DELETED their grandfather entries (the
# backoffice row's allowlist is now empty and stays green -- no app.services imports remain).
_BACKOFFICE_BARE = ()


def _forward_allow(bare=(), temp_pairs=(), perm_shell=(), perm_conn=()):
    """Build one forward row's mixed-key allowlist from the committed grandfather tuples."""
    allow = {}
    for rel in bare:
        allow[rel] = _FWD_TEMP_REASON
    for pair in temp_pairs:
        allow[tuple(pair)] = _FWD_TEMP_REASON
    for pair in perm_shell:
        allow[tuple(pair)] = _FWD_SHELL_REASON
    for pair in perm_conn:
        allow[tuple(pair)] = _FWD_CONN_REASON
    return allow


PORT_CONTRACTS += [
    # REVERSE (real) -- core may not import a shell/framework. EMPTY allowlist that stays
    # green: after K1 relocated app/services/delivery.py -> app.web.deps.mime_for_fmt into
    # app/services/mime.py, there is no core->shell edge left. The reverse fixture is
    # owner-SKIPPED here (it plants a SYNTHETIC app.web import on its own row below) so this
    # row's allowlist stays genuinely empty.
    Contract(
        port="reverse_core_no_shell",
        target="app.web*/app.cli*/app.backoffice*/integrations*/frameworks",  # display label
        prefixes=_REVERSE_SHELLS,
        subject_prefixes=_REVERSE_SUBJECTS,
        owner=(REVERSE_PROBE_FILE,),
        allowlist={},
        pending=False,
        enabled_in="K1 (loc-core boundary)",
        note="core (app/services + app/filters + spine) may not import a shell/framework; "
        "EMPTY allowlist stays green (the one reverse edge was relocated in K1)",
    ),
    # REVERSE (fixture) -- the core-located guard-of-the-guard on its OWN synthetic-target
    # row. app/services/_selftest_reverse_probe.py plants a forbidden import at the synthetic
    # app.web._selftest_reverse_forbidden; allowlisted here so the normal suite is green, and
    # re-scanned with allow_override={} by verify_kernel_facade.py to prove the reverse
    # scanner fires on a core->app.web-spelled edge.
    Contract(
        port="reverse_selftest_probe",
        target=REVERSE_PROBE_FORBIDDEN_TARGET,
        subject_prefixes=_REVERSE_SUBJECTS,
        allowlist={
            REVERSE_PROBE_FILE: "permanent inert reverse anti-vacuity fixture (see the module docstring); "  # noqa: E501
            "its planted synthetic shell import is what proves the reverse scanner bites"
        },
        pending=False,
        enabled_in="K1 (loc-core boundary)",
        note="reverse-row core-located planted-violation fixture (synthetic app.web target)",
    ),
    # FORWARD -- web shell may not import app.services.* (grandfather ratchet).
    Contract(
        port="forward_web",
        target="app.services.* (web shell -> app.kernel)",
        prefixes=("app.services",),
        subject_prefixes=_WEB_SUBJECTS,
        allowlist=_forward_allow(_WEB_BARE, _WEB_TEMP_PAIRS, _WEB_PERM_SHELL, _WEB_PERM_CONN),
        pending=False,
        enabled_in="K1 (loc-core boundary); grandfather deletions staged in K2",
        note="web shell app.services importers grandfathered (self-cleaning ratchet); "
        "deps->trust_auth + vendors->vendor_studio_sso permanent shell-facing pairs; "
        "connectors.base/.wordpress permanent connectors sub-depth pairs",
    ),
    # FORWARD -- cli shell.
    Contract(
        port="forward_cli",
        target="app.services.* (cli shell -> app.kernel)",
        prefixes=("app.services",),
        subject_prefixes=_CLI_SUBJECTS,
        allowlist=_forward_allow(_CLI_BARE, _CLI_TEMP_PAIRS, (), _CLI_PERM_CONN),
        pending=False,
        enabled_in="K1 (loc-core boundary); grandfather deletions staged in K3",
        note="cli shell app.services importers grandfathered (self-cleaning ratchet); "
        "__main__->connectors.base permanent connectors sub-depth pair",
    ),
    # FORWARD -- backoffice shell.
    Contract(
        port="forward_backoffice",
        target="app.services.* (backoffice shell -> app.kernel)",
        prefixes=("app.services",),
        subject_prefixes=_BACKOFFICE_SUBJECTS,
        allowlist=_forward_allow(_BACKOFFICE_BARE),
        pending=False,
        enabled_in="K1 (loc-core boundary); grandfather deletions staged in K2",
        note="backoffice shell app.services importers grandfathered (self-cleaning ratchet)",
    ),
]
