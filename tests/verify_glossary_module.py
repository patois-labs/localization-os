"""Dedicated Glossary module verification (Phase 5-B).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider).
ZERO live calls. Covers the two genuinely new concepts (approval + language-pair
association) plus the workflow-step dropdown filter, CSV import via the module,
web/CLI/API parity, permission gates, org/customer isolation, and the run-time payoff
(a step-pinned approved glossary drives match_terms).

Covers:

  schema
    - glossary + glossarypair tables and the glossaryentry.glossary_id /
      workflowstep.glossary_id columns exist

  approval lifecycle
    - create -> DRAFT; approve -> APPROVED records approved_by/approved_at;
      unapprove -> DRAFT clears them
    - editing an approved glossary's TERMS or PAIRS reverts it to DRAFT (re-approval
      required); RENAME does NOT revert (metadata)

  language-pair association + canonicalization
    - set_pairs stores canonical locale codes (es_ES -> es-ES); dedups; a blank
      side is rejected
    - covers_pair matches normalized (ES_es lookup hits an es-ES row); a wildcard
      (blank-source) stored pair matches any source

  workflow-step dropdown filter (options_for_step) — filters on EXACTLY:
    - APPROVED only (a draft never appears)
    - pair coverage (only glossaries whose pairs cover the step's pair)
    - org scope + R2-3 customer confinement (a foreign org's/customer's glossary
      never appears)

  CSV import via the module (glossary_module.import_csv) + reverts approval

  import approval semantics (2026-07-29) -- an import reverts an APPROVED
  glossary only when it CHANGED the content:
    - an idempotent CSV/TBX re-import (same file, byte-identical rows) leaves it
      APPROVED, so a workflow step pinned to it keeps running
    - a changed target, a DNT flip, a meta-only change, an added sense and an
      edited sense each revert it to DRAFT
    - sense rows UPSERT by (entry, sense_label) instead of inserting a duplicate
    - EXPORT -> RE-IMPORT of our own file (CSV and the lossy TBX) is a true
      no-op: no stored value moves and the glossary stays APPROVED
    - pre-existing DUPLICATE sense rows (written by the pre-fix blind INSERT)
      CONVERGE -- every row for the label is written, never just the last
    - fail-pre-fix proven against the HEAD copy of app/services/glossary.py

  run-time payoff
    - a workflow step pinned to an approved covering glossary makes the orchestrator
      match THAT glossary's terms (glossary_id) instead of the project's; an
      un-approved or non-covering pin safely degrades to project-scoped matching
    - legacy project glossary matching is unchanged (glossary_id NULL entries)

  permission gates (web + api)
    - a non-privileged role (translator, below linguist's rank) cannot APPROVE
      or CREATE (403)
    - a viewer cannot CREATE via /api (can_tune_ai gate); a linguist can; approval
      is ALSO can_tune_ai (per the maintainer, no manager sign-off needed) -- linguist is
      allowed on both create and approve

  CLI round-trip (glossary module create/set-pairs/add-term/approve/show/list/delete)

  /api round-trip (create/detail/pairs/approve/list) + cross-org 404 isolation

Final line: "GLOSSARY MODULE OK".
"""

import base64
import io
import json as _json
import os
import sys
import tempfile
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_glossmod_")

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Document,
    Glossary,
    GlossaryEntry,
    GlossaryPair,
    GlossaryStatus,
    Project,
    Segment,
    SegStatus,
    TransMode,
    Workflow,
    WorkflowStep,
)
from app.services import (
    apikeys,
    content,
    orchestrator,
    security,
    settings,
)
from app.services import (
    glossary as gloss_engine,
)
from app.services import (
    glossary_module as gm,
)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


_CSV = (
    b"source_term,target_term,dnt,sense_label,context_cue\n"
    b"login,ingreso,,,\n"
    b"bank,banco,,finance,money\n"
    b"bank,orilla,,river,water\n"
)


# ============================================================================
def test_schema():
    insp = inspect(engine)
    names = set(insp.get_table_names())
    assert "glossary" in names and "glossarypair" in names, names
    assert "glossary_id" in {c["name"] for c in insp.get_columns("glossaryentry")}
    assert "glossary_id" in {c["name"] for c in insp.get_columns("workflowstep")}
    print("  schema: OK")


def test_approval_lifecycle():
    with Session(engine) as s:
        g = gm.create(s, "Approval GL", created_by="lin@x.com")
        assert g.status == GlossaryStatus.DRAFT
        gid = g.id
    with Session(engine) as s:
        g = gm.approve(s, gid, approver_email="mgr@x.com")
        assert g.status == GlossaryStatus.APPROVED
        assert g.approved_by == "mgr@x.com" and g.approved_at is not None
    # editing a term reverts to draft
    with Session(engine) as s:
        gm.add_term(
            s, gid, "hello", target_term="hola", source_locale="en-US", target_locale="es-ES"
        )
        assert gm.get(s, gid).status == GlossaryStatus.DRAFT, "term edit must revert"
    # re-approve, then a RENAME must NOT revert
    with Session(engine) as s:
        gm.approve(s, gid, approver_email="mgr@x.com")
        gm.rename(s, gid, "Approval GL v2")
        assert gm.get(s, gid).status == GlossaryStatus.APPROVED, "rename must not revert"
    # editing PAIRS reverts
    with Session(engine) as s:
        gm.set_pairs(s, gid, ["en-US:es-ES"])
        assert gm.get(s, gid).status == GlossaryStatus.DRAFT, "pair edit must revert"
    # unapprove clears fields
    with Session(engine) as s:
        gm.approve(s, gid, approver_email="mgr@x.com")
        g = gm.unapprove(s, gid)
        assert g.status == GlossaryStatus.DRAFT and g.approved_by == "" and g.approved_at is None
    print("  approval lifecycle (revert-on-content-edit, rename-safe): OK")


def test_pairs_canonicalization():
    with Session(engine) as s:
        g = gm.create(s, "Pairs GL")
        gid = g.id
        # tuple + string forms; legacy underscore casing canonicalizes
        gm.set_pairs(s, gid, [("en_us", "es_ES"), "en-US>fr-FR", ("en-US", "es-ES")])
    with Session(engine) as s:
        pairs = gm.pairs_for(s, gid)
        stored = {(p["source_locale"], p["target_locale"]) for p in pairs}
        assert ("en-US", "es-ES") in stored, stored  # canonicalized
        assert ("en-US", "fr-FR") in stored, stored
        assert len(pairs) == 2, ("dedup failed", pairs)  # the dup es-ES collapsed
        # covers_pair normalizes both sides
        assert gm.covers_pair(s, gid, "EN-us", "ES_es"), "normalized lookup must hit"
        assert not gm.covers_pair(s, gid, "en-US", "de-DE")
    # a blank side is rejected
    with Session(engine) as s:
        try:
            gm.set_pairs(s, gid, [("en-US", "")])
            _fail("expected reject: blank target")
        except gm.GlossaryModuleError:
            pass
    # a wildcard (blank source) pair matches any source
    with Session(engine) as s:
        g2 = gm.create(s, "Wildcard GL")
        s.add(GlossaryPair(glossary_id=g2.id, source_locale="", target_locale="ja-JP"))
        s.commit()
        assert gm.covers_pair(s, g2.id, "en-US", "ja-JP"), "wildcard source"

    # DICT-SHAPED pairs (BUG-09 QA finding, 2026-07-29): pairs_for()/to_dict()
    # RETURN {"source_locale":..., "target_locale":...} dicts, and a client that
    # round-trips those unchanged into set_pairs (a naive edit-form resubmit, or
    # a raw POST of a GET body) used to fall through to the str(p) branch, which
    # leaks the dict's Python repr (a colon-bearing string) into a garbled/wrong
    # "not a known locale code" error instead of parsing it. Reproduced pre-fix:
    # _parse_pairs([{"source_locale": "en-US", "target_locale": "es-ES"}]) raised
    # GlossaryModuleError("\"{'source_locale'\" is not a known locale code...").
    with Session(engine) as s:
        g3 = gm.create(s, "Dict Pairs GL")
        gm.set_pairs(
            s,
            g3.id,
            [
                {"source_locale": "en-US", "target_locale": "es-ES"},
                {"source_locale": "en_us", "target_locale": "fr-FR"},
            ],
        )
        pairs3 = gm.pairs_for(s, g3.id)
        assert {(p["source_locale"], p["target_locale"]) for p in pairs3} == {
            ("en-US", "es-ES"),
            ("en-US", "fr-FR"),
        }, pairs3
    # the exact round-trip: read a glossary's own reported pairs, feed them
    # straight back into set_pairs unchanged -- must be a no-op, not an error.
    with Session(engine) as s:
        round_tripped = gm.to_dict(s, gm.get(s, gid))["pairs"]
        gm.set_pairs(s, gid, round_tripped)
        assert gm.pairs_for(s, gid) == round_tripped, (
            "re-submitting a glossary's own reported pairs must round-trip"
        )
    print(
        "  pair association + canonicalization + dedup + wildcard + dict-shaped "
        "pair round-trip (BUG-09) OK"
    )


def test_dropdown_filter():
    """options_for_step filters on approved-only + pair coverage + org/customer."""
    from app.models import Customer, Organization

    with Session(engine) as s:
        # org_id/customer_id on Glossary are real FKs (-> organization.id /
        # customer.id): use the seeded Default org as "org 1", and create a REAL
        # second org + a REAL customer for the isolation cases rather than bare
        # id=2 / id=5 sentinels (which never persisted on Postgres). customer 7
        # appears ONLY in read queries below, so it needs no row.
        org1_id = s.exec(select(Organization.id).order_by(Organization.id).limit(1)).first()
        org2 = Organization(name="GM Dropdown Org 2")
        cust5 = Customer(name="GM Dropdown Cust 5")
        s.add(org2)
        s.add(cust5)
        s.commit()
        s.refresh(org2)
        s.refresh(cust5)
        org2_id, cust5_id = org2.id, cust5.id
        # org 1: approved + covering -> appears
        a = gm.create(s, "A approved covering", org_id=org1_id)
        gm.set_pairs(s, a.id, ["en-US:es-ES"])
        gm.approve(s, a.id)
        # org 1: draft + covering -> hidden (not approved)
        b = gm.create(s, "B draft", org_id=org1_id)
        gm.set_pairs(s, b.id, ["en-US:es-ES"])
        # org 1: approved but WRONG pair -> hidden
        c = gm.create(s, "C wrong pair", org_id=org1_id)
        gm.set_pairs(s, c.id, ["en-US:de-DE"])
        gm.approve(s, c.id)
        # org 2: approved + covering -> hidden from org 1 (isolation)
        d = gm.create(s, "D other org", org_id=org2_id)
        gm.set_pairs(s, d.id, ["en-US:es-ES"])
        gm.approve(s, d.id)
        # org 1 + customer 5: approved covering -> hidden from a customer-7 caller
        e = gm.create(s, "E cust5", org_id=org1_id, customer_id=cust5_id)
        gm.set_pairs(s, e.id, ["en-US:es-ES"])
        gm.approve(s, e.id)
        aid, eid = a.id, e.id
    with Session(engine) as s:
        # org-1 staff caller (no customer binding): sees A + E (staff sees all
        # customers), never B/C (draft/wrong pair) nor D (other org).
        got = {g.id for g in gm.options_for_step(s, "en-US", "es-ES", org_id=org1_id)}
        assert aid in got and eid in got, got
        names = {g.name for g in gm.options_for_step(s, "en-US", "es-ES", org_id=org1_id)}
        assert "B draft" not in names and "C wrong pair" not in names, names
        assert "D other org" not in names, names
        # org-1 caller BOUND to customer 7: sees A (LEGACY unassigned/NULL — the
        # customer-first change offers a customer's own glossaries PLUS the shared
        # legacy ones) but NEVER E (another customer's). (2026-07-14: options_for_step
        # confines to customer_id OR unassigned; a foreign customer stays hidden.)
        got7 = {
            g.id for g in gm.options_for_step(s, "en-US", "es-ES", org_id=org1_id, customer_id=7)
        }
        assert aid in got7 and eid not in got7, got7
        # a blank pair (universal workflow) offers every approved visible glossary
        blank = {g.name for g in gm.options_for_step(s, "", "", org_id=org1_id)}
        assert "A approved covering" in blank and "C wrong pair" in blank, blank
        assert "B draft" not in blank, blank
    print("  dropdown filter (approved-only + pair + org/customer): OK")


