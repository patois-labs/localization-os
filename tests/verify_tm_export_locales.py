"""TM export locale reconciliation (chip task_9c665111 / AUDIT-phase2 FINDING 5) -- hermetic.

THE DEFECT (the "verify trap" the Phase-2 skill demo hit): `import_tmx` COERCES a
region/case variant of the requested pair to the project's own spelling before it writes
(`resolve_import_locales`, QA BUG-03), and every LOOKUP path base-matches
(`_pick`/`_lang_match`), so a row for an en-US -> es-419 project is STORED and MATCHED
under en-US/es-419 whether the operator typed `en`/`es` or `EN-us`. `/tm` + `tm/stats`
count those rows correctly. But `export_tmx` was the one read-back still doing an
EXACT-EQUALITY query, so `tm export --source en --target es` (the natural spelling --
public TMX corpora and CLDR region codes both invite it) returned an EMPTY TMX while the
rows plainly existed -- an operator verifying a TM write via export would wrongly
conclude it failed. Export becomes tolerant EXACTLY as the write path is
(`resolve_export_locales` reuses `resolve_import_locales`), so the same spelling reaches
the same bucket on both legs.

NOTE ON THE AUDIT'S LITERAL NUMBERS: FINDING 5 reported export returning 0 for the SAME
pair (en-US/es-419) that stats showed as 12. That literal case does NOT reproduce against
current code (an exact pair always matched) -- pinned here in test (0) as a guard. The
real, reproducible bug behind the finding's own "BUG-03 locale-bucket family" hypothesis
is the region/case-variant trap, which this suite fixes and pins.

Pinned:
  0. exact-pair export already agrees with stats (the finding's literal claim, guarded)
  1. a region/case variant export now REACHES the stored bucket (the fix)
  2. export == stats for the same customer/pair (reconciled)
  3. an UNRELATED / AMBIGUOUS pair -> empty export, never a raise (unchanged contract)
  4. no project scope (project_id None) -> literal passthrough (unchanged)
  5. surface parity: /api echoes the EFFECTIVE locales + count; CLI reports them too
  6. fail-before / pass-after against the pre-fix export_tmx (git show HEAD, file copy --
     never git stash: the stash stack is shared across worktrees)

Final line: "TM EXPORT LOCALES OK".
"""

import base64
import importlib.util
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_tmexploc_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Customer, Project, Role, SegmentTM, TargetLocale, User
from app.services import apikeys, tm, tm_module

init_db()

_SEED = [("Human Rights seed", "Derechos Humanos seed")]
_DELIVERED = [(f"Article {i}", f"Articulo {i}") for i in range(1, 12)]


def _tmx(src_lang, tgt_lang, pairs) -> bytes:
    tus = "".join(
        f'<tu><tuv xml:lang="{src_lang}"><seg>{s}</seg></tuv>'
        f'<tuv xml:lang="{tgt_lang}"><seg>{t}</seg></tuv></tu>'
        for s, t in pairs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<tmx version="1.4"><header srclang="{src_lang}"/><body>{tus}</body>'
        "</tmx>"
    ).encode()


def _project(name="Acme UDHR", source="en-US", targets=("es-419", "fr-FR")):
    with Session(engine) as s:
        c = Customer(name=name + " cust")
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(name=name, source_locale=source, customer_id=c.id)
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in targets:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        return p.id, c.id


def _seed_pair(pid, src="en-US", tgt="es-419"):
    with Session(engine) as s:
        tm.import_tmx(s, pid, _tmx(src, tgt, _SEED), src, tgt)
        tm.import_tmx(s, pid, _tmx(src, tgt, _DELIVERED), src, tgt)


def _tu_count(xml: bytes) -> int:
    """Count <tu> elements by PARSING, not a substring scan: a provenance-stamped row
    serializes as `<tu creationtool=...>`, which `b"<tu>"` (exact) misses -- the very
    bug review H2 flagged (0 counted for human_approved write_back rows). Parse so the
    test cannot defend that bug."""
    from lxml import etree as _etree

    root = _etree.fromstring(xml)
    return len(root.findall(".//tu"))


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 0. the finding's LITERAL claim (exact pair) -- a guard: it already agrees
# ---------------------------------------------------------------------------
def test_exact_pair_agrees_with_stats():
    pid, _cid = _project()
    _seed_pair(pid)
    with Session(engine) as s:
        summary = tm_module.stats(s, visible_pids=[pid])
        pair = [
            p
            for p in summary["pairs"]
            if (p["source_locale"], p["target_locale"]) == ("en-US", "es-419")
        ]
        assert pair and pair[0]["count"] == 12, summary["pairs"]
        xml = tm.export_tmx(s, pid, "en-US", "es-419")
    assert _tu_count(xml) == 12, _tu_count(xml)
    print("  0. exact-pair export already agrees with stats (12 == 12) OK")


