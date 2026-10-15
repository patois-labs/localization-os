"""Seed a ready-made multilingual GLOSSARY + TRANSLATION MEMORY corpus for demos.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _ROOT, SEED_MARKER, GLOSSARY_NAME, PROJECT_NAME,
SOURCE_LOCALE, ALL_LOCALES, _G, GLOSSARY_TERMS, TM_STRINGS, _emit from
scripts.seed_demo_content_common.

The maintainer's ask: a ready-to-use "Demo Glossary (seeded)" and a Translation Memory of 100
English strings translated into 5 locales, so glossary + TM features can be exercised
(and screenshotted) without hand-authoring or uploading anything.

WHAT IT CREATES (all deterministic, authored INLINE below -- no LLM/provider/network):
  * a Project "Demo Content (seeded)" (source en-US, the 5 target locales) to hold the
    TM -- OR, with --project <id>, it attaches the TM to an EXISTING project instead;
  * an APPROVED, General (NULL content-type) Glossary "Demo Glossary (seeded)" with 25
    "Acme Cloud" SaaS terms, each translated into es-ES/fr-FR/de-DE/ja-JP/zh-CN (3 are
    Do-Not-Translate brand terms), plus the en-US->target language pairs;
  * exactly 100 English source strings, each written into the segment TM in all 5
    locales (500 SegmentTM rows), via the same write seam the CAT editor/approve path
    uses -- some carry {placeholders} and some are near-duplicates (fuzzy fodder).

SERVICE SEAMS ONLY (never raw SQL for writes):
  content.project_create / content.delete_project, glossary_module.create / set_pairs /
  add_term / approve / delete / list_glossaries, tm.write_back, tm_module.delete_entry.

SAFETY: this is an ops SCRIPT and it NEVER assumes a target (issue #523). An explicit
  target is REQUIRED -- pass --data-dir <dir> or export LOC_TMS_DATA_DIR first; with
  neither set the script refuses (exit 2) instead of falling back to the app default
  'data/', which is a live install. A target carrying the '.protected' marker
  (app.config.data_dir_is_protected, the same posture as scripts/test_instance.py) is
  likewise refused.
  ** Pointing --data-dir (or LOC_TMS_DATA_DIR) at a REAL, UNMARKED install still writes
     into that real database. ** It is additive-only + idempotent: re-running
  updates/skips rather than duplicating (glossary matched by name + seed marker, TM
  upserted by source hash).
  --cleanup removes EXACTLY what a prior seed created (tracked by the fixed names + the
  seed marker stamped in created_by, and by the known source strings) and nothing else.
  --dry-run prints what would happen and never touches the database at all: no
  init_db()/migration, no file creation (against a dir with no DB it reports that and
  exits 0; against an existing DB it reads without migrating).

  USER-DATA SAFETY IN --project ATTACH MODE: the 100 seed sources are ultra-common UI
  strings ("Cancel", "Save changes", "Delete"). When attaching into an EXISTING project,
  the seeder SKIPS (never overwrites) any TM row that already exists for a seed
  (source, locale) — a user's own translation is left untouched — and --cleanup deletes a
  TM row ONLY when its current target still equals the seed's exact expected value, so it
  is provably incapable of removing a user's own or edited translation. Default mode (the
  seeder owns a freshly-created project) has no pre-existing rows, so it never skips —
  behavior there is byte-identical.

  NO NETWORK / NO SPEND — with ONE caveat: the content is authored inline (no LLM/
  provider calls). But seeding writes SegmentTM rows, so if the REAL install has semantic
  TM enabled (settings semantic_tm_enabled=1) AND a real embedder key configured, the 500
  writes will trigger embedding calls (network / possible spend). Default / keyless
  installs stay fully offline.

ASCII-safe console: the CONTENT stored in the DB is unicode, but every PRINTED line is
routed through app.cli._common._write_ascii_safe so a cp1252 Windows console never dies.
"""

from __future__ import annotations

import argparse
import os
import sys

from scripts.seed_demo_content_common import (
    _G,
    _ROOT,
    ALL_LOCALES,
    GLOSSARY_NAME,
    GLOSSARY_TERMS,
    PROJECT_NAME,
    SEED_MARKER,
    SOURCE_LOCALE,
    TM_STRINGS,
    _emit,
)