def test_module_csv_import_and_legacy_isolation():
    with Session(engine) as s:
        g = gm.create(s, "CSV GL")
        gm.approve(s, g.id)
        gid = g.id
    with Session(engine) as s:
        res = gm.import_csv(s, gid, _CSV, "en-US", "es-ES")
        assert res["entries"] >= 2 and res["senses"] == 2, res
        assert gm.get(s, gid).status == GlossaryStatus.DRAFT, "import reverts approval"
    # a legacy project entry (glossary_id NULL) is NOT swept into the module glossary
    with Session(engine) as s:
        proj = content.project_create(
            s, "Legacy P", source_locale="en-US", target_locales="es-ES", default_provider="mock"
        )
        gloss_engine.add_term(
            s, proj.id, "legacyterm", target_term="x", source_locale="en-US", target_locale="es-ES"
        )
        # module list for this glossary must not include the project's legacy term
        terms = {it["entry"].source_term for it in gm.list_entries(s, gid)}
        assert "legacyterm" not in terms, terms
        # project count/list excludes module-owned entries
        assert gloss_engine.count(s, proj.id) == 1, "project count = only legacy"
    print("  module CSV import + legacy/project isolation: OK")


# ============================================================================
# Import approval semantics (2026-07-29). The gate used to be "the import
# CREATED entries or senses", so an import that only REWROTE existing terms --
# a fresh translation pass over the whole termbase, a DNT sweep, new metadata --
# left an APPROVED glossary APPROVED with different content underneath it, while
# editing ONE term in the UI correctly reverted. The naive fix (gate on the
# row-count `updated`) breaks the other way: `updated` counts byte-identical
# rewrites, so an IDEMPOTENT re-import would revert a published glossary to
# draft and break every workflow step pinned to it (a step requires APPROVED).
# The shipped rule gates on entries OR senses OR `changed` -- a value-changed
# count with tm.import_tmx's semantics.
_APPROVAL_CSV = (
    b"source_term,target_term,dnt,sense_label,context_cue,definition\r\n"
    b"widget,artilugio,,,,\r\n"
    b"Acme,Acme,true,,,\r\n"
    b"bank,banco,,finance,money savings,\r\n"
)


def _tbx_bytes(pairs):
    body = "".join(
        f'<conceptEntry id="c{i}">'
        f'<langSec xml:lang="en-US"><termSec><term>{s}</term></termSec></langSec>'
        f'<langSec xml:lang="es-ES"><termSec><term>{t}</term></termSec></langSec>'
        f"</conceptEntry>"
        for i, (s, t) in enumerate(pairs)
    )
    return (
        '<?xml version="1.0"?><tbx xml:lang="en"><text><body>' + body + "</body></text></tbx>"
    ).encode("utf-8")


def _json_bytes(pairs, defn=None, start=0):
    """A `.glossary.json` (glossary-concept/v1) doc with STABLE external_ids so a re-import
    upserts the same concepts. T2 defers the concept-preserving TBX IMPORT to T1b, so the
    concept-shaped import the M4 approval-fingerprint tests exercise is the KEPT `.glossary.json`
    arm (glossary_concepts.from_v1)."""
    from app.services import glossary_concepts as _gc

    concepts = []
    for i, (s, t) in enumerate(pairs):
        c = {
            "external_id": f"c{start + i}",
            "terms": [
                {"locale": "en-US", "term": s, "status": "preferred"},
                {"locale": "es-ES", "term": t, "status": "preferred"},
            ],
        }
        if defn is not None:
            c["definition"] = defn
        concepts.append(c)
    return _gc.dumps_v1({"schema": _gc.SCHEMA_V1, "concepts": concepts})


def _approved_glossary(name, seed, filename=None):
    """A glossary seeded with `seed` and then APPROVED -- the state every case
    below re-imports into."""
    with Session(engine) as s:
        gid = gm.create(s, name).id
        # T2: a concept-preserving (TBX) seed needs >=1 language pair so rematerialize
        # projects; harmless for the CSV seeds.
        gm.set_pairs(s, gid, ["en-US:es-ES"])
        gm.import_bytes(s, gid, seed, "en-US", "es-ES", filename=filename)
        gm.approve(s, gid, approver_email="mgr@x.com")
        assert gm.get(s, gid).status == GlossaryStatus.APPROVED
    return gid


def _reimport(gid, data, filename=None):
    with Session(engine) as s:
        res = gm.import_bytes(s, gid, data, "en-US", "es-ES", filename=filename)
    with Session(engine) as s:
        return gm.get(s, gid).status, res


def _sense_rows(gid):
    from app.models import GlossarySense

    with Session(engine) as s:
        ids = [
            e.id
            for e in s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        ]
        rows = s.exec(select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(ids))).all()
        return [(r.sense_label, r.context_cue, r.target_term) for r in rows]


def test_import_approval_revert_semantics():
    # --- 1. IDEMPOTENT re-import (CSV, including a sense row) stays APPROVED ---
    gid = _approved_glossary("Idempotent CSV GL", _APPROVAL_CSV)
    before_senses = _sense_rows(gid)
    status, res = _reimport(gid, _APPROVAL_CSV)
    assert status == GlossaryStatus.APPROVED, (
        "re-importing the SAME file must not revert an approved glossary -- a "
        "workflow step pinned to it requires APPROVED",
        res,
    )
    assert res["entries"] == 0 and res["senses"] == 0, res
    assert res["changed"] == 0 and res["unchanged"] == 3, (
        "every row is a true no-op, and they are reported as such",
        res,
    )
    assert res["updated"] == res["changed"] + res["unchanged"], res
    # ...and the sense row was UPSERTED, not duplicated (the quirk that used to
    # make every sense-bearing re-import look like it created content).
    assert _sense_rows(gid) == before_senses == [("finance", "money savings", "banco")], (
        "a re-imported sense row must update in place, never insert a second "
        "row for the same (entry, sense_label)",
        _sense_rows(gid),
    )

    # --- 2. a CHANGED TARGET reverts ---
    gid = _approved_glossary("Changed target GL", _APPROVAL_CSV)
    status, res = _reimport(gid, _APPROVAL_CSV.replace(b"widget,artilugio", b"widget,artefacto"))
    assert status == GlossaryStatus.DRAFT, ("a rewritten target must revert", res)
    assert res["entries"] == 0 and res["senses"] == 0, (
        "the revert cannot come from a creation count -- this is the whole bug",
        res,
    )
    assert res["changed"] == 1 and res["unchanged"] == 2, res

    # --- 3. a DNT FLIP reverts ---
    gid = _approved_glossary("DNT flip GL", _APPROVAL_CSV)
    status, res = _reimport(gid, _APPROVAL_CSV.replace(b"Acme,Acme,true", b"Acme,Acme,false"))
    assert status == GlossaryStatus.DRAFT, ("a DNT flip must revert", res)
    assert res["changed"] == 1, res

    # --- 4. a META-ONLY change reverts ---
    gid = _approved_glossary("Meta only GL", _APPROVAL_CSV)
    status, res = _reimport(
        gid,
        _APPROVAL_CSV.replace(
            b"widget,artilugio,,,,\r\n", b"widget,artilugio,,,,A small UI control.\r\n"
        ),
    )
    assert status == GlossaryStatus.DRAFT, ("new term metadata is a content change", res)
    assert res["changed"] == 1 and res["entries"] == 0, res

    # --- 5. a NEW SENSE reverts (and a byte-identical sense re-import does not) ---
    gid = _approved_glossary("Sense add GL", _APPROVAL_CSV)
    with_river = _APPROVAL_CSV + b"bank,orilla,,river,river water,\r\n"
    status, res = _reimport(gid, with_river)
    assert status == GlossaryStatus.DRAFT, ("a new sense must revert", res)
    assert res["senses"] == 1, res
    assert len(_sense_rows(gid)) == 2, _sense_rows(gid)
    # re-approve, then feed the SAME extended file back: nothing changes.
    with Session(engine) as s:
        gm.approve(s, gid, approver_email="mgr@x.com")
    status, res = _reimport(gid, with_river)
    assert status == GlossaryStatus.APPROVED, (
        "a byte-identical sense row must be a no-op -- this pins the sense "
        "UPSERT; with the old blind INSERT it re-created the sense every time, "
        "so `senses` was non-zero and the glossary reverted on every re-import",
        res,
    )
    assert res["senses"] == 0 and res["changed"] == 0, res
    assert len(_sense_rows(gid)) == 2, ("still two sense rows, not four", _sense_rows(gid))

    # --- 6. an EDITED SENSE (same label, different target) reverts ---
    gid = _approved_glossary("Sense edit GL", _APPROVAL_CSV)
    status, res = _reimport(
        gid,
        _APPROVAL_CSV.replace(
            b"bank,banco,,finance,money savings,", b"bank,banca,,finance,money savings,"
        ),
    )
    assert status == GlossaryStatus.DRAFT, ("an edited sense target must revert", res)
    assert res["senses"] == 0 and res["changed"] == 1, (
        "an upsert of an existing label is a CHANGE, not a creation",
        res,
    )
    assert _sense_rows(gid) == [("finance", "money savings", "banca")], _sense_rows(gid)

    # --- 7. TBX default import (round-1 H1: the flat entry path) follows the SAME
    # idempotency rule as CSV. A byte-identical TBX re-import into an APPROVED module glossary
    # reports changed=0 and leaves it APPROVED (round-1 M4: it must NOT re-arm the pinned-
    # workflow breakage the guard exists to prevent); a re-import that rewrites a target
    # reports changed>=1 and reverts it to draft for re-approval. Counterfactual: on the
    # pre-fix branch the default TBX import took the concept path and `_import_changed_content`
    # gated on res["terms"], so BOTH re-imports reverted -- the idempotent one wrongly.
    tbx = _tbx_bytes([("widget", "artilugio"), ("Acme", "Acme")])
    gid = _approved_glossary("Reimport TBX GL", tbx, filename="seed.tbx")
    status, res = _reimport(gid, tbx, filename="again.tbx")
    assert status == GlossaryStatus.APPROVED, (
        "a byte-identical TBX re-import must leave an APPROVED glossary approved (M4)",
        res,
    )
    assert res["format"] == "tbx" and res.get("changed") == 0, res

    gid = _approved_glossary("Changed TBX GL", tbx, filename="seed.tbx")
    status, res = _reimport(
        gid, _tbx_bytes([("widget", "artefacto"), ("Acme", "Acme")]), filename="again.tbx"
    )
    assert status == GlossaryStatus.DRAFT, ("a rewritten TBX target must revert", res)
    assert res["format"] == "tbx" and res.get("changed", 0) >= 1, res

    # --- 8. the legacy module import_csv seam follows the SAME rule, and keeps
    # its narrow legacy return shape.
    gid = _approved_glossary("Legacy seam GL", _APPROVAL_CSV)
    with Session(engine) as s:
        legacy = gm.import_csv(s, gid, _APPROVAL_CSV, "en-US", "es-ES")
    assert set(legacy) == {"entries", "senses"}, (
        "gm.import_csv's public return must stay the narrow legacy shape",
        legacy,
    )
    with Session(engine) as s:
        assert gm.get(s, gid).status == GlossaryStatus.APPROVED, (
            "import_csv must not revert on a no-op re-import either"
        )
    with Session(engine) as s:
        gm.import_csv(
            s, gid, _APPROVAL_CSV.replace(b"widget,artilugio", b"widget,cosa"), "en-US", "es-ES"
        )
        assert gm.get(s, gid).status == GlossaryStatus.DRAFT, (
            "import_csv must revert on a real content change"
        )
    print(
        "  import approval semantics: an idempotent CSV/TBX re-import leaves an "
        "APPROVED glossary approved (senses upserted, not duplicated); a changed "
        "target, DNT flip, meta-only change, added sense or edited sense all "
        "revert it to draft OK"
    )


