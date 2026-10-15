"""Content-type GLOSSARY scoping verification (SPEC-content-types, PHASE 2).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked). ZERO live
calls. Covers §3 (glossary scoping) only — TM scoping (§4) is phase 3, untouched here.

Covers:

  schema
    - glossary.content_type_id column exists

  service (set/create/clear + validation + non-revert)
    - create(content_type_id=) attaches; set_content_type sets/clears
    - a FOREIGN-customer / unknown content type is rejected (GlossaryModuleError)
    - set_content_type on an APPROVED glossary does NOT revert it to draft (scoping
      metadata, same rationale as rename) — the deliberate difference from term edits
    - to_dict exposes content_type_id

  delete FAILS CLOSED (phase-2 extension)
    - content_types.reference_count now counts glossaries; delete refused while a
      glossary references the type; allowed once cleared

  dropdown filter (options_for_step)
    - a PROJECT-scoped dropdown (content_type_id passed) offers only (that type OR NULL)
    - a GLOBAL/customer-scoped workflow (the _NO_CT_FILTER sentinel = every current
      caller) offers ALL approved glossaries (run-time enforcement is the guard there)

  content_type_allows predicate (General matches every project; typed matches own only)

  the AUTO-ATTACH GATE + plan (build_glossary_plan)
    - an UNTYPED project auto-attaches NOTHING even with a matching approved General
      glossary present (the byte-identical gate: glossary_ids == (), legacy only)
    - a TYPED project auto-attaches its type + General, ordered specific-first id-asc

  merge PRECEDENCE (match_terms_merged)
    - content-type-specific beats General on a source-term conflict
    - deterministic tie-break: among two General glossaries, lower id wins
    - a NULL-content-type (General) glossary DOES apply to a typed project

  run-time pin SKIP (orchestrator._resolved_glossary_id)
    - a Marketing-pinned step on a Product project is SKIPPED (returns None -> fall
      through to auto-attach), with a run-trace note; a matching pin is honoured

  AUTO-ATTACH end-to-end on BOTH paths (resolved term set asserted via the provider
  payload's injected glossary)
    - plain pretranslate path: a Product project auto-gets Product + General terms,
      NOT Marketing; an untyped project auto-gets NONE (byte-identical)
    - orchestrator segmented path: same; AND a Marketing-pinned step is skipped so a
      Product project still injects Product + General, never Marketing

  THREE-SURFACE parity + RBAC (web / CLI / api set-content-type) + can_tune_ai gate
  multi-org / multi-customer isolation of auto-attach

Final line: "CONTENT TYPE GLOSSARY OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ctg_")

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Glossary,
    GlossaryStatus,
    Segment,
    TransMode,
    User,
    WorkflowStep,
)
from app.services import (
    apikeys,
    content,
    orchestrator,
    security,
    settings,
    translate,
    workflows,
)
from app.services import (
    content_types as ct,
)
from app.services import (
    glossary_module as gm,
)
from app.services.providers import Provider, register_provider


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --- a non-mock capturing provider so we can read the EXACT injected glossary ----
class CaptureProvider(Provider):
    name = "capture"
    calls = []  # noqa: RUF012

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        CaptureProvider.calls.append({"task": task, "payload": payload or {}})
        self.last_usage = {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        payload = payload or {}
        if task == "translate":
            return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"
        return (prompt or "").strip() or "ok"

    @classmethod
    def reset(cls):
        cls.calls = []


def _injected_source_terms():
    """Every glossary source_term injected into a translate call this run."""
    terms = set()
    for c in CaptureProvider.calls:
        if c["task"] != "translate":
            continue
        for t in c["payload"].get("glossary") or []:
            terms.add(t.get("source_term"))
    return terms


# --- helpers -----------------------------------------------------------------
def _mk_glossary(
    session,
    name,
    *,
    content_type_id=None,
    customer_id=None,
    org_id=None,
    terms=(),
    src="en-US",
    tgt="fr-FR",
    approve=True,
):
    """A module glossary with terms + the (src,tgt) pair, content type set BEFORE
    approval (approve LAST — term/pair edits revert to draft, set_content_type does
    not). `terms` = iterable of (source_term, target_term)."""
    g = gm.create(session, name, org_id=org_id, customer_id=customer_id)
    for st_, tt_ in terms:
        gm.add_term(session, g.id, st_, target_term=tt_, source_locale=src, target_locale=tgt)
    gm.set_pairs(session, g.id, [(src, tgt)])
    if content_type_id is not None:
        gm.set_content_type(session, g.id, content_type_id)
    if approve:
        gm.approve(session, g.id)
    session.refresh(g)
    return g


def _seg_doc(session, project_id, source_text, locale="fr-FR"):
    """A one-segment SEGMENTED document ready for pretranslate/orchestrator."""
    doc = Document(project_id=project_id, filename="d.txt", mode=TransMode.SEGMENTED)
    session.add(doc)
    session.commit()
    session.refresh(doc)
    seg = Segment(
        document_id=doc.id,
        target_locale=locale,
        order_index=0,
        source_text=source_text,
        target_text="",
    )
    session.add(seg)
    session.commit()
    return doc.id


# text that contains every probe source term so match_terms can find them
PROBE_SRC = "The zzprodword and zzgenword and zzmktword all appear here."


# ============================================================================
def test_schema():
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("glossary")}
    assert "content_type_id" in cols, cols
    print("  schema: glossary.content_type_id column exists: OK")


def test_service_set_create_clear_and_validation():
    with Session(engine) as s:
        prod = ct.create(s, "Svc Product")
        pid = prod.id
        # create with content type
        g = gm.create(s, "Svc G1", content_type_id=pid)
        assert g.content_type_id == pid
        d = gm.to_dict(s, g)
        assert d["content_type_id"] == pid, d
        # set to clear
        gm.set_content_type(s, g.id, "")
        assert s.get(Glossary, g.id).content_type_id is None
        # set back
        gm.set_content_type(s, g.id, pid)
        assert s.get(Glossary, g.id).content_type_id == pid
        # unknown id rejected
        try:
            gm.set_content_type(s, g.id, 999999)
            _fail("unknown content type must reject")
        except gm.GlossaryModuleError:
            pass
    # foreign-customer type rejected on a differently-scoped glossary
    with Session(engine) as s:
        custA = Customer(name="Svc CustA")
        custB = Customer(name="Svc CustB")
        s.add(custA)
        s.add(custB)
        s.commit()
        s.refresh(custA)
        s.refresh(custB)
        b_type = ct.create(s, "Svc B type", customer_id=custB.id)
        # a glossary scoped to customer A cannot attach customer B's type
        gA = gm.create(s, "Svc GA", customer_id=custA.id)
        try:
            gm.set_content_type(s, gA.id, b_type.id)
            _fail("foreign-customer content type must reject")
        except gm.GlossaryModuleError:
            pass
        # create() validates the same way
        try:
            gm.create(s, "Svc GA2", customer_id=custA.id, content_type_id=b_type.id)
            _fail("foreign-customer content type must reject at create")
        except gm.GlossaryModuleError:
            pass
    print("  service set/create/clear + foreign-type rejection: OK")


def test_approved_not_reverted_on_content_type_change():
    with Session(engine) as s:
        t1 = ct.create(s, "Rev Type")
        g = _mk_glossary(s, "Rev G", terms=[("x", "y")], approve=True)
        assert s.get(Glossary, g.id).status == GlossaryStatus.APPROVED
        gm.set_content_type(s, g.id, t1.id)
        assert s.get(Glossary, g.id).status == GlossaryStatus.APPROVED, (
            "set_content_type must NOT revert an approved glossary to draft"
        )
        gm.set_content_type(s, g.id, "")  # clearing likewise does not revert
        assert s.get(Glossary, g.id).status == GlossaryStatus.APPROVED
    print("  APPROVED not reverted on content-type set/clear: OK")


def test_delete_fails_closed_with_glossary():
    with Session(engine) as s:
        t = ct.create(s, "DelG Type")
        cid = t.id
        g = gm.create(s, "DelG Glossary", content_type_id=cid)
        gid = g.id
    with Session(engine) as s:
        refs = ct.reference_count(s, cid)
        assert refs["glossaries"] == 1 and refs["total"] == 1, refs
        try:
            ct.delete(s, cid)
            _fail("delete must fail closed while a glossary references the type")
        except ct.ContentTypeError as exc:
            assert "glossary" in str(exc).lower(), str(exc)
    with Session(engine) as s:
        gm.set_content_type(s, gid, "")  # clear the reference
    with Session(engine) as s:
        assert ct.reference_count(s, cid)["glossaries"] == 0
        ct.delete(s, cid)  # now allowed
    print("  delete FAILS CLOSED on a glossary reference (then allowed): OK")


def test_options_for_step_filter():
    with Session(engine) as s:
        prod = ct.create(s, "Opt Product")
        mkt = ct.create(s, "Opt Marketing")
        gp = _mk_glossary(s, "Opt GP", content_type_id=prod.id, terms=[("a", "b")])
        gm_ = _mk_glossary(s, "Opt GM", content_type_id=mkt.id, terms=[("a", "b")])
        gn = _mk_glossary(s, "Opt GN", content_type_id=None, terms=[("a", "b")])
        pid, mid, nid = gp.id, gm_.id, gn.id
        prod_id = prod.id
    with Session(engine) as s:
        # PROJECT-scoped dropdown for a Product project -> Product + General, not Mktg
        offered = {g.id for g in gm.options_for_step(s, "en-US", "fr-FR", content_type_id=prod_id)}
        assert pid in offered and nid in offered, offered
        assert mid not in offered, ("marketing glossary must not be offered", offered)
        # PROJECT-scoped dropdown for an UNTYPED project -> General only (membership:
        # the shared DB holds other approved General glossaries from earlier tests, so
        # assert on THESE ids, not exact set equality).
        untyped = {g.id for g in gm.options_for_step(s, "en-US", "fr-FR", content_type_id=None)}
        assert nid in untyped, untyped
        assert pid not in untyped and mid not in untyped, untyped
        # GLOBAL/customer workflow (sentinel = default) -> ALL approved shown
        allg = {g.id for g in gm.options_for_step(s, "en-US", "fr-FR")}
        assert {pid, mid, nid} <= allg, ("sentinel must not filter by content type", allg)
    print("  options_for_step: project-scoped filters, global sentinel shows all: OK")


def test_content_type_allows():
    assert gm.content_type_allows(None, 7) is True  # General matches any project
    assert gm.content_type_allows(None, None) is True  # General matches untyped
    assert gm.content_type_allows(7, 7) is True  # same type
    assert gm.content_type_allows(7, 9) is False  # different type
    assert gm.content_type_allows(7, None) is False  # typed glossary, untyped proj
    print("  content_type_allows predicate (General-matches-all, typed-own-only): OK")


def test_build_plan_gate_and_autoattach():
    with Session(engine) as s:
        prod = ct.create(s, "Plan Product")
        # a matching approved GENERAL glossary + a Product glossary
        gn = _mk_glossary(s, "Plan Gen", content_type_id=None, terms=[("zzgenword", "ZZGEN")])
        gp = _mk_glossary(s, "Plan Prod", content_type_id=prod.id, terms=[("zzprodword", "ZZPROD")])
        # UNTYPED project — the byte-identical GATE: auto-attach NOTHING even though an
        # approved General glossary matches the pair.
        untyped = content.project_create(s, "Plan Untyped", target_locales="fr-FR")
        plan_u = gm.build_glossary_plan(s, untyped, "en-US", "fr-FR")
        assert plan_u.glossary_ids == () and plan_u.include_legacy is True, plan_u
        # TYPED project — auto-attach Product + General, specific FIRST.
        typed = content.project_create(
            s, "Plan Typed", target_locales="fr-FR", content_type_id=prod.id
        )
        plan_t = gm.build_glossary_plan(s, typed, "en-US", "fr-FR")
        assert plan_t.include_legacy is True
        # gp is the SOLE glossary of this (unique) content type, so it is the single
        # content-type-specific entry and must sort FIRST (before every General one);
        # gn (General) is present too. Other tests' approved General glossaries also
        # appear in the General tier (shared DB), so assert ordering, not exact tuple.
        assert plan_t.glossary_ids[0] == gp.id, (
            "content-type-specific must sort first",
            plan_t.glossary_ids,
            gp.id,
        )
        assert gn.id in plan_t.glossary_ids[1:], (
            "General glossary present after the specific one",
            plan_t.glossary_ids,
        )
    print("  build_glossary_plan: untyped GATE (no auto-attach) + typed ordered: OK")


def test_merge_precedence_and_tiebreak():
    with Session(engine) as s:
        prod = ct.create(s, "Merge Product")
        pid = prod.id
        # conflict: specific vs general on the SAME source term
        _mk_glossary(s, "Merge Spec", content_type_id=pid, terms=[("sharedterm", "SPECWIN")])
        _mk_glossary(s, "Merge Gen", content_type_id=None, terms=[("sharedterm", "GENLOSE")])
        typed = content.project_create(s, "Merge Proj", target_locales="fr-FR", content_type_id=pid)
        plan = gm.build_glossary_plan(s, typed, "en-US", "fr-FR")
        merged = gm.match_terms_merged(
            s, typed.id, "here is sharedterm now", "en-US", "fr-FR", plan
        )
        by_src = {t["source_term"]: t["target_term"] for t in merged}
        assert by_src.get("sharedterm") == "SPECWIN", (
            "content-type-specific must beat General",
            by_src,
        )
    # tie-break: two GENERAL glossaries, lower id wins
    with Session(engine) as s:
        prod = ct.create(s, "Tie Product")
        gA = _mk_glossary(s, "Tie GenA", content_type_id=None, terms=[("tieterm", "AWIN")])
        gB = _mk_glossary(s, "Tie GenB", content_type_id=None, terms=[("tieterm", "BLOSE")])
        assert gA.id < gB.id
        typed = content.project_create(
            s, "Tie Proj", target_locales="fr-FR", content_type_id=prod.id
        )
        plan = gm.build_glossary_plan(s, typed, "en-US", "fr-FR")
        merged = gm.match_terms_merged(s, typed.id, "has tieterm here", "en-US", "fr-FR", plan)
        by_src = {t["source_term"]: t["target_term"] for t in merged}
        assert by_src.get("tieterm") == "AWIN", ("lower glossary id wins tie", by_src)
        # a General glossary DOES apply to a typed project (the bucket is preserved)
        assert "tieterm" in by_src, "General glossary must apply to a typed project"
    print("  merge precedence (specific > general) + id-asc tie-break + General-applies: OK")


def test_pin_skip_resolver():
    with Session(engine) as s:
        prod = ct.create(s, "Pin Product")
        mkt = ct.create(s, "Pin Marketing")
        gmk = _mk_glossary(s, "Pin Mktg", content_type_id=mkt.id, terms=[("m", "M")])
        gpr = _mk_glossary(s, "Pin Prod", content_type_id=prod.id, terms=[("p", "P")])
        prod_id, _mkt_id, gmk_id, gpr_id = prod.id, mkt.id, gmk.id, gpr.id

    class _Step:
        def __init__(self, gid):
            self.glossary_id = gid

    # a Marketing-pinned step on a PRODUCT project -> SKIP (returns None), note emitted
    with Session(engine) as s:
        notes = []
        res = orchestrator._resolved_glossary_id(
            s, [_Step(gmk_id)], "en-US", "fr-FR", project_content_type_id=prod_id, notes=notes
        )
        assert res is None, ("marketing pin must be skipped on a product project", res)
        assert notes and "skipped" in notes[0].lower(), notes
        # a matching (Product) pin on a Product project IS honoured
        res2 = orchestrator._resolved_glossary_id(
            s, [_Step(gpr_id)], "en-US", "fr-FR", project_content_type_id=prod_id, notes=[]
        )
        assert res2 == gpr_id, res2
        # a General/NULL pin matches ANY project (untyped or typed)
        gnull = _mk_glossary(s, "Pin Null", content_type_id=None, terms=[("n", "N")])
        res3 = orchestrator._resolved_glossary_id(
            s, [_Step(gnull.id)], "en-US", "fr-FR", project_content_type_id=prod_id, notes=[]
        )
        assert res3 == gnull.id, res3
    print("  _resolved_glossary_id: content-type pin SKIP (+note), match honoured: OK")


def _setup_three_glossaries(s, prod_id, mkt_id):
    _mk_glossary(s, "E2E Prod", content_type_id=prod_id, terms=[("zzprodword", "ZZPROD")])
    _mk_glossary(s, "E2E Gen", content_type_id=None, terms=[("zzgenword", "ZZGEN")])
    _mk_glossary(s, "E2E Mktg", content_type_id=mkt_id, terms=[("zzmktword", "ZZMKT")])


def test_autoattach_plain_path():
    with Session(engine) as s:
        prod = ct.create(s, "PP Product")
        mkt = ct.create(s, "PP Marketing")
        _setup_three_glossaries(s, prod.id, mkt.id)
        typed = content.project_create(
            s,
            "PP Typed",
            target_locales="fr-FR",
            default_provider="capture",
            content_type_id=prod.id,
        )
        untyped = content.project_create(
            s, "PP Untyped", target_locales="fr-FR", default_provider="capture"
        )
        doc_typed = _seg_doc(s, typed.id, PROBE_SRC)
        doc_untyped = _seg_doc(s, untyped.id, PROBE_SRC)
    # typed project: Product + General auto-attached, NOT Marketing
    CaptureProvider.reset()
    with Session(engine) as s:
        list(translate.pretranslate_stream(s, doc_typed, "fr-FR"))
    got = _injected_source_terms()
    assert "zzprodword" in got and "zzgenword" in got, ("typed must get prod+gen", got)
    assert "zzmktword" not in got, ("typed must NOT get marketing", got)
    # untyped project: NOTHING auto-attached (byte-identical gate)
    CaptureProvider.reset()
    with Session(engine) as s:
        list(translate.pretranslate_stream(s, doc_untyped, "fr-FR"))
    got_u = _injected_source_terms()
    assert not ({"zzprodword", "zzgenword", "zzmktword"} & got_u), (
        "untyped project must auto-attach nothing (byte-identical)",
        got_u,
    )
    print("  auto-attach PLAIN path: typed=prod+gen (not mktg); untyped=none: OK")


def test_autoattach_orchestrator_path():
    with Session(engine) as s:
        prod = ct.create(s, "OP Product")
        mkt = ct.create(s, "OP Marketing")
        _setup_three_glossaries(s, prod.id, mkt.id)
        typed = content.project_create(
            s,
            "OP Typed",
            target_locales="fr-FR",
            default_provider="capture",
            content_type_id=prod.id,
        )
        typed_id = typed.id
        # workflow with a glossary-injecting prompt; a step pinned to Marketing
        wf = workflows.create_workflow(s, "OP WF")
        step = workflows.add_step(
            s,
            wf.id,
            name="T",
            step_type="llm_agent",
            provider="capture",
            prompt_text="Terms: {glossary}\n{text}",
        )
        wf_id, step_id = wf.id, step.id
        mktg_glossary = s.exec(
            select(Glossary).where(Glossary.name == "E2E Mktg", Glossary.content_type_id == mkt.id)
        ).first()
        assert mktg_glossary is not None
        mktg_gid = mktg_glossary.id
        doc1 = _seg_doc(s, typed_id, PROBE_SRC)
    # no pin -> Product + General auto-attached, not Marketing
    CaptureProvider.reset()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_id, doc1, "fr-FR"))
    got = _injected_source_terms()
    assert "zzprodword" in got and "zzgenword" in got, ("orch typed prod+gen", got)
    assert "zzmktword" not in got, ("orch must not inject marketing", got)
    # NOW pin the Marketing glossary on the step -> it must be SKIPPED (content-type
    # mismatch), and auto-attach still injects Product + General, never Marketing.
    with Session(engine) as s:
        st = s.get(WorkflowStep, step_id)
        st.glossary_id = mktg_gid
        s.add(st)
        s.commit()
        doc2 = _seg_doc(s, typed_id, PROBE_SRC)
    CaptureProvider.reset()
    with Session(engine) as s:
        list(orchestrator.run_workflow_stream_segmented(s, wf_id, doc2, "fr-FR"))
    got2 = _injected_source_terms()
    assert "zzmktword" not in got2, ("pinned marketing must be SKIPPED on product", got2)
    assert "zzprodword" in got2 and "zzgenword" in got2, (
        "skipped pin falls through to auto-attach (prod+gen)",
        got2,
    )
    print("  auto-attach ORCHESTRATOR path: prod+gen (not mktg); pinned-mktg skipped: OK")


# --- three-surface parity + RBAC ---------------------------------------------
def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def test_cli_surface():
    from app.models import Customer

    with Session(engine) as s:
        t = ct.create(s, "CLI G Type")
        g = gm.create(s, "CLI G Glossary")
        cust = Customer(name="CLI G Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        tid, gid, custid = t.id, g.id, cust.id
    rc, out = _run_cli(["glossary", "module", "set-content-type", str(gid), str(tid)])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Glossary, gid).content_type_id == tid
    # --clear
    rc, out = _run_cli(["glossary", "module", "set-content-type", str(gid), "--clear"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Glossary, gid).content_type_id is None
    # create --content-type (customer-first: --customer now required)
    rc, out = _run_cli(
        [
            "--json",
            "glossary",
            "module",
            "create",
            "CLI G2",
            "--customer",
            str(custid),
            "--content-type",
            str(tid),
        ]
    )
    assert rc == 0, out
    g2id = _json.loads(out)["id"]
    with Session(engine) as s:
        assert s.get(Glossary, g2id).content_type_id == tid
    print("  CLI surface (module set-content-type + create --content-type): OK")


_UN = 0


def _mk_user(role):
    global _UN
    _UN += 1
    return security.create_user(f"ctg{role}{_UN}@x.com", role.title(), role, "ctg-pw-123456")


def _key(user):
    _row, raw = apikeys.create_key(user.id, "ctg-test", scopes="api docs")
    return raw


def test_web_and_api_surface_and_gate():
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        viewer = _mk_user("viewer")
        linguist = _mk_user("linguist")
        vh = {"X-Loc-Token": _key(viewer)}
        lh = {"X-Loc-Token": _key(linguist)}
        from app.models import Customer

        with Session(engine) as s:
            t = ct.create(s, "API G Type")
            g = gm.create(s, "API G Glossary")
            apicust = Customer(name="API G Cust")
            s.add(apicust)
            s.commit()
            s.refresh(apicust)
            tid, gid, apicustid = t.id, g.id, apicust.id
        # /api set-content-type: viewer 403 (can_tune_ai gate), linguist ok
        assert (
            c.post(
                f"/api/glossaries/{gid}/content-type", headers=vh, json={"content_type_id": tid}
            ).status_code
            == 403
        )
        r = c.post(f"/api/glossaries/{gid}/content-type", headers=lh, json={"content_type_id": tid})
        assert r.status_code == 200 and r.json()["content_type_id"] == tid, r.text
        # clear via api
        r = c.post(f"/api/glossaries/{gid}/content-type", headers=lh, json={"content_type_id": ""})
        assert r.status_code == 200 and r.json()["content_type_id"] is None, r.text
        # a bad id -> clean 400
        r = c.post(
            f"/api/glossaries/{gid}/content-type", headers=lh, json={"content_type_id": 987654}
        )
        assert r.status_code == 400, r.text
        # /api create with content_type_id (customer-first: customer_id now required)
        r = c.post(
            "/api/glossaries",
            headers=lh,
            json={"name": "API G Created", "content_type_id": tid, "customer_id": apicustid},
        )
        assert r.status_code == 200 and r.json()["content_type_id"] == tid, r.text
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    # web route (auth off -> can_edit): sets + does not 500
    c2 = TestClient(app)
    with Session(engine) as s:
        t = ct.create(s, "Web G Type")
        g = gm.create(s, "Web G Glossary")
        tid, gid = t.id, g.id
    r = c2.post(
        f"/glossaries/{gid}/content-type",
        data={"content_type_id": str(tid)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    with Session(engine) as s:
        assert s.get(Glossary, gid).content_type_id == tid
    print("  web + /api surface (set + create) + can_tune_ai gate + bad-id 400: OK")


def test_multi_customer_isolation():
    """Auto-attach honours customer scope: a Product glossary bound to customer B is
    NOT attached to customer A's Product project (even for the SAME content type)."""
    with Session(engine) as s:
        custA = Customer(name="Iso CustA")
        custB = Customer(name="Iso CustB")
        s.add(custA)
        s.add(custB)
        s.commit()
        s.refresh(custA)
        s.refresh(custB)
        prod = ct.create(s, "Iso Product")  # global type (attachable to both)
        # a customer-B-scoped glossary of type Product
        _mk_glossary(
            s,
            "Iso B Glossary",
            content_type_id=prod.id,
            customer_id=custB.id,
            terms=[("zzbword", "ZZB")],
        )
        # a GLOBAL glossary of type Product (should attach to A)
        _mk_glossary(
            s, "Iso Global Glossary", content_type_id=prod.id, terms=[("zzprodword", "ZZPROD")]
        )
        projA = content.project_create(
            s,
            "Iso Proj A",
            customer_id=custA.id,
            target_locales="fr-FR",
            default_provider="capture",
            content_type_id=prod.id,
        )
        plan = gm.build_glossary_plan(s, projA, "en-US", "fr-FR")
        merged = gm.match_terms_merged(
            s, projA.id, "zzbword and zzprodword here", "en-US", "fr-FR", plan
        )
        srcs = {t["source_term"] for t in merged}
        assert "zzprodword" in srcs, ("global product glossary must attach to A", srcs)
        assert "zzbword" not in srcs, (
            "customer B's glossary must NOT attach to customer A's project",
            srcs,
        )
    print("  multi-customer isolation of auto-attach (B's glossary not on A): OK")


