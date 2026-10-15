"""Issue #605: small UI findings from the 2026-10-07 pressure test (track T5).

Each case is independent (a failing case does not stop the others, so the fail-before run on
origin/main names every item that was broken). Pinned, anchored on element ids and parsed
values, never on copy:

  1. /audit table sits in a scroll region (.data-table-region), not bare in the page.
  2. Timestamps carry a UTC label (utcdt filter; project page).
  3. Editor intro says "pre-translated" only when some target exists
     (#editor-intro[data-untranslated]).
  4. TMX import redirect names the withheld rows; the project page shows #tm-withheld-rows.
  5. Run page output hides the internal segment joiner (run_text filter and the rendered page).
  6. Document page preselects the workflow matching the locale; a workflow for another
     language is never preselected (preferred_workflow + the rendered <select>).
  7. No em dash (U+2014 or &mdash;) in a template, static script or English UI string.

Hermetic: throwaway LOC_TMS_DATA_DIR, MockProvider. ASCII-only. Final line: "OVN POLISH 605 OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ovnpolish_")

import pathlib

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Project,
    Role,
    Segment,
    SegStatus,
    StepType,
    TargetLocale,
    TransMode,
    User,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, orchestrator
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
def case_01_audit_table_scrolls():
    r = client.get("/audit")
    assert r.status_code == 200, r.status_code
    # Make sure there is at least one row so the table renders.
    if "data-table-region" not in r.text:
        client.post("/projects", data={"name": f"audit-seed-{_seq()}", "source_locale": "en-US"})
        r = client.get("/audit")
    assert 'class="data-table-region' in r.text, "audit table is not inside a scroll region"
    assert re.search(
        r'<div class="data-table-region[^"]*" role="region" aria-label="Audit log"', r.text
    )


@case
def case_02_utc_labels():
    pid = _project()
    doc_id = _doc(pid)
    html = client.get(f"/projects/{pid}").text
    created = re.search(r'<span class="nowrap">(\d{4}-\d{2}-\d{2})([^<]*)</span>', html)
    assert created and created.group(2).strip() == "UTC", created and created.group(0)
    uploaded = re.search(
        r'<td class="muted data-mono">(\d{4}-\d{2}-\d{2} \d{2}:\d{2})([^<]*)</td>', html
    )
    assert uploaded and uploaded.group(2).strip() == "UTC", uploaded and uploaded.group(0)
    assert doc_id


@case
def case_02b_utc_filter():
    from datetime import date, datetime

    from app.web.deps import short_dt, utc_dt

    assert utc_dt(datetime(2026, 10, 8, 4, 28, 9)) == "2026-10-08 04:28 UTC"
    assert utc_dt(date(2026, 10, 8)) == "2026-10-08 UTC"
    assert utc_dt(None) == "" and utc_dt("") == ""
    assert utc_dt("not-a-date") == "not-a-date"
    # short_dt itself is unchanged (verify_queries_surfaces pins it).
    assert short_dt(datetime(2026, 10, 8, 4, 28, 9)) == "2026-10-08 04:28"


@case
def case_03_editor_intro_only_when_translated():
    pid = _project()
    doc_id = _doc(pid)
    html = client.get(f"/documents/{doc_id}/editor?locale=de-DE").text
    assert "pre-translated" not in html, "an empty locale must not claim to be pre-translated"
    m = re.search(r'<p class="help" id="editor-intro"([^>]*)>', html)
    assert m and "data-untranslated" in m.group(1), "editor intro not marked untranslated"
    sid = _segs(doc_id)[0][0]
    with Session(engine) as s:
        sg = s.get(Segment, sid)
        sg.target_text = "Erster Satz."
        sg.status = SegStatus.MT
        s.add(sg)
        s.commit()
    html = client.get(f"/documents/{doc_id}/editor?locale=de-DE").text
    m = re.search(r'<p class="help" id="editor-intro"([^>]*)>([^<]*)</p>', html)
    assert m and "data-untranslated" not in m.group(1) and "pre-translated" in m.group(2), m


def _tu(src, tgt):
    return (
        f'<tu><tuv xml:lang="en-US"><seg>{src}</seg></tuv>'
        f'<tuv xml:lang="de-DE"><seg>{tgt}</seg></tuv></tu>'
    )


_TMX = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<tmx version="1.4"><header srclang="en-US" '
    'datatype="plaintext" segtype="sentence" adminlang="en" o-tmf="x" creationtool="t" '
    'creationtoolversion="1"/><body>\n'
    + _tu("Good morning, team.", "Guten Morgen, Team.")
    + _tu("You have {n} new messages.", "Sie haben neue Nachrichten.")
    + _tu("Open {name} now.", "Jetzt oeffnen.")
    + "</body></tmx>\n"
)


@case
def case_04_tmx_import_names_withheld_rows():
    pid = _project()
    r = client.post(
        f"/projects/{pid}/tm/import",
        data={"source_locale": "en-US", "target_locale": "de-DE"},
        files={"file": ("mem.tmx", _TMX.encode("utf-8"), "application/xml")},
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    assert r.status_code == 303 and "tm_imported=1" in loc and "tm_skipped_integrity=2" in loc, loc
    assert "tm_withheld_rows=2,3" in loc, loc
    # Only row numbers travel in the URL, never the TM text.
    assert "messages" not in loc and "Open" not in loc, loc
    html = client.get(loc).text
    m = re.search(r'<span id="tm-withheld-rows">([^<]*)</span>', html)
    assert m, "the project page does not name the withheld units"
    assert "2, 3" in m.group(1), m.group(1)
    # A hostile query value never renders as markup.
    evil = client.get(
        f"/projects/{pid}?tm_imported=0&tm_skipped_integrity=1&tm_withheld_rows=<b>x</b>,7"
    ).text
    assert "<b>x</b>" not in evil


@case
def case_05_run_output_hides_joiner():
    from app.services.orchestrator_terminology_backen import SEG_JOINER

    pid = _project()
    with Session(engine) as s:
        p = s.get(Project, pid)
        p.default_provider = "mock"
        s.add(p)
        s.commit()
    doc_id = _doc(pid, "Alpha line here.\n\nBeta line here.\n\nGamma line here.")
    with Session(engine) as s:
        wf = Workflow(name=f"ovn-mt-{_seq()}")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        s.add(
            WorkflowStep(workflow_id=wf.id, order_index=0, name="MT", step_type=StepType.LLM_AGENT)
        )
        s.commit()
        out = orchestrator.run_workflow(s, wf.id, doc_id, "de-DE")
        assert out["status"] == "completed", out
        run = s.exec(select(WorkflowRun).where(WorkflowRun.document_id == doc_id)).first()
        rid = run.id
    html = client.get(f"/runs/{rid}").text
    assert "Alpha line here." in html and "Gamma line here." in html
    assert chr(0x27E6) not in html.split('id="stepLog"', 1)[1].split("</section>", 1)[0], (
        "the internal segment joiner is shown in the step output"
    )
    assert SEG_JOINER


@case
def case_05b_run_text_filter():
    from app.services.orchestrator_terminology_backen import SEG_JOINER
    from app.web.deps import run_text

    assert run_text("a" + SEG_JOINER + "b") == "a\n\nb"
    assert run_text(None) == ""


@case
def case_06_workflow_picker_matches_locale():
    pid = _project()
    doc_id = _doc(pid)
    with Session(engine) as s:
        wes = Workflow(name="AAA EN to ES419", target_locale="es-419", source_locale="en-US")
        wde = Workflow(name="ZZZ EN to DE", target_locale="de-DE", source_locale="en-US")
        s.add(wes)
        s.add(wde)
        s.commit()
        s.refresh(wes)
        s.refresh(wde)
        ids = (wes.id, wde.id)
    html = client.get(f"/documents/{doc_id}?locale=de-DE").text
    sel = re.search(r'<select name="workflow_id"[^>]*>(.*?)</select>', html, re.S)
    assert sel, "run picker not found"
    chosen = re.findall(r'<option value="(\d+)" selected>', sel.group(1))
    assert chosen and int(chosen[0]) == ids[1], (chosen, ids)
    assert "required" not in sel.group(0).split(">", 1)[0]


@case
def case_06b_preferred_workflow_rules():
    from app.web.queries import preferred_workflow

    class W:
        def __init__(self, id, t=None, s=None):
            self.id, self.target_locale, self.source_locale = id, t, s

    es, de, neutral = W(1, "es-419", "en-US"), W(2, "de-DE", "en-US"), W(3)
    assert preferred_workflow([es, de, neutral], "en-US", "de-DE").id == 2
    assert preferred_workflow([es, neutral], "en-US", "de-DE").id == 3
    assert preferred_workflow([es], "en-US", "de-DE") is None, (
        "another language is never preselected"
    )
    assert preferred_workflow([W(4, "de-DE", "fr-FR"), de], "en-US", "de-DE").id == 2
    assert preferred_workflow([], "en-US", "de-DE") is None


@case
def case_07_no_em_dash_in_ui_copy():
    EM = chr(0x2014)
    root = pathlib.Path(_ROOT) / "app"
    bad = []
    for sub, pats in (("templates", ("*.html",)), ("static", ("*.js",))):
        for pat in pats:
            for f in sorted((root / sub).rglob(pat)):
                t = f.read_bytes().decode("utf-8", "replace")
                n = (
                    t.count(EM)
                    + t.lower().count("&mdash;")
                    + t.count("&#8212;")
                    + t.count("&#x2014;")
                )
                if n:
                    bad.append(f"{f.name}:{n}")
    en = (root / "i18n" / "en.json").read_bytes().decode("utf-8")
    if EM in en:
        bad.append(f"en.json:{en.count(EM)}")
    assert not bad, "em dashes remain: " + ", ".join(bad[:12])


failed = [(n, r) for n, r in RESULTS if r != "ok"]
for n, r in RESULTS:
    print(f"  {n:<48} " + r.encode("ascii", "backslashreplace").decode("ascii"))
if failed:
    print(f"OVN POLISH 605 FAILED: {len(failed)} of {len(RESULTS)} cases")
    sys.exit(1)
print("OVN POLISH 605 OK")