def test_reimport_blank_or_absent_dnt_keeps_the_flag():
    """Issue #497: a BLANK or MISSING dnt cell keeps the stored flag -- "last
    NON-BLANK target_term/dnt/meta values win" (_apply_rows docstring). The old
    base-row branch reparsed the cell unconditionally, so re-importing a CSV with
    no dnt column (or a blank cell) silently UN-flagged a dnt=True term, counted
    the row as changed and reverted the APPROVED glossary to draft -- the brand
    name started getting translated. An explicit false/0/no still clears it."""

    def _dnt(gid, term):
        with Session(engine) as s:
            entry = s.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == term
                )
            ).first()
            assert entry is not None, (gid, term)
            return entry.dnt

    seed = b"source_term,target_term,dnt\r\nAcme,Acme,true\r\n"

    # --- 1. a CSV with ONLY source_term,target_term columns: dnt stays True, the
    # row is a true no-op, so the APPROVED glossary stays APPROVED ---
    gid = _approved_glossary("No dnt column GL", seed)
    status, res = _reimport(gid, b"source_term,target_term\r\nAcme,Acme\r\n")
    assert _dnt(gid, "Acme") is True, (
        "a re-import whose file carries no dnt column must not clear the flag",
        res,
    )
    assert res["changed"] == 0 and res["unchanged"] == 1, (
        "the row moved nothing, so it must not count as changed",
        res,
    )
    assert status == GlossaryStatus.APPROVED, (
        "a no-op re-import must not revert an approved glossary",
        res,
    )

    # --- 2. a CSV whose dnt CELL is blank: same contract ---
    gid = _approved_glossary("Blank dnt cell GL", seed)
    status, res = _reimport(gid, b"source_term,target_term,dnt\r\nAcme,Acme,\r\n")
    assert _dnt(gid, "Acme") is True, ("a blank dnt cell must keep the flag", res)
    assert res["changed"] == 0 and res["unchanged"] == 1, res
    assert status == GlossaryStatus.APPROVED, res

    # --- 3. an EXPLICIT false/0/no still CLEARS the flag, and that flip is a
    # real change: the glossary reverts like any other DNT flip ---
    for value in ("false", "0", "no"):
        gid = _approved_glossary(f"DNT clear {value} GL", seed)
        status, res = _reimport(
            gid, f"source_term,target_term,dnt\r\nAcme,Acme,{value}\r\n".encode()
        )
        assert _dnt(gid, "Acme") is False, (value, "an explicit falsy dnt must clear", res)
        assert res["changed"] == 1, (value, res)
        assert status == GlossaryStatus.DRAFT, (value, "the DNT flip must revert", res)

    # --- 4. within ONE file the last NON-BLANK dnt wins: `Acme,,true` followed
    # by `Acme,Acme,` ends dnt True (the bug ended it False) ---
    with Session(engine) as s:
        gid = gm.create(s, "In-file last non-blank dnt GL").id
        gm.set_pairs(s, gid, ["en-US:es-ES"])
        gm.import_bytes(
            s, gid, b"source_term,target_term,dnt\r\nAcme,,true\r\nAcme,Acme,\r\n", "en-US", "es-ES"
        )
    assert _dnt(gid, "Acme") is True, (
        "the blank-cell second row must not clobber the first row's true",
        _dnt(gid, "Acme"),
    )
    print(
        "  re-import with a blank or missing dnt keeps the stored flag (last "
        "non-blank wins, in-file and across imports); explicit false/0/no still "
        "clears and reverts OK"
    )


def test_reimport_short_csv_row_keeps_the_flag():
    """Review round 1 of issue #497: a CSV row with FEWER cells than its header
    makes csv.DictReader fill the missing column with None (its restval), and
    str(None) is the truthy string "None" -- read as a NON-BLANK cell it would
    clear the flag all over again. A None cell must read as BLANK, so the stored
    dnt stands and the row stays a no-op."""
    seed = b"source_term,target_term,dnt\r\nAcme,Acme,true\r\n"
    gid = _approved_glossary("Short row dnt GL", seed)
    # the header declares dnt, the data row stops after target_term -> dnt None
    status, res = _reimport(gid, b"source_term,target_term,dnt\r\nAcme,Acme\r\n")
    with Session(engine) as s:
        entry = s.exec(
            select(GlossaryEntry).where(
                GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "Acme"
            )
        ).first()
        assert entry is not None, (gid, res)
        stored = entry.dnt
    assert stored is True, ("a short row's None dnt cell must read as blank", stored, res)
    assert res["changed"] == 0 and res["unchanged"] == 1, res
    assert status == GlossaryStatus.APPROVED, (
        "a short row moved nothing, so approval must stand",
        res,
    )
    print("  a short CSV row (a None dnt cell from DictReader) keeps the stored flag OK")


def test_concept_import_idempotent_reimport_keeps_approved():
    """round-1 M4 (concept path): a concept-PRESERVING import that changes nothing must NOT
    revert an APPROVED glossary -- change is measured on the compat store, not the raw
    written-`terms` count. A concept re-import that ADDS an enforced entry reverts. Exercised on
    the KEPT `.glossary.json` concept arm (the TBX `--concepts` arm is deferred to T1b).
    Counterfactual: on the pre-fix branch _import_changed_content gated on res["terms"], so
    EVERY concept import (idempotent or not) reverted, re-arming the pinned-workflow breakage."""
    seed = _json_bytes([("widget", "artilugio"), ("gadget", "chisme")])
    with Session(engine) as s:
        gid = gm.create(s, "Concept idem GL").id
        gm.set_pairs(s, gid, ["en-US:es-ES"])
        gm.import_bytes(s, gid, seed, "en-US", "es-ES", filename="seed.glossary.json")
        # our own export carries the store's external_ids, so a re-import upserts idempotently.
        exported = gm.export_bytes(s, gid, fmt="json")
        gm.approve(s, gid, approver_email="mgr@x.com")
        assert gm.get(s, gid).status == GlossaryStatus.APPROVED

    # idempotent concept re-import (our own export) -> compat store unchanged -> stays APPROVED.
    with Session(engine) as s:
        res = gm.import_bytes(s, gid, exported, "en-US", "es-ES", filename="rt.glossary.json")
    with Session(engine) as s:
        assert gm.get(s, gid).status == GlossaryStatus.APPROVED, (
            "an idempotent concept re-import must NOT revert (M4)",
            res,
        )
    assert res.get("changed") == 0, res

    # a concept import that ADDS an enforced entry (a new source concept) changes the compat
    # store -> reverts (change is measured on the enforcement surface, LAW-TERM-17: an existing
    # entry is authoritative and a re-import never rewrites it, so ADDED content is the change).
    added = _json_bytes([("sprocket", "rueda")], start=9)
    with Session(engine) as s:
        res2 = gm.import_bytes(s, gid, added, "en-US", "es-ES", filename="add.glossary.json")
    with Session(engine) as s:
        assert gm.get(s, gid).status == GlossaryStatus.DRAFT, "an added-concept import reverts"
    assert res2.get("changed") == 1, res2
    print(
        "  M4 (concept path): idempotent concept re-import keeps APPROVED; an added enforced "
        "entry reverts to draft OK"
    )


def test_concept_reimport_changed_concept_content_reverts():
    """round-2 M2: a concept-preserving re-import that changes concept-store content the T3
    prompt block renders -- a rewritten DEFINITION -- must revert an APPROVED glossary to
    draft, while a byte-identical re-import keeps it approved. The change is INVISIBLE to the
    compat store (rematerialize is append/update-only, LAW-TERM-17: it never rewrites an
    existing entry's meta), so the round-1 fingerprint (compat store only) reported changed=0
    and the glossary stayed APPROVED while sending the model a different terminology note.
    Exercised on the KEPT `.glossary.json` concept arm.

    Counterfactual: on the pre-fix tree _compat_fingerprint covered only the compat entries,
    so the definition rewrite produced changed=0 and the glossary stayed APPROVED (bug); the
    change-definition assertion below (status == DRAFT) fails there."""
    seed = _json_bytes([("widget", "artilugio")], defn="a small UI control")
    with Session(engine) as s:
        gid = gm.create(s, "M2 concept-change GL").id
        gm.set_pairs(s, gid, ["en-US:es-ES"])
        gm.import_bytes(s, gid, seed, "en-US", "es-ES", filename="seed.glossary.json")
        gm.approve(s, gid, approver_email="mgr@x.com")
        assert gm.get(s, gid).status == GlossaryStatus.APPROVED

    # byte-identical concept re-import -> concept store unchanged -> stays APPROVED.
    with Session(engine) as s:
        res = gm.import_bytes(s, gid, seed, "en-US", "es-ES", filename="same.glossary.json")
    with Session(engine) as s:
        assert gm.get(s, gid).status == GlossaryStatus.APPROVED, (
            "an idempotent concept re-import must NOT revert",
            res,
        )
    assert res.get("changed") == 0, res

    # a re-import that only rewrites the concept DEFINITION (a T3-rendered field, invisible to
    # the compat entries) MUST revert.
    changed = _json_bytes([("widget", "artilugio")], defn="a COMPLETELY DIFFERENT definition")
    with Session(engine) as s:
        res2 = gm.import_bytes(s, gid, changed, "en-US", "es-ES", filename="def.glossary.json")
    with Session(engine) as s:
        assert gm.get(s, gid).status == GlossaryStatus.DRAFT, (
            "a rewritten concept definition reaches the T3 prompt block and must revert (M2)",
            res2,
        )
    assert res2.get("changed") == 1, res2
    print(
        "  M2 (concept path): a changed concept definition (T3-rendered, compat-invisible) "
        "reverts APPROVED to draft; a byte-identical re-import keeps it approved OK"
    )


