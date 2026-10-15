"""Issue #562: approving a segment with a dropped placeholder must not seed shared TM.

Usability pass 1 (2026-10-06): in the editor a linguist dropped {count} from a target, the
QA flag "Placeholder integrity broken" appeared, Approve was accepted and the row read
"APPROVED ->TM", so the broken target became a future 100% match. Second half of the issue:
the QA badge read "tags" right after the save but the raw code "placeholder_integrity" after
a reload.

Policy under test (one seam, segment_review.approve_and_write_tm, shared by the web route,
POST /api/segments/{id}/approve, `doc approve-segment` and the MCP `approve_segment` tool):
  * a segment whose target drops or adds a real placeholder is still APPROVED (a human may
    knowingly accept it, exactly like a hedge) but the TM write is WITHHELD:
    tm_written false + tm_skipped (+ tm_skipped_code "placeholder_integrity");
  * an explicit acknowledgement (acknowledge_placeholder_loss) writes it anyway;
  * a clean segment still writes TM, and other QA categories are unchanged;
  * the editor badge label is the same human label live (save response) and after reload.

Hermetic: throwaway LOC_TMS_DATA_DIR + MockProvider. ASCII-only.
Final line: "APPROVE PLACEHOLDER 562 OK".
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

init_env("loctms_approveph562_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Document, Project, Role, Segment, SegStatus, TargetLocale, User
from app.services import apikeys, segment_review, settings, tm

init_db()
_SRC, _TGT = "en-US", "es-MX"
_SOURCE = "You have {count} items in the cart"
_DROPPED = "Tienes articulos en el carrito"
_KEPT = "Tienes {count} articulos en el carrito"

client = TestClient(app)


def _fail(msg):
    print("FAIL:", msg)
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


def _doc(pid, pairs):
    with Session(engine) as s:
        d = Document(project_id=pid, filename="d.txt", fmt="txt", mode="segmented", status="parsed")
        s.add(d)
        s.commit()
        s.refresh(d)
        ids = []
        for i, (src, tgt) in enumerate(pairs):
            seg = Segment(
                document_id=d.id,
                target_locale=_TGT,
                order_index=i,
                source_text=src,
                target_text=tgt,
                status=SegStatus.MT,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            ids.append(seg.id)
        return d.id, ids


def _tm_has(pid, source):
    with Session(engine) as s:
        hit = tm.lookup(s, pid, source, _SRC, _TGT)
    return bool(hit and hit.get("match_pct") == 100)


def _key(scopes="api docs"):
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "ph562-admin@x.io")).first()
        if not u:
            u = User(email="ph562-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "ph562", scopes=scopes)
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


def _approve_seam(pid, doc_id, seg_id, **kw):
    with Session(engine) as s:
        return segment_review.approve_and_write_tm(
            s, s.get(Segment, seg_id), s.get(Project, pid), s.get(Document, doc_id), **kw
        )


def test_service_seam():
    pid = _project("ph562-svc")
    doc_id, ids = _doc(
        pid,
        [
            (_SOURCE, _DROPPED),
            (_SOURCE + " two", _KEPT + " dos"),
            (_SOURCE + " three", _DROPPED + " tres"),
        ],
    )
    out = _approve_seam(pid, doc_id, ids[0])
    if out["status"] != SegStatus.APPROVED:
        _fail(f"a dropped-placeholder segment is still approved: {out}")
    if out["tm_written"] is not False or not out["tm_skipped"]:
        _fail(f"a dropped placeholder must be approved-but-withheld from TM: {out}")
    if out.get("tm_skipped_code") != "placeholder_integrity":
        _fail(f"the withhold must carry a machine-readable code: {out}")
    if _tm_has(pid, _SOURCE):
        _fail("a dropped placeholder must NOT become a TM match")
    # a clean segment still writes
    out = _approve_seam(pid, doc_id, ids[1])
    if out["tm_written"] is not True or out.get("tm_skipped_code"):
        _fail(f"a placeholder-clean segment must still write TM: {out}")
    # an explicit acknowledgement writes it anyway
    out = _approve_seam(pid, doc_id, ids[2], acknowledge_placeholder_loss=True)
    if out["tm_written"] is not True:
        _fail(f"an acknowledged placeholder loss must be written: {out}")
    if not _tm_has(pid, _SOURCE + " three"):
        _fail("the acknowledged pair must be a TM match")
    from app.models import AuditLog

    with Session(engine) as s:
        details = [
            r.detail
            for r in s.exec(select(AuditLog).where(AuditLog.action == "segment.approve")).all()
            if f"segment {ids[2]} " in (r.target or "")
        ]
    if not any("placeholder_loss_acknowledged" in d for d in details):
        _fail(f"an acknowledged loss must leave an audit trace: {details}")
    print("  1. service: dropped withheld (code), clean writes, acknowledged writes OK")


def test_other_categories_unchanged():
    """A whitespace-only or number-free difference must not be newly withheld."""
    pid = _project("ph562-other")
    doc_id, ids = _doc(pid, [("Plain source", "Origen  plano")])
    out = _approve_seam(pid, doc_id, ids[0])
    if out["tm_written"] is not True:
        _fail(f"a non-placeholder QA finding must not withhold TM: {out}")
    print("  2. other QA categories unchanged OK")


def test_api_surface():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        h = {"X-Loc-Token": _key()}
        pid = _project("ph562-api")
        _d, ids = _doc(pid, [(_SOURCE + " api1", _DROPPED), (_SOURCE + " api2", _DROPPED)])
        r = client.post(f"/api/segments/{ids[0]}/approve", headers=h)
        b = r.json()
        if r.status_code != 200 or b.get("tm_written") is not False or not b.get("tm_skipped"):
            _fail(f"/api approve of a dropped placeholder must 200 + withhold: {r.status_code} {b}")
        if b.get("tm_skipped_code") != "placeholder_integrity":
            _fail(f"/api must carry tm_skipped_code: {b}")
        if b.get("status") != SegStatus.APPROVED:
            _fail(f"/api: still approved: {b}")
        if _tm_has(pid, _SOURCE + " api1"):
            _fail("/api: a dropped placeholder must not be a TM match")
        r = client.post(
            f"/api/segments/{ids[1]}/approve",
            headers=h,
            json={"acknowledge_placeholder_loss": True},
        )
        b = r.json()
        if r.status_code != 200 or b.get("tm_written") is not True:
            _fail(f"/api acknowledged approve must write TM: {r.status_code} {b}")
        r = client.post(
            f"/api/segments/{ids[1]}/approve",
            headers=h,
            json={"acknowledge_placeholder_loss": "false"},
        )
        if r.status_code != 400:
            _fail(f"a non-boolean acknowledge flag must be a 400, got {r.status_code} {r.text}")
        print("  3. /api: withheld + code, acknowledge writes, non-bool 400 OK")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


def test_web_route():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    pid = _project("ph562-web")
    _d, ids = _doc(pid, [(_SOURCE + " web1", _KEPT + " web"), (_SOURCE + " web2", _KEPT)])
    # the editor flow: save the edit that drops {count}, then approve
    sid = ids[0]
    r = client.post(f"/segments/{sid}/save", data={"target_text": _DROPPED, "tte_ms": "10"})
    saved = r.json()
    if r.status_code != 200 or saved.get("ok") is not True:
        _fail(f"save of the edit must succeed: {r.status_code} {saved}")
    # approve: withheld on the web route too, same verdict as /api
    r = client.post(f"/segments/{sid}/approve")
    b = r.json()
    if r.status_code != 200 or b.get("tm_written") is not False or not b.get("tm_skipped"):
        _fail(f"web approve of a dropped placeholder must withhold: {r.status_code} {b}")
    if b.get("tm_skipped_code") != "placeholder_integrity":
        _fail(f"web must carry tm_skipped_code: {b}")
    if _tm_has(pid, _SOURCE + " web1"):
        _fail("web: a dropped placeholder must not be a TM match")
    # acknowledge (the editor's confirm re-POST)
    r = client.post(f"/segments/{sid}/approve", data={"acknowledge_placeholder_loss": "1"})
    b = r.json()
    if r.status_code != 200 or b.get("tm_written") is not True:
        _fail(f"web acknowledged approve must write TM: {r.status_code} {b}")
    # clean segment unchanged
    r = client.post(f"/segments/{ids[1]}/approve")
    if r.json().get("tm_written") is not True:
        _fail(f"web clean approve must write TM: {r.text}")
    print("  4. web: approve withheld, acknowledge writes OK")


def test_cli_surface():
    pid = _project("ph562-cli")
    _d, ids = _doc(pid, [(_SOURCE + " cli1", _DROPPED), (_SOURCE + " cli2", _DROPPED)])
    rc, out = _run_cli(["doc", "approve-segment", str(ids[0])])
    if rc != 0 or "NOT saved to TM" not in out or "placeholder" not in out:
        _fail(f"CLI must say the placeholder withheld the TM write: rc={rc} {out}")
    if _tm_has(pid, _SOURCE + " cli1"):
        _fail("CLI: a dropped placeholder must not be a TM match")
    rc, out = _run_cli(["doc", "approve-segment", str(ids[1]), "--acknowledge-placeholder-loss"])
    if rc != 0 or "Saved to TM" not in out:
        _fail(f"CLI acknowledged approve must save TM: rc={rc} {out}")
    print("  5. CLI: withheld with a reason, --acknowledge-placeholder-loss writes OK")


def test_labels_live_equal_reload():
    """The QA badge must read the same right after a save and after a reload."""
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    pid = _project("ph562-label")
    doc_id, ids = _doc(pid, [(_SOURCE + " lbl", _KEPT + " lbl")])
    sid = ids[0]
    r = client.post(f"/segments/{sid}/save", data={"target_text": _DROPPED, "tte_ms": "10"})
    saved = r.json()
    if r.status_code != 200 or saved.get("ok") is not True:
        _fail(f"save of the edit must succeed: {r.status_code} {saved}")
    live = saved.get("qa") or []
    live_types = {f.get("type") for f in live}
    if "placeholder_integrity" not in live_types:
        _fail(f"the save response must carry the persisted placeholder flag: {live}")
    if "tags" in live_types:
        _fail(f"the save response must not double-badge a pure placeholder drop: {live}")
    live_labels = {f.get("label") for f in live if f.get("type") == "placeholder_integrity"}
    if live_labels != {"placeholders"}:
        _fail(f"the live placeholder badge needs the human label: {live}")
    page = client.get(f"/documents/{doc_id}/editor?locale={_TGT}").text
    cell = re.search(r'id="qa-%d">(.*?)</td>' % sid, page, re.S)  # noqa: UP031
    if not cell:
        _fail("editor page must render the QA cell")
    html = cell.group(1)
    if ">placeholder_integrity<" in html:
        _fail(f"the reloaded badge must not show the raw code: {html}")
    if ">placeholders<" not in html:
        _fail(f"the reloaded badge must read 'placeholders': {html}")
    print("  7. label: save response == reloaded grid, no raw code, no tags dup OK")


_L, _R = "⟦", "⟧"  # inline-fidelity token brackets (file stays ASCII)


def _tok(name):
    return _L + name + _R


def test_inline_tags_named():
    """#581 (production API test): a dropped inline tag withheld the TM write with NO reason.
    Every withhold must carry tm_skipped + tm_skipped_code, and no acknowledgement may push
    a token-bearing pair (or a target with damaged tags) into TM."""
    pid = _project("ph562-tags")
    tagged_src = "Click " + _tok("g1") + "here" + _tok("/g1") + " now"
    dropped_tgt = "Haga clic aqui ahora"
    intact_tgt = "Haga clic " + _tok("g1") + "aqui" + _tok("/g1") + " ahora"
    added_tgt = "Origen " + _tok("g1") + "plano" + _tok("/g1")
    doc_id, ids = _doc(
        pid,
        [
            (tagged_src, dropped_tgt),
            (tagged_src + " 2", intact_tgt + " 2"),
            ("Plain source tags", added_tgt),
            (tagged_src + " 3", dropped_tgt + " 3"),
        ],
    )
    out = _approve_seam(pid, doc_id, ids[0])
    if out["tm_written"] is not False or not out["tm_skipped"]:
        _fail(f"a dropped inline tag must be withheld WITH a reason: {out}")
    if out.get("tm_skipped_code") != "tag_integrity":
        _fail(f"a dropped inline tag needs tm_skipped_code tag_integrity: {out}")
    out = _approve_seam(pid, doc_id, ids[1])
    if out["tm_written"] is not False or out.get("tm_skipped_code") != "tagged_source":
        _fail(f"an intact tagged segment is withheld with a named reason: {out}")
    out = _approve_seam(pid, doc_id, ids[2])
    if out["tm_written"] is not False or out.get("tm_skipped_code") != "tag_integrity":
        _fail(f"a target that ADDS inline tags to a tagless source must be withheld: {out}")
    if _tm_has(pid, "Plain source tags"):
        _fail("a target with stray inline tokens must not become a TM match")
    out = _approve_seam(pid, doc_id, ids[3], acknowledge_placeholder_loss=True)
    if out["tm_written"] is not False or out.get("tm_skipped_code") != "tag_integrity":
        _fail(f"the placeholder acknowledgement must not push damaged tags into TM: {out}")
    # /api carries the reason and code too
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        h = {"X-Loc-Token": _key()}
        _d, aids = _doc(pid, [(tagged_src + " api", dropped_tgt + " api")])
        r = client.post(f"/api/segments/{aids[0]}/approve", headers=h)
        b = r.json()
        if r.status_code != 200 or b.get("tm_written") is not False:
            _fail(f"/api approve of a dropped tag must 200 + withhold: {r.status_code} {b}")
        if not b.get("tm_skipped") or b.get("tm_skipped_code") != "tag_integrity":
            _fail(f"/api must name the inline-tag withhold: {b}")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  8. inline tags: dropped/added/tagged named (code), ack cannot override, /api OK")


def test_every_withhold_has_a_code():
    """#581 (remaining item): approve never says tm_written:false without a reason AND a
    machine code, including the refusal guard, an empty target and the unmodelled case."""
    pid = _project("ph581-codes")
    doc_id, ids = _doc(
        pid,
        [
            ("Please translate this sentence", "I'm sorry, but I cannot translate this text."),
            ("Another plain sentence here", "Otra frase simple aqui"),
        ],
    )
    out = _approve_seam(pid, doc_id, ids[0])
    if out["tm_written"] is not False or not out["tm_skipped"]:
        _fail(f"a hedge must be withheld with a reason: {out}")
    if out.get("tm_skipped_code") != "suspected_refusal":
        _fail(f"a hedge withhold needs tm_skipped_code suspected_refusal: {out}")
    # an unmodelled write_back refusal must not be silent
    real = tm.write_back
    tm.write_back = lambda *a, **k: None
    try:
        out = _approve_seam(pid, doc_id, ids[1])
    finally:
        tm.write_back = real
    if out["tm_written"] is not False or not out["tm_skipped"]:
        _fail(f"an unmodelled withhold must still carry a reason: {out}")
    if out.get("tm_skipped_code") != "tm_write_refused":
        _fail(f"an unmodelled withhold needs tm_skipped_code tm_write_refused: {out}")
    print("  9. every withhold carries a code (refusal, unmodelled) OK")


def test_mcp_schema():
    mcp_dir = os.path.join(_ROOT, "integrations", "mcp-server")
    sys.path.insert(0, mcp_dir)
    import registry

    tool = next((t for t in registry.TOOLS if t.name == "approve_segment"), None)
    if tool is None:
        _fail("approve_segment must be a registered MCP tool")
    props = tool.input_schema.get("properties", {})
    if props.get("acknowledge_placeholder_loss", {}).get("type") != "boolean":
        _fail(f"approve_segment must expose acknowledge_placeholder_loss: {props}")
    with open(os.path.join(mcp_dir, "server.py"), encoding="utf-8") as fh:
        src = fh.read()
    if "acknowledge_placeholder_loss" not in src:
        _fail("the MCP handler must forward acknowledge_placeholder_loss")
    print("  6. MCP: approve_segment exposes acknowledge_placeholder_loss OK")


def main():
    test_service_seam()
    test_other_categories_unchanged()
    test_api_surface()
    test_web_route()
    test_cli_surface()
    test_mcp_schema()
    test_labels_live_equal_reload()
    test_inline_tags_named()
    test_every_withhold_has_a_code()
    print("APPROVE PLACEHOLDER 562 OK")


if __name__ == "__main__":
    main()