__all__ = [
    "ALL_LOCALES",
    "GLOSSARY_NAME",
    "GLOSSARY_TERMS",
    "PROJECT_NAME",
    "SEED_MARKER",
    "SOURCE_LOCALE",
    "TM_STRINGS",
    "_G",
    "_ROOT",
    "_emit",
]


if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# --- seed helpers (all through service seams) ---------------------------------
def _find_seed_glossary(session, glossary_module):
    """The seed-created glossary (name + seed marker), or None. Never matches a
    same-named glossary a human created (the marker in created_by disambiguates)."""
    for g in glossary_module.list_glossaries(session):
        if g.name == GLOSSARY_NAME and (g.created_by or "") == SEED_MARKER:
            return g
    return None


def _find_seed_project(session):
    """The seed-created demo project (name + seed marker), or None."""
    from sqlmodel import select

    from app.models import Project

    for p in session.exec(select(Project).where(Project.name == PROJECT_NAME)).all():
        if (p.created_by or "") == SEED_MARKER:
            return p
    return None


def _selected_locales(locales_arg: str):
    """The target locales to seed: the intersection of the requested list and the
    5 the content is authored for, canonical order preserved. Requested locales the
    content doesn't cover are skipped with a warning."""
    if not locales_arg:
        return list(ALL_LOCALES), []
    from app.services import languages_catalog

    requested = []
    for tok in locales_arg.replace(",", " ").split():
        canon = languages_catalog.validate_codes([tok])[0]
        requested.append(canon[0] if canon else tok)
    chosen = [loc for loc in ALL_LOCALES if loc in requested]
    unknown = [loc for loc in requested if loc not in ALL_LOCALES]
    return chosen, unknown


def _seed_glossary(session, locales, dry_run):
    """Create (or reuse) the approved demo glossary with 25 terms x len(locales)
    entries + the en-US->target pairs. Idempotent: reuses an existing seed glossary
    and upserts its terms (glossary_module.add_term upserts on the locale-pair key)."""
    from app.services import glossary_module

    existing = _find_seed_glossary(session, glossary_module)
    n_terms = len(GLOSSARY_TERMS)
    n_entries = n_terms * len(locales)
    if dry_run:
        verb = "update" if existing else "create"
        _emit(
            f"[dry-run] would {verb} glossary {GLOSSARY_NAME!r}: "
            f"{n_terms} terms x {len(locales)} locales = {n_entries} entries "
            f"({sum(1 for _, _, d in GLOSSARY_TERMS if d)} DNT), "
            f"pairs en-US -> {', '.join(locales)}, then approve."
        )
        return None

    if existing is not None:
        g = existing
    else:
        g = glossary_module.create(
            session, GLOSSARY_NAME, created_by=SEED_MARKER, content_type_id=None
        )
    # Pairs: en-US -> each selected locale (set_pairs replaces + reverts to draft).
    glossary_module.set_pairs(session, g.id, [(SOURCE_LOCALE, loc) for loc in locales])
    # Terms: one entry per (term, locale). add_term upserts, so a re-run is a no-op.
    for source_term, translations, dnt in GLOSSARY_TERMS:
        by_locale = dict(zip(ALL_LOCALES, translations, strict=False))
        for loc in locales:
            glossary_module.add_term(
                session,
                g.id,
                source_term=source_term,
                target_term=by_locale[loc],
                source_locale=SOURCE_LOCALE,
                target_locale=loc,
                dnt=dnt,
            )
    # Approve so it is usable in workflow dropdowns immediately.
    glossary_module.approve(session, g.id, approver_email=SEED_MARKER)
    _emit(
        f"glossary {GLOSSARY_NAME!r} (id={g.id}) approved: "
        f"{n_terms} terms x {len(locales)} locales = {n_entries} entries."
    )
    return g