def test_web_import_flash_shows_changed_and_revert_note():
    """Parity pin (2026-07-29): the module glossary's web import flash used to
    render ONLY gl_entries/gl_senses/gl_skipped, so a rewrite-everything import
    (0 new entries, 0 new senses, N rewritten values) displayed "Imported 0
    entries, 0 senses." while SILENTLY flipping an APPROVED glossary to DRAFT --
    the exact gap this train closes. A changed re-import must show the `changed`
    count AND say, explicitly, that the revert happened; a byte-identical
    (idempotent) re-import must show neither the revert wording nor a nonzero
    changed count, and the glossary must stay APPROVED."""
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    gid = _approved_glossary("Web flash GL", _APPROVAL_CSV)

    # --- a REWRITE (0 entries, 0 senses, 1 changed) reverts, and the flash says so
    changed_csv = _APPROVAL_CSV.replace(b"widget,artilugio", b"widget,artefacto")
    r = c.post(
        f"/glossaries/{gid}/import",
        data={"source_locale": "en-US", "target_locale": "es-ES"},
        files={"file": ("gl.csv", changed_csv, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert "gl_entries=0" in loc and "gl_senses=0" in loc, (
        "the revert must NOT come from a creation count -- this is the whole bug",
        loc,
    )
    assert "gl_changed=1" in loc and "gl_reverted=1" in loc, loc
    with Session(engine) as s:
        assert gm.get(s, gid).status == GlossaryStatus.DRAFT, "must have reverted"
    page = c.get(loc).text
    assert "1" in page and "returned to draft" in page and "re-approval" in page, (
        "the flash must show the changed count AND explicitly say the glossary reverted",
        page,
    )

    # --- re-approve, then re-import the SAME (now-stored) file: idempotent, no revert
    with Session(engine) as s:
        gm.approve(s, gid, approver_email="mgr@x.com")
    r = c.post(
        f"/glossaries/{gid}/import",
        data={"source_locale": "en-US", "target_locale": "es-ES"},
        files={"file": ("gl.csv", changed_csv, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert "gl_changed=0" in loc, loc
    assert "gl_reverted" not in loc, ("an idempotent re-import must not claim a revert", loc)
    with Session(engine) as s:
        assert gm.get(s, gid).status == GlossaryStatus.APPROVED, (
            "an idempotent re-import must leave an approved glossary approved",
        )
    page = c.get(loc).text
    assert "returned to draft" not in page, ("no revert note on a no-op re-import", page)
    print(
        "  web import flash: changed re-import shows the changed count + an "
        "explicit revert note; idempotent re-import shows neither OK"
    )


def test_cli_import_human_line_shows_changed_counts():
    """Parity pin: the CLI human-readable import line (both the legacy per-project
    `glossary import` and the module `glossary module import`) must surface the
    `changed`/`unchanged` counters -- --json already carried them; only the prose
    line silently dropped them, so a rewrite-everything import printed "0
    entries, 0 senses" with no hint that anything happened."""
    # --- module glossary (`glossary module import`) ---
    gid = _approved_glossary("CLI changed GL", _APPROVAL_CSV)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "gl.csv")
        with open(path, "wb") as fh:
            fh.write(_APPROVAL_CSV.replace(b"widget,artilugio", b"widget,artefacto"))
        rc, out = _run_cli(
            [
                "glossary",
                "module",
                "import",
                str(gid),
                path,
                "--source",
                "en-US",
                "--target",
                "es-ES",
            ]
        )
        assert rc == 0, out
        assert "0 entr" in out and "0 sense" in out, (
            "creation counts must stay 0 -- the rewrite creates nothing",
            out,
        )
        assert "1 term(s) updated" in out and "2 unchanged" in out, out
    with Session(engine) as s:
        assert gm.get(s, gid).status == GlossaryStatus.DRAFT, "must have reverted"

    # --- legacy per-project glossary (`glossary import`) -- no approval lifecycle,
    # but the SAME counts gap applied to its human line.
    with Session(engine) as s:
        proj = Project(name="CLI changed proj", source_locale="en-US", created_by="t@example.com")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        pid = proj.id
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "gl.csv")
        with open(path, "wb") as fh:
            fh.write(_APPROVAL_CSV)
        rc, out = _run_cli(
            ["glossary", "import", str(pid), path, "--source", "en-US", "--target", "es-ES"]
        )
        assert rc == 0, out
        with open(path, "wb") as fh:
            fh.write(_APPROVAL_CSV.replace(b"widget,artilugio", b"widget,artefacto"))
        rc, out = _run_cli(
            ["glossary", "import", str(pid), path, "--source", "en-US", "--target", "es-ES"]
        )
        assert rc == 0, out
        assert "1 term(s) updated" in out and "2 unchanged" in out, out
    print(
        "  CLI human import line: both `glossary import` and `glossary module "
        "import` surface changed/unchanged counts OK"
    )


def test_export_reimport_round_trip_is_a_no_op():
    """THE case an approval-revert rule lives or dies on: **export the glossary,
    re-import the file you just exported**. It is what a user actually does
    (export -> edit two rows in Excel -> import), and every cell in it was
    produced by our own writer, so any asymmetry between the exporter and the
    importer -- a meta key written blank and read back as "", a re-sanitized
    value, a sense row emitted after the base row -- would report `changed` and
    silently unpublish an APPROVED glossary that a workflow step is pinned to.

    Covers BOTH formats, and the LOSSY one on purpose: TBX-Core has no home for
    senses / usage_example / notes, so a TBX export of a CSV-built glossary is a
    strict SUBSET of what is stored. Re-importing it must still be a no-op --
    which it is BY DEFINITION, because an import MERGES and never deletes: the
    fields the file does not mention are not "changed to blank", they are not
    mentioned. That is the correct reading, and it is asserted here so nobody
    later "fixes" the importer into a replace-semantics diff."""
    # --- CSV arm: a true no-op that keeps the glossary APPROVED, and a byte-identical
    # export -> import -> export FIXED POINT (the legacy contract, unchanged by T2). ---
    gid = _approved_glossary("Round trip csv GL", _APPROVAL_CSV)
    with Session(engine) as s:
        before = _snapshot(s, gid)
        data = gloss_engine.export_bytes(s, None, glossary_id=gid, fmt="csv")
    status, res = _reimport(gid, data, filename="round-trip.csv")
    assert status == GlossaryStatus.APPROVED, (
        "re-importing our OWN CSV export must change nothing and leave the glossary approved",
        res,
    )
    assert res["entries"] == 0 and res["senses"] == 0 and res["changed"] == 0, res
    assert res["unchanged"] == res["updated"] > 0, ("every row landed, every row a no-op", res)
    with Session(engine) as s:
        assert _snapshot(s, gid) == before, (_snapshot(s, gid), before)
        assert gloss_engine.export_bytes(s, None, glossary_id=gid, fmt="csv") == data, (
            "CSV export -> import -> export must be byte-identical"
        )

    # --- TBX arm (round-1 H1: the default TBX import is the flat entry path, like CSV). A
    # user's real round trip -- export TBX, re-import the exported file -- must be a no-op that
    # KEEPS the glossary APPROVED (round-1 M4), and must reproduce the concept store's term
    # DATA (source/target/dnt/status per term, LAW-TERM-11). ---
    from app.services import glossary_concepts as _gc

    tbx_seed = _tbx_bytes([("widget", "artilugio"), ("gadget", "artilugio2"), ("Acme", "Acme")])
    tbx_seed = tbx_seed.replace(
        b"<term>Acme</term>",
        b'<term>Acme</term><termNote type="doNotTranslate">true</termNote>',
        1,
    )
    gid_t = _approved_glossary("Round trip tbx GL", tbx_seed, filename="seed.tbx")

    def _term_shape(g):
        with Session(engine) as s:
            d = _gc.to_v1(s, g)
        shape = {}
        for c in d.get("concepts", []):
            for t in c.get("terms", []):
                shape[(t["locale"], t["term"])] = (t["status"], t["dnt"])
        return shape

    before_shape = _term_shape(gid_t)
    with Session(engine) as s:
        exported = gloss_engine.export_bytes(s, None, glossary_id=gid_t, fmt="tbx")
    gid_t2 = _approved_glossary("Round trip tbx GL2", tbx_seed, filename="seed2.tbx")
    status, res = _reimport(gid_t2, exported, filename="round-trip.tbx")
    assert status == GlossaryStatus.APPROVED, (
        "re-importing our OWN TBX export must change nothing and leave the glossary approved",
        res,
    )
    assert res.get("changed") == 0, res
    assert _term_shape(gid_t2) == before_shape, (
        "the TBX round trip must reproduce the concept store's term data",
        _term_shape(gid_t2),
        before_shape,
    )
    print(
        "  round trip: CSV and TBX are both byte-identical no-ops keeping APPROVED; a TBX "
        "export re-imports the same term data OK"
    )


def _snapshot(session, gid):
    from app.models import GlossarySense

    out = []
    for e in session.exec(
        select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid).order_by(GlossaryEntry.id)
    ).all():
        senses = session.exec(
            select(GlossarySense)
            .where(GlossarySense.glossary_entry_id == e.id)
            .order_by(GlossarySense.id)
        ).all()
        out.append(
            (
                e.source_term,
                e.target_term,
                e.dnt,
                e.meta or {},
                [(x.id, x.sense_label, x.context_cue, x.target_term) for x in senses],
            )
        )
    return out


def test_pre_existing_duplicate_senses_converge():
    """ADVERSARIAL REVIEW, 2026-07-29. The upsert has to cope with the mess the
    bug it replaces already wrote: before the fix a blind INSERT added a second
    (third, fourth) GlossarySense row for the SAME (entry, sense_label) on every
    re-import, so every sense-bearing glossary that was ever re-imported carries
    duplicates NOW.

    The first version of the upsert collapsed those to the LAST id and updated
    only that one. Reproduced consequence: after an import that changed the
    sense's target, the term carried TWO 'finance' rows with DIFFERENT targets,
    and nothing downstream prefers the last -- `_choose_sense` offers the model
    both as separate options, and `_cue_fallback` sees two cue hits for one
    label and abstains entirely. The import reported success while the term went
    on resolving to a value the file no longer contained.

    Now every row for the label is written, so the duplicates CONVERGE (an
    import still never deletes a row). Converging rows that DISAGREE counts as a
    `changed` row even when the file is byte-identical -- the term's effective
    translation really does move -- so an APPROVED glossary correctly reverts."""
    from app.models import GlossarySense

    def _dupe(gid, target):
        """Replay the pre-fix bug: a second 'finance' row on the `bank` term."""
        with Session(engine) as s:
            e = s.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "bank"
                )
            ).first()
            s.add(
                GlossarySense(
                    glossary_entry_id=e.id,
                    sense_label="finance",
                    context_cue="money savings",
                    target_term=target,
                )
            )
            s.commit()
            gm.approve(s, gid, approver_email="mgr@x.com")

    def _finance(gid):
        with Session(engine) as s:
            e = s.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.glossary_id == gid, GlossaryEntry.source_term == "bank"
                )
            ).first()
            return [
                (r.id, r.target_term)
                for r in s.exec(
                    select(GlossarySense)
                    .where(
                        GlossarySense.glossary_entry_id == e.id,
                        GlossarySense.sense_label == "finance",
                    )
                    .order_by(GlossarySense.id)
                ).all()
            ]

    # (a) the duplicates DISAGREE and the file is byte-identical: the import must
    # still write, converge them, and report the change.
    gid = _approved_glossary("Dup sense stale GL", _APPROVAL_CSV)
    _dupe(gid, "STALE")
    assert [t for _, t in _finance(gid)] == ["banco", "STALE"], _finance(gid)
    status, res = _reimport(gid, _APPROVAL_CSV)
    assert [t for _, t in _finance(gid)] == ["banco", "banco"], (
        "every row for the label must be written, not just the last",
        _finance(gid),
    )
    assert res["changed"] == 1, ("converging DIVERGENT duplicates is a change", res)
    assert status == GlossaryStatus.DRAFT, (
        "the term's effective translation moved, so approval must lapse",
        res,
    )

    # (b) the duplicates AGREE and the file changes the sense: BOTH must move,
    # or the stale twin stays selectable (the reproduced defect).
    gid = _approved_glossary("Dup sense edit GL", _APPROVAL_CSV)
    _dupe(gid, "banco")
    status, res = _reimport(
        gid,
        _APPROVAL_CSV.replace(
            b"bank,banco,,finance,money savings,", b"bank,BANCA,,finance,money savings,"
        ),
    )
    assert [t for _, t in _finance(gid)] == ["BANCA", "BANCA"], (
        "an earlier duplicate left holding the OLD target is still offered to "
        "_choose_sense -- the import must move every row",
        _finance(gid),
    )
    assert status == GlossaryStatus.DRAFT and res["changed"] == 1, res

    # (c) the duplicates AGREE and the file is byte-identical: nothing to do --
    # a no-op stays a no-op and does NOT unpublish the glossary.
    gid = _approved_glossary("Dup sense noop GL", _APPROVAL_CSV)
    _dupe(gid, "banco")
    status, res = _reimport(gid, _APPROVAL_CSV)
    assert status == GlossaryStatus.APPROVED, (
        "identical duplicates plus an identical file is still a true no-op",
        res,
    )
    assert res["changed"] == 0 and res["senses"] == 0, res
    assert [t for _, t in _finance(gid)] == ["banco", "banco"], _finance(gid)
    print(
        "  pre-existing DUPLICATE sense rows converge on import (every row for "
        "the label is written, none is deleted) OK"
    )


