"""Real-Postgres schema smoke check (Phase 4-D).

This is the validation that CANNOT be done offline / on SQLite: after
``alembic upgrade head`` has been run against a LIVE Postgres server, assert that

  1. every table in ``SQLModel.metadata`` actually exists in the Postgres DB,
     plus alembic's ``alembic_version`` bookkeeping table;
  2. the four hot-path indexes from app/db.py exist on Postgres, INCLUDING the
     UNIQUE ``ux_segment_doc_locale_order`` (checked to actually be unique);
  3. the single seeded ``'Default'`` organization row is present;
  4. a functional FK slice (insert chain + cascade deletes), rolled back;
  5. the SETTINGS cross-process version machinery (app/services/settings.py). This
     one is Postgres-ONLY evidence by construction: the version row is bumped with
     ``CAST(CAST(value AS INTEGER) + 1 AS VARCHAR)``, and a non-numeric value in that
     row is a SILENT 0 on SQLite but a HARD ERROR on Postgres. So the poisoned-row
     self-heal — the guard that keeps every future settings save runnable after
     someone pokes the row — is the kind of code a fully green SQLite suite cannot
     tell from its own absence. Same for the raw ``INSERT INTO setting`` first-save
     path and the CAST arithmetic itself.

It is DELIBERATELY GUARDED: it only runs when ``DATABASE_URL`` is a Postgres DSN.
On the SQLite default (or with no ``DATABASE_URL``) it prints a skip line and
exits 0, so it is safe to invoke from anywhere. CI's ``postgres-smoke`` job sets
``DATABASE_URL`` to the service DB and runs ``alembic upgrade head`` first, then
invokes this script.

Run it directly:
    DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/db \
        python tests/pg_smoke.py

Exit code 0 = pass (or cleanly skipped on non-Postgres); non-zero = failure.
ASCII-only output (matches the verify_*.py console convention).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _is_postgres(url: str) -> bool:
    return url.startswith("postgres") or url.startswith("postgresql")


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "")
    if not _is_postgres(db_url):
        print("PG SMOKE SKIPPED (DATABASE_URL is not a Postgres DSN; nothing to check)")
        return 0

    from sqlalchemy import create_engine, inspect, text

    # Import models so SQLModel.metadata is fully populated (all tables register).
    from app import models

    engine = create_engine(db_url)
    try:
        insp = inspect(engine)

        # --- 1. every model table exists (plus alembic bookkeeping) ---
        present = set(insp.get_table_names())
        expected = set(models.SQLModel.metadata.tables.keys())
        missing = expected - present
        if missing:
            print(f"FAIL: migration did not create tables on Postgres: {sorted(missing)}")
            return 1
        if "alembic_version" not in present:
            print(
                "FAIL: alembic_version table absent -- `alembic upgrade head` "
                "was not run against this Postgres DB"
            )
            return 1
        print("  pg: %d model tables present on Postgres (+ alembic_version)" % len(expected))  # noqa: UP031

        # --- 2. the four hot-path indexes exist (incl. UNIQUE segment index) ---
        def _index_names(table):
            return {ix["name"] for ix in insp.get_indexes(table)}

        def _unique_index_names(table):
            return {ix["name"] for ix in insp.get_indexes(table) if ix.get("unique")}

        seg_ix = _index_names("segment")
        checks = [
            ("ix_segment_doc_locale", "segment", seg_ix),
            ("ux_segment_doc_locale_order", "segment", seg_ix),
            ("ix_segmenttm_lookup", "segmenttm", _index_names("segmenttm")),
            ("ix_fulltexttm_lookup", "fulltexttm", _index_names("fulltexttm")),
        ]
        for name, table, names in checks:
            if name not in names:
                print(f"FAIL: missing index {name} on {table}: have {sorted(names)}")
                return 1
        if "ux_segment_doc_locale_order" not in _unique_index_names("segment"):
            print("FAIL: ux_segment_doc_locale_order exists but is not UNIQUE on Postgres")
            return 1
        print("  pg: 4 hot-path indexes present (incl. UNIQUE ux_segment_doc_locale_order)")

        # --- 3. the single 'Default' organization row is seeded ---
        with engine.connect() as c:
            orgs = c.execute(text("SELECT name FROM organization ORDER BY name")).fetchall()
        names = [row[0] for row in orgs]
        if "Default" not in names:
            print(f"FAIL: 'Default' organization row not seeded on Postgres; got {names!r}")
            return 1
        print("  pg: 'Default' organization row present")

        # --- 4. functional FK slice: an end-to-end insert chain + cascade delete
        # that EXERCISES the FK-sensitive paths on real Postgres (the schema check
        # above only proves tables/indexes exist; it never inserts a row, so it
        # could not have caught the class of bug where an FK-invalid write reaches
        # main). Postgres always enforces these FKs, so this is where a delete-order
        # or dangling-FK regression surfaces even though SQLite now also enforces
        # them. Runs inside ONE transaction that is ROLLED BACK, so it is
        # self-cleaning and leaves the smoke DB byte-unchanged.
        if _functional_fk_slice(engine) != 0:
            return 1

        # --- 4b. concept/term store FK delete order + idempotent backfill
        # (SPEC-terminology-backend T1, LAW-TERM-2b/6). Postgres enforces the
        # glossaryentry.term_id / glossaryterm self-FK the H1 shared-source guard
        # depends on, so a wrong delete order raises here rather than only on
        # SQLite-under-PRAGMA. Rolled back like the slice above.
        if _concept_store_fk_slice(engine) != 0:
            return 1

        # --- 4c. bulk glossary import (Core executemany reconcile) THEN an ORM add_term
        # seam, on REAL Postgres. The terminology bulk importer inserts new concepts/terms
        # with ``insert(...).returning(id)`` and lets the DB assign ids; if it ever supplied
        # ids explicitly (round-1 H2), the Postgres SERIAL sequence would NOT advance and the
        # very next ORM autoincrement insert (the per-entry dual-write behind add_term) would
        # collide. SQLite hides this (rowid tracks max(id)); only Postgres proves the sequence
        # advanced. Rolled back like the slices above.
        if _bulk_import_sequence_slice(engine) != 0:
            return 1

        # --- 5. the settings cross-process version machinery on real Postgres ---
        if _settings_version_slice(engine) != 0:
            return 1

        # --- 6. the dormant super-admin flag is CLEARED on Postgres (gate R4 MED-1).
        # The back-office and the super-admin were removed 2026-09-20. The column stays
        # (dropping it is a destructive migration) and app/db.py clears it at boot, because
        # the deleted bootstrap had FLAGGED the first admin on every install that ever
        # booted: dormant AND SET is fail-OPEN.
        #
        # This slice exists because the clear shipped BROKEN on Postgres twice. First it
        # lived in a SQLite-only helper so it never ran here at all; then it ran and raised,
        # because `user` is a reserved word that must be quoted and the column is BOOLEAN so
        # `= 0`/`= 1` are type errors. Both times the function's own except-swallow reduced
        # the failure to a log line, and this file never called init_db(), so nothing in the
        # repo could see it. A source-text ratchet in verify_multiorg now pins the two
        # conventions; this is the check that proves the real thing on the real backend.
        if _superadmin_clear_slice(engine) != 0:
            return 1

        # --- 7. Postgres boots with the multi-org gate ON (#494). The #73 org-confinement
        # invariant runs on this backend's init_db branch and used to name the user table
        # unquoted (`FROM user`), a syntax error on Postgres that stopped every boot with
        # multi-org on. SQLite tolerates the bare word, so only this slice can see it.
        if _multi_org_boot_slice(engine) != 0:
            return 1
    finally:
        engine.dispose()

    print("PG SMOKE OK")
    return 0


def _functional_fk_slice(engine) -> int:
    """Insert org -> customer -> project -> target locale -> glossary(+entry, NULL
    project) -> SegmentTM(+usage bump) -> SegmentEmbedding -> WebhookEvent(via a
    connection) -> EngineProfile -> ProjectPairProvider, then exercise the two
    delete-order-sensitive paths (delete_project_tm's embedding-before-TM sweep and
    a connection delete's child sweep). All in one rolled-back transaction.

    A dangling-FK INSERT is also attempted in a SAVEPOINT and asserted to raise, so
    this proves enforcement is LIVE on the Postgres side too, not merely declared.
    Returns 0 on success, 1 on failure (ASCII-only output, matching the file)."""
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session, select

    from app.models import (
        Connection,
        Customer,
        Document,
        EngineProfile,
        Glossary,
        GlossaryEntry,
        Organization,
        Project,
        ProjectPairProvider,
        SegmentEmbedding,
        SegmentTM,
        SyncLog,
        TargetLocale,
        WebhookEvent,
    )
    from app.services import retention

    with Session(engine) as s:
        try:
            org = Organization(name="PGSmoke Org")
            s.add(org)
            s.flush()
            cust = Customer(name="PGSmoke Cust", org_id=org.id)
            s.add(cust)
            s.flush()
            proj = Project(
                name="PGSmoke Proj", source_locale="en-US", org_id=org.id, customer_id=cust.id
            )
            s.add(proj)
            s.flush()
            s.add(TargetLocale(project_id=proj.id, locale="fr-FR"))
            # Module glossary + a MODULE-owned entry with NULL project (the exact
            # shape of the sentinel bug: an FK-invalid project_id=0 would be rejected
            # here on Postgres; NULL is the correct FK-valid value).
            gl = Glossary(name="PGSmoke GL", org_id=org.id, customer_id=cust.id)
            s.add(gl)
            s.flush()
            s.add(
                GlossaryEntry(
                    glossary_id=gl.id,
                    project_id=None,
                    source_locale="en-US",
                    target_locale="fr-FR",
                    source_term="login",
                    target_term="connexion",
                )
            )
            # SegmentTM + a usage bump (in-place UPDATE) + a child embedding.
            seg_tm = SegmentTM(
                project_id=proj.id,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="hi",
                target_text="salut",
                source_hash="pgsmoke-h1",
            )
            s.add(seg_tm)
            s.flush()
            seg_tm.use_count = (seg_tm.use_count or 0) + 1  # usage bump path
            s.add(seg_tm)
            s.add(
                SegmentEmbedding(
                    segment_tm_id=seg_tm.id,
                    project_id=proj.id,
                    source_locale="en-US",
                    target_locale="fr-FR",
                    model="pgsmoke",
                    dim=1,
                    embedding=b"\x00\x00\x80?",
                )
            )
            # A connection + a webhook event row + a sync log (both connection_id FKs).
            conn = Connection(
                kind="airtable",
                name="PGSmoke Conn",
                org_id=org.id,
                customer_id=cust.id,
                created_by="pgsmoke",
            )
            s.add(conn)
            s.flush()
            s.add(WebhookEvent(connection_id=conn.id, event_id="pgsmoke-evt-1", kind="airtable"))
            s.add(SyncLog(connection_id=conn.id, direction="test", actor="pgsmoke"))
            # Engine profile + a per-pair provider row (both real FKs).
            s.add(EngineProfile(name="PGSmoke Engine", kind="deepl", org_id=org.id))
            s.add(
                ProjectPairProvider(
                    project_id=proj.id,
                    source_locale="en-US",
                    target_locale="fr-FR",
                    provider="claude",
                )
            )
            s.flush()
            print(
                "  pg: FK insert chain (org->customer->project->glossary->TM->"
                "embedding->webhook->engine->pair-provider) OK"
            )

            # Dangling FK must be REJECTED even inside Postgres (SAVEPOINT so the
            # outer transaction survives the expected error).
            rejected = False
            try:
                with s.begin_nested():
                    s.add(Document(project_id=987654321, filename="dangling.txt", fmt="txt"))
                    s.flush()
            except IntegrityError:
                rejected = True
            if not rejected:
                print("FAIL: dangling document.project_id INSERT was not rejected on Postgres")
                return 1
            print("  pg: dangling-FK INSERT rejected (enforcement is LIVE) OK")

            # delete_project_tm must sweep the embedding BEFORE the SegmentTM it
            # references (the retention delete-order fix). On Postgres a wrong order
            # would raise here.
            removed = retention.delete_project_tm(s, proj.id)
            assert removed >= 1, "delete_project_tm removed nothing"
            assert (
                s.exec(
                    select(SegmentEmbedding.id).where(SegmentEmbedding.project_id == proj.id)
                ).first()
                is None
            ), "SegmentEmbedding survived delete_project_tm on Postgres"
            print("  pg: delete_project_tm cascade (embedding-before-TM) OK")

            # A connection delete must sweep its WebhookEvent + SyncLog children
            # first (the connector store delete-order fix). conn_store.delete opens
            # its OWN session/engine + commits, so it can't run inside this rolled-
            # back transaction; assert the child-first ORDER here directly instead,
            # which is the load-bearing invariant.
            for child in (WebhookEvent, SyncLog):
                for row in s.exec(select(child).where(child.connection_id == conn.id)).all():
                    s.delete(row)
            s.flush()
            s.delete(conn)
            s.flush()
            assert (
                s.exec(select(WebhookEvent.id).where(WebhookEvent.connection_id == conn.id)).first()
                is None
            )
            print("  pg: connection delete cascade (children-before-parent) OK")
        finally:
            # Self-cleaning: undo everything this slice inserted.
            s.rollback()
    return 0


def _concept_store_fk_slice(engine) -> int:
    """The concept/term store's LAW-TERM-2b delete order + LAW-TERM-6 idempotent backfill,
    on REAL Postgres (where the glossaryentry.term_id FK + the glossaryterm self-FK are
    always enforced). Builds a THREE-language concept whose three compat entries SHARE one
    source GlossaryTerm (the H1 shape), then:

      * deletes ONE pair -> the shared source term + the other two entries SURVIVE with no
        IntegrityError, only the orphaned target term goes;
      * deletes the remaining pairs -> the source term and the (now termless) concept go;
      * a term that is ANOTHER glossary's deprecated_replacement is cleared before its
        delete (no IntegrityError, M11);
      * backfill_glossary is idempotent (a glossary that already has a concept returns 0).

    All inside ONE rolled-back transaction (self-cleaning). Returns 0/1."""
    from sqlmodel import Session, select

    from app.models import (
        Customer,
        Glossary,
        GlossaryConcept,
        GlossaryEntry,
        GlossaryTerm,
        Organization,
    )
    from app.services import glossary_concepts as gc

    with Session(engine) as s:
        try:
            org = Organization(name="PGSmoke ConceptOrg")
            s.add(org)
            s.flush()
            cust = Customer(name="PGSmoke ConceptCust", org_id=org.id)
            s.add(cust)
            s.flush()
            gl = Glossary(name="PGSmoke ConceptGL", org_id=org.id, customer_id=cust.id)
            s.add(gl)
            s.flush()
            # one concept, a shared en source term + es/fr/de targets, 3 compat entries.
            concept = GlossaryConcept(glossary_id=gl.id)
            s.add(concept)
            s.flush()
            src = GlossaryTerm(concept_id=concept.id, locale="en-US", term="bank")
            s.add(src)
            s.flush()
            entries = {}
            for loc, surface in (("es-ES", "banco"), ("fr-FR", "banque"), ("de-DE", "Bank")):
                tgt = GlossaryTerm(concept_id=concept.id, locale=loc, term=surface)
                s.add(tgt)
                s.flush()
                e = GlossaryEntry(
                    glossary_id=gl.id,
                    project_id=None,
                    source_locale="en-US",
                    target_locale=loc,
                    source_term="bank",
                    target_term=surface,
                    concept_id=concept.id,
                    term_id=src.id,
                )
                s.add(e)
                s.flush()
                entries[loc] = e
            src_id = src.id
            # delete the en->es pair; the shared source + fr/de must survive.
            es = entries["es-ES"]
            s.delete(es)
            s.flush()
            gc.delete_entry_unit(s, gl.id, concept.id, src_id, "es-ES")
            if s.get(GlossaryTerm, src_id) is None:
                print("FAIL(pg concept): shared source term dropped on a sibling-pair delete (H1)")
                return 1
            live = s.exec(select(GlossaryEntry.id).where(GlossaryEntry.glossary_id == gl.id)).all()
            if len(list(live)) != 2:
                print("FAIL(pg concept): expected 2 surviving pairs after the H1 delete")
                return 1
            print("  pg: concept H1 shared-source delete (source + other pairs survive) OK")

            # idempotent backfill: this glossary already has a concept -> guard skips it.
            # Judged on THIS glossary only: backfill_glossary scans every glossary, and an
            # earlier slice's glossary can persist (delete_project_tm commits its session),
            # so a backfill of THAT glossary is correct and must not fail this check.
            def _concepts(gid):
                return len(
                    s.exec(
                        select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == gid)
                    ).all()
                )

            before = _concepts(gl.id)
            gc.backfill_glossary(s)
            if _concepts(gl.id) != before:
                print("FAIL(pg concept): backfill_glossary was not idempotent (guard failed)")
                return 1
            # cross-glossary deprecated_replacement clear (M11): a second glossary's term
            # points at the surviving source term; deleting the whole first glossary must
            # clear that inbound ref first.
            gl2 = Glossary(name="PGSmoke ConceptGL2", org_id=org.id, customer_id=cust.id)
            s.add(gl2)
            s.flush()
            c2 = GlossaryConcept(glossary_id=gl2.id)
            s.add(c2)
            s.flush()
            dep = GlossaryTerm(
                concept_id=c2.id,
                locale="en-US",
                term="banc",
                status="deprecated",
                deprecated_replacement_term_id=src_id,
            )
            s.add(dep)
            s.flush()
            for e in list(entries.values()):
                if s.get(GlossaryEntry, e.id) is not None:
                    s.delete(e)
            s.flush()
            gc.delete_glossary_concepts(s, gl.id)  # drops src_id -> must clear dep's ref
            dep_after = s.get(GlossaryTerm, dep.id)
            if dep_after is None or dep_after.deprecated_replacement_term_id is not None:
                print("FAIL(pg concept): cross-glossary deprecated_replacement not cleared (M11)")
                return 1
            print("  pg: cross-glossary deprecated_replacement cleared before delete (M11) OK")
        finally:
            s.rollback()
    return 0


def _bulk_import_sequence_slice(engine) -> int:
    """Round-1 H2 on REAL Postgres: the bulk concept reconcile must let the DB assign ids so
    the SERIAL sequence advances, or a later ORM autoincrement insert collides.

    Builds a module glossary with several concept-less entries, runs THE bulk importer's
    reconcile (``reconcile_glossary_from_entries`` -> ``_bulk_sync_glossary_from_entries``,
    the Core executemany-with-RETURNING path), then inserts a BRAND-NEW entry and mirrors it
    with ``sync_entry_concept`` -- the exact ORM autoincrement path ``glossary.add_term``
    drives. If the bulk path had minted ids in Python and inserted them explicitly, the
    ``glossaryconcept``/``glossaryterm`` sequences would still point at 1 here and this ORM
    insert would raise a duplicate-key IntegrityError. All inside ONE rolled-back
    transaction (self-cleaning). Returns 0/1."""
    from sqlmodel import Session, select

    from app.models import (
        Customer,
        Glossary,
        GlossaryConcept,
        GlossaryEntry,
        GlossaryTerm,
        Organization,
    )
    from app.services import glossary_concepts as gc

    with Session(engine) as s:
        try:
            org = Organization(name="PGSmoke BulkOrg")
            s.add(org)
            s.flush()
            cust = Customer(name="PGSmoke BulkCust", org_id=org.id)
            s.add(cust)
            s.flush()
            gl = Glossary(name="PGSmoke BulkGL", org_id=org.id, customer_id=cust.id)
            s.add(gl)
            s.flush()
            # concept-less module entries -> the bulk reconcile creates their concepts/terms
            # with the Core executemany-RETURNING path (the id authority under test).
            for i in range(5):
                s.add(
                    GlossaryEntry(
                        glossary_id=gl.id,
                        project_id=None,
                        source_locale="en-US",
                        target_locale="es-ES",
                        source_term=f"bulkterm{i}",
                        target_term=f"destino{i}",
                    )
                )
            s.flush()
            gc.reconcile_glossary_from_entries(s, gl.id)
            s.flush()
            concepts_after_bulk = s.exec(
                select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == gl.id)
            ).all()
            if len(list(concepts_after_bulk)) != 5:
                print("FAIL(pg bulk): reconcile did not create 5 concepts")
                return 1
            # Gate round 2 M3: the batched RETURNING path runs ONLY on Postgres (SQLite is
            # per-row), so prove the returned ids were folded back in ROW ORDER: every bulk-linked
            # entry must point at the term carrying ITS OWN surface, and that term's concept
            # must hold a target term carrying the entry's target surface.
            for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gl.id)).all():
                src = s.get(GlossaryTerm, e.term_id) if e.term_id else None
                if src is None or src.term != e.source_term or (src.locale or "") != "en-US":
                    print(
                        f"FAIL(pg bulk): entry {e.source_term!r} links term_id={e.term_id} "
                        f"carrying {getattr(src, 'term', None)!r} -- RETURNING ids misaligned"
                    )
                    return 1
                tgt_surfaces = {
                    t.term
                    for t in s.exec(
                        select(GlossaryTerm).where(
                            GlossaryTerm.concept_id == e.concept_id,
                            GlossaryTerm.locale == "es-ES",
                        )
                    ).all()
                }
                if e.target_term not in tgt_surfaces:
                    print(
                        f"FAIL(pg bulk): entry {e.source_term!r} concept {e.concept_id} holds "
                        f"targets {sorted(tgt_surfaces)} not {e.target_term!r} -- misaligned"
                    )
                    return 1
            # NOW the ORM autoincrement path (what add_term runs): a brand-new entry mirrored
            # by sync_entry_concept. On Postgres this is the insert that collides under H2.
            fresh = GlossaryEntry(
                glossary_id=gl.id,
                project_id=None,
                source_locale="en-US",
                target_locale="es-ES",
                source_term="freshsurface",
                target_term="superficienueva",
            )
            s.add(fresh)
            s.flush()
            try:
                gc.sync_entry_concept(s, fresh)
                s.flush()
            except Exception as exc:
                print(
                    "FAIL(pg bulk): ORM add_term after a bulk import collided -- the bulk path "
                    f"did not advance the id sequence (H2): {type(exc).__name__}: {str(exc)[:120]}"
                )
                return 1
            if fresh.concept_id is None or fresh.term_id is None:
                print("FAIL(pg bulk): the post-import ORM add_term did not link a concept/term")
                return 1
            # the new concept/term ids must be DISTINCT from the bulk-created ones (no reuse).
            all_cids = s.exec(
                select(GlossaryConcept.id).where(GlossaryConcept.glossary_id == gl.id)
            ).all()
            if len(set(all_cids)) != 6:
                print(f"FAIL(pg bulk): expected 6 distinct concepts, got {sorted(set(all_cids))}")
                return 1
            print("  pg: bulk import + subsequent ORM add_term (SERIAL sequence advanced, H2) OK")
        finally:
            s.rollback()
    return 0


def _superadmin_clear_slice(engine) -> int:
    """The dormant super-admin flag is cleared AT BOOT on REAL Postgres (gate R4 MED-1).

    Seeds a flagged row, runs `init_db()`, and asserts the flag comes back clear. That is
    the whole point: the statement was wrong on Postgres TWICE (first it lived in a
    SQLite-only helper so it never ran here; then it ran and raised, because `user` is a
    reserved word that must be quoted and the column is BOOLEAN so an integer literal is a
    type error). Both failures were swallowed into a log line by the clear's own except arm,
    and this file never called `init_db()`, so nothing in the repo could see either one.

    GATE R5 rewrote this. The first version called `_clear_dormant_superadmin()` directly,
    which would have proved the STATEMENT while leaving the REACHABILITY unproven - and
    unreachability was the round-3 bug. It also could not run at all: its INSERT named
    `is_active` (the column is `active`) and omitted three NOT NULL columns whose defaults
    are Python-side only, so a raw text() INSERT does not apply them. Column list copied
    from tests/verify_user_email_unique.py:180, which does this correctly.

    Cleans up after itself rather than rolling back, because the clear commits and
    committing is the point. Returns 0 on success, 1 on failure."""
    from sqlalchemy import text

    from app.db import engine as app_engine
    from app.db import init_db

    if str(app_engine.url) != str(engine.url):
        print("  pg: SKIP superadmin clear slice (app engine is not the smoke DB)")
        return 0

    marker = "pg-smoke-superadmin@local.test"
    try:
        try:
            with app_engine.begin() as c:
                c.execute(text('DELETE FROM "user" WHERE email = :e'), {"e": marker})
                # EVERY NOT NULL column with a Python-side-only default is named: a raw
                # text() INSERT does not run SQLModel's defaults. This list was wrong
                # TWICE. First it named `is_active`, which is not a column (it is
                # `active`). Then it was copied from verify_user_email_unique.py:180 and
                # still omitted `department` and `ui_locale`. Both times reading the
                # template looked sufficient; only EXECUTING the slice found it.
                c.execute(
                    text(
                        'INSERT INTO "user" (email, name, role, password_hash, '
                        "auth_source, active, department, ui_locale, is_superadmin, "
                        "created_at) VALUES "
                        "(:e, :n, :r, :p, 'local', TRUE, '', 'en-US', TRUE, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"e": marker, "n": "pg smoke", "r": "admin", "p": "x"},
                )
        except Exception as exc:
            # Reach the diagnostic instead of propagating: the first version of this slice
            # raised on its own INSERT, which is a worse failure than the one it guards.
            print(f"FAIL: pg smoke could not seed a flagged row ({type(exc).__name__}: {exc})")
            return 1
        with app_engine.connect() as c:
            before = c.execute(
                text('SELECT is_superadmin FROM "user" WHERE email = :e'), {"e": marker}
            ).scalar()
        # Truthiness, not `is True`: Postgres returns a Python bool and SQLite returns
        # 1/0, and this slice is worth being able to drive on either. `is None` is kept
        # separate so a MISSING row can never read as a cleared one.
        if before is None:
            print("FAIL: pg smoke seeded no row at all (marker absent after INSERT)")
            return 1
        if not before:
            print(f"FAIL: pg smoke could not seed a FLAGGED row (got {before!r})")
            return 1

        # Through init_db(), NOT the private helper. Round 3's bug was that the clear was
        # unreachable from boot on this backend, so proving the statement without proving
        # the reachability would re-open exactly the hole this slice exists to close.
        init_db()

        with app_engine.connect() as c:
            after = c.execute(
                text('SELECT is_superadmin FROM "user" WHERE email = :e'), {"e": marker}
            ).scalar()
        if after is None:
            print(
                "FAIL: the marker row disappeared during init_db(). That is not a pass: "
                "this slice proves the flag was CLEARED, not that the row is gone."
            )
            return 1
        if after:
            print(
                "FAIL: the dormant super-admin flag was NOT cleared on Postgres "
                f"(still {after!r}). Two things can cause this and both have already "
                "happened once: the clear is not REACHED from init_db() on this backend "
                "(it lived in a SQLite-only helper until 2026-09-20), or it is reached and "
                "RAISES into its own except arm (`user` is a reserved word that must be "
                "quoted; the column is BOOLEAN so an integer literal is a type error). "
                "Check the log for 'could NOT clear' to tell them apart. See "
                "app/db.py::_clear_dormant_superadmin."
            )
            return 1
        # Name the dialect actually in use rather than asserting "Postgres": main()
        # guards this file on a Postgres DSN, but the slice can be driven directly
        # on SQLite, and a line that claims the wrong backend is how an unexecuted
        # check looks like an executed one.
        print(f"  {app_engine.dialect.name}: dormant super-admin flag cleared at boot")
        return 0
    finally:
        with app_engine.begin() as c:
            c.execute(text('DELETE FROM "user" WHERE email = :e'), {"e": marker})


def _multi_org_boot_slice(engine) -> int:
    """init_db() boots on REAL Postgres with the multi-org gate ON (#494).

    Turns on `security_auth_required` and `multi_org_enabled`, seeds one NULL-org user,
    runs `init_db()`, and asserts boot succeeded AND the user was adopted into an org.
    That drives `_assert_org_confinement_invariant`'s SELECT and backfill UPDATE against
    the `user` table on Postgres, where USER is a reserved word and the table must be
    written `"user"`.

    Never adopts anyone else: if the DB already holds a real NULL-org user it SKIPS (the
    backfill would move that account on a real install). Restores both settings rows to
    their prior state and deletes the marker user. Returns 0 on success, 1 on failure."""
    from sqlalchemy import text

    from app.db import engine as app_engine
    from app.db import init_db

    if str(app_engine.url) != str(engine.url):
        print("  pg: SKIP multi-org boot slice (app engine is not the smoke DB)")
        return 0

    marker = "pg-smoke-multiorg@local.test"
    keys = ("security_auth_required", "multi_org_enabled")
    with app_engine.connect() as c:
        c.execute(text('DELETE FROM "user" WHERE email = :e'), {"e": marker})
        c.commit()
        others = c.execute(text('SELECT COUNT(*) FROM "user" WHERE org_id IS NULL')).scalar()
        prior = {
            r[0]: r[1]
            for r in c.execute(
                text("SELECT key, value FROM setting WHERE key IN (:a, :b)"),
                {"a": keys[0], "b": keys[1]},
            ).fetchall()
        }
    if others:
        print(f"  pg: SKIP multi-org boot slice ({others} NULL-org user(s) already present)")
        return 0
    try:
        try:
            with app_engine.begin() as c:
                for k in keys:
                    c.execute(text("DELETE FROM setting WHERE key = :k"), {"k": k})
                    c.execute(text("INSERT INTO setting (key, value) VALUES (:k, '1')"), {"k": k})
                # Same column list as _superadmin_clear_slice: a raw INSERT does not run
                # SQLModel's Python-side defaults. org_id is left NULL on purpose.
                c.execute(
                    text(
                        'INSERT INTO "user" (email, name, role, password_hash, '
                        "auth_source, active, department, ui_locale, is_superadmin, "
                        "created_at) VALUES "
                        "(:e, :n, :r, :p, 'local', TRUE, '', 'en-US', FALSE, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"e": marker, "n": "pg smoke multiorg", "r": "admin", "p": "x"},
                )
        except Exception as exc:
            print(
                f"FAIL: pg smoke could not seed the multi-org slice ({type(exc).__name__}: {exc})"
            )
            return 1
        try:
            init_db()
        except Exception as exc:
            print(
                "FAIL: init_db() raised on Postgres with multi-org ON "
                f"({type(exc).__name__}: {exc}). The #73 confinement check runs on this "
                'branch; raw SQL there must name the user table as "user" (#494).'
            )
            return 1
        with app_engine.connect() as c:
            org = c.execute(
                text('SELECT org_id FROM "user" WHERE email = :e'), {"e": marker}
            ).fetchone()
        if org is None or org[0] is None:
            print(f"FAIL: the NULL-org user was not adopted at boot under multi-org (got {org!r})")
            return 1
        print(
            f"  {app_engine.dialect.name}: init_db boots with multi-org ON; NULL-org user adopted"
        )
        return 0
    finally:
        with app_engine.begin() as c:
            c.execute(text('DELETE FROM "user" WHERE email = :e'), {"e": marker})
            for k in keys:
                c.execute(text("DELETE FROM setting WHERE key = :k"), {"k": k})
                if k in prior:
                    c.execute(
                        text("INSERT INTO setting (key, value) VALUES (:k, :v)"),
                        {"k": k, "v": prior[k]},
                    )


def _settings_version_slice(engine) -> int:
    """The cross-process settings version row (audit K3) on REAL Postgres.

    tests/verify_settings_propagation.py pins this whole mechanism — but only on
    SQLite, where the two most backend-sensitive pieces are untestable:

      * ``CAST(CAST(value AS INTEGER) + 1 AS VARCHAR)`` — the atomic increment. SQLite
        will CAST anything (silently 0 for garbage); Postgres raises
        ``invalid input syntax for type integer``.
      * the SELF-HEAL for a poked/garbage version row. On SQLite a build with NO
        self-heal still limps along; on Postgres its absence makes EVERY FUTURE
        SETTINGS SAVE raise — the install's kill switches become unusable. This is
        the only place that difference is observable.

    So this slice runs the real service seam (``settings.set_many`` / ``settings.get``)
    against Postgres and asserts: a version round-trip (save -> bump -> read), that a
    FOREIGN writer's change propagates into a running process's cache, and that a
    poisoned version row self-heals with propagation still working afterwards.

    Unlike the FK slice this cannot run inside a rolled-back transaction — set_many
    commits, and committing is the point — so it CLEANS UP AFTER ITSELF instead,
    deleting the rows it created. Returns 0 on success, 1 on failure."""
    import time

    from sqlalchemy import text

    from app import config
    from app.db import engine as app_engine
    from app.services import settings

    # The service seam binds to app.db.engine at import; CI points both at the same
    # DSN via DATABASE_URL. Refuse to "pass" if that ever stops being true.
    if not config.DB_URL.startswith(("postgres", "postgresql")):
        print(
            f"FAIL: app.db.engine is not on Postgres ({config.DB_URL!r}) -- the settings slice "
            "would silently be testing SQLite"
        )
        return 1

    # A dedicated key OUTSIDE DEFAULTS -- never a real setting -- so this slice can
    # never clobber an install's actual configured value (an earlier version of this
    # probed "org_name", a REAL setting, and its cleanup unconditionally DELETEd it;
    # harmless in CI's ephemeral container but the module docstring above explicitly
    # invites running this against ANY DATABASE_URL, including a real install's).
    probe_key = "__pgsmoke_probe__"
    version_key = settings._VERSION_KEY
    orig_check_interval = settings.CHECK_INTERVAL_SECONDS
    settings.CHECK_INTERVAL_SECONDS = 0.2  # keep the smoke fast; ships at 2.0

    def raw_value(key):
        """Read a setting row on the OTHER engine (never through the cache)."""
        with engine.connect() as c:
            row = c.execute(text("SELECT value FROM setting WHERE key = :k"), {"k": key}).fetchone()
        return None if row is None else row[0]

    # Snapshot the version row BEFORE this slice ever writes to it, so `finally` can
    # put it back exactly (restore its prior value if it existed; delete it only if
    # it did not) instead of deleting it outright. probe_key needs no such snapshot --
    # it is never a real setting, so unconditionally deleting it afterward is safe.
    _orig_version_value = raw_value(version_key)

    def foreign_write(value):
        """What a second PROCESS does: write the value and bump the version with the
        module's own portable SQL, on a connection this process's cache knows nothing
        about."""
        with engine.begin() as c:
            c.execute(
                text("UPDATE setting SET value = :v WHERE key = :k"), {"v": value, "k": probe_key}
            )
            c.execute(
                text(
                    "UPDATE setting SET value = "
                    "CAST(CAST(value AS INTEGER) + 1 AS VARCHAR) "
                    "WHERE key = :k"
                ),
                {"k": version_key},
            )

    def wait_for(want, budget=15.0):
        started = time.monotonic()
        while time.monotonic() - started < budget:
            if settings.get(probe_key) == want:
                return True
            time.sleep(0.05)
        return False

    try:
        # --- a. round trip: the first save CREATES the row, the next INCREMENTS it.
        # This is the raw INSERT and the CAST arithmetic executing on Postgres for the
        # first time anywhere in the suite.
        settings.set_many({probe_key: "pgsmoke-settings-1"})
        first = raw_value(version_key)
        if first is None or not first.isdigit():
            print(
                "FAIL: the settings version row is missing or non-numeric after a "
                f"save on Postgres: {first!r}"
            )
            return 1
        settings.set_many({probe_key: "pgsmoke-settings-2"})
        second = raw_value(version_key)
        if second is None or int(second) != int(first) + 1:
            print(
                f"FAIL: the Postgres version bump did not advance by exactly 1: {first!r} -> {second!r}"  # noqa: E501
            )
            return 1
        if settings.get(probe_key) != "pgsmoke-settings-2":
            print(
                f"FAIL: settings.get did not serve the value just saved on Postgres: {settings.get(probe_key)!r}"  # noqa: E501
            )
            return 1
        print("  pg: settings version round-trip (create -> CAST increment -> read) OK")

        # --- b. a FOREIGN writer's change reaches this process's cache.
        foreign_write("pgsmoke-from-elsewhere")
        if not wait_for("pgsmoke-from-elsewhere"):
            print(
                "FAIL: an out-of-process settings write never propagated on "
                f"Postgres; still serving {settings.get(probe_key)!r}"
            )
            return 1
        print("  pg: an out-of-process settings write propagates into a running process OK")

        # --- c. THE POSTGRES-ONLY ONE: a poisoned version row must self-heal.
        # Without the guard in _bump_version this set_many raises
        # (invalid input syntax for type integer) and the install can never save a
        # setting again -- kill switches included.
        with engine.begin() as c:
            c.execute(
                text("UPDATE setting SET value = 'not-a-number' WHERE key = :k"), {"k": version_key}
            )
        try:
            settings.set_many({probe_key: "pgsmoke-after-the-poison"})
        except Exception as exc:
            print(
                "FAIL: a poisoned version row made a settings SAVE unrunnable on "
                "Postgres ({}: {}). Every kill switch on the install is now stuck.".format(
                    type(exc).__name__, str(exc).split("\n")[0][:200]
                )
            )
            return 1
        healed = raw_value(version_key)
        if healed != "1":
            print(f"FAIL: the version row did not self-heal on Postgres; it holds {healed!r}")
            return 1
        if settings.get(probe_key) != "pgsmoke-after-the-poison":
            print(f"FAIL: the save after the poison did not land: {settings.get(probe_key)!r}")
            return 1

        # ...and propagation still works on the healed sequence.
        foreign_write("pgsmoke-propagating-after-the-heal")
        if not wait_for("pgsmoke-propagating-after-the-heal"):
            print(
                "FAIL: propagation broke after the version row was poisoned + "
                f"healed on Postgres; serving {settings.get(probe_key)!r}"
            )
            return 1
        print("  pg: a poisoned version row self-heals and propagation survives it OK")
    finally:
        # Self-cleaning: put the version row back to what it held before this slice
        # touched it (restore its prior value, or delete it if it never existed --
        # NEVER an unconditional delete, since on a real install this row is live
        # cross-process machinery, not a throwaway probe), and drop the dedicated
        # probe key this slice created.
        try:
            with engine.begin() as c:
                if _orig_version_value is None:
                    c.execute(text("DELETE FROM setting WHERE key = :k"), {"k": version_key})
                else:
                    c.execute(
                        text("UPDATE setting SET value = :v WHERE key = :k"),
                        {"v": _orig_version_value, "k": version_key},
                    )
                c.execute(text("DELETE FROM setting WHERE key = :k"), {"k": probe_key})
        except Exception:
            pass
        settings.CHECK_INTERVAL_SECONDS = orig_check_interval
        settings.invalidate_cache()
        app_engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