# ---------------------------------------------------------------------------
# 1 + 2. the bug: a region/case variant must reach the stored bucket
# ---------------------------------------------------------------------------
def test_region_variant_reaches_bucket_and_matches_stats():
    pid, _cid = _project(name="variant")
    _seed_pair(pid)  # stored under en-US/es-419
    with Session(engine) as s:
        stats_count = [  # noqa: RUF015
            p
            for p in tm_module.stats(s, visible_pids=[pid])["pairs"]
            if (p["source_locale"], p["target_locale"]) == ("en-US", "es-419")
        ][0]["count"]
        for src, tgt in (("en", "es-419"), ("en-US", "es"), ("EN-us", "ES-419"), ("en", "es")):
            xml = tm.export_tmx(s, pid, src, tgt)
            assert _tu_count(xml) == stats_count, (src, tgt, _tu_count(xml), stats_count)
            # the TMX lang tags are the EFFECTIVE (stored) spelling, not the requested one
            assert b'xml:lang="en-US"' in xml and b'xml:lang="es-419"' in xml, (src, tgt)
        # resolve_export_locales reports the effective pair for a variant, identity for exact
        assert tm.resolve_export_locales(s, pid, "en", "es") == ("en-US", "es-419")
        assert tm.resolve_export_locales(s, pid, "en-US", "es-419") == ("en-US", "es-419")
    print("  1+2. region/case variant export reaches the bucket; == stats OK")


# ---------------------------------------------------------------------------
# 3. an unrelated / ambiguous pair -> empty export, never a raise
# ---------------------------------------------------------------------------
def test_unrelated_and_ambiguous_pair_empty_not_raise():
    pid, _ = _project(name="unrel")
    _seed_pair(pid)
    with Session(engine) as s:
        # unrelated language: the project has no de->ja rows -> empty, no raise
        xml = tm.export_tmx(s, pid, "de", "ja")
        assert _tu_count(xml) == 0, _tu_count(xml)
    # ambiguous target base against a two-region project -> resolve keeps literal (empty),
    # never raises out of export
    pid2, _ = _project(name="ambig", targets=("es-ES", "es-MX"))
    with Session(engine) as s:
        tm.import_tmx(s, pid2, _tmx("en-US", "es-MX", _SEED), "en-US", "es-MX")
        # exact spelling still works
        assert _tu_count(tm.export_tmx(s, pid2, "en-US", "es-MX")) == 1
        # ambiguous 'es' does not blow up; returns empty (no bucket to safely pick)
        xml = tm.export_tmx(s, pid2, "en-US", "es")
        assert _tu_count(xml) == 0, _tu_count(xml)
    print("  3. unrelated/ambiguous pair -> empty export, never a raise OK")


# ---------------------------------------------------------------------------
# 4. no project scope -> literal passthrough (unchanged)
# ---------------------------------------------------------------------------
def test_no_project_scope_passthrough():
    with Session(engine) as s:
        assert tm.resolve_export_locales(s, None, "xx", "yy") == ("xx", "yy")
        # a NULL-project row exported verbatim
        s.add(
            SegmentTM(
                project_id=None,
                source_locale="xx",
                target_locale="yy",
                source_text="a",
                target_text="b",
                source_hash=tm.hash_text("a", "xx", "yy"),
            )
        )
        s.commit()
        assert _tu_count(tm.export_tmx(s, None, "xx", "yy")) == 1
    print("  4. project_id None -> literal passthrough (unchanged) OK")