def _head_module(rel_path: str, mod_name: str):
    """Import the HEAD (pre-fix) revision of a source file as a separate module
    via a FILE COPY -- never `git stash` (the stash stack is shared across
    worktrees, so concurrent agents mis-pop each other). None when git/HEAD is
    unavailable."""
    import importlib.util
    import subprocess

    try:
        src = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"], cwd=_ROOT, capture_output=True, timeout=60
        )
    except Exception:
        return None
    if src.returncode != 0 or not src.stdout:
        return None
    path = os.path.join(tempfile.mkdtemp(prefix="loctms_head_"), mod_name + ".py")
    with open(path, "wb") as fh:
        fh.write(src.stdout)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def test_import_approval_fail_before_pass_after():
    """FAIL-PRE-FIX PROOF for the two coupled defects, run against the HEAD copy
    of app/services/glossary.py (file copy, not git stash).

    Defect A (sense-free file -- a TBX termbase or any ordinary term CSV): a
    re-import that rewrites every target reports entries=0, senses=0, so the old
    `entries or senses` gate had NOTHING to revert on.
    Defect B (sense-bearing file): a byte-identical sense row was INSERTED again,
    so a NO-OP re-import reported senses>0 and the same gate reverted a published
    glossary for a file that changed nothing.
    The two defects hid each other: on a sense-bearing file the duplication bug
    made the gate fire for the WRONG reason, which is exactly why the counter has
    to mean "a stored value changed" rather than "a row was written"."""
    old = _head_module("app/services/glossary.py", "_head_glossary_approval")
    if old is None or hasattr(old, "import_csv_full"):
        print(
            "  import approval fail-pre-fix: HEAD already carries the fix (or "
            "git unavailable) -- the behavioural delta is asserted above"
        )
        return
    plain = b"source_term,target_term,dnt\r\nwidget,artilugio,\r\nAcme,Acme,true\r\n"
    # A: a wholesale target rewrite of a SENSE-FREE file, on the PRE-FIX engine.
    with Session(engine) as s:
        gid_a = gm.create(s, "Pre-fix probe A").id
        old.import_bytes(s, None, plain, "en-US", "es-ES", glossary_id=gid_a)
    with Session(engine) as s:
        before = old.import_bytes(
            s,
            None,
            plain.replace(b"widget,artilugio", b"widget,artefacto"),
            "en-US",
            "es-ES",
            glossary_id=gid_a,
        )
    assert before["entries"] == 0 and before["senses"] == 0, (
        "pre-fix: a changed-target re-import reports NO creations -- that is why "
        "the old `entries or senses` gate never reverted",
        before,
    )
    assert "changed" not in before, ("pre-fix has no value-changed counter at all", before)
    with Session(engine) as s:
        assert (
            s.exec(
                select(GlossaryEntry).where(
                    GlossaryEntry.glossary_id == gid_a, GlossaryEntry.source_term == "widget"
                )
            )
            .first()
            .target_term
            == "artefacto"
        ), "pre-fix: the rewrite DID land; only the signal was lost"
    # B: a byte-identical re-import duplicated the sense row on the pre-fix code.
    with Session(engine) as s:
        gid_b = gm.create(s, "Pre-fix probe B").id
        old.import_bytes(s, None, _APPROVAL_CSV, "en-US", "es-ES", glossary_id=gid_b)
    n_before = len(_sense_rows(gid_b))
    with Session(engine) as s:
        dup = old.import_bytes(s, None, _APPROVAL_CSV, "en-US", "es-ES", glossary_id=gid_b)
    assert dup["senses"] == 1 and len(_sense_rows(gid_b)) == n_before + 1, (
        "pre-fix: a byte-identical sense row was inserted a SECOND time, so a "
        "no-op re-import looked like it created content",
        dup,
        _sense_rows(gid_b),
    )
    # ...and the SHIPPED code does neither: the rewrite reverts, the no-op does not.
    with Session(engine) as s:
        gm.approve(s, gid_a, approver_email="mgr@x.com")
        gm.approve(s, gid_b, approver_email="mgr@x.com")
    status_a, res_a = _reimport(gid_a, plain.replace(b"widget,artefacto", b"widget,chisme"))
    assert status_a == GlossaryStatus.DRAFT and res_a["changed"] == 1, res_a
    status_b, res_b = _reimport(gid_b, _APPROVAL_CSV)
    assert status_b == GlossaryStatus.APPROVED and res_b["senses"] == 0, res_b
    assert len(_sense_rows(gid_b)) == n_before + 1, (
        "the shipped importer must not add a third copy of the duplicated sense "
        "the pre-fix run left behind -- it upserts onto the last of them",
        _sense_rows(gid_b),
    )
    print(
        "  import approval fail-pre-fix: the HEAD engine reports 0 creations for "
        "a wholesale target rewrite (nothing for the old gate to revert on) and "
        "duplicates a byte-identical sense row (reverting for the wrong reason); "
        "the shipped code does neither OK"
    )