def _resolve_project(session, project_arg, dry_run):
    """The project the TM attaches to: an EXISTING --project id, or the seed demo
    project (created/reused). Returns (project_or_None, created_bool)."""
    from app.models import Project
    from app.services import content

    if project_arg is not None:
        p = session.get(Project, int(project_arg))
        if p is None:
            raise SystemExit(f"error: --project {project_arg} does not exist.")
        _emit(f"attaching TM to existing project {p.name!r} (id={p.id}).")
        return p, False

    existing = _find_seed_project(session)
    if existing is not None:
        _emit(f"reusing seed project {PROJECT_NAME!r} (id={existing.id}).")
        return existing, False
    if dry_run:
        _emit(
            f"[dry-run] would create project {PROJECT_NAME!r} "
            f"(source {SOURCE_LOCALE}, targets {', '.join(ALL_LOCALES)})."
        )
        return None, True
    p = content.project_create(
        session,
        name=PROJECT_NAME,
        source_locale=SOURCE_LOCALE,
        target_locales=",".join(ALL_LOCALES),
        created_by=SEED_MARKER,
    )
    _emit(f"created project {PROJECT_NAME!r} (id={p.id}).")
    return p, True


def _seed_tm(session, project, locales, dry_run, attach_mode):
    """Write the 100 source strings x len(locales) into the segment TM via
    tm.write_back (the same idempotent, hash-keyed seam approve/import use).

    USER-DATA SAFETY (HIGH-fix): in --project ATTACH mode the seeder must never clobber
    a user's own TM. The seed sources are ultra-common UI strings, so before writing each
    (source, locale) it checks whether a row ALREADY EXISTS for that hash in the target
    project; if so it SKIPS it (leaving the user's translation untouched) and never counts
    it as seed-authored. Only genuinely absent rows are written. Default mode is NOT
    attach mode — its freshly-created project has no rows, so nothing is ever skipped and
    behavior is byte-identical (tm.write_back on every row, as before)."""
    from sqlmodel import select

    from app.models import SegmentTM
    from app.services import tm

    n = len(TM_STRINGS) * len(locales)
    if dry_run:
        _emit(
            f"[dry-run] would write up to {len(TM_STRINGS)} sources x {len(locales)} "
            f"locales = {n} SegmentTM rows into the project's TM"
            + (" (skipping any that collide with existing user rows)." if attach_mode else ".")
        )
        return 0
    written = 0
    skipped = 0
    for row in TM_STRINGS:
        en = row[0]
        by_locale = dict(zip(ALL_LOCALES, row[1:], strict=False))
        for loc in locales:
            if attach_mode:
                h = tm.hash_text(en, SOURCE_LOCALE, loc)
                existing = session.exec(
                    select(SegmentTM).where(
                        SegmentTM.project_id == project.id,
                        SegmentTM.source_locale == SOURCE_LOCALE,
                        SegmentTM.target_locale == loc,
                        SegmentTM.source_hash == h,
                    )
                ).first()
                if existing is not None:
                    # A row already exists for this (source, locale) — a user's own TM.
                    # SKIP it: never overwrite, never claim it as seed-authored.
                    skipped += 1
                    continue
            tm.write_back(session, project.id, en, by_locale[loc], SOURCE_LOCALE, loc)
            written += 1
    _emit(
        f"wrote {written} TM entries to project id={project.id}"
        + (f"; skipped {skipped} pre-existing (user) rows left untouched." if attach_mode else ".")
    )
    return written


