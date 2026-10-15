"""Issue #610: TM round-trip polish from the 2026-10-07 pressure test (track T8).

Each case is independent (a failing case does not stop the others, so the fail-before run on
origin/main names every item that was broken). Pinned, anchored on element ids and parsed values:

  1. A 100% TM fill reads "TM" in the editor status chip; the stored status stays "mt".
  2. A fuzzy TM suggestion gets an inline badge (#fz-<id>) in the target cell.
  3. The tm_match step's tally lands on the run (context, GET /api/runs/{id}, run page, CLI line).
  4. /api/tm and /api/tm/stats accept customer_id like customer; garbage or a conflict is a 400.

Hermetic: throwaway LOC_TMS_DATA_DIR, MockProvider. ASCII-only. Final line: "OVN POLISH 610 OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ovnpolish_")


from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
    Role,
    Segment,
    SegmentTM,
    SegStatus,
    StepType,
    TargetLocale,
    TransMode,
    User,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, orchestrator, tm
from app.services.ingest import ingest_document

init_db()
client = TestClient(app)
_SEQ = [0]
RESULTS: list[tuple[str, str]] = []


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _project(source="en-US", locales=("de-DE",), provider="mock"):
    with Session(engine) as s:
        p = Project(
            name=f"ovn-{_seq()}",
            org_id=1,
            source_locale=source,
            default_provider=provider,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in locales:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        return p.id


def _doc(pid, text="First sentence is here. Second sentence is here.", name="a.txt"):
    with Session(engine) as s:
        p = s.get(Project, pid)
        doc = ingest_document(s, p, name, text.encode(), TransMode.SEGMENTED)
        s.commit()
        s.refresh(doc)
        return doc.id


def _segs(doc_id, loc="de-DE"):
    with Session(engine) as s:
        return [
            (x.id, x.order_index, x.source_text)
            for x in s.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == loc)
                .order_by(Segment.order_index)
            ).all()
        ]


def _api():
    """Headers for /api calls (an admin key; created once)."""
    if "hdr" not in _SEQ_KEYS:
        with Session(engine) as s:
            u = User(email="ovn-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
            uid = u.id
        _SEQ_KEYS["hdr"] = {"X-Loc-Token": apikeys.create_key(uid, "ovn-api", scopes="api docs")[1]}
    return _SEQ_KEYS["hdr"]


_SEQ_KEYS: dict = {}


def case(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, "ok"))
    except Exception as exc:
        RESULTS.append((fn.__name__, f"FAIL: {type(exc).__name__}: {str(exc)[:300]}"))
    return fn


@case
def case_01_tm_fill_chip_says_tm():
    pid = _project()
    doc_id = _doc(pid)
    (sid0, _, _), (sid1, _, _) = _segs(doc_id)[:2]
    with Session(engine) as s:
        a = s.get(Segment, sid0)
        a.target_text, a.status, a.tm_match_pct, a.lock_band = (
            "Erster Satz.",
            SegStatus.MT,
            100,
            "100",
        )
        b = s.get(Segment, sid1)
        b.target_text, b.status, b.mt_confidence = "Zweiter Satz.", SegStatus.MT, 0.9
        s.add(a)
        s.add(b)
        s.commit()
    html = client.get(f"/documents/{doc_id}/editor?locale=de-DE").text
    chip_a = re.search(rf'<span class="chip st-mt" id="st-{sid0}"([^>]*)>([^<]*)</span>', html)
    chip_b = re.search(rf'<span class="chip st-mt" id="st-{sid1}"([^>]*)>([^<]*)</span>', html)
    assert chip_a and chip_a.group(2) == "TM" and "data-tm-fill" in chip_a.group(1), (
        chip_a and chip_a.group(0)
    )
    assert chip_b and chip_b.group(2) == "MT", chip_b and chip_b.group(0)
    with Session(engine) as s:
        assert s.get(Segment, sid0).status == SegStatus.MT, "the stored status must stay mt"


@case
def case_02_fuzzy_inline_badge():
    pid = _project()
    doc_id = _doc(pid, "Please save your changes now.")
    with Session(engine) as s:
        tm.write_back(
            s,
            pid,
            "Please save your changes today.",
            "Bitte speichern Sie Ihre Aenderungen heute.",
            "en-US",
            "de-DE",
        )
    ((sid, _, _),) = _segs(doc_id)[:1]
    with Session(engine) as s:
        a = s.get(Segment, sid)
        a.target_text, a.status = "Bitte speichern Sie jetzt.", SegStatus.MT
        s.add(a)
        s.commit()
    html = client.get(f"/documents/{doc_id}/editor?locale=de-DE").text
    m = re.search(
        rf'id="fz-{sid}"><span class="badge fuzzy">Fuzzy (\d+)%</span>',
        html,
    )
    assert m and 75 <= int(m.group(1)) < 100, "no inline fuzzy badge on the suggested row"


@case
def case_03_run_page_shows_tm_tally():
    pid = _project(locales=("fr-FR",))
    with Session(engine) as s:
        tm.write_back(s, pid, "Save changes.", "Enregistrer.", "en-US", "fr-FR")
        doc = Document(
            project_id=pid, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, src in enumerate(["Save changes.", "Fresh line one.", "Fresh line two."]):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="fr-FR",
                    order_index=i,
                    source_text=src,
                    target_text="",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        wf = Workflow(name=f"ovn-tm-{_seq()}")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="TM match", step_type=StepType.TM_MATCH
            )
        )
        s.add(
            WorkflowStep(workflow_id=wf.id, order_index=1, name="MT", step_type=StepType.LLM_AGENT)
        )
        s.commit()
        out = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        assert out["status"] == "completed", out
        run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc.id)).first()
        rid = run.id
        summ = (run.context or {}).get("tm_match_summary")
    assert summ == {"filled": 1, "relocked": 0, "total": 3, "unmatched": 2}, (
        summ,
        run.context,
        run.used_tm_match,
        out,
    )
    body = client.get(f"/api/runs/{rid}", headers=_api()).json()
    assert body.get("tm_match") == summ, body.get("tm_match")
    html = client.get(f"/runs/{rid}").text
    m = re.search(r'<p id="run-tm-summary"[^>]*>(.*?)</p>', html, re.S)
    assert m and "<strong>1</strong> of 3" in m.group(1), (m and m.group(1), len(html), html[:300])
    from app.services import run_overlay

    line = run_overlay.format_tm_summary_ascii(summ)
    assert "1 of 3" in line and "2 not matched" in line and line.isascii(), line
    assert run_overlay.format_tm_summary_ascii(None) == ""


@case
def case_04_api_tm_accepts_customer_id():
    with Session(engine) as s:
        c = Customer(name=f"ovn-cust-{_seq()}", org_id=1)
        s.add(c)
        s.commit()
        s.refresh(c)
        cid = c.id
        p = Project(
            name=f"ovn-tmapi-{_seq()}",
            org_id=1,
            source_locale="en-US",
            default_provider="mock",
            customer_id=cid,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.add(
            SegmentTM(
                project_id=p.id,
                source_locale="en-US",
                target_locale="de-DE",
                source_text="Hello there.",
                target_text="Hallo.",
                source_hash=tm.hash_text("Hello there.", "en-US", "de-DE"),
            )
        )
        s.commit()
        other = Customer(name=f"ovn-other-{_seq()}", org_id=1)
        s.add(other)
        s.commit()
    a = client.get(f"/api/tm?customer={cid}", headers=_api()).json()
    b = client.get(f"/api/tm?customer_id={cid}", headers=_api()).json()
    assert a.get("ok") and not a.get("needs_customer"), a
    assert (
        b.get("ok")
        and not b.get("needs_customer")
        and b.get("total") == a.get("total")
        and b["total"] >= 1
    ), b
    assert (
        client.get(f"/api/tm?customer_id={cid}&customer={cid}", headers=_api()).json().get("total")
        == a["total"]
    )
    bad = client.get("/api/tm?customer_id=abc", headers=_api())
    assert bad.status_code == 400 and bad.json().get("ok") is False, (
        bad.status_code,
        bad.text[:200],
    )
    conflict = client.get(f"/api/tm?customer_id={cid}&customer={cid + 1}", headers=_api())
    assert conflict.status_code == 400, conflict.status_code
    st = client.get(f"/api/tm/stats?customer_id={cid}", headers=_api()).json()
    assert st.get("ok") and not st.get("needs_customer"), st


def _cust_with_tm(label, n_entries):
    """A customer with one project holding `n_entries` TM rows; returns its id."""
    with Session(engine) as s:
        c = Customer(name=f"ovn-{label}-{_seq()}", org_id=1)
        s.add(c)
        s.commit()
        s.refresh(c)
        p = Project(
            name=f"ovn-{label}-p-{_seq()}",
            org_id=1,
            source_locale="en-US",
            default_provider="mock",
            customer_id=c.id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        for i in range(n_entries):
            src = f"{label} sentence number {i}."
            s.add(
                SegmentTM(
                    project_id=p.id,
                    source_locale="en-US",
                    target_locale="de-DE",
                    source_text=src,
                    target_text=f"{label} Satz {i}.",
                    source_hash=tm.hash_text(src, "en-US", "de-DE"),
                )
            )
        s.commit()
        return c.id


def _bound_asserts(hdr, own, foreign):
    mine = client.get("/api/tm", headers=hdr).json()
    assert mine.get("ok") and mine["total"] == 2, mine
    for q in (f"customer_id={foreign}", f"customer={foreign}"):
        got = client.get(f"/api/tm?{q}", headers=hdr).json()
        assert got.get("ok") and got["total"] == 2, (q, got)
        assert not any("foreign" in (e.get("source_text") or "") for e in got.get("entries", [])), q
        stats = client.get(f"/api/tm/stats?{q}", headers=hdr).json()
        assert stats.get("ok"), (q, stats)
    own_q = client.get(f"/api/tm?customer_id={own}", headers=hdr).json()
    assert own_q["total"] == 2, own_q


@case
def case_05_customer_id_cannot_widen_a_bound_key():
    """Review: a customer-BOUND key that sends another customer's id as customer_id still sees
    only its own customer's memory (the alias goes through the same visible-set resolver)."""
    own = _cust_with_tm("own", 2)
    foreign = _cust_with_tm("foreign", 5)
    from app.services import security, settings

    u = security.create_user(
        f"ovn-bound-{_seq()}@x.io", "Bound", Role.LINGUIST, "ovn-password-123", customer_id=own
    )
    hdr = {"X-Loc-Token": apikeys.create_key(u.id, "ovn-bound", scopes="api docs")[1]}
    # Customer binding is inert until the login wall is on (deps._ident_customer_id).
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        _bound_asserts(hdr, own, foreign)
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()