def _mk_doc(session, project_id, locale="es-ES", n=2):
    d = Document(
        project_id=project_id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    for i in range(n):
        session.add(
            Segment(
                document_id=d.id,
                target_locale=locale,
                order_index=i,
                source_text="Please login to the bank",
                status=SegStatus.NEW,
            )
        )
    session.commit()
    return d.id


def test_run_time_payoff():
    """A step pinned to an approved covering glossary drives match_terms(glossary_id)."""
    with Session(engine) as s:
        proj = content.project_create(
            s, "Run P", source_locale="en-US", target_locales="es-ES", default_provider="mock"
        )
        pid = proj.id
        # module glossary with a distinctive term, approved + covering
        g = gm.create(s, "Run GL")
        gm.add_term(
            s,
            g.id,
            "login",
            target_term="ACCESO_MODULE",
            source_locale="en-US",
            target_locale="es-ES",
        )
        gm.set_pairs(s, g.id, ["en-US:es-ES"])
        gm.approve(s, g.id)
        gid = g.id
        # a DIFFERENT term in the project's own (legacy) glossary
        gloss_engine.add_term(
            s,
            pid,
            "login",
            target_term="ACCESO_PROJECT",
            source_locale="en-US",
            target_locale="es-ES",
        )
        # a single-step workflow pinning the module glossary
        wf = Workflow(name="Run WF", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        st = WorkflowStep(
            workflow_id=wf.id, order_index=0, name="MT", step_type="llm_agent", glossary_id=gid
        )
        s.add(st)
        s.commit()
        _mk_doc(s, pid)
        wid = wf.id
    # pinned + approved + covering -> the module glossary's term is matched
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wid)
        resolved = orchestrator._resolved_glossary_id(s, steps, "en-US", "es-ES")
        assert resolved == gid, ("expected module glossary pinned", resolved)
        terms = gloss_engine.match_terms(
            s, pid, "Please login to the bank", "en-US", "es-ES", glossary_id=resolved
        )
        tt = {t["target_term"] for t in terms}
        assert "ACCESO_MODULE" in tt and "ACCESO_PROJECT" not in tt, tt
    # un-approve the glossary -> resolver degrades to project-scoped (None)
    with Session(engine) as s:
        gm.unapprove(s, gid)
        steps = orchestrator.get_steps(s, wid)
        assert orchestrator._resolved_glossary_id(s, steps, "en-US", "es-ES") is None
        # project-scoped match still sees the legacy project term, not the module one
        terms = gloss_engine.match_terms(
            s, pid, "Please login to the bank", "en-US", "es-ES", glossary_id=None
        )
        tt = {t["target_term"] for t in terms}
        assert "ACCESO_PROJECT" in tt and "ACCESO_MODULE" not in tt, tt
    # re-approve but change pairs so it no longer covers -> degrades to None
    with Session(engine) as s:
        gm.set_pairs(s, gid, ["en-US:de-DE"])
        gm.approve(s, gid)
        steps = orchestrator.get_steps(s, wid)
        assert orchestrator._resolved_glossary_id(s, steps, "en-US", "es-ES") is None
    print("  run-time payoff (pinned/approved/covering drives match_terms): OK")


# --- permission gates (web + api) -------------------------------------------
_UN = 0


def _mk_user(role, org_id=None):
    global _UN
    _UN += 1
    # #181: org_id at creation (the seam refuses an org-less create under the gate).
    return security.create_user(
        f"gm{role}{_UN}@x.com", role.title(), role, "gloss-module-pw-123", org_id=org_id
    )


def _key(user):
    _row, raw = apikeys.create_key(user.id, "gm-test", scopes="api docs")
    return raw


_CN = 0


def _mk_customer(org_id=None):
    """A customer for the customer-first create seams (2026-07-14: every glossary
    belongs to a customer). org_id optional -> a real Customer row; returns its id."""
    global _CN
    _CN += 1
    from app.models import Customer

    with Session(engine) as s:
        c = Customer(name=f"GM Cust {_CN}", org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def test_api_permission_gates():
    """CREATE and APPROVE are both can_tune_ai (admin or linguist) -- per the maintainer,
    linguists manage glossaries end-to-end without a manager sign-off. viewer/
    translator (below linguist's RANK 2) are denied; linguist is allowed for
    BOTH create and approve. A manager is NOT special-cased here: can_tune_ai is
    not on the rank ladder, so a plain manager (not also admin/linguist) would
    still be blocked -- but that's not the scenario under test; the point is
    that linguist no longer needs a manager to approve."""
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        viewer = _mk_user("viewer")
        translator = _mk_user("translator")
        linguist = _mk_user("linguist")
        vh = {"X-Loc-Token": _key(viewer)}
        th = {"X-Loc-Token": _key(translator)}
        lh = {"X-Loc-Token": _key(linguist)}
        # CREATE is can_tune_ai: viewer 403, translator 403, linguist ok
        r = c.post("/api/glossaries", headers=vh, json={"name": "V GL"})
        assert r.status_code == 403, r.text
        r = c.post("/api/glossaries", headers=th, json={"name": "T GL"})
        assert r.status_code == 403, ("translator (below linguist) must not create", r.text)
        r = c.post(
            "/api/glossaries", headers=lh, json={"name": "L GL", "customer_id": _mk_customer()}
        )
        assert r.status_code == 200, r.text
        gid = r.json()["id"]
        # APPROVE is ALSO can_tune_ai (per the maintainer, no manager gate): translator 403,
        # linguist now ALLOWED (previously 403 under the old can_manage gate).
        r = c.post(f"/api/glossaries/{gid}/approve", headers=th)
        assert r.status_code == 403, ("translator must not approve", r.text)
        r = c.post(f"/api/glossaries/{gid}/approve", headers=lh)
        assert r.status_code == 200, ("linguist CAN approve glossary", r.text)
        assert r.json()["status"] == "approved", r.json()
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  api permission gates (create=can_tune_ai, approve=can_tune_ai; "
        "linguist allowed on both, translator denied): OK"
    )


# --- CLI round-trip ----------------------------------------------------------
def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def test_cli_round_trip():
    rc, out = _run_cli(
        ["glossary", "module", "create", "CLI GL", "--customer", str(_mk_customer())]
    )
    assert rc == 0, out
    with Session(engine) as s:
        g = s.exec(select(Glossary).where(Glossary.name == "CLI GL")).first()
        gid = g.id
    rc, out = _run_cli(
        [
            "glossary",
            "module",
            "set-pairs",
            str(gid),
            "--pair",
            "en-US:es-ES",
            "--pair",
            "en_us:fr_FR",
        ]
    )
    assert rc == 0, out
    rc, out = _run_cli(
        [
            "glossary",
            "module",
            "add-term",
            str(gid),
            "login",
            "--target-term",
            "acceso",
            "--source",
            "en-US",
            "--target",
            "es-ES",
        ]
    )
    assert rc == 0, out
    rc, out = _run_cli(["glossary", "module", "approve", str(gid)])
    assert rc == 0, out
    rc, out = _run_cli(["--json", "glossary", "module", "show", str(gid)])
    assert rc == 0, out
    info = _json.loads(out)
    assert info["status"] == "approved", info
    assert info["entry_count"] == 1, info
    assert len(info["pairs"]) == 2, info["pairs"]
    assert any(p["target_locale"] == "es-ES" for p in info["pairs"]), info["pairs"]
    # list shows it
    rc, out = _run_cli(["--json", "glossary", "module", "list"])
    assert rc == 0, out
    assert any(g["id"] == gid for g in _json.loads(out)["glossaries"])
    # delete
    rc, out = _run_cli(["glossary", "module", "delete", str(gid), "--yes"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Glossary, gid) is None
    print("  CLI round-trip (create/set-pairs/add-term/approve/show/list/delete): OK")


def test_api_round_trip_and_isolation():
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        admin = _mk_user("admin")
        h = {"X-Loc-Token": _key(admin)}
        r = c.post(
            "/api/glossaries", headers=h, json={"name": "API GL", "customer_id": _mk_customer()}
        )
        assert r.status_code == 200, r.text
        gid = r.json()["id"]
        # set pairs (dict/list form)
        r = c.post(f"/api/glossaries/{gid}/pairs", headers=h, json={"pairs": ["en-US:es-ES"]})
        assert r.status_code == 200, r.text
        assert any(p["target_locale"] == "es-ES" for p in r.json()["pairs"]), r.json()
        # add term
        r = c.post(
            f"/api/glossaries/{gid}/terms",
            headers=h,
            json={
                "source_term": "bank",
                "target_term": "banco",
                "source": "en-US",
                "target": "es-ES",
            },
        )
        assert r.status_code == 200, r.text
        # import CSV
        r = c.post(
            f"/api/glossaries/{gid}/import",
            headers=h,
            json={"source": "en-US", "target": "es-ES", "csv_b64": base64.b64encode(_CSV).decode()},
        )
        assert r.status_code == 200, r.text
        # approve + detail
        r = c.post(f"/api/glossaries/{gid}/approve", headers=h)
        assert r.status_code == 200, r.text
        r = c.get(f"/api/glossaries/{gid}", headers=h)
        assert r.status_code == 200 and r.json()["status"] == "approved", r.text
        # list
        r = c.get("/api/glossaries", headers=h)
        assert r.status_code == 200 and any(g["id"] == gid for g in r.json()["glossaries"])
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  /api round-trip (create/pairs/terms/import/approve/detail/list): OK")


def test_api_cross_org_isolation():
    """Under multi-org, org A's token cannot see/approve org B's glossary (404)."""
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        from app.models import Organization, User

        # two real orgs + one admin bound to each
        with Session(engine) as s:
            oa = Organization(name="GM Org A")
            ob = Organization(name="GM Org B")
            s.add(oa)
            s.add(ob)
            s.commit()
            s.refresh(oa)
            s.refresh(ob)
            oaid, obid = oa.id, ob.id
        a = _mk_user("admin", org_id=oaid)
        b = _mk_user("admin", org_id=obid)
        with Session(engine) as s:
            ua = s.get(User, a.id)
            ub = s.get(User, b.id)
            ua.org_id = oaid
            ub.org_id = obid
            s.add(ua)
            s.add(ub)
            s.commit()
        ha = {"X-Loc-Token": _key(a)}
        hb = {"X-Loc-Token": _key(b)}
        r = c.post(
            "/api/glossaries",
            headers=hb,
            json={"name": "Org B GL", "customer_id": _mk_customer(org_id=obid)},
        )
        assert r.status_code == 200, r.text
        gid = r.json()["id"]
        # org A cannot GET or approve org B's glossary -> 404 (not 403)
        assert c.get(f"/api/glossaries/{gid}", headers=ha).status_code == 404
        assert c.post(f"/api/glossaries/{gid}/approve", headers=ha).status_code == 404
        # org A's list does not include it
        r = c.get("/api/glossaries", headers=ha)
        assert all(g["id"] != gid for g in r.json()["glossaries"]), r.json()
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  /api cross-org isolation (404, not 403): OK")


# ============================================================================
# Adversarial-review regressions (F1..F6). The first two CLOSE BLIND SPOTS that
# the pre-fix suite could not see.
# ============================================================================
def test_module_entry_fk_enforced_project_id_null():
    """F1 blind-spot (CRITICAL): module entries must NOT violate the
    glossaryentry.project_id -> project.id FK.

    The base suite never sees this because app/db.py never enables SQLite FK
    enforcement (PRAGMA foreign_keys=OFF by default) and CI Postgres only ran a
    schema smoke — so a project_id=0 sentinel silently "passed". Here we build a
    SEPARATE engine with FK enforcement ON and exercise the module write paths
    (add_term + import_csv). With the fix (project_id stored NULL, not 0) these
    INSERTs satisfy the FK; before the fix they'd raise IntegrityError on the
    bogus project 0. Also asserts the stored project_id is NULL."""
    from sqlalchemy import create_engine, event
    from sqlmodel import SQLModel

    fk_dir = tempfile.mkdtemp(prefix="loctms_glossmod_fk_")
    fk_engine = create_engine(f"sqlite:///{fk_dir}/fk.db")

    @event.listens_for(fk_engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    SQLModel.metadata.create_all(fk_engine)
    # Prove enforcement is really ON: a legacy entry pointing at a non-existent
    # project MUST be rejected — otherwise this test would be a no-op.
    with Session(fk_engine) as s:
        try:
            s.add(
                GlossaryEntry(
                    project_id=999999,
                    glossary_id=None,
                    source_locale="en-US",
                    target_locale="es-ES",
                    source_term="x",
                    target_term="y",
                )
            )
            s.commit()
            _fail("FK enforcement not active — a dangling project_id was accepted")
        except Exception:
            s.rollback()

    with Session(fk_engine) as s:
        g = gm.create(s, "FK GL")
        gid = g.id
    # add_term via the module: with project_id NULL this satisfies the FK.
    with Session(fk_engine) as s:
        entry = gm.add_term(
            s, gid, "login", target_term="acceso", source_locale="en-US", target_locale="es-ES"
        )
        assert entry is not None and entry.project_id is None, (
            "module add_term must store project_id NULL",
            getattr(entry, "project_id", "?"),
        )
    # import_csv via the module: same FK, same NULL project_id.
    with Session(fk_engine) as s:
        # "login" already exists from add_term above, so import adds only "bank".
        res = gm.import_csv(s, gid, _CSV, "en-US", "es-ES")
        assert res["entries"] >= 1 and res["senses"] == 2, res
    with Session(fk_engine) as s:
        rows = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).all()
        assert rows and all(r.project_id is None for r in rows), [r.project_id for r in rows]
    fk_engine.dispose()
    print("  F1 FK-ON module inserts (project_id NULL, FK satisfied): OK")


def test_cli_item_commands_org_scoped():
    """F2 blind-spot (CRITICAL, R2-3): under multi-org, --org A must NOT be able to
    show/approve/unapprove/set-pairs/add-term/import/delete an org-B glossary.

    The base suite never sees this because it exercised the item commands only
    single-tenant (gate off), where gm.get() and gm.visible() are indistinguishable.
    With the gate ON, a bare gm.get() would happily return org B's glossary to an
    --org A operator; the fix routes every item command through gm.visible(org_id=)
    so a foreign id 404s (exit 2)."""
    from app.models import Organization

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            oa = Organization(name="CLI Org A")
            ob = Organization(name="CLI Org B")
            s.add(oa)
            s.add(ob)
            s.commit()
            s.refresh(oa)
            s.refresh(ob)
            oaid, obid = oa.id, ob.id
            # a glossary owned by org B (with a CSV to import into) + a pair
            g = gm.create(s, "Org B CLI GL", org_id=obid)
            gid = g.id
        # write a CSV file to import (import needs a real path)
        csv_path = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "f2.csv")
        with open(csv_path, "wb") as fh:
            fh.write(_CSV)

        a = str(oaid)
        b = str(obid)
        gs = str(gid)
        # Every item command under --org A against org-B's glossary -> exit 2.
        foreign = [
            ["--org", a, "glossary", "module", "show", gs],
            ["--org", a, "glossary", "module", "approve", gs],
            ["--org", a, "glossary", "module", "unapprove", gs],
            ["--org", a, "glossary", "module", "set-pairs", gs, "--pair", "en-US:es-ES"],
            ["--org", a, "glossary", "module", "add-term", gs, "login", "--target", "es-ES"],
            [
                "--org",
                a,
                "glossary",
                "module",
                "import",
                gs,
                csv_path,
                "--source",
                "en-US",
                "--target",
                "es-ES",
            ],
            ["--org", a, "glossary", "module", "delete", gs],
        ]
        for argv in foreign:
            rc, out = _run_cli(argv)
            # exit 2 (EXIT_USAGE) = the 404-as-usage refusal; the "no such glossary"
            # message itself goes to stderr (not captured here).
            assert rc == 2, ("F2: cross-org must be refused (exit 2)", argv, rc, out)
        # And org B (the owner) still works for the same commands.
        assert _run_cli(["--org", b, "glossary", "module", "show", gs])[0] == 0
        assert _run_cli(["--org", b, "glossary", "module", "approve", gs])[0] == 0
        assert _run_cli(["--org", b, "glossary", "module", "unapprove", gs])[0] == 0
        assert (
            _run_cli(["--org", b, "glossary", "module", "set-pairs", gs, "--pair", "en-US:es-ES"])[
                0
            ]
            == 0
        )
        assert (
            _run_cli(
                ["--org", b, "glossary", "module", "add-term", gs, "login", "--target", "es-ES"]
            )[0]
            == 0
        )
        assert (
            _run_cli(
                [
                    "--org",
                    b,
                    "glossary",
                    "module",
                    "import",
                    gs,
                    csv_path,
                    "--source",
                    "en-US",
                    "--target",
                    "es-ES",
                ]
            )[0]
            == 0
        )
        assert _run_cli(["--org", b, "glossary", "module", "delete", gs, "--yes"])[0] == 0
        with Session(engine) as s:
            assert s.get(Glossary, gid) is None
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  F2 CLI item commands org-scoped (cross-org 404 for every command): OK")


def test_cli_add_step_glossary_pin():
    """F3: `workflow add-step --glossary` pins any APPROVED glossary visible to the
    caller; an unapproved / foreign id is a CLEAR ERROR (not silently dropped). A
    glossary that does NOT cover the workflow's own pair is now pinnable too (a
    workflow is language-agnostic; a non-covering pin is skipped at run time with a
    note), so it must succeed here, not error."""
    with Session(engine) as s:
        wf = Workflow(name="F3 WF", scope="global", source_locale="en-US", target_locale="es-ES")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
        # an approved covering glossary
        good = gm.create(s, "F3 approved covering")
        gm.set_pairs(s, good.id, ["en-US:es-ES"])
        gm.approve(s, good.id)
        good_id = good.id
        # a DRAFT (unapproved) covering glossary
        draft = gm.create(s, "F3 draft")
        gm.set_pairs(s, draft.id, ["en-US:es-ES"])
        draft_id = draft.id
        # an approved but NON-covering glossary (covers de-DE, not the wf's es-ES)
        wrong = gm.create(s, "F3 wrong pair")
        gm.set_pairs(s, wrong.id, ["en-US:de-DE"])
        gm.approve(s, wrong.id)
        wrong_id = wrong.id
    # valid pin -> step gets glossary_id
    rc, out = _run_cli(
        [
            "--json",
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--glossary",
            str(good_id),
        ]
    )
    assert rc == 0, out
    assert _json.loads(out)["glossary_id"] == good_id, out
    # unapproved -> error (exit 2). The message is on stderr; rc==2 is the signal.
    rc, out = _run_cli(["workflow", "add-step", str(wid), "--glossary", str(draft_id)])
    assert rc == 2, ("unapproved must error", rc, out)
    # T2: approved but non-covering -> PINNED (a workflow is language-agnostic; the run
    # resolver skips a pin that misses the run's languages, with a note).
    rc, out = _run_cli(
        [
            "--json",
            "workflow",
            "add-step",
            str(wid),
            "--type",
            "llm_agent",
            "--glossary",
            str(wrong_id),
        ]
    )
    assert rc == 0, ("non-covering must pin now (not error)", rc, out)
    assert _json.loads(out)["glossary_id"] == wrong_id, out
    # a nonexistent id -> error
    rc, out = _run_cli(["workflow", "add-step", str(wid), "--glossary", "999999"])
    assert rc == 2, ("foreign/absent must error", rc, out)
    print("  F3 CLI add-step --glossary (approved pins incl non-covering; bad ids error): OK")


def test_cli_edit_step_glossary_pin():
    """T2: `workflow edit-step --glossary` mirrors add-step -- any APPROVED glossary visible
    to the caller is pinnable, INCLUDING one that does NOT cover the workflow's pair (skipped
    at run time with a note); an unapproved / foreign id is still a CLEAR ERROR."""
    with Session(engine) as s:
        wf = Workflow(name="F3b WF", scope="global", source_locale="en-US", target_locale="es-ES")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
        wrong = gm.create(s, "F3b noncover")  # approved, covers de-DE (not the wf's es-ES)
        gm.set_pairs(s, wrong.id, ["en-US:de-DE"])
        gm.approve(s, wrong.id)
        wrong_id = wrong.id
        draft = gm.create(s, "F3b draft")
        gm.set_pairs(s, draft.id, ["en-US:es-ES"])
        draft_id = draft.id
    # a plain llm_agent step to edit (no glossary yet)
    rc, out = _run_cli(["workflow", "add-step", str(wid), "--type", "llm_agent"])
    assert rc == 0, out
    with Session(engine) as s:
        step_id = orchestrator.get_steps(s, wid)[0].id
    # edit-step --glossary a NON-covering approved glossary -> pinned (rc 0), not an error
    rc, out = _run_cli(
        ["--json", "workflow", "edit-step", str(wid), str(step_id), "--glossary", str(wrong_id)]
    )
    assert rc == 0, ("edit-step non-covering must pin now (not error)", rc, out)
    assert _json.loads(out)["glossary_id"] == wrong_id, out
    # edit-step --glossary an UNAPPROVED id -> error (exit 2)
    rc, out = _run_cli(
        ["workflow", "edit-step", str(wid), str(step_id), "--glossary", str(draft_id)]
    )
    assert rc == 2, ("edit-step unapproved must error", rc, out)
    # a nonexistent id -> error
    rc, out = _run_cli(["workflow", "edit-step", str(wid), str(step_id), "--glossary", "999999"])
    assert rc == 2, ("edit-step foreign/absent must error", rc, out)
    print("  F3b CLI edit-step --glossary (non-covering pins; unapproved/foreign error): OK")


def test_resolver_scans_past_non_covering():
    """F4: a step-0 pin that does NOT cover the run's pair must not suppress a
    covering pin on a LATER step (first COVERING pin wins)."""
    with Session(engine) as s:
        wf = Workflow(name="F4 WF", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        # G1: approved but non-covering for en-US->es-ES (covers de-DE)
        g1 = gm.create(s, "F4 G1 noncover")
        gm.set_pairs(s, g1.id, ["en-US:de-DE"])
        gm.approve(s, g1.id)
        # G2: approved AND covering en-US->es-ES
        g2 = gm.create(s, "F4 G2 cover")
        gm.set_pairs(s, g2.id, ["en-US:es-ES"])
        gm.approve(s, g2.id)
        g2id = g2.id
        # step 0 pins G1 (non-covering), step 1 pins G2 (covering)
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=0,
                name="s0",
                step_type="llm_agent",
                glossary_id=g1.id,
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id,
                order_index=1,
                name="s1",
                step_type="llm_agent",
                glossary_id=g2.id,
            )
        )
        s.commit()
        wid = wf.id
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wid)
        resolved = orchestrator._resolved_glossary_id(s, steps, "en-US", "es-ES")
        assert resolved == g2id, (
            "must scan past the non-covering step-0 pin to the covering step-1 pin",
            resolved,
        )
    print("  F4 resolver scans past a non-covering pin to a later covering one: OK")