def test_api_staff_create_customer_scoped():
    """FINDING 1 (MED parity): /api glossary create must honor a STAFF caller's
    customer_id (like web + CLI). Pre-fix the route forced the glossary GLOBAL, so a
    customer-scoped content_type_id then 400'd — a 2-surface-vs-3rd parity gap. A
    customer-BOUND caller must still be force-stamped (confinement not regressed)."""
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        staff = _mk_user("linguist")  # customer_id None -> internal staff
        sh = {"X-Loc-Token": _key(staff)}
        with Session(engine) as s:
            cust = Customer(name="F1 Cust")
            s.add(cust)
            s.commit()
            s.refresh(cust)
            custid = cust.id
            ctype = ct.create(s, "F1 CustType", customer_id=custid)  # customer-scoped
            ctid = ctype.id
        # staff create: customer-scoped glossary WITH a matching customer-scoped type
        r = c.post(
            "/api/glossaries",
            headers=sh,
            json={"name": "F1 G", "customer_id": custid, "content_type_id": ctid},
        )
        assert r.status_code == 200, ("staff customer-scoped create must succeed", r.text)
        assert r.json()["customer_id"] == custid, r.text
        assert r.json()["content_type_id"] == ctid, r.text
        # an unknown customer_id -> clean 400 (no 500)
        r = c.post("/api/glossaries", headers=sh, json={"name": "F1 bad", "customer_id": 987654})
        assert r.status_code == 400, r.text
        # a customer-BOUND caller cannot forge a DIFFERENT customer_id (force-stamped)
        bound = _mk_user("linguist")
        with Session(engine) as s:
            u = s.get(User, bound.id)
            u.customer_id = custid
            s.add(u)
            other = Customer(name="F1 Other")
            s.add(other)
            s.commit()
            s.refresh(other)
            otherid = other.id
        bh = {"X-Loc-Token": _key(bound)}
        r = c.post("/api/glossaries", headers=bh, json={"name": "F1 Bound", "customer_id": otherid})
        assert r.status_code == 200, r.text
        assert r.json()["customer_id"] == custid, (
            "bound caller force-stamped to OWN customer, not the forged one",
            r.text,
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  FINDING 1: /api staff create honors customer_id (parity) + bound confinement: OK")


def test_content_types_list_shows_glossary_count():
    """FINDING 2 (LOW, misleading): the content-types list must render the glossary
    reference count (delete is refused while a glossary references the type, so a
    hidden count showed '0 references' for an undeletable type)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with Session(engine) as s:
        t = ct.create(s, "F2 ListType")
        gm.create(s, "F2 ListGlossary", content_type_id=t.id)
    c = TestClient(app)  # auth off
    r = c.get("/content-types")
    assert r.status_code == 200, r.status_code
    assert "glossary(ies)" in r.text, "list must render the glossaries reference count"
    print("  FINDING 2: content-types list renders glossary reference count: OK")


def test_glossary_create_form_data_customer():
    """FINDING 3 (LOW, alignment): the create-form content-type dropdown carries the
    data-customer filter attributes (the phase-1 new-project-dialog pattern), so it
    offers the same attachable/OR-NULL set the edit form does."""
    from fastapi.testclient import TestClient

    from app.main import app

    with Session(engine) as s:
        cust = Customer(name="F3 Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        custid = cust.id
        ct.create(s, "F3 CustType", customer_id=custid)
    c = TestClient(app)
    # Customer-first: the create form renders under a SELECTED customer (?customer=).
    r = c.get(f"/glossaries?customer={custid}")
    assert r.status_code == 200, r.status_code
    assert "data-gl-content-type" in r.text and "data-customer=" in r.text, (
        "create form must carry the data-customer filter attributes (phase-1 pattern)"
    )
    print("  FINDING 3: glossary create form uses data-customer filter (aligned): OK")


# --- T2: language-agnostic glossary dropdown + run-time skip note ------------
def test_option_label_and_api_pin():
    """T2 scope 3: the glossary option label lists the glossary's own pairs (ASCII '>'),
    a glossary with no pairs shows "(no languages set)", and the API glossary pin offers
    + accepts an APPROVED glossary that does NOT cover the workflow's pair, while a
    draft or a foreign/absent id is still refused (the real seam, not a mocked guard)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with Session(engine) as s:
        de = _mk_glossary(s, "T2 de-only", terms=[("zzword", "WORT")], src="en-US", tgt="de-DE")
        depairs = gm.pairs_for(s, de.id)
        assert depairs and depairs[0]["target_locale"] == "de-DE"
        de_label = gm.option_label(s, de)
        nopair = gm.create(s, "T2 no-pair")
        nopair_label = gm.option_label(s, nopair)
        de_id = de.id
    assert "en-US > de-DE" in de_label and de.name in de_label, de_label
    assert "(no languages set)" in nopair_label, nopair_label

    # API pin: a workflow with NO pair (language-agnostic) can pin a glossary covering
    # only en-US->de-DE. A draft and an absent id are refused (400), not silently.
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        ling = _mk_user("linguist")
        h = {"X-Loc-Token": _key(ling)}
        with Session(engine) as s:
            wf = workflows.create_workflow(s, "T2 pin WF")
            wid = wf.id
            draft = gm.create(s, "T2 draft pin")
            gm.set_pairs(s, draft.id, [("en-US", "de-DE")])  # left as DRAFT
            draft_id = draft.id
        # non-covering (relative to any en-US source) approved glossary -> accepted
        r = c.post(
            f"/api/workflows/{wid}/steps",
            headers=h,
            json={
                "step_type": "llm_agent",
                "name": "T",
                "prompt_text": "Hi {text}",
                "glossary_id": de_id,
            },
        )
        assert r.status_code == 200, r.text
        # draft -> 400
        r = c.post(
            f"/api/workflows/{wid}/steps",
            headers=h,
            json={
                "step_type": "llm_agent",
                "name": "D",
                "prompt_text": "Hi {text}",
                "glossary_id": draft_id,
            },
        )
        assert r.status_code == 400, r.text
        # absent id -> 400
        r = c.post(
            f"/api/workflows/{wid}/steps",
            headers=h,
            json={
                "step_type": "llm_agent",
                "name": "X",
                "prompt_text": "Hi {text}",
                "glossary_id": 987654,
            },
        )
        assert r.status_code == 400, r.text
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  option_label + API pin (non-covering accepted; draft/absent refused): OK")


def _run_and_get_notes(runner):
    """Run a workflow stream, return the persisted WorkflowRun.notes (from the run_id
    carried in the run_started event)."""
    run_id = None
    for ev in runner():
        if ev.get("type") == "run_started":
            run_id = ev.get("run_id")
    from app.models import WorkflowRun

    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        return run.notes or ""


def test_noncovering_skip_note_run():
    """T2 scope 4: a run whose pinned glossary does not cover the run's languages
    records "Glossary <name> skipped: it does not cover <src> to <tgt>." on the run's
    notes (both the whole-document and per-segment paths), the run-view page shows it,
    and GET /api/runs/{id} returns it; a covering pin produces NO such note."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Workflow

    # glossary covering en-US -> de-DE (does NOT cover the en-US -> fr-FR run)
    with Session(engine) as s:
        de = _mk_glossary(s, "T2 skip de", terms=[("zzword", "WORT")], src="en-US", tgt="de-DE")
        fr = _mk_glossary(s, "T2 cover fr", terms=[("zzword", "MOT")], src="en-US", tgt="fr-FR")
        proj = content.project_create(
            s, "T2 skip proj", target_locales="fr-FR", default_provider="capture"
        )
        pid = proj.id
        de_id, fr_id = de.id, fr.id

    def _mk_wf(gid):
        with Session(engine) as s:
            wf = Workflow(name="T2 run WF", scope="global", source_locale=None, target_locale=None)
            s.add(wf)
            s.commit()
            s.refresh(wf)
            workflows.add_step(
                s,
                wf.id,
                name="T",
                step_type="llm_agent",
                provider="capture",
                prompt_text="Terms: {glossary}\n{text}",
                glossary_id=gid,
            )
            return wf.id

    expected = "Glossary T2 skip de skipped: it does not cover en-US to fr-FR."

    # non-covering pin, whole-document path
    wid = _mk_wf(de_id)
    with Session(engine) as s:
        did = _seg_doc(s, pid, "zzword here")
    notes = _run_and_get_notes(lambda: orchestrator.run_workflow_stream(_s(), wid, did, "fr-FR"))
    assert expected in notes, ("whole-doc skip note missing", notes)

    # non-covering pin, per-segment path
    with Session(engine) as s:
        did2 = _seg_doc(s, pid, "zzword here")
    notes2 = _run_and_get_notes(
        lambda: orchestrator.run_workflow_stream_segmented(_s(), wid, did2, "fr-FR")
    )
    assert expected in notes2, ("per-segment skip note missing", notes2)

    # covering pin -> NO skip note (glossary used as before)
    wid_fr = _mk_wf(fr_id)
    with Session(engine) as s:
        did3 = _seg_doc(s, pid, "zzword here")
    notes_fr = _run_and_get_notes(
        lambda: orchestrator.run_workflow_stream(_s(), wid_fr, did3, "fr-FR")
    )
    assert "skipped: it does not cover" not in notes_fr, ("covering pin must not note", notes_fr)

    # run-view page + API run detail both surface the note (auth off)
    c = TestClient(app)
    # last run on did (wid) is the whole-doc run; grab its id
    from app.models import WorkflowRun

    with Session(engine) as s:
        run = s.exec(
            select(WorkflowRun).where(WorkflowRun.document_id == did).order_by(WorkflowRun.id)
        ).all()[-1]
        rid = run.id
    rv = c.get(f"/runs/{rid}")
    assert rv.status_code == 200 and expected in rv.text, "run_view must show the note"
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c2 = TestClient(app)
        h = {"X-Loc-Token": _key(_mk_user("linguist"))}
        api = c2.get(f"/api/runs/{rid}", headers=h)
        assert api.status_code == 200, api.status_code
        assert expected in (api.json().get("notes") or ""), (
            "API run detail must return notes",
            api.json(),
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  non-covering pin skip note: whole-doc + per-segment + run_view + API: OK")


def _s():
    return Session(engine)


def test_builder_and_create_render_langagnostic():
    """T2 scope 1+2 (acceptance 1+6): the builder prints NO header pair for a paired
    ungrouped workflow; a grouped variant shows the group link + default badge but still
    no pair; the copy panel sits inside an Advanced disclosure; the create page's group
    card reads "Advanced: per-language variants (workflow group)"; no Jinja leftovers."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Workflow, WorkflowGroup

    c = TestClient(app)  # auth off
    with Session(engine) as s:
        wf = Workflow(
            name="T2 render plain", scope="global", source_locale="en-US", target_locale="de-DE"
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        plain_id = wf.id
        grp = WorkflowGroup(name="T2 render grp")
        s.add(grp)
        s.commit()
        s.refresh(grp)
        wfv = Workflow(
            name="T2 render variant",
            scope="global",
            source_locale="en-US",
            target_locale="fr-FR",
            group_id=grp.id,
            is_default=True,
        )
        s.add(wfv)
        s.commit()
        s.refresh(wfv)
        var_id = wfv.id

    def _no_jinja(text):
        assert "{{" not in text and "{'{" not in text and "{%" not in text, "Jinja leftover"

    p = c.get(f"/workflows/{plain_id}")
    assert p.status_code == 200, p.status_code
    _no_jinja(p.text)
    # the header "pair <src> → <tgt>" line is gone; no group link for an ungrouped wf
    assert "\u00b7 pair " not in p.text, "builder must not print the pair"
    assert "\u2192 de-DE" not in p.text, "the removed pair arrow line must be gone"
    assert "in group" not in p.text
    # copy panel sits inside an Advanced disclosure (form action unchanged)
    assert "Advanced: per-language variants" in p.text
    assert f'action="/workflows/{plain_id}/copy-to-pairs"' in p.text

    v = c.get(f"/workflows/{var_id}")
    assert v.status_code == 200, v.status_code
    _no_jinja(v.text)
    assert "in group" in v.text and "group default" in v.text
    assert "\u00b7 pair " not in v.text, "grouped variant must not print the pair either"

    n = c.get("/workflows/new")
    assert n.status_code == 200, n.status_code
    _no_jinja(n.text)
    assert "Advanced: per-language variants (workflow group)" in n.text
    print(
        "  builder + create render: no header pair; group link/badge kept; Advanced disclosures: OK"
    )


def test_web_builder_offers_and_pins_noncovering():
    """T2 acceptance 3 (WEB surface, not just API/CLI): the builder GET LISTS an approved
    glossary that does NOT cover the workflow's pair (offered), the web add-step form
    PINs it (not silently cleared), and a DRAFT glossary is neither offered nor pinnable
    (dropped to project-scoped). Exercises the real web route, not a mocked guard."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Workflow

    c = TestClient(app)  # auth off -> can_edit, CSRF not enforced
    with Session(engine) as s:
        wf = Workflow(
            name="T2 web pin WF", scope="global", source_locale="en-US", target_locale="de-DE"
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
        fr = _mk_glossary(s, "T2 web fr-only", terms=[("zzword", "MOT")], src="en-US", tgt="fr-FR")
        fr_id = fr.id
        draft = gm.create(s, "T2 web draft")
        gm.set_pairs(s, draft.id, [("en-US", "fr-FR")])  # left DRAFT
        draft_id = draft.id

    page = c.get(f"/workflows/{wid}").text
    # the non-covering approved glossary is OFFERED (its id option + pair-carrying label show).
    # Jinja autoescape renders the label's ASCII ">" as "&gt;" in HTML (the CLI shows ">").
    assert f'value="{fr_id}"' in page, "non-covering approved glossary must be offered"
    assert "T2 web fr-only" in page and "en-US &gt; fr-FR" in page, "label must list its pairs"
    # a DRAFT glossary is NOT offered
    assert f'value="{draft_id}"' not in page, "draft glossary must not be offered"

    # web add-step PINs the non-covering glossary (kept, not cleared to project-scoped)
    r = c.post(
        f"/workflows/{wid}/steps",
        data={
            "name": "T",
            "step_type": "llm_agent",
            "prompt_text": "Hi {text}",
            "glossary_id": fr_id,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wid)
        assert steps[-1].glossary_id == fr_id, "web add-step must pin the non-covering glossary"

    # a DRAFT id POSTed to the web form is silently dropped (project-scoped, never an error)
    r = c.post(
        f"/workflows/{wid}/steps",
        data={
            "name": "D",
            "step_type": "llm_agent",
            "prompt_text": "Hi {text}",
            "glossary_id": draft_id,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with Session(engine) as s:
        steps = orchestrator.get_steps(s, wid)
        assert steps[-1].glossary_id is None, "draft pin must be dropped to project-scoped"
    print("  web builder: non-covering approved glossary offered + pinnable; draft refused: OK")


def test_run_notes_only_pin_skips():
    """code review F6: only the pin-resolution skip notes become user-visible run notes.
    build_glossary_plan's own trace ("project-pinned N approved glossary(ies) [..]") stays
    log-only: a run on a project with a project-level glossary pin and NO step pin must
    leave WorkflowRun.notes free of it on both the whole-document and per-segment paths."""
    from app.models import Workflow
    from app.services import project_sources

    with Session(engine) as s:
        g = _mk_glossary(s, "T2 F6 proj pin", terms=[("zzword", "MOT")], src="en-US", tgt="fr-FR")
        proj = content.project_create(
            s, "T2 F6 proj", target_locales="fr-FR", default_provider="capture"
        )
        project_sources.add_glossary_pin(s, proj.id, g.id)
        pid = proj.id
        wf = Workflow(name="T2 F6 WF", scope="global", source_locale=None, target_locale=None)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        workflows.add_step(
            s,
            wf.id,
            name="T",
            step_type="llm_agent",
            provider="capture",
            prompt_text="Terms: {glossary}\n{text}",
        )
        wid = wf.id
        did = _seg_doc(s, pid, "zzword here")
        did2 = _seg_doc(s, pid, "zzword here")
    n1 = _run_and_get_notes(lambda: orchestrator.run_workflow_stream(_s(), wid, did, "fr-FR"))
    n2 = _run_and_get_notes(
        lambda: orchestrator.run_workflow_stream_segmented(_s(), wid, did2, "fr-FR")
    )
    for label, n in (("whole-doc", n1), ("per-segment", n2)):
        assert "project-pinned" not in n and "auto-attached" not in n, (label, n)
        assert "skipped" not in n, (label, n)
    print("  run notes carry only pin-skip notes (no plan trace): OK")


def test_noncovering_note_dedup_and_content_type_ascii():
    """code review F4/de-dup: the same non-covering glossary pinned on two steps yields ONE
    note (de-duplicated), and the content-type skip note is plain ASCII (no em dash) now that
    it is user-visible."""
    from app.models import Workflow
    from app.services import orchestrator_part2

    with Session(engine) as s:
        de = _mk_glossary(s, "T2 dd de", terms=[("zzword", "WORT")], src="en-US", tgt="de-DE")
        proj = content.project_create(
            s, "T2 dd proj", target_locales="fr-FR", default_provider="capture"
        )
        pid = proj.id
        wf = Workflow(name="T2 dd WF", scope="global", source_locale=None, target_locale=None)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for nm in ("A", "B"):
            workflows.add_step(
                s,
                wf.id,
                name=nm,
                step_type="llm_agent",
                provider="capture",
                prompt_text="{text}",
                glossary_id=de.id,
            )
        wid = wf.id
        did = _seg_doc(s, pid, "zzword here")
        did2 = _seg_doc(s, pid, "zzword here")
        # content-type mismatch note (ASCII only): a typed glossary on another type's project
        ct_notes: list = []
        t_a = ct.create(s, "T2 dd type A")
        t_b = ct.create(s, "T2 dd type B")
        gt = _mk_glossary(s, "T2 dd typed", content_type_id=t_a.id, terms=[("q", "Q")])

        class _St:
            glossary_id = gt.id

        orchestrator_part2._resolved_glossary_id(s, [_St()], "en-US", "fr-FR", t_b.id, ct_notes)
        assert any("does not match project content type" in n for n in ct_notes), ct_notes
        assert all(n.isascii() for n in ct_notes), ct_notes
    want = "Glossary T2 dd de skipped: it does not cover en-US to fr-FR."
    n1 = _run_and_get_notes(lambda: orchestrator.run_workflow_stream(_s(), wid, did, "fr-FR"))
    n2 = _run_and_get_notes(
        lambda: orchestrator.run_workflow_stream_segmented(_s(), wid, did2, "fr-FR")
    )
    assert n1.count(want) == 1, n1
    assert n2.count(want) == 1, n2
    print("  skip note de-duplicated (2 steps, 1 note); content-type note ASCII: OK")


def test_web_other_customer_glossary_refused():
    """code review (acceptance 3, confinement unchanged): a workflow owned by customer A
    (via its group) is NOT offered customer B's approved glossary and a hand-posted B id is
    dropped on add-step and edit-step (org confinement is covered by verify_multiorg)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Customer, Workflow, WorkflowGroup

    c = TestClient(app)
    with Session(engine) as s:
        ca, cb = Customer(name="T2 conf A"), Customer(name="T2 conf B")
        s.add(ca)
        s.add(cb)
        s.commit()
        s.refresh(ca)
        s.refresh(cb)
        grp = WorkflowGroup(name="T2 conf grp", customer_id=ca.id)
        s.add(grp)
        s.commit()
        s.refresh(grp)
        wf = Workflow(name="T2 conf WF", scope="customer", customer_id=ca.id, group_id=grp.id)
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
        ga = _mk_glossary(s, "T2 conf GA", customer_id=ca.id, src="en-US", tgt="ja-JP")
        gb = _mk_glossary(s, "T2 conf GB", customer_id=cb.id, src="en-US", tgt="ja-JP")
        ga_id, gb_id = ga.id, gb.id
    page = c.get(f"/workflows/{wid}").text
    assert f'value="{ga_id}"' in page, "own customer's non-covering glossary must be offered"
    assert f'value="{gb_id}"' not in page, "another customer's glossary must NOT be offered"
    r = c.post(
        f"/workflows/{wid}/steps",
        data={
            "name": "T",
            "step_type": "llm_agent",
            "prompt_text": "Hi {text}",
            "glossary_id": gb_id,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    with Session(engine) as s:
        step = orchestrator.get_steps(s, wid)[-1]
        assert step.glossary_id is None, "another customer's glossary must be dropped on add"
        sid = step.id
    r = c.post(
        f"/workflows/{wid}/steps/{sid}/edit",
        data={
            "name": "T",
            "step_type": "llm_agent",
            "prompt_text": "Hi {text}",
            "glossary_id": gb_id,
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text
    with Session(engine) as s:
        assert orchestrator.get_steps(s, wid)[-1].glossary_id is None, "dropped on edit too"
    print("  other customer's glossary still refused (web offer/add/edit): OK")


def main():
    init_db()
    register_provider("capture", CaptureProvider())
    test_schema()
    test_service_set_create_clear_and_validation()
    test_approved_not_reverted_on_content_type_change()
    test_delete_fails_closed_with_glossary()
    test_options_for_step_filter()
    test_content_type_allows()
    test_build_plan_gate_and_autoattach()
    test_merge_precedence_and_tiebreak()
    test_pin_skip_resolver()
    test_autoattach_plain_path()
    test_autoattach_orchestrator_path()
    test_option_label_and_api_pin()
    test_noncovering_skip_note_run()
    test_builder_and_create_render_langagnostic()
    test_web_builder_offers_and_pins_noncovering()
    test_run_notes_only_pin_skips()
    test_noncovering_note_dedup_and_content_type_ascii()
    test_web_other_customer_glossary_refused()
    test_cli_surface()
    test_web_and_api_surface_and_gate()
    test_multi_customer_isolation()
    # --- adversarial parity-review fixes (2026-07-08) ---
    test_api_staff_create_customer_scoped()
    test_content_types_list_shows_glossary_count()
    test_glossary_create_form_data_customer()
    print("CONTENT TYPE GLOSSARY OK")


if __name__ == "__main__":
    main()