# --- cleanup (removes EXACTLY what a prior seed created) -----------------------
def _cleanup(session, project_arg, dry_run):
    """Remove the seed glossary, the seed TM rows, and (default mode) the seed
    project. Matches only seed-marked rows / known seed sources — never touches a
    row it didn't create."""
    from sqlmodel import select

    from app.models import SegmentTM
    from app.services import content, glossary_module, tm, tm_module

    # 1) Resolve which project holds the seed TM (an explicit --project, else the
    #    marker-tagged seed project). If neither, there is no seed TM to remove.
    target_project = None
    created_by_seed = False
    if project_arg is not None:
        from app.models import Project

        target_project = session.get(Project, int(project_arg))
    else:
        target_project = _find_seed_project(session)
        created_by_seed = target_project is not None

    # 2) Seed TM rows: matched by (project, source_locale, target_locale, source_hash)
    #    for every known seed source in every locale. VERIFY-BEFORE-DELETE (HIGH-fix): a
    #    row is deleted ONLY when its CURRENT target_text still equals the seed's exact
    #    expected value for that (source, locale). If the target differs — a user edited
    #    it, or it pre-existed and was skipped on seed — it is NOT the seed's row and is
    #    left alone. This makes cleanup provably incapable of removing a user's own or
    #    edited translation, even for the ultra-common UI strings the corpus uses.
    tm_ids = []
    if target_project is not None:
        for row in TM_STRINGS:
            en = row[0]
            by_locale = dict(zip(ALL_LOCALES, row[1:], strict=False))
            for loc in ALL_LOCALES:
                h = tm.hash_text(en, SOURCE_LOCALE, loc)
                hit = session.exec(
                    select(SegmentTM).where(
                        SegmentTM.project_id == target_project.id,
                        SegmentTM.source_locale == SOURCE_LOCALE,
                        SegmentTM.target_locale == loc,
                        SegmentTM.source_hash == h,
                    )
                ).first()
                if hit is not None and hit.target_text == by_locale[loc]:
                    tm_ids.append(hit.id)

    # 3) Seed glossary (name + marker).
    g = _find_seed_glossary(session, glossary_module)

    if dry_run:
        _emit(
            f"[dry-run] would delete {len(tm_ids)} seed TM rows"
            + (f" from project id={target_project.id}" if target_project else "")
            + "."
        )
        _emit(
            f"[dry-run] would delete glossary {GLOSSARY_NAME!r}"
            + (f" (id={g.id})" if g else " (not found)")
            + "."
        )
        if created_by_seed:
            _emit(f"[dry-run] would delete seed project {PROJECT_NAME!r} (id={target_project.id}).")
        return

    for tid in tm_ids:
        tm_module.delete_entry(session, tid)
    _emit(f"deleted {len(tm_ids)} seed TM rows.")

    if g is not None:
        glossary_module.delete(session, g.id)
        _emit(f"deleted glossary {GLOSSARY_NAME!r} (id={g.id}).")
    else:
        _emit(f"glossary {GLOSSARY_NAME!r} not found (nothing to delete).")

    # 4) The seed project itself — ONLY when the seed created it (never an existing
    #    --project the user asked us to attach into). delete_project cascades the rest.
    if created_by_seed:
        content.delete_project(session, target_project.id, actor=SEED_MARKER)
        _emit(f"deleted seed project {PROJECT_NAME!r} (id={target_project.id}).")


# --- entry point --------------------------------------------------------------
def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="seed_demo_content",
        description="Seed a demo multilingual glossary + TM corpus (additive, idempotent).",
    )
    p.add_argument(
        "--data-dir",
        default=None,
        help="Data dir to write to (REQUIRED unless LOC_TMS_DATA_DIR is already set; "
        "the app default 'data/' is never assumed). Protected dirs (the "
        "'.protected' marker) are refused. WARNING: pointing this at a real "
        "install writes into that real database.",
    )
    p.add_argument(
        "--project",
        default=None,
        help="Attach the TM to this EXISTING project id instead of creating the demo project.",
    )
    p.add_argument(
        "--locales",
        default=",".join(ALL_LOCALES),
        help="Comma/space-separated target locales to seed (default: the 5 "
        "the content is authored for).",
    )
    p.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove exactly what a prior seed created, then exit.",
    )
    p.add_argument("--dry-run", action="store_true", help="Print what would happen; write nothing.")
    return p.parse_args(argv)


def _live_dir_reason(target: str) -> str:
    """Why `target` must not be seeded, or "" if it looks like a throwaway dir.

    Pure path logic (no import of app.*, nothing created): resolves symlinks/junctions
    (realpath), case, trailing slashes, relative paths and UNC spellings, then refuses
    the repo's live data/ dir (or anything under it) and any dir at or under one that
    carries the '.protected' marker (same posture as scripts/test_instance.py)."""
    repo_data = os.path.realpath(os.path.join(_ROOT, "data"))
    resolved = os.path.realpath(os.path.abspath(target))
    cand = resolved
    while True:
        same = os.path.normcase(cand) == os.path.normcase(repo_data)
        if not same:
            try:
                same = os.path.exists(cand) and os.path.samefile(cand, repo_data)
            except OSError:
                same = False
        if same:
            return f"{resolved} is the live repo data dir (or under it): {repo_data}"
        if os.path.exists(os.path.join(cand, ".protected")):
            return f"{cand} carries the '.protected' marker"
        parent = os.path.dirname(cand)
        if parent == cand:
            return ""
        cand = parent


