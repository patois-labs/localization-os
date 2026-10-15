"""Issue #617 item 6: approving one copy of a repeated source propagates to the other copies.

The maintainer's decision 2026-10-08 (industry-standard CAT behaviour, Trados / memoQ): when a
linguist
approves a segment, every OTHER segment of the same document with the same source (the
normalization the INCONSISTENT check uses) and target locale that is not yet approved, not
edited by a human, not locked and not unlinked receives the same target and becomes
"propagated" (EDITED + meta.propagated_from, never auto-approved).

Policy under test (one seam, segment_review.approve_and_write_tm, shared by the web route,
POST /api/segments/{id}/approve, `doc approve-segment` and the MCP `approve_segment` tool):
  * propagation: the open repeats receive the target, listed by id in the reply;
  * confirmed untouched: an APPROVED repeat, a human-EDITED repeat and an unrelated source
    keep their text; so does the same source in ANOTHER document (same-document scope);
  * locked untouched: a hard lock and a soft lock both protect a repeat;
  * opt-out: an unlinked segment neither receives nor sends, persisted, on web/API/CLI;
  * integrity: a copy whose own source the target would break (placeholder, inline tag) is
    REFUSED and reported, never written; an acknowledged placeholder loss carries over;
  * audit: ONE segment.propagate row lists the propagated ids;
  * undo: restores the copies still exactly as propagated and keeps the ones a human touched;
  * editor: a "Propagated" badge, the unlink control, INCONSISTENT clears, a human edit or
    approve of a copy drops the badge.

Hermetic: throwaway LOC_TMS_DATA_DIR + MockProvider. ASCII-only.
Final line: "REPEAT PROPAGATION 617 OK".
"""

import io
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_repprop617_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Document,
    Project,
    Role,
    Segment,
    SegStatus,
    TargetLocale,
    User,
)
from app.services import apikeys, segment_review, settings

init_db()
_SRC, _TGT = "en-US", "es-MX"
_S = "Open the settings menu"
_T = "Abra el menu de configuracion"

client = TestClient(app)


def _fail(msg):
    print("FAIL:", ascii(msg))
    sys.exit(1)


def _project(name):
    with Session(engine) as s:
        p = Project(name=name, source_locale=_SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=_TGT))
        s.commit()
        return p.id


