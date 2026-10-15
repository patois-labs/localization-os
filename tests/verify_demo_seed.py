"""Demo-content seeder verification (offline, hermetic).

Exercises scripts/seed_demo_content.py end to end on a throwaway LOC_TMS_DATA_DIR:

  (1) --dry-run on an empty DB writes NOTHING (no glossary / project / TM rows)
  (2) seed creates the approved General glossary "Demo Glossary (seeded)" with
      25 terms x 5 locales = 125 entries (each term has exactly 5 entries, one per
      locale; 3 DNT brand terms => 15 DNT entries) + 5 en-US->target pairs, and the
      demo project with EXACTLY 100 x 5 = 500 SegmentTM rows
  (3) re-running is idempotent — no duplicate glossary / project / TM rows
  (4) --cleanup removes EXACTLY the seeded rows and nothing else: a pre-existing
      NON-seed glossary + non-seed project + non-seed TM row all survive
  (5) --project <id> attaches the TM to an EXISTING project (no demo project created)
      and its cleanup leaves that project (and its own TM) intact
  (6) issue #523 guards (fresh SUBPROCESSes): with no --data-dir and no
      LOC_TMS_DATA_DIR the seeder refuses BEFORE importing app.db (message names
      --data-dir); a data dir carrying the real '.protected' marker is refused;
      --dry-run against a fresh temp dir creates no loc_tms.db and runs no migration

Runs the whole file against ONE throwaway DB (the engine binds to LOC_TMS_DATA_DIR at
import time), so the scenarios are ordered to compose on a single database. ASCII-only
source; the seeder's own console output is routed through _write_ascii_safe.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout, suppress

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
from tests._bootstrap import init_env

init_env("loctms_demoseed_")

import seed_demo_content as seed
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.models import (
    Glossary,
    GlossaryEntry,
    GlossaryPair,
    GlossaryStatus,
    Project,
    SegmentTM,
)
from app.services import content, glossary_module, tm


def _fail(msg):
    raise AssertionError(msg)


def _run(argv):
    """Invoke the seeder, swallowing its (ascii-safe) stdout, and assert clean exit."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = seed.main(argv)
    if rc != 0:
        _fail(f"seed.main({argv!r}) returned {rc}\n{buf.getvalue()}")
    return buf.getvalue()


def _counts(s):
    g = s.exec(select(Glossary).where(Glossary.name == seed.GLOSSARY_NAME)).all()
    p = s.exec(select(Project).where(Project.name == seed.PROJECT_NAME)).all()
    ents = (
        s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == g[0].id)).all() if g else []
    )
    tm_rows = s.exec(select(SegmentTM).where(SegmentTM.project_id == p[0].id)).all() if p else []
    return g, p, ents, tm_rows


def test_dry_run_writes_nothing():
    init_db()
    _run(["--dry-run"])
    with Session(engine) as s:
        g, p, _, _ = _counts(s)
        if g:
            _fail("dry-run created a glossary")
        if p:
            _fail("dry-run created a project")
        if s.exec(select(SegmentTM)).all():
            _fail("dry-run wrote TM rows")


def test_seed_creates_expected_shape():
    _run([])
    with Session(engine) as s:
        g, p, ents, tm_rows = _counts(s)
        if len(g) != 1:
            _fail(f"expected 1 seed glossary, got {len(g)}")
        gl = g[0]
        if gl.status != GlossaryStatus.APPROVED:
            _fail(f"glossary not approved: {gl.status}")
        if gl.content_type_id is not None:
            _fail("glossary should be General (content_type_id None)")
        if len(ents) != 125:
            _fail(f"expected 125 glossary entries (25x5), got {len(ents)}")
        # each source term has exactly 5 entries (one per locale)
        per_term = {}
        for e in ents:
            per_term.setdefault(e.source_term, set()).add(e.target_locale)
        if len(per_term) != 25:
            _fail(f"expected 25 distinct terms, got {len(per_term)}")
        for term, locs in per_term.items():
            if locs != set(seed.ALL_LOCALES):
                _fail(f"term {term!r} covers {sorted(locs)}, expected the 5 locales")
        dnt = sum(1 for e in ents if e.dnt)
        if dnt != 15:
            _fail(f"expected 15 DNT entries (3 brands x 5), got {dnt}")
        pairs = s.exec(select(GlossaryPair).where(GlossaryPair.glossary_id == gl.id)).all()
        if len(pairs) != 5:
            _fail(f"expected 5 language pairs, got {len(pairs)}")
        if len(p) != 1:
            _fail(f"expected 1 demo project, got {len(p)}")
        if len(tm_rows) != 500:
            _fail(f"expected 500 TM rows (100x5), got {len(tm_rows)}")