@case
def case_06_tally_counts_heal_and_protected_rows():
    """Gate finding: a HEALED row (drifted MT re-filled from a 100% TM hit) is a genuine TM fill,
    and a content-PROTECTED row (needs_review that already byte-equals its 100% hit) already
    held the exact TM text and is kept. Neither is "not matched and sent on to the next step"."""
    from app.services import settings

    settings.set_many({"tm_hundreds_enter_lqa": "1"})
    pid = _project(locales=("fr-FR",))
    with Session(engine) as s:
        tm.write_back(s, pid, "Heal this line.", "Ligne soignee.", "en-US", "fr-FR")
        tm.write_back(s, pid, "Keep this line.", "Ligne gardee.", "en-US", "fr-FR")
        doc = Document(
            project_id=pid, filename="h.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        rows = [
            ("Heal this line.", "Une autre traduction.", SegStatus.MT, 100),
            ("Keep this line.", "Ligne gardee.", SegStatus.NEEDS_REVIEW, 100),
            ("Fresh line three.", "", SegStatus.NEW, None),
        ]
        for i, (src, tgt, st, pct) in enumerate(rows):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale="fr-FR",
                    order_index=i,
                    source_text=src,
                    target_text=tgt,
                    status=st,
                    tm_match_pct=pct,
                )
            )
        s.commit()
        wf = Workflow(name=f"ovn-tm-heal-{_seq()}")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=0, name="TM match", step_type=StepType.TM_MATCH
            )
        )
        s.add(
            WorkflowStep(
                workflow_id=wf.id, order_index=1, name="Review", step_type=StepType.HUMAN_REVIEW
            )
        )
        s.commit()
        out = orchestrator.run_workflow(s, wf.id, doc.id, "fr-FR")
        tm_ev = out["steps"][0]["tm_match"]
        assert tm_ev.get("tm_healed") == 1 and tm_ev.get("content_protected") == 1, tm_ev
        run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc.id)).first()
        summ = (run.context or {}).get("tm_match_summary")
    assert summ == {"filled": 1, "relocked": 1, "total": 3, "unmatched": 1}, (summ, tm_ev)


failed = [(n, r) for n, r in RESULTS if r != "ok"]
for n, r in RESULTS:
    print(f"  {n:<48} " + r.encode("ascii", "backslashreplace").decode("ascii"))
if failed:
    print(f"OVN POLISH 610 FAILED: {len(failed)} of {len(RESULTS)} cases")
    sys.exit(1)
print("OVN POLISH 610 OK")