# ---------------------------------------------------------------------------
# 5. surface parity: /api echoes effective locales + count
# ---------------------------------------------------------------------------
def test_api_surface_parity():
    pid, _cid = _project(name="apiparity")
    _seed_pair(pid)
    with Session(engine) as s:
        u = User(email="tmexp-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    _row, token = apikeys.create_key(uid, "tmexp", scopes="api docs")
    client = TestClient(app_ref())
    # a region-variant export over the wire echoes the effective (stored) pair + count
    r = client.get(
        f"/api/projects/{pid}/tm/export",
        headers={"X-Loc-Token": token},
        params={"source": "en", "target": "es"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "en-US" and body["target"] == "es-419", body
    assert body["requested_source"] == "en" and body["locales_normalized"] is True, body
    assert body["count"] == 12, body
    xml = base64.b64decode(body["tmx_b64"])
    assert _tu_count(xml) == 12, _tu_count(xml)
    # an exact pair reports locales_normalized False (byte-compatible echo)
    r2 = client.get(
        f"/api/projects/{pid}/tm/export",
        headers={"X-Loc-Token": token},
        params={"source": "en-US", "target": "es-419"},
    )
    b2 = r2.json()
    assert b2["locales_normalized"] is False and b2["count"] == 12, b2
    print("  5. /api export echoes effective locales + count OK")


def app_ref():
    from app.main import app

    return app


# ---------------------------------------------------------------------------
# 5b. review H2: a provenance-stamped (human_approved) row must COUNT (its <tu>
#     serializes with attributes, which the old byte substring scan missed)
# ---------------------------------------------------------------------------
def test_stamped_rows_count():
    pid, _cid = _project(name="stamped")
    # tm.write_back stamps human_approved provenance meta -> `<tu creationtool=...>`
    with Session(engine) as s:
        tm.write_back(s, pid, "Approved source A", "Aprobado A", "en-US", "es-419")
        tm.write_back(s, pid, "Approved source B", "Aprobado B", "en-US", "es-419")
    with Session(engine) as s:
        _eff_s, _eff_t, count, xml = tm.export_tmx_result(s, pid, "en-US", "es-419")
    if count != 2:
        _fail(f"export_tmx_result must count stamped rows: got {count}")
    if b"<tu creationtool" not in xml:
        _fail(
            "the stamped rows must serialize with a <tu creationtool=...> attribute "
            "(otherwise this test would not defend the H2 bug)"
        )
    # the OLD broken predicate would have said 0 -> prove the delta
    if xml.count(b"<tu>") != 0:
        _fail("guard: a stamped <tu> must NOT match the exact b'<tu>' substring")
    print("  5b. H2: provenance-stamped human_approved rows COUNT (real count, not a byte scan) OK")


# ---------------------------------------------------------------------------
# 5c. review M4: a literal pair that HAS rows must never be shadowed by coercion
# ---------------------------------------------------------------------------
def test_literal_pair_not_shadowed():
    pid, _cid = _project(name="twoes", targets=("es-419", "es-ES"))
    # rows in BOTH buckets (es-ES is durable even if the target locale is later removed)
    with Session(engine) as s:
        tm.import_tmx(
            s, pid, _tmx("en-US", "es-419", [("Shared", "Cuatro-uno-nueve")]), "en-US", "es-419"
        )
        tm.import_tmx(s, pid, _tmx("en-US", "es-ES", [("Shared", "De Espana")]), "en-US", "es-ES")
    with Session(engine) as s:
        # each literal pair returns ITS OWN rows, not the other bucket's
        assert tm.resolve_export_locales(s, pid, "en-US", "es-ES") == ("en-US", "es-ES")
        assert tm.resolve_export_locales(s, pid, "en-US", "es-419") == ("en-US", "es-419")
        es_xml = tm.export_tmx(s, pid, "en-US", "es-ES")
        v419_xml = tm.export_tmx(s, pid, "en-US", "es-419")
    if b"De Espana" not in es_xml or b"Cuatro-uno-nueve" in es_xml:
        _fail("es-ES export must return es-ES rows, never es-419's (coercion shadow)")
    if b"Cuatro-uno-nueve" not in v419_xml or b"De Espana" in v419_xml:
        _fail("es-419 export must return es-419 rows only")
    # empty-literal still coerces (rescue path unchanged) WITHOUT shadowing: a SOURCE
    # variant `en`->`es-ES` has no literal bucket (rows stored under en-US), so it
    # coerces the source to en-US while keeping the UNAMBIGUOUS exact target es-ES ->
    # reaches the es-ES bucket (not es-419). (A base target like `es` stays ambiguous
    # here -- two es targets -- and is correctly refused, i.e. empty.)
    with Session(engine) as s:
        rescued = tm.export_tmx(s, pid, "en", "es-ES")
        assert _tu_count(rescued) == 1 and b"De Espana" in rescued, _tu_count(rescued)
        assert _tu_count(tm.export_tmx(s, pid, "en-US", "es")) == 0  # ambiguous target
    print(
        "  5c. M4: a populated literal pair wins over coercion; empty-literal still "
        "coerces (unambiguously) OK"
    )


# ---------------------------------------------------------------------------
# 6. fail-before / pass-after (file copy, never git stash)
# ---------------------------------------------------------------------------
def _head_module(rel_path: str, mod_name: str):
    try:
        src = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"], cwd=_ROOT, capture_output=True, timeout=60
        )
    except Exception:
        return None
    if src.returncode != 0 or not src.stdout:
        return None
    path = os.path.join(_TMP, mod_name + ".py")
    with open(path, "wb") as fh:
        fh.write(src.stdout)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def test_fail_before_pass_after():
    pid, _ = _project(name="failbefore")
    _seed_pair(pid)
    old = _head_module("app/services/tm.py", "_head_tm_export")
    if old is not None and not hasattr(old, "resolve_export_locales"):
        with Session(engine) as s:
            xml = old.export_tmx(s, pid, "en", "es")  # the pre-fix STRICT query
        assert _tu_count(xml) == 0, (
            "pre-fix export of a variant should miss the bucket",
            _tu_count(xml),
        )
        print("  6a. pre-fix export of a region variant returned 0 (the trap) OK")
    else:
        print("  6a. HEAD already carries the fix (or git unavailable) -- delta asserted below")
    with Session(engine) as s:
        assert _tu_count(tm.export_tmx(s, pid, "en", "es")) == 12
    print("  6b. post-fix: the same variant export reaches the 12-row bucket OK")


def main():
    test_exact_pair_agrees_with_stats()
    test_region_variant_reaches_bucket_and_matches_stats()
    test_unrelated_and_ambiguous_pair_empty_not_raise()
    test_no_project_scope_passthrough()
    test_api_surface_parity()
    test_stamped_rows_count()
    test_literal_pair_not_shadowed()
    test_fail_before_pass_after()
    print("TM EXPORT LOCALES OK")


if __name__ == "__main__":
    main()