def test_reseed_is_idempotent():
    _run([])  # second seed
    with Session(engine) as s:
        g, p, ents, tm_rows = _counts(s)
        if len(g) != 1 or len(p) != 1:
            _fail(f"re-seed duplicated container rows: glossaries={len(g)} projects={len(p)}")
        if len(ents) != 125:
            _fail(f"re-seed changed glossary entry count: {len(ents)}")
        if len(tm_rows) != 500:
            _fail(f"re-seed changed TM row count: {len(tm_rows)}")


def test_cleanup_removes_only_seeded():
    # A NON-seed glossary + non-seed project + non-seed TM row that MUST survive.
    with Session(engine) as s:
        keep_g = glossary_module.create(s, "Keep Me (real)", created_by="human@x.com")
        keep_p = content.project_create(
            s,
            name="Real User Project",
            source_locale="en-US",
            target_locales="es-ES",
            created_by="human@x.com",
        )
        tm.write_back(
            s, keep_p.id, "A genuine user string", "Una cadena de usuario real", "en-US", "es-ES"
        )
        keep_g_id, keep_p_id = keep_g.id, keep_p.id

    _run(["--cleanup"])

    with Session(engine) as s:
        g, p, _, _ = _counts(s)
        if g:
            _fail("cleanup left the seed glossary")
        if p:
            _fail("cleanup left the seed project")
        # the non-seed glossary + project survive
        if s.get(Glossary, keep_g_id) is None:
            _fail("cleanup deleted a NON-seed glossary")
        if s.get(Project, keep_p_id) is None:
            _fail("cleanup deleted a NON-seed project")
        remaining = s.exec(select(SegmentTM)).all()
        if len(remaining) != 1:
            _fail(f"cleanup should leave exactly the 1 user TM row, got {len(remaining)}")
        if remaining[0].source_text != "A genuine user string":
            _fail("cleanup left the wrong TM row")


def test_project_attach_mode_and_cleanup():
    # Attach the corpus to the surviving NON-seed project (no demo project created).
    with Session(engine) as s:
        keep_p = s.exec(select(Project).where(Project.name == "Real User Project")).all()[0]
        keep_p_id = keep_p.id

    _run(["--project", str(keep_p_id)])

    with Session(engine) as s:
        # no demo project was created
        if s.exec(select(Project).where(Project.name == seed.PROJECT_NAME)).all():
            _fail("attach mode wrongly created the demo project")
        rows = s.exec(select(SegmentTM).where(SegmentTM.project_id == keep_p_id)).all()
        # 1 pre-existing user row + 500 seeded rows
        if len(rows) != 501:
            _fail(f"expected 501 TM rows on the attach project, got {len(rows)}")
        g = s.exec(select(Glossary).where(Glossary.name == seed.GLOSSARY_NAME)).all()
        if len(g) != 1:
            _fail("attach mode did not (re)create the glossary")

    # cleanup --project must strip the seeded rows + glossary but keep the project
    # AND its own pre-existing user TM row.
    _run(["--cleanup", "--project", str(keep_p_id)])
    with Session(engine) as s:
        if s.get(Project, keep_p_id) is None:
            _fail("cleanup --project deleted the user's own existing project")
        rows = s.exec(select(SegmentTM).where(SegmentTM.project_id == keep_p_id)).all()
        if len(rows) != 1:
            _fail(f"cleanup --project should leave the 1 user row, got {len(rows)}")
        if rows[0].source_text != "A genuine user string":
            _fail("cleanup --project removed the wrong TM row")
        if s.exec(select(Glossary).where(Glossary.name == seed.GLOSSARY_NAME)).all():
            _fail("cleanup --project left the seed glossary")