def test_dashboard_count_excludes_module_entries():
    """F5: the unrestricted admin-dashboard glossary total must count ONLY legacy
    (project) entries — module entries must not inflate it."""
    with Session(engine) as s:
        proj = content.project_create(
            s, "F5 P", source_locale="en-US", target_locales="es-ES", default_provider="mock"
        )
        gloss_engine.add_term(
            s, proj.id, "legacyf5", target_term="x", source_locale="en-US", target_locale="es-ES"
        )
        # count the legacy total directly (unrestricted branch semantics)
        legacy_total = len(
            s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == None)).all()  # noqa: E711
        )
        # now add a MODULE entry — it must NOT change the legacy total
        g = gm.create(s, "F5 module GL")
        gm.add_term(s, g.id, "modf5", target_term="y", source_locale="en-US", target_locale="es-ES")
        legacy_after = len(
            s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == None)).all()  # noqa: E711
        )
        assert legacy_after == legacy_total, (
            "module entry inflated the legacy count",
            legacy_total,
            legacy_after,
        )
    print("  F5 dashboard glossary total excludes module entries: OK")


def test_cli_create_customer_org_validated():
    """F6: `glossary module create --customer` must reject a customer from another
    org (matching the web/api _valid_customer_id gate)."""
    from app.models import Organization

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            oa = Organization(name="F6 Org A")
            ob = Organization(name="F6 Org B")
            s.add(oa)
            s.add(ob)
            s.commit()
            s.refresh(oa)
            s.refresh(ob)
            oaid, obid = oa.id, ob.id
            # a customer that belongs to org B
            cust = content.customer_create(s, "F6 Cust B", creator=None)
            cust.org_id = obid
            s.add(cust)
            s.commit()
            s.refresh(cust)
            cid = cust.id
        # --org A trying to scope a new glossary to org B's customer -> rejected.
        rc, out = _run_cli(
            [
                "--org",
                str(oaid),
                "glossary",
                "module",
                "create",
                "F6 GL foreign cust",
                "--customer",
                str(cid),
            ]
        )
        # exit 2 = "no such customer" refusal (message on stderr).
        assert rc == 2, ("foreign customer must be rejected", rc, out)
        # --org B (the owner) is accepted.
        rc, out = _run_cli(
            [
                "--org",
                str(obid),
                "glossary",
                "module",
                "create",
                "F6 GL own cust",
                "--customer",
                str(cid),
            ]
        )
        assert rc == 0, ("own-org customer must be accepted", rc, out)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  F6 CLI create --customer validates org ownership: OK")


# ============================================================================
# Issue #19: `glossary module show --json` returned only entry_count (no terms
# array), so the rich TBX metadata accepted by add-term/edit-term (definition,
# part_of_speech, usage_example, subject_field, notes) + senses could not be read
# back via the CLI. Fix: a `terms` array on --json (id/source/target/locales/dnt/
# meta/senses) + a compact 'meta' indicator column on the human table.
# ============================================================================
def test_cli_show_reveals_full_term_metadata():
    with Session(engine) as s:
        g = gm.create(s, "Meta Show GL", customer_id=_mk_customer())
        gid = g.id
    # a term with every TBX-ish meta field set, via the CLI (the real ingestion path)
    rc, out = _run_cli(
        [
            "glossary",
            "module",
            "add-term",
            str(gid),
            "termination",
            "--target-term",
            "rescision",
            "--source",
            "en-US",
            "--target",
            "es-ES",
            "--part-of-speech",
            "noun",
            "--definition",
            "end of a contract",
            "--usage-example",
            "the termination clause",
            "--subject-field",
            "legal",
            "--notes",
            "handle with care",
        ]
    )
    assert rc == 0, out
    with Session(engine) as s:
        entry = s.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == gid)).first()
        eid = entry.id
        gm.add_sense(
            s,
            gid,
            eid,
            "contract-law",
            "rescision contractual",
            context_cue="legal contract context",
        )

    # --json: the terms array carries the term + ALL meta fields + its sense.
    rc, out = _run_cli(["--json", "glossary", "module", "show", str(gid)])
    assert rc == 0, out
    info = _json.loads(out)
    assert "terms" in info, ("show --json must carry a terms array", info)
    assert len(info["terms"]) == 1, info["terms"]
    t = info["terms"][0]
    assert t["id"] == eid, t
    assert t["source_term"] == "termination" and t["target_term"] == "rescision", t
    assert t["source_locale"] == "en-US" and t["target_locale"] == "es-ES", t
    assert t["dnt"] is False, t
    meta = t["meta"]
    assert meta["part_of_speech"] == "noun", meta
    assert meta["definition"] == "end of a contract", meta
    assert meta["usage_example"] == "the termination clause", meta
    assert meta["subject_field"] == "legal", meta
    assert meta["notes"] == "handle with care", meta
    assert len(t["senses"]) == 1, t["senses"]
    sense = t["senses"][0]
    assert sense["sense_label"] == "contract-law", sense
    assert sense["target_term"] == "rescision contractual", sense
    assert sense["context_cue"] == "legal contract context", sense

    # human table: a compact meta indicator is visible (not the pre-fix table with
    # no meta signal at all).
    rc, out = _run_cli(["glossary", "module", "show", str(gid)])
    assert rc == 0, out
    assert "meta" in out, ("human table must have a meta column header", out)
    assert "pos" in out and "def" in out, ("human table must indicate populated meta fields", out)

    # a term with NO meta shows '-' (not a blank/misleading indicator).
    rc, out = _run_cli(
        [
            "glossary",
            "module",
            "add-term",
            str(gid),
            "plain",
            "--target-term",
            "llano",
            "--source",
            "en-US",
            "--target",
            "es-ES",
        ]
    )
    assert rc == 0, out
    rc, out = _run_cli(["--json", "glossary", "module", "show", str(gid)])
    assert rc == 0, out
    plain = next(t for t in _json.loads(out)["terms"] if t["source_term"] == "plain")
    assert plain["meta"] == {}, plain
    print(
        "  #19 `glossary module show` reveals full TBX term metadata + senses (human + --json) OK"
    )