def _doc(pid, rows, name="d.txt"):
    """rows: (source, target, status[, extra Segment kwargs]) -> (doc_id, [segment ids])."""
    with Session(engine) as s:
        d = Document(project_id=pid, filename=name, fmt="txt", mode="segmented", status="parsed")
        s.add(d)
        s.commit()
        s.refresh(d)
        ids = []
        for i, row in enumerate(rows):
            src, tgt, status = row[0], row[1], row[2]
            extra = row[3] if len(row) > 3 else {}
            seg = Segment(
                document_id=d.id,
                target_locale=_TGT,
                order_index=i,
                source_text=src,
                target_text=tgt,
                status=status,
                **extra,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            ids.append(seg.id)
        return d.id, ids


def _seg(sid):
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        s.expunge(seg)
        return seg


def _audit(action, sid):
    with Session(engine) as s:
        return [
            r
            for r in s.exec(select(AuditLog).where(AuditLog.action == action)).all()
            if f"segment {sid} " in (r.target or "")
        ]


def _key(scopes="api docs"):
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "rp617-admin@x.io")).first()
        if not u:
            u = User(email="rp617-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "rp617", scopes=scopes)
    return raw


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    combined = out.getvalue() + err.getvalue()
    if not combined.isascii():
        _fail(("CLI output must be ASCII", combined))
    return rc, combined


def _web_approve(sid, text=None, **form):
    """The editor flow: save the textarea, then approve."""
    if text is not None:
        r = client.post(
            f"/segments/{sid}/save", data={"target_text": text, "tte_ms": "5", "confirm": "1"}
        )
        if r.status_code != 200 or r.json().get("ok") is not True:
            _fail(f"save must succeed: {r.status_code} {r.text}")
    r = client.post(f"/segments/{sid}/approve", data=form)
    if r.status_code != 200:
        _fail(f"approve must 200: {r.status_code} {r.text}")
    return r.json()


def test_propagation_and_untouched():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    pid = _project("rp-main")
    mt = SegStatus.MT
    doc_id, ids = _doc(
        pid,
        [
            (_S, "Abrir el menu (MT)", mt),  # 0 the one the linguist approves
            (_S, "Abra el menu de ajustes (MT)", mt),  # 1 open repeat
            ("  open the  SETTINGS menu ", "Abre el menu", mt),  # 2 open, case/space variant
            (_S, "", SegStatus.NEW),  # 3 untranslated repeat
            (_S, "Texto aprobado antes", SegStatus.APPROVED),  # 4 confirmed
            (_S, "Texto escrito a mano", SegStatus.EDITED),  # 5 human edit
            (_S, "Bloqueo duro", mt, {"lock_band": "100", "lock_hard": True}),  # 6 hard lock
            (_S, "Bloqueo suave", mt, {"lock_band": "ice", "lock_hard": False}),  # 7 soft lock
            (_S, "Contexto propio", mt, {"meta": {"no_propagate": True}}),  # 8 unlinked
            ("A different sentence", "Otra frase", mt),  # 9 other source
        ],
    )
    _other_doc, oids = _doc(pid, [(_S, "Otro documento", mt)], name="other.txt")
    before_inconsistent = client.get(f"/documents/{doc_id}/editor?locale={_TGT}").text
    if "inconsistent</span>" not in before_inconsistent:
        _fail("precondition: the grid must show the INCONSISTENT count before propagation")

    b = _web_approve(ids[0], text=_T)
    if b.get("status") != SegStatus.APPROVED:
        _fail(f"the approved segment is approved: {b}")
    if b.get("propagated") != [ids[1], ids[2], ids[3]]:
        _fail(f"propagated ids must be the three open repeats, in order: {b}")
    if b.get("propagation_refused") != []:
        _fail(f"nothing should be refused here: {b}")
    for i in (1, 2, 3):
        c = _seg(ids[i])
        if c.target_text != _T:
            _fail(f"repeat {i} must carry the approved target, got {c.target_text!r}")
        if c.status != SegStatus.EDITED or c.status == SegStatus.APPROVED:
            _fail(f"a propagated copy is EDITED, never auto-approved: {c.status}")
        if (c.meta or {}).get("propagated_from") != ids[0]:
            _fail(f"a propagated copy records its source: {c.meta}")
    # untouched
    expect = {
        4: ("Texto aprobado antes", SegStatus.APPROVED),
        5: ("Texto escrito a mano", SegStatus.EDITED),
        6: ("Bloqueo duro", SegStatus.MT),
        7: ("Bloqueo suave", SegStatus.MT),
        8: ("Contexto propio", SegStatus.MT),
        9: ("Otra frase", SegStatus.MT),
    }
    for i, (txt, st) in expect.items():
        c = _seg(ids[i])
        if c.target_text != txt or c.status != st:
            _fail(f"segment {i} must be untouched: {c.target_text!r} {c.status}")
    c = _seg(oids[0])
    if c.target_text != "Otro documento" or c.status != SegStatus.MT:
        _fail("the same source in ANOTHER document must be untouched (document scope)")
    # one audit row listing the ids
    rows = _audit("segment.propagate", ids[0])
    if len(rows) != 1:
        _fail(f"exactly one segment.propagate audit row expected, got {len(rows)}")
    want = "propagated=" + ",".join(str(i) for i in (ids[1], ids[2], ids[3]))
    if want not in rows[0].detail:
        _fail(f"the audit row must list the propagated ids: {rows[0].detail}")
    # the grid: Propagated badge, unlink control, INCONSISTENT gone for this group
    page = client.get(f"/documents/{doc_id}/editor?locale={_TGT}").text
    cell = re.search(r'id="qa-%d">(.*?)</td>' % ids[1], page, re.S)  # noqa: UP031
    if not cell or "Propagated" not in cell.group(1):
        _fail("the editor must badge a propagated copy")
    if 'data-action="propagation" data-id="%d"' % ids[1] not in page:  # noqa: UP031
        _fail("the editor must offer the unlink control on a repeated segment")
    if 'data-action="propagation" data-id="%d"' % ids[9] in page:  # noqa: UP031
        _fail("a segment with no repeats gets no unlink control")
    print("  1. propagation: open repeats updated (ids, EDITED, marker), audited once OK")
    print(
        "  2. approved, human-edited, hard+soft locked, unlinked, other source and doc untouched OK"
    )
    print("  3. editor: Propagated badge + unlink control, INCONSISTENT group resolved OK")
    return doc_id, ids


def test_marker_cleared_by_human_action():
    pid = _project("rp-marker")
    _doc_id, ids = _doc(
        pid, [(_S, "", SegStatus.NEW), (_S, "x", SegStatus.MT), (_S, "y", SegStatus.MT)]
    )
    b = _web_approve(ids[0], text=_T)
    if b["propagated"] != [ids[1], ids[2]]:
        _fail(f"setup: {b}")
    # a human edit of a propagated copy: marker gone, text protected from the next propagation
    r = client.post(f"/segments/{ids[1]}/save", data={"target_text": "Mi version", "tte_ms": "1"})
    if r.status_code != 200:
        _fail(r.text)
    c = _seg(ids[1])
    if (c.meta or {}).get("propagated_from") is not None or c.status != SegStatus.EDITED:
        _fail(f"a human edit must clear the propagated marker: {c.meta} {c.status}")
    # approve the third copy as is: marker gone, approved
    b = _web_approve(ids[2])
    c = _seg(ids[2])
    if (c.meta or {}).get("propagated_from") is not None or c.status != SegStatus.APPROVED:
        _fail(f"approving a propagated copy clears the marker: {c.meta} {c.status}")
    # the human-edited copy is now protected; the approved one is untouched
    _web_approve(ids[0], text="Version final")
    if _seg(ids[1]).target_text != "Mi version" or _seg(ids[2]).target_text != _T:
        _fail("a later propagation must not overwrite a human edit or an approved copy")
    # but an EARLIER propagation is overwritten by a newer approval (it was never human text)
    pid2 = _project("rp-marker2")
    _d, j = _doc(pid2, [(_S, "", SegStatus.NEW), (_S, "x", SegStatus.MT), (_S, "y", SegStatus.MT)])
    _web_approve(j[0], text="Primera")
    b = _web_approve(j[0], text="Segunda")
    if _seg(j[1]).target_text != "Segunda" or b["propagated"] != [j[1], j[2]]:
        _fail(f"a newer approval replaces an earlier propagation: {b}")
    print(
        "  4. a human edit or approve of a copy drops the marker; a newer approval replaces it OK"
    )


def test_unlink_and_optout():
    pid = _project("rp-unlink")
    _doc_id, ids = _doc(
        pid, [(_S, "", SegStatus.NEW), (_S, "a", SegStatus.MT), (_S, "b", SegStatus.MT)]
    )
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        h = {"X-Loc-Token": _key()}
        # unlink the source: it keeps its own approval, copies are not touched
        r = client.post(f"/api/segments/{ids[0]}/propagation", headers=h, json={"linked": False})
        if r.status_code != 200 or r.json().get("changed") is not True:
            _fail(f"unlink via /api: {r.status_code} {r.text}")
        if (_seg(ids[0]).meta or {}).get("no_propagate") is not True:
            _fail("the unlink must be persisted on the segment")
        with Session(engine) as s:
            seg = s.get(Segment, ids[0])
            seg.target_text = _T
            s.add(seg)
            s.commit()
        r = client.post(f"/api/segments/{ids[0]}/approve", headers=h)
        b = r.json()
        if (
            r.status_code != 200
            or b.get("propagated") != []
            or "unlinked" not in b.get("propagation_skipped", "")
        ):
            _fail(f"an unlinked segment must not propagate its approval: {b}")
        if _seg(ids[1]).target_text != "a":
            _fail("an unlinked source must leave its repeats alone")
        # relink, then bad bodies
        r = client.post(f"/api/segments/{ids[0]}/propagation", headers=h, json={"linked": True})
        if r.json().get("changed") is not True or (_seg(ids[0]).meta or {}).get("no_propagate"):
            _fail(f"relink must clear the opt-out: {r.text}")
        r = client.post(f"/api/segments/{ids[0]}/propagation", headers=h, json={"linked": "no"})
        if r.status_code != 400:
            _fail(f"non-boolean linked is a 400: {r.status_code}")
        r = client.post(f"/api/segments/{ids[0]}/propagation", headers=h, json={})
        if r.status_code != 400:
            _fail(f"an empty body is a 400: {r.status_code}")
        r = client.post("/api/segments/999999/propagation", headers=h, json={"linked": False})
        if r.status_code != 404:
            _fail(f"unknown segment is a 404: {r.status_code}")
        # propagate=false per call, and a non-boolean is rejected
        r = client.post(f"/api/segments/{ids[0]}/approve", headers=h, json={"propagate": "no"})
        if r.status_code != 400:
            _fail(f"non-boolean propagate is a 400: {r.status_code} {r.text}")
        r = client.post(f"/api/segments/{ids[0]}/approve", headers=h, json={"propagate": False})
        b = r.json()
        if b.get("propagated") != [] or _seg(ids[1]).target_text != "a":
            _fail(f"propagate=false approves only this segment: {b}")
        r = client.post(f"/api/segments/{ids[0]}/approve", headers=h, json={})
        b = r.json()
        if b.get("propagated") != [ids[1], ids[2]]:
            _fail(f"default /api approve propagates and lists the ids: {b}")
        # a read-only key cannot change propagation state
        ro = {"X-Loc-Token": _key(scopes="api")}
        r = client.post(f"/api/segments/{ids[1]}/propagation", headers=ro, json={"linked": False})
        if r.status_code not in (200, 401, 403):
            _fail(f"unexpected status for a limited key: {r.status_code}")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    # web twin: unlink a COPY, then approve another copy -> the unlinked one is skipped
    pid = _project("rp-unlink-web")
    _d, w = _doc(
        pid, [(_S, "", SegStatus.NEW), (_S, "keep me", SegStatus.MT), (_S, "x", SegStatus.MT)]
    )
    r = client.post(f"/segments/{w[1]}/propagation", data={"action": "unlink"})
    if r.status_code != 200 or r.json().get("linked") is not False:
        _fail(f"web unlink: {r.status_code} {r.text}")
    r = client.post(f"/segments/{w[1]}/propagation", data={"action": "bogus"})
    if r.status_code != 400:
        _fail(f"web: unknown action is a 400: {r.status_code}")
    b = _web_approve(w[0], text=_T)
    if b["propagated"] != [w[2]] or _seg(w[1]).target_text != "keep me":
        _fail(f"an unlinked copy keeps its context-specific rendering: {b}")
    if not _audit("segment.propagation_unlink", w[1]):
        _fail("the unlink must be audited")
    print("  5. opt-out: unlink/link persisted + audited (web, /api), propagate=false, 400s OK")


def test_integrity_refusal():
    pid = _project("rp-integrity")
    src = "You have {count} items"
    _doc_id, ids = _doc(
        pid,
        [
            (src, "", SegStatus.NEW),
            (src, "x", SegStatus.MT),
            ("YOU HAVE {COUNT} ITEMS", "y", SegStatus.MT),  # case variant: its own placeholder
        ],
    )
    # the approver drops {count}: every copy would break its own placeholder -> refused
    b = _web_approve(ids[0], text="Tienes articulos")
    refused = {r["segment_id"]: r for r in b.get("propagation_refused", [])}
    if b["propagated"] != [] or set(refused) != {ids[1], ids[2]}:
        _fail(f"a propagation that would break a placeholder is refused and reported: {b}")
    if any(r["code"] != "placeholder_integrity" for r in refused.values()):
        _fail(f"refusals carry the integrity code: {b}")
    if _seg(ids[1]).target_text != "x" or _seg(ids[2]).target_text != "y":
        _fail("a refused copy must be left exactly as it was")
    rows = _audit("segment.propagate", ids[0])
    if len(rows) != 1 or "refused=" not in rows[0].detail:
        _fail(f"the refusal must be in the (single) audit row: {[r.detail for r in rows]}")
    # an acknowledged placeholder loss carries over to the copies
    b = _web_approve(ids[0], acknowledge_placeholder_loss="1")
    if sorted(b["propagated"]) != sorted([ids[1], ids[2]]):
        _fail(f"an acknowledged placeholder loss propagates: {b}")
    # a clean target propagates; tags: a dropped inline tag is refused
    tok = lambda n: "⟦" + n + "⟧"  # noqa: E731
    tsrc = "Click " + tok("g1") + "here" + tok("/g1")
    _d2, t = _doc(pid, [(tsrc, "", SegStatus.NEW), (tsrc, "z", SegStatus.MT)])
    b = _web_approve(t[0], text="Haga clic aqui")
    if b["propagated"] != [] or [r["code"] for r in b["propagation_refused"]] != ["tag_integrity"]:
        _fail(f"a dropped inline tag is refused with tag_integrity: {b}")
    if _seg(t[1]).target_text != "z":
        _fail("tag-refused copy untouched")
    ok = "Haga clic " + tok("g1") + "aqui" + tok("/g1")
    b = _web_approve(t[0], text=ok)
    if b["propagated"] != [t[1]] or _seg(t[1]).target_text != ok:
        _fail(f"intact inline tags propagate: {b}")
    print(
        "  6. integrity: placeholder + tag breaks refused and reported, acknowledge carries over OK"
    )


def test_undo():
    pid = _project("rp-undo")
    _doc_id, ids = _doc(
        pid,
        [
            (_S, "", SegStatus.NEW),
            (_S, "uno", SegStatus.MT),
            (_S, "dos", SegStatus.NEW),
            (_S, "tres", SegStatus.MT),
        ],
    )
    b = _web_approve(ids[0], text=_T)
    if b["propagated"] != ids[1:]:
        _fail(f"setup: {b}")
    # a human edits the third copy after the propagation
    client.post(f"/segments/{ids[3]}/save", data={"target_text": "Mia", "tte_ms": "1"})
    r = client.post(f"/segments/{ids[0]}/propagation", data={"action": "undo"})
    res = r.json()
    if (
        r.status_code != 200
        or res.get("restored") != [ids[1], ids[2]]
        or res.get("kept") != [ids[3]]
    ):
        _fail(f"undo restores the untouched copies and keeps the edited one: {res}")
    c1, c2, c3 = _seg(ids[1]), _seg(ids[2]), _seg(ids[3])
    if (c1.target_text, c1.status) != ("uno", SegStatus.MT) or (c2.target_text, c2.status) != (
        "dos",
        SegStatus.NEW,
    ):
        _fail(f"undo must restore text and status: {c1.target_text!r} {c1.status} / {c2.status}")
    if (c1.meta or {}).get("propagated_from") is not None:
        _fail("undo clears the marker")
    if c3.target_text != "Mia":
        _fail("undo never touches a copy a human edited since")
    if _seg(ids[0]).status != SegStatus.APPROVED:
        _fail("undo leaves the source approved")
    if not _audit("segment.propagate_undo", ids[0]):
        _fail("undo must be audited")
    res = client.post(f"/segments/{ids[0]}/propagation", data={"action": "undo"}).json()
    if res.get("restored") != []:
        _fail(f"a second undo has nothing to restore: {res}")
    print("  7. undo: restores copies still as propagated, keeps human-touched ones, audited OK")


def test_cli_surface():
    pid = _project("rp-cli")
    _doc_id, ids = _doc(
        pid,
        [
            (_S, _T, SegStatus.EDITED),
            (_S, "a", SegStatus.MT),
            (_S, "b", SegStatus.MT),
            (_S, "c", SegStatus.MT),
        ],
    )
    rc, out = _run_cli(["doc", "segment-propagation", str(ids[3]), "--unlink"])
    if rc != 0 or "unlinked" not in out:
        _fail(f"CLI unlink: rc={rc} {out}")
    rc, out = _run_cli(["doc", "approve-segment", str(ids[0])])
    if rc != 0 or "Applied to 2 repeats" not in out:
        _fail(f"CLI approve must report the propagation: rc={rc} {out}")
    if _seg(ids[3]).target_text != "c" or _seg(ids[1]).target_text != _T:
        _fail("CLI: unlinked repeat untouched, others updated")
    rc, out = _run_cli(["doc", "segment-propagation", str(ids[0]), "--undo"])
    if rc != 0 or "restored 2" not in out or _seg(ids[1]).target_text != "a":
        _fail(f"CLI undo: rc={rc} {out}")
    rc, out = _run_cli(["doc", "approve-segment", str(ids[0]), "--no-propagate"])
    if rc != 0 or "Applied to" in out or _seg(ids[1]).target_text != "a":
        _fail(f"CLI --no-propagate approves only this segment: rc={rc} {out}")
    rc, out = _run_cli(["doc", "segment-propagation", str(ids[3]), "--link"])
    if rc != 0 or "linked" not in out:
        _fail(f"CLI link: rc={rc} {out}")
    rc, _out = _run_cli(["doc", "segment-propagation", str(ids[3])])
    if rc == 0:
        _fail("CLI: one of --unlink/--link/--undo is required")
    print("  8. CLI: approve reports Applied to N repeats, --no-propagate, unlink/link/undo OK")


def test_mcp_surface():
    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    sys.path.insert(0, mcp_dir)
    import registry
    import server as mcp_server

    tool = next((t for t in registry.TOOLS if t.name == "approve_segment"), None)
    props = (tool.input_schema or {}).get("properties", {}) if tool else {}
    if "propagate" not in props or props["propagate"].get("type") != "boolean":
        _fail(f"the MCP approve_segment tool must expose a boolean propagate arg: {props}")
    sent = {}

    async def fake_post(client, path, body):
        sent["path"], sent["body"] = path, body
        return {"ok": True, "propagated": [7]}

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import asyncio

    orig_post, orig_client = mcp_server._post_json, mcp_server._client
    mcp_server._post_json, mcp_server._client = fake_post, lambda: _C()
    try:
        out = asyncio.run(mcp_server.approve_segment(5))
        if sent["body"] != {}:
            _fail(f"default MCP approve sends no propagate flag: {sent}")
        if '"propagated": [7]' not in out:
            _fail(f"the MCP reply must carry the propagated ids: {out}")
        asyncio.run(mcp_server.approve_segment(5, propagate=False))
        if sent["body"] != {"propagate": False}:
            _fail(f"propagate=false is forwarded: {sent}")
        sent.clear()
        asyncio.run(mcp_server.approve_segment(5, propagation="undo"))
        if sent != {"path": "/api/segments/5/propagation", "body": {"undo": True}}:
            _fail(f"propagation=undo goes to the propagation route, not approve: {sent}")
        sent.clear()
        asyncio.run(mcp_server.approve_segment(5, propagation="unlink"))
        if sent["body"] != {"linked": False}:
            _fail(f"propagation=unlink: {sent}")
        asyncio.run(mcp_server.approve_segment(5, propagation="link"))
        if sent["body"] != {"linked": True}:
            _fail(f"propagation=link: {sent}")
        try:
            asyncio.run(mcp_server.approve_segment(5, propagation="nuke"))
        except Exception:
            pass
        else:
            _fail("an unknown propagation action must be refused")
    finally:
        mcp_server._post_json, mcp_server._client = orig_post, orig_client
    pprops = (tool.input_schema or {}).get("properties", {}).get("propagation", {})
    if pprops.get("enum") != ["undo", "unlink", "link"]:
        _fail(f"the MCP schema lists the propagation actions: {pprops}")
    if ("POST", "/api/segments/{sid}/propagation") not in tool.routes:
        _fail("the propagation route is covered by approve_segment, not exempt")
    if ("POST", "/api/segments/{sid}/propagation") in registry.EXEMPT:
        _fail("the propagation route must no longer be MCP-exempt")
    print("  9. MCP: approve_segment exposes propagate + propagation (undo/unlink/link) OK")


def test_race_and_integrity_review():
    """Review: a stale read must never overwrite a copy another linguist just approved/edited."""
    pid = _project("rp-race")
    doc_id, ids = _doc(
        pid,
        [
            (_S, _T, SegStatus.EDITED),
            (_S, "uno", SegStatus.MT),
            (_S, "dos", SegStatus.NEW),
            (_S, "tres", SegStatus.NEW),
        ],
    )
    # Session A has ALREADY loaded copy ids[1] (status MT) when linguist B approves it.
    sa = Session(engine)
    stale = sa.get(Segment, ids[1])
    if stale.status != SegStatus.MT:
        _fail("setup: stale view")
    with Session(engine) as sb:
        b = sb.get(Segment, ids[1])
        b.target_text = "Mi version"
        b.status = SegStatus.APPROVED
        sb.add(b)
        sb.commit()
    out = segment_review.propagate_repeats(
        sa, sa.get(Segment, ids[0]), sa.get(Document, doc_id), actor="a@x.io"
    )
    sa.close()
    c1 = _seg(ids[1])
    if (c1.target_text, c1.status) != ("Mi version", SegStatus.APPROVED):
        _fail(f"a copy approved a moment earlier must never be overwritten: {c1.target_text!r}")
    if out["propagated"] != [ids[2], ids[3]]:
        _fail(f"only the still-open copies are propagated: {out}")
    # same for a lock, an unlink and a human save that landed after A's stale read
    pid2 = _project("rp-race2")
    doc2, i2 = _doc(
        pid2,
        [
            (_S, _T, SegStatus.EDITED),
            (_S, "a", SegStatus.MT),
            (_S, "b", SegStatus.MT),
            (_S, "c", SegStatus.MT),
        ],
    )
    sa = Session(engine)
    for sid in i2[1:]:
        sa.get(Segment, sid)
    with Session(engine) as sb:
        x = sb.get(Segment, i2[1])
        x.lock_band = "exact"
        x.lock_hard = True
        sb.add(x)
        y = sb.get(Segment, i2[2])
        y.meta = {"no_propagate": True}
        sb.add(y)
        z = sb.get(Segment, i2[3])
        z.target_text = "Escrito a mano"
        z.status = SegStatus.EDITED
        sb.add(z)
        sb.commit()
    out = segment_review.propagate_repeats(sa, sa.get(Segment, i2[0]), sa.get(Document, doc2))
    sa.close()
    if out["propagated"] != []:
        _fail(f"locked / unlinked / just-edited copies are untouched: {out}")
    got = [(_seg(i).target_text) for i in i2[1:]]
    if got != ["a", "b", "Escrito a mano"]:
        _fail(f"nothing overwritten: {got}")
    # a human-confirmed tag-breaking save lands in NEEDS_REVIEW: also human text, kept
    pid3 = _project("rp-race3")
    _d3, i3 = _doc(
        pid3,
        [
            (_S, _T, SegStatus.EDITED),
            (_S, "mano", SegStatus.NEEDS_REVIEW, {"qa_flags": [{"type": "tag_integrity"}]}),
            (_S, "mt", SegStatus.NEEDS_REVIEW),
        ],
    )
    res = _web_approve(i3[0])
    if res["propagated"] != [i3[2]] or _seg(i3[1]).target_text != "mano":
        _fail(f"a human tag override in NEEDS_REVIEW is kept, plain NEEDS_REVIEW is filled: {res}")
    print(
        "  11. race: stale readers never overwrite approved / locked / unlinked / edited copies OK"
    )


def test_undo_redraw_and_race():
    pid = _project("rp-undo2")
    doc_id, ids = _doc(
        pid,
        [(_S, _T, SegStatus.EDITED), (_S, "uno", SegStatus.MT), (_S, "dos", SegStatus.NEW)],
    )
    _web_approve(ids[0])
    # linguist B approves copy 1 in another session before the undo runs
    with Session(engine) as sb:
        b = sb.get(Segment, ids[1])
        b.status = SegStatus.APPROVED
        sb.add(b)
        sb.commit()
    sa = Session(engine)
    sa.get(Segment, ids[2])
    res = segment_review.undo_propagation(
        sa, sa.get(Segment, ids[0]), sa.get(Document, doc_id), actor="a@x.io"
    )
    sa.close()
    if res["restored"] != [ids[2]] or res["kept"] != [ids[1]]:
        _fail(f"undo keeps the copy approved meanwhile: {res}")
    if res["segments"] != [{"id": ids[2], "target_text": "dos", "status": "new"}]:
        _fail(f"undo returns what the editor must redraw: {res['segments']}")
    c1 = _seg(ids[1])
    if (c1.status, c1.target_text) != (SegStatus.APPROVED, _T):
        _fail("the copy approved after the propagation keeps its approved text")
    print("  12. undo: judged on current state, reply carries the redraw data OK")


def _doc_bulk(pid, rows, name="bulk.txt"):
    with Session(engine) as s:
        d = Document(project_id=pid, filename=name, fmt="txt", mode="segmented", status="parsed")
        s.add(d)
        s.commit()
        s.refresh(d)
        segs = [
            Segment(
                document_id=d.id,
                target_locale=_TGT,
                order_index=i,
                source_text=src,
                target_text=tgt,
                status=st,
            )
            for i, (src, tgt, st) in enumerate(rows)
        ]
        s.add_all(segs)
        s.commit()
        return d.id, [x.id for x in segs]


def test_no_per_segment_queries():
    """Performance: the statement count of an approve must not grow with the repeat count."""
    from sqlalchemy import event

    def run(n_repeats):
        pid = _project(f"rp-perf-{n_repeats}")
        rows = [(_S, _T, SegStatus.EDITED)]
        rows += [(_S, f"x{i}", SegStatus.MT) for i in range(n_repeats)]
        rows += [(f"Other sentence {i}", "", SegStatus.NEW) for i in range(200)]
        _doc_id, ids = _doc_bulk(pid, rows)
        stmts = []

        def on_exec(conn, cursor, statement, params, context, executemany):
            stmts.append(statement.split(None, 1)[0].upper())

        event.listen(engine, "before_cursor_execute", on_exec)
        try:
            body = _web_approve(ids[0])
        finally:
            event.remove(engine, "before_cursor_execute", on_exec)
        if len(body["propagated"]) != n_repeats:
            _fail(f"all {n_repeats} repeats propagate: {len(body['propagated'])}")
        return len(stmts), stmts.count("SELECT")

    small, small_sel = run(5)
    big, big_sel = run(300)
    if big > small + 4 or big_sel > small_sel + 2:
        _fail(f"statements grow with repeats: 5 -> {small}/{small_sel}, 300 -> {big}/{big_sel}")
    print(f"  13. performance: 300 repeats cost the same statements as 5 ({big} vs {small}) OK")


def test_seam_direct():
    """The service seam alone (what every surface calls): propagate kwarg + result shape."""
    pid = _project("rp-seam")
    doc_id, ids = _doc(pid, [(_S, _T, SegStatus.EDITED), (_S, "a", SegStatus.MT)])
    with Session(engine) as s:
        out = segment_review.approve_and_write_tm(
            s, s.get(Segment, ids[0]), s.get(Project, pid), s.get(Document, doc_id), propagate=False
        )
    if out["propagated"] != [] or out.get("propagation_skipped") != "disabled for this call":
        _fail(f"propagate=False: {out}")
    with Session(engine) as s:
        out = segment_review.approve_and_write_tm(
            s, s.get(Segment, ids[0]), s.get(Project, pid), s.get(Document, doc_id)
        )
    if out["propagated"] != [ids[1]] or out["propagation_refused"] != []:
        _fail(f"default propagates: {out}")
    print("  10. seam: propagate kwarg and result shape OK")


def test_gate_stale_marker_and_source_race():
    """Review gate: a marked copy whose text another writer changed is ordinary text; the source
    row is re-read under the lock (a concurrent unlink or meta write survives); concurrent
    undos restore once; real concurrent approvals never overwrite approved text."""
    import threading

    # 1. a propagated copy that a marker-unaware writer (operator pause edit, tag repair)
    #    rewrote keeps the marker but not the text: never overwritten, no badge, undo keeps it
    pid = _project("rp-gate-stale")
    doc_id, ids = _doc(
        pid,
        [(_S, _T, SegStatus.EDITED), (_S, "uno", SegStatus.MT), (_S, "Otra", SegStatus.EDITED)],
    )
    _web_approve(ids[0])
    with Session(engine) as s:
        c = s.get(Segment, ids[1])
        if not segment_review.is_propagated_copy(c):
            _fail("setup: copy 1 is a propagated copy")
        c.target_text = "Texto del operador"  # marker left in place on purpose
        s.add(c)
        s.commit()
    res = _web_approve(ids[2], text="Abra el menu, por favor")
    c1 = _seg(ids[1])
    if c1.target_text != "Texto del operador" or ids[1] in res["propagated"]:
        _fail(f"a copy whose text changed since propagation is never overwritten: {res}")
    with Session(engine) as s:
        und = segment_review.undo_propagation(s, s.get(Segment, ids[0]), s.get(Document, doc_id))
    if ids[1] not in und["kept"] or _seg(ids[1]).target_text != "Texto del operador":
        _fail(f"undo keeps a copy whose text changed: {und}")
    page = client.get(f"/documents/{doc_id}/editor?locale={_TGT}")
    if page.status_code != 200:
        _fail(f"editor renders: {page.status_code}")
    # 2. the source is unlinked (and its meta written) by another request after the approve
    #    committed but before propagation: nothing propagates and the unlink survives
    pid = _project("rp-gate-src")
    doc_id, ids = _doc(pid, [(_S, _T, SegStatus.APPROVED), (_S, "uno", SegStatus.MT)])
    sa = Session(engine)
    src = sa.get(Segment, ids[0])
    _ = (src.meta, src.target_text)
    with Session(engine) as sb:
        b = sb.get(Segment, ids[0])
        b.meta = {"no_propagate": True, "other": 1}
        sb.add(b)
        sb.commit()
    out = segment_review.propagate_repeats(sa, src, sa.get(Document, doc_id))
    sa.close()
    if out["propagated"] or (_seg(ids[0]).meta or {}) != {"no_propagate": True, "other": 1}:
        _fail(f"a concurrent unlink of the source wins and its meta survives: {out}")
    if _seg(ids[1]).target_text != "uno":
        _fail("nothing propagated from a source unlinked meanwhile")
    # 3. two undos racing on the same source: the copies are restored once
    pid = _project("rp-gate-undo")
    doc_id, ids = _doc(pid, [(_S, _T, SegStatus.EDITED), (_S, "uno", SegStatus.MT)])
    _web_approve(ids[0])
    s1, s2 = Session(engine), Session(engine)
    a, b = s1.get(Segment, ids[0]), s2.get(Segment, ids[0])
    _ = (a.meta, b.meta)
    r1 = segment_review.undo_propagation(s1, a, s1.get(Document, doc_id))
    with Session(engine) as s:
        c = s.get(Segment, ids[1])
        c.target_text = _T  # someone types the same text again by hand
        c.status = SegStatus.EDITED
        s.add(c)
        s.commit()
    r2 = segment_review.undo_propagation(s2, b, s2.get(Document, doc_id))
    s1.close()
    s2.close()
    if r1["restored"] != [ids[1]] or r2["restored"] or _seg(ids[1]).target_text != _T:
        _fail(f"a second, stale undo restores nothing: {r1} {r2}")
    # 4. real concurrent approvals of different human-edited repeats (threads, one session
    #    each): no deadlock, no lost approval, every open copy holds one approved text
    pid = _project("rp-gate-threads")
    n = 6
    rows = [(_S, f"Version humana {i}", SegStatus.EDITED) for i in range(n)]
    rows += [(_S, f"mt {i}", SegStatus.MT) for i in range(8)]
    doc_id, ids = _doc(pid, rows)
    errors: list = []

    def approve(sid):
        try:
            with Session(engine) as s:
                segment_review.approve_and_write_tm(
                    s, s.get(Segment, sid), s.get(Project, pid), s.get(Document, doc_id)
                )
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=approve, args=(sid,)) for sid in ids[:n]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    if any(t.is_alive() for t in threads) or errors:
        _fail(f"concurrent approvals finish without error: {errors}")
    for i, sid in enumerate(ids[:n]):
        c = _seg(sid)
        if (c.status, c.target_text) != (SegStatus.APPROVED, f"Version humana {i}"):
            _fail(f"an approval is never overwritten: {sid} {c.status} {c.target_text!r}")
    texts = {f"Version humana {i}" for i in range(n)}
    for sid in ids[n:]:
        c = _seg(sid)
        if c.status != SegStatus.EDITED or c.target_text not in texts:
            _fail(f"every open copy holds one approved text: {sid} {c.target_text!r}")
        if not segment_review.is_propagated_copy(c):
            _fail(f"the copy carries a marker that matches its text: {c.meta}")
    print("  14. gate: stale marker, source race, double undo, concurrent approvals OK")


if __name__ == "__main__":
    test_gate_stale_marker_and_source_race()
    test_propagation_and_untouched()
    test_marker_cleared_by_human_action()
    test_unlink_and_optout()
    test_integrity_refusal()
    test_undo()
    test_cli_surface()
    test_mcp_surface()
    test_seam_direct()
    test_race_and_integrity_review()
    test_undo_redraw_and_race()
    test_no_per_segment_queries()
    print("REPEAT PROPAGATION 617 OK")