def test_attach_mode_never_clobbers_colliding_user_row():
    """The reviewer's exact HIGH scenario: the seed sources are ultra-common UI strings,
    so a user's pre-existing TM row in an existing project can collide on hash. Attach
    mode must (a) SKIP that row on seed (never overwrite), and (b) --cleanup must NOT
    delete it (its target differs from the seed value), while a genuinely seed-authored
    row in the same project IS removed by cleanup."""
    CUSTOM = "MI-CANCELAR-PERSONALIZADO"  # a user's own es-ES translation of "Cancel"
    with Session(engine) as s:
        proj = content.project_create(
            s,
            name="Collide Project",
            source_locale="en-US",
            target_locales="es-ES",
            created_by="human@x.com",
        )
        # A user TM row whose SOURCE collides with a seed string ("Cancel"), but whose
        # target is the user's own custom value (differs from the seed's "Cancelar").
        tm.write_back(s, proj.id, "Cancel", CUSTOM, "en-US", "es-ES")
        proj_id = proj.id

    out = _run(["--project", str(proj_id), "--locales", "es-ES"])
    if "skipped 1" not in out:
        _fail(f"seed should report 1 skipped pre-existing row; output:\n{out}")

    with Session(engine) as s:
        # (a) the user's colliding row is UNCHANGED (not overwritten).
        h = tm.hash_text("Cancel", "en-US", "es-ES")
        user_row = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == proj_id,
                SegmentTM.target_locale == "es-ES",
                SegmentTM.source_hash == h,
            )
        ).all()
        if len(user_row) != 1:
            _fail(f"expected exactly 1 'Cancel' es-ES row, got {len(user_row)}")
        if user_row[0].target_text != CUSTOM:
            _fail(f"seed OVERWROTE the user's row: {user_row[0].target_text!r}")
        # a genuinely seed-authored row exists ("Save changes" did not pre-exist)
        h2 = tm.hash_text("Save changes", "en-US", "es-ES")
        seed_row = s.exec(
            select(SegmentTM).where(SegmentTM.project_id == proj_id, SegmentTM.source_hash == h2)
        ).first()
        if seed_row is None or seed_row.target_text != "Guardar cambios":
            _fail("expected a seed-authored 'Save changes' es-ES row")

    _run(["--cleanup", "--project", str(proj_id)])
    with Session(engine) as s:
        # (b) the user's row SURVIVES cleanup (target != seed value => not seed's row).
        user_row = s.exec(
            select(SegmentTM).where(
                SegmentTM.project_id == proj_id,
                SegmentTM.target_locale == "es-ES",
                SegmentTM.source_hash == h,
            )
        ).all()
        if len(user_row) != 1 or user_row[0].target_text != CUSTOM:
            _fail("cleanup DELETED or mutated the user's colliding row")
        # the seed-authored row IS removed.
        seed_row = s.exec(
            select(SegmentTM).where(SegmentTM.project_id == proj_id, SegmentTM.source_hash == h2)
        ).first()
        if seed_row is not None:
            _fail("cleanup left a seed-authored row behind")
        if s.get(Project, proj_id) is None:
            _fail("cleanup --project deleted the user's existing project")


# --- issue #523 guards (fresh SUBPROCESSes) ------------------------------------
# The guards must fire BEFORE app.db is imported (that import itself pre-creates
# the SQLite file via config.prepare_sqlite_file), and in THIS process app.db is
# already imported by the bootstrap -- so each guard is probed in a child.

_SEEDER = os.path.join(_ROOT, "scripts", "seed_demo_content.py")

# Child-side wrapper: runs the seeder script and reports whether app.db ever got
# imported, preserving the seeder's exit code. sys.argv is rewritten to the seeder's
# own argv first (python -c leaves the script path at argv[1], which the seeder's
# argparse would choke on, and its usage block happens to mention --data-dir).
_LAUNCHER = (
    "import runpy, sys\n"
    "script = sys.argv[1]\n"
    "sys.argv = [script] + sys.argv[2:]\n"
    "code = 0\n"
    "try:\n"
    "    runpy.run_path(script, run_name='__main__')\n"
    "except SystemExit as e:\n"
    "    code = e.code if isinstance(e.code, int) else 1\n"
    "print('APPDB', 'app.db' in sys.modules)\n"
    "sys.exit(code)\n"
)