# ============================================================================
# Issue #18: `glossary module delete` had NO confirmation gate (immediate,
# unconditional delete). Fix: standard `confirm()` gate matching tm delete/doc
# delete -- refuses in non-tty without --yes, --yes skips the prompt.
# ============================================================================
def test_cli_delete_requires_confirm_gate():
    """Pre-fix this deleted unconditionally; the gate must now refuse without
    --yes (non-tty test process = confirm() auto-refuses) and require --yes to
    actually delete."""
    with Session(engine) as s:
        g = gm.create(s, "Gate GL", customer_id=_mk_customer())
        gid = g.id
    # no --yes, non-tty -> refused, glossary still exists
    rc, out = _run_cli(["glossary", "module", "delete", str(gid)])
    assert rc != 0, ("delete without --yes must be refused", rc, out)
    with Session(engine) as s:
        assert s.get(Glossary, gid) is not None, "glossary must NOT be deleted without --yes"
    # --json + no --yes -> same refusal (machine caller must pass --yes explicitly)
    rc, out = _run_cli(["--json", "glossary", "module", "delete", str(gid)])
    assert rc != 0, ("--json delete without --yes must be refused", rc, out)
    with Session(engine) as s:
        assert s.get(Glossary, gid) is not None
    # --yes -> succeeds
    rc, out = _run_cli(["glossary", "module", "delete", str(gid), "--yes"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Glossary, gid) is None
    print("  #18 delete confirm gate (refuses without --yes, --yes deletes): OK")


# ============================================================================
# CLI-parity backlog item 1: `glossary module export` -- mirrors the legacy
# per-project `glossary export`. Round-trip (export -> import reproduces the
# glossary incl. TBX-ish meta), CLI local arm, /api read parity, cross-org RBAC.
# ============================================================================
def test_export_round_trip_local_and_api():
    cid = _mk_customer()
    with Session(engine) as s:
        g = gm.create(s, "Export GL", customer_id=cid)
        gid = g.id
        # "greeting" carries TBX-ish meta, distinct from the CSV's own terms below so
        # the import doesn't clobber it.
        gm.add_term(
            s,
            gid,
            "greeting",
            target_term="saludo",
            source_locale="en-US",
            target_locale="es-ES",
            meta={"definition": "a hello", "part_of_speech": "noun"},
        )
        gm.import_csv(s, gid, _CSV, "en-US", "es-ES")

    # --- service export_csv: header + the term's meta survive ---
    with Session(engine) as s:
        data = gm.export_csv(s, gid)
    text = data.decode("utf-8")
    assert text.startswith(
        "source_term,target_term,dnt,sense_label,context_cue,"
        "part_of_speech,definition,usage_example,subject_field,notes"
    ), text[:120]
    assert "greeting,saludo" in text and "a hello" in text and "noun" in text, text
    assert "login,ingreso" in text, text
    assert "bank,banco" in text and "bank,orilla" in text, "sense rows survive"

    # --- round-trip: export -> import into a FRESH glossary reproduces the terms,
    # including the TBX-ish meta (the round-trip guarantee the backlog item asks for).
    cid2 = _mk_customer()
    with Session(engine) as s:
        g2 = gm.create(s, "Export GL Reimport", customer_id=cid2)
        gid2 = g2.id
        res = gm.import_csv(s, gid2, data, "en-US", "es-ES")
    assert res["entries"] == 3, res  # greeting, login, bank (2 senses -> 1 entry)
    with Session(engine) as s:
        entries2 = {it["entry"].source_term: it for it in gm.list_entries(s, gid2)}
        greeting2 = entries2["greeting"]
        assert greeting2["entry"].target_term == "saludo"
        assert greeting2["entry"].meta == {"definition": "a hello", "part_of_speech": "noun"}
        login2 = entries2["login"]
        assert login2["entry"].target_term == "ingreso"
        bank2 = entries2["bank"]
        assert len(bank2["senses"]) == 2, "both senses (finance/river) round-trip"

    # --- CLI local arm: `glossary module export <gid> <path>` writes the SAME bytes
    # the service produces.
    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, "gl.csv")
        rc, out = _run_cli(["glossary", "module", "export", str(gid), out_path])
        assert rc == 0, out
        with open(out_path, "rb") as fh:
            written = fh.read()
        assert written == data, "CLI export must write byte-identical CSV to the service"
        # --force required to overwrite an existing file (mirrors the legacy export).
        rc, out = _run_cli(["glossary", "module", "export", str(gid), out_path])
        assert rc != 0, "a second export without --force must refuse (exists)"
        rc, out = _run_cli(["glossary", "module", "export", str(gid), out_path, "--force"])
        assert rc == 0, out

    # --- /api read parity: GET /api/glossaries/{id}/export returns the SAME bytes
    # base64-encoded, and a foreign org's/customer's caller is refused (404, RBAC).
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        from app.models import Organization, User

        with Session(engine) as s:
            oa = Organization(name="ExportOrgA")
            ob = Organization(name="ExportOrgB")
            s.add(oa)
            s.add(ob)
            s.commit()
            s.refresh(oa)
            s.refresh(ob)
            oaid, obid = oa.id, ob.id
            row = s.get(Glossary, gid)
            row.org_id = oaid
            s.add(row)
            s.commit()
        a = _mk_user("admin", org_id=oaid)
        b = _mk_user("admin", org_id=obid)
        with Session(engine) as s:
            ua = s.get(User, a.id)
            ub = s.get(User, b.id)
            ua.org_id = oaid
            ub.org_id = obid
            s.add(ua)
            s.add(ub)
            s.commit()
        c = TestClient(app)
        # same-org caller: 200, csv_b64 decodes to the exact service bytes.
        r = c.get(f"/api/glossaries/{gid}/export", headers={"X-Loc-Token": _key(a)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert base64.b64decode(body["csv_b64"]) == data, "/api export bytes must match"
        assert body["bytes"] == len(data)
        # cross-org caller: RBAC probe -- 404, never the foreign glossary's CSV.
        r = c.get(f"/api/glossaries/{gid}/export", headers={"X-Loc-Token": _key(b)})
        assert r.status_code == 404, ("a foreign-org caller must be refused (404)", r.text)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print(
        "  #1 `glossary module export`: round-trip (export->import reproduces terms "
        "+ TBX meta), CLI local arm byte-identical, /api read parity, cross-org RBAC "
        "refused (404) OK"
    )


def test_paging_correctness():
    with Session(engine) as session:
        g = gm.create(session, "Paging correctness")
        gid = g.id
        ids = []
        for i in range(5):
            e = gm.add_term(
                session,
                gid,
                f"page-{i}",
                target_term="target",
                source_locale="en-US",
                target_locale="es-ES",
            )
            ids.append(e.id)
            gm.add_sense(session, gid, e.id, f"sense-{i}", f"target-{i}")
        cursor, seen = None, []
        for n in (2, 2, 1):
            page = gm.page_entries(session, gid, cursor=cursor, limit=2)
            assert len(page["entries"]) == n
            assert page["has_more"] == (n == 2)
            for item in page["entries"]:
                e = item["entry"]
                seen.append(e.id)
                assert len(item["senses"]) == 1
                assert item["senses"][0].glossary_entry_id == e.id
                assert item["senses"][0].sense_label == "sense-" + e.source_term.split("-")[-1]
            cursor = page["next_cursor"]
        assert seen == ids and len(set(seen)) == 5
        for bad_cursor in ("bad", "-5", "0", "1e9"):
            try:
                gloss_engine.page_entries(session, glossary_id=gid, cursor=bad_cursor, limit=0)
                raise AssertionError("invalid cursor silently restarted the walk")
            except ValueError as exc:
                assert str(exc) == "cursor must be a positive integer"
        # The defensive service limit clamp remains independent of cursor validation.
        assert (
            gloss_engine.page_entries(session, glossary_id=gid, cursor="", limit=0)["entries"][0][
                "entry"
            ].id
            == ids[0]
        )
        for kwargs in ({}, {"project_id": 1, "glossary_id": gid}):
            try:
                gloss_engine.page_entries(session, **kwargs)
                raise AssertionError("scope selector accepted neither/both owners")
            except ValueError:
                pass
        other = gm.create(session, "Other paging scope")
        gm.add_term(
            session,
            other.id,
            "foreign",
            target_term="target",
            source_locale="en-US",
            target_locale="es-ES",
        )
        assert [
            it["entry"].id for it in gloss_engine.iter_entries(session, glossary_id=gid, chunk=2)
        ] == ids
        assert len(gloss_engine.page_entries(session, glossary_id=gid, limit=10000)["entries"]) == 5
        page = gm.page_entries(session, gid, cursor=ids[-1] + 100, limit=2)
        assert page == {"entries": [], "has_more": False, "next_cursor": None}
    print("  glossary paging correctness: OK")


def main():
    init_db()
    # T2: a MODULE TBX import is concept-preserving (LAW-TERM-9), whose write seams sit behind
    # the DEFAULT-OFF terminology_concept_editor flag (T1-lite). Enable it so the TBX arms
    # exercise the concept path; CSV import is unaffected by the flag.
    from app.services import settings as _settings_mod

    _settings_mod.set_many({"terminology_concept_editor_enabled": "1"})
    _settings_mod.invalidate_cache()
    test_paging_correctness()
    test_schema()
    test_approval_lifecycle()
    test_pairs_canonicalization()
    test_dropdown_filter()
    test_module_csv_import_and_legacy_isolation()
    test_import_approval_revert_semantics()
    test_reimport_blank_or_absent_dnt_keeps_the_flag()
    test_reimport_short_csv_row_keeps_the_flag()
    test_concept_import_idempotent_reimport_keeps_approved()
    test_concept_reimport_changed_concept_content_reverts()
    test_web_import_flash_shows_changed_and_revert_note()
    test_cli_import_human_line_shows_changed_counts()
    test_export_reimport_round_trip_is_a_no_op()
    test_pre_existing_duplicate_senses_converge()
    test_import_approval_fail_before_pass_after()
    test_run_time_payoff()
    test_api_permission_gates()
    test_cli_round_trip()
    test_api_round_trip_and_isolation()
    test_api_cross_org_isolation()
    # Adversarial-review regressions (F1..F6):
    test_module_entry_fk_enforced_project_id_null()
    test_cli_item_commands_org_scoped()
    test_cli_add_step_glossary_pin()
    test_cli_edit_step_glossary_pin()
    test_resolver_scans_past_non_covering()
    test_dashboard_count_excludes_module_entries()
    test_cli_create_customer_org_validated()
    test_cli_show_reveals_full_term_metadata()
    test_cli_delete_requires_confirm_gate()
    test_export_round_trip_local_and_api()
    print("GLOSSARY MODULE OK")


if __name__ == "__main__":
    main()