def main(argv=None) -> int:
    args = _parse_args(argv)

    # Issue #523: never fall back to the app default data dir (the live repo data/).
    # An explicit target is REQUIRED, and a protected dir (the '.protected' marker,
    # the same posture as scripts/test_instance.py's assert_data_dir_safe) is
    # refused. Both guards run BEFORE app.config/app.db are imported: importing
    # app.db pre-creates the SQLite file and init_db() migrates it, so a refusal
    # must precede them, not follow them.
    target = args.data_dir or os.environ.get("LOC_TMS_DATA_DIR", "").strip()
    if not target:
        _emit(
            "error: refusing to run without an explicit target. Pass --data-dir <dir>"
            " (or set LOC_TMS_DATA_DIR) to a throwaway data dir; the app default"
            " 'data/' is a live install and is never seeded by surprise."
        )
        return 2
    reason = _live_dir_reason(target)
    if reason:
        _emit(
            f"error: refusing to touch a protected/live data dir: {reason}. Point"
            " --data-dir at a throwaway dir instead."
        )
        return 2
    if args.data_dir:
        os.environ["LOC_TMS_DATA_DIR"] = args.data_dir
    if os.environ.get("DATABASE_URL", "").strip():
        _emit(
            "error: refusing to run with DATABASE_URL set; the seeder only targets a"
            " throwaway SQLite data dir, never an external database."
        )
        return 2

    # The data dir MUST be resolved before app.config/app.db are imported (the engine
    # is built from it at import time). The env var is set above, then imported lazily.
    from app.config import DB_PATH, IS_SQLITE, PROTECT_MARKER_NAME, data_dir_is_protected

    if data_dir_is_protected():
        _emit(
            f"error: refusing to touch the protected data dir {DB_PATH.parent} (it"
            f" carries the '{PROTECT_MARKER_NAME}' marker). Point --data-dir at a"
            " throwaway dir instead."
        )
        return 2

    if not IS_SQLITE or os.environ.get("DATABASE_URL", "").strip():
        # DATABASE_URL can also arrive via the repo .env that app.config loads
        # (load_dotenv fills os.environ). A sqlite:/// URL there would move DB_PATH
        # off the target dir (#525), possibly onto the live file, so refuse any.
        _emit("error: refusing to run with DATABASE_URL set (repo .env or environment).")
        return 2

    if args.dry_run:
        # Issue #523: a dry-run must not touch the DB at all. init_db() on a real
        # SQLite install runs `alembic upgrade head` in-process regardless of
        # --dry-run. No DB file means there is nothing to report; an existing DB is
        # READ without migrating. The check runs before app.db is imported because
        # that import itself pre-creates the SQLite file (config.prepare_sqlite_file).
        if IS_SQLITE and not DB_PATH.exists():
            _emit(f"[dry-run] no database at {DB_PATH}; nothing exists to report.")
            return 0
        _emit("[dry-run] no changes will be written.")

    from sqlmodel import Session

    from app.db import engine, init_db

    if not args.dry_run:
        init_db()

    locales, unknown = _selected_locales(args.locales)
    for loc in unknown:
        _emit(f"warning: no authored content for locale {loc!r}; skipping it.")
    if not locales:
        _emit(
            "error: no seedable locales selected (content covers: " + ", ".join(ALL_LOCALES) + ")."
        )
        return 2

    with Session(engine) as session:
        if args.cleanup:
            _cleanup(session, args.project, args.dry_run)
            return 0

        # Attach mode = the caller pointed us at an EXISTING project (--project). Only
        # then does the skip-on-collision guard apply; a seed-owned project is safe.
        attach_mode = args.project is not None
        _seed_glossary(session, locales, args.dry_run)
        project, _created = _resolve_project(session, args.project, args.dry_run)
        if project is not None:
            _seed_tm(session, project, locales, args.dry_run, attach_mode)
        elif args.dry_run:
            # dry-run default mode: no project row exists to attach to; still report
            # the TM plan against the would-be project.
            _seed_tm(session, None, locales, args.dry_run, attach_mode)

    _emit("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