def _child_env():
    """Seeder-subprocess env: LOC_TMS_DATA_DIR removed everywhere, repo root first on
    PYTHONPATH (the script imports scripts.seed_demo_content_common)."""
    env = dict(os.environ)
    env.pop("LOC_TMS_DATA_DIR", None)
    env["PYTHONPATH"] = _ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _spawn(extra_argv, launcher=False, extra_env=None):
    """Run the seeder in a fresh interpreter; return (returncode, combined output)."""
    env = _child_env()
    env.update(extra_env or {})
    argv = [sys.executable]
    if launcher:
        argv += ["-c", _LAUNCHER, _SEEDER]
    else:
        argv.append(_SEEDER)
    proc = subprocess.run(
        argv + list(extra_argv),
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
        cwd=_ROOT,
        timeout=600,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_refuses_without_explicit_data_dir():
    # No --data-dir AND LOC_TMS_DATA_DIR stripped from the child env: refuse with a
    # message naming --data-dir, BEFORE importing app.db. --dry-run so a pre-fix
    # tree cannot write anything real beyond what app.db's import itself creates.
    rc, out = _spawn(["--dry-run"], launcher=True)
    if rc == 0:
        _fail(f"seeder ran with no explicit target; it must refuse (rc {rc})\n{out}")
    if "APPDB True" in out:
        _fail("the refusal happened AFTER app.db was imported; it must precede it\n" + out)
    if not any(line.startswith("error:") and "--data-dir" in line for line in out.splitlines()):
        _fail("the refusal must be an 'error:' line naming --data-dir\n" + out)


def test_refuses_protected_data_dir():
    # A data dir carrying the real '.protected' marker -- the exact file
    # app.config.data_dir_is_protected checks in the child -- is refused outright,
    # and nothing is created inside it.
    d = tempfile.mkdtemp(prefix="loctms_demoseed_prot_")
    try:
        with open(os.path.join(d, config.PROTECT_MARKER_NAME), "w", encoding="utf-8") as fh:
            fh.write("test marker\n")
        rc, out = _spawn(["--data-dir", d, "--dry-run"])
        if rc == 0:
            _fail(f"seeder accepted a PROTECTED data dir (rc 0)\n{out}")
        if "protected" not in out.lower():
            _fail("the refusal message should name the protection\n" + out)
        if os.path.exists(os.path.join(d, "loc_tms.db")):
            _fail("a refused run must not create the database file\n" + out)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_refuses_live_data_dir_spellings():
    # The repo's own data/ is refused however it is spelled (trailing slash, odd case,
    # relative, a child path) even WITHOUT a marker; and a child of a protected dir is
    # refused. Pure refusals: nothing is created (the child path is never made).
    live = os.path.join(_ROOT, "data")
    spellings = [
        live + os.sep,
        live.upper() if os.name == "nt" else live,
        os.path.join(_ROOT, "scripts", "..", "data"),
        os.path.join(live, "zz_nonexistent_i523"),
    ]
    for sp in spellings:
        rc, out = _spawn(["--data-dir", sp, "--dry-run"])
        if rc == 0 or "error:" not in out:
            _fail(f"live data dir spelling {sp!r} was accepted (rc {rc})\n{out}")
    d = tempfile.mkdtemp(prefix="loctms_demoseed_prot2_")
    try:
        with open(os.path.join(d, config.PROTECT_MARKER_NAME), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        child = os.path.join(d, "child")
        rc, out = _spawn(["--data-dir", child, "--dry-run"])
        if rc == 0 or os.path.exists(child):
            _fail(f"child of a protected dir was accepted or created (rc {rc})\n{out}")
        # junction/symlink pointing at the protected dir
        link = d + "_link"
        try:
            if os.name == "nt":
                mk = ["cmd", "/c", "mklink", "/J", link, d]
                made = subprocess.run(mk, capture_output=True).returncode == 0
            else:
                os.symlink(d, link)
                made = True
        except OSError:
            made = False
        if made:
            try:
                rc, out = _spawn(["--data-dir", link, "--dry-run"])
                if rc == 0:
                    _fail(f"junction/symlink to a protected dir was accepted\n{out}")
            finally:
                with suppress(OSError):
                    os.rmdir(link) if os.name == "nt" else os.unlink(link)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dry_run_fresh_dir_creates_no_db():
    # --dry-run against a FRESH temp data dir: exit 0, no loc_tms.db, no migration
    # (pre-fix, init_db() ran first and created + migrated the file).
    d = tempfile.mkdtemp(prefix="loctms_demoseed_fresh_")
    try:
        rc, out = _spawn(["--data-dir", d, "--dry-run"])
        if rc != 0:
            _fail(f"dry-run against a fresh dir should exit 0 (rc {rc})\n{out}")
        if os.path.exists(os.path.join(d, "loc_tms.db")):
            _fail("dry-run created the database file (init_db/migration ran)\n" + out)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_refuses_database_url():
    # A sqlite DATABASE_URL moves DB_PATH off the target dir (#525), so the data-dir
    # guards would not cover the file actually written. Refused, nothing created.
    d = tempfile.mkdtemp(prefix="loctms_demoseed_dburl_")
    try:
        other = os.path.join(d, "elsewhere.db")
        rc, out = _spawn(
            ["--data-dir", os.path.join(d, "target")],
            extra_env={"DATABASE_URL": "sqlite:///" + other.replace("\\", "/")},
        )
        if rc == 0 or "DATABASE_URL" not in out:
            _fail(f"seeder ran with DATABASE_URL set (rc {rc})\n{out}")
        if os.path.exists(other):
            _fail("a refused run must not create the DATABASE_URL file\n" + out)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_dry_run_writes_nothing()
    test_seed_creates_expected_shape()
    test_reseed_is_idempotent()
    test_cleanup_removes_only_seeded()
    test_project_attach_mode_and_cleanup()
    test_attach_mode_never_clobbers_colliding_user_row()
    test_refuses_without_explicit_data_dir()
    test_refuses_protected_data_dir()
    test_refuses_live_data_dir_spellings()
    test_dry_run_fresh_dir_creates_no_db()
    test_refuses_database_url()
    print("verify_demo_seed OK")
