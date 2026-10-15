"""Issue #617 item 6 (review): the editor's Undo control for "Applied to N repeats".

Real Chrome against the real app (uvicorn on a free port, throwaway data dir, mock provider):
  * approving a segment that has repeats shows an "Undo repeats" BUTTON in the same row, right
    after Approve (a native button: keyboard reachable with Tab, no inline handler), and the live
    announcer says "Applied to 2 repeats.";
  * pressing Enter on it calls the existing POST /segments/{id}/propagation route, redraws the
    copies in place (text and status chip), drops the Propagated badges, removes the button,
    returns focus to Approve and announces "Undone: 2 repeats restored.";
  * the database agrees (copies back to their pre-approval text and status);
  * a copy a person edited after the propagation is kept and the announcer says so;
  * the Spanish editor shows the Spanish label (i18n es).
Final line: "REPEAT PROPAGATION UNDO UI 617 OK".
"""

import os
import socket
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_rpundoui617_")

from playwright.sync_api import sync_playwright
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Document, Project, Segment, SegStatus, TargetLocale

init_db()
_S = "Open the settings menu"
_T = "Abra el menu de configuracion"


def _fail(msg):
    print("FAIL:", ascii(msg))
    sys.exit(1)


def _seed(name):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="es-MX"))
        s.commit()
        d = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        ids = []
        rows = [
            (_S, _T, SegStatus.EDITED),
            (_S, "uno", SegStatus.MT),
            (_S, "dos", SegStatus.NEW),
            (_S, "tres", SegStatus.MT),
        ]
        for i, (src, tgt, st) in enumerate(rows):
            seg = Segment(
                document_id=d.id,
                target_locale="es-MX",
                order_index=i,
                source_text=src,
                target_text=tgt,
                status=st,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            ids.append(seg.id)
        return d.id, ids


def _db(sid):
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        s.expunge(seg)
        return seg


def _free_port():
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    return port


class _Server:
    def __init__(self, port):
        import uvicorn

        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise AssertionError("uvicorn did not start in time")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _status_text(page):
    return (page.locator("#edStatus").inner_text() or "").strip()


def _wait_status(page, needle, what):
    deadline = time.time() + 10
    while time.time() < deadline:
        if needle in _status_text(page):
            return
        page.wait_for_timeout(100)
    _fail(f"{what}: announcer never said {needle!r}, has {_status_text(page)!r}")


def run():
    port = _free_port()
    srv = _Server(port)
    srv.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome")
            try:
                _english(browser, base)
                _spanish(browser, base)
            finally:
                browser.close()
    finally:
        srv.stop()


def _english(browser, base):
    doc_id, ids = _seed("undo-ui-en")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/documents/{doc_id}/editor?locale=es-MX", wait_until="networkidle")
    sel_undo = '[data-action="undo-propagation"]'
    if page.locator(sel_undo).count():
        _fail("no Undo control before any propagation")
    approve = page.locator(f'[data-action="approve"][data-id="{ids[0]}"]')
    approve.click()
    page.wait_for_selector(f"tr:has(#tgt-{ids[0]}) {sel_undo}", timeout=10000)
    _wait_status(page, "Applied to 3 repeats.", "approve")
    undo = page.locator(f"tr:has(#tgt-{ids[0]}) {sel_undo}")
    if undo.count() != 1:
        _fail("exactly one Undo control in the approved row")
    if undo.evaluate("e => e.tagName") != "BUTTON" or undo.get_attribute("type") != "button":
        _fail("Undo must be a native type=button button (keyboard accessible)")
    if any((undo.get_attribute(a) or "") for a in ("onclick", "onkeydown", "onmousedown")):
        _fail("no inline handlers on the Undo control")
    if "Undo repeats" not in undo.inner_text():
        _fail(f"visible label: {undo.inner_text()!r}")
    for sid in ids[1:]:
        if page.locator(f"#tgt-{sid}").input_value() != _T:
            _fail("copies are redrawn with the approved text before Undo")
        if not page.locator(f"#qa-{sid} [data-propagated]").count():
            _fail("each copy shows the Propagated badge")
    # Keyboard: Tab from Approve lands on Undo (it sits right after it in the row).
    approve.focus()
    page.keyboard.press("Tab")
    if page.evaluate("document.activeElement && document.activeElement.dataset.action") != (
        "undo-propagation"
    ):
        _fail("Tab from Approve must reach the Undo control")
    # A person edits the third copy after the propagation: Undo must keep it.
    page.locator(f"#tgt-{ids[3]}").fill("Mia")
    page.locator(f"#tgt-{ids[3]}").blur()
    page.wait_for_timeout(600)
    approve.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    page.wait_for_function("sel => !document.querySelector(sel)", arg=sel_undo, timeout=10000)
    _wait_status(page, "Undone: 2 repeats restored.", "undo")
    if "1 repeat was kept because it changed since." not in _status_text(page):
        _fail(f"the kept copy is announced: {_status_text(page)!r}")
    if page.evaluate("document.activeElement && document.activeElement.dataset.action") != (
        "approve"
    ):
        _fail("focus returns to Approve after Undo")
    if page.locator(f"#tgt-{ids[1]}").input_value() != "uno":
        _fail("copy 1 redrawn to its old text")
    if page.locator(f"#tgt-{ids[2]}").input_value() != "dos":
        _fail("copy 2 redrawn to its old text")
    if page.locator(f"#tgt-{ids[3]}").input_value() != "Mia":
        _fail("the human-edited copy is untouched in the page")
    for sid in ids[1:3]:
        if page.locator(f"#qa-{sid} [data-propagated]").count():
            _fail("Propagated badge goes with the undo")
    if page.locator(f"#st-{ids[2]}").inner_text().strip().lower() != "new":
        _fail(f"status chip redrawn: {page.locator('#st-' + str(ids[2])).inner_text()!r}")
    c1, c2, c3 = _db(ids[1]), _db(ids[2]), _db(ids[3])
    if (c1.target_text, c1.status) != ("uno", SegStatus.MT):
        _fail(f"db copy 1: {c1.target_text!r} {c1.status}")
    if (c2.target_text, c2.status) != ("dos", SegStatus.NEW):
        _fail(f"db copy 2: {c2.target_text!r} {c2.status}")
    if c3.target_text != "Mia":
        _fail("db: the human-edited copy survives the undo")
    if errors:
        _fail(("page errors", errors))
    page.close()
    print("  1. editor: Undo repeats button, keyboard, announced, focus back, redraw, kept copy OK")


def _spanish(browser, base):
    from app.services import settings

    settings.set_many({"default_ui_locale": "es"})
    try:
        doc_id, ids = _seed("undo-ui-es")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{base}/documents/{doc_id}/editor?locale=es-MX", wait_until="networkidle")
        page.locator(f'[data-action="approve"][data-id="{ids[0]}"]').click()
        sel = f'tr:has(#tgt-{ids[0]}) [data-action="undo-propagation"]'
        page.wait_for_selector(sel, timeout=10000)
        label = page.locator(sel).inner_text()
        if "Deshacer repeticiones" not in label:
            _fail(f"the Undo label must be Spanish: {label!r}")
        _wait_status(page, "Aplicado a 3 repeticiones", "es approve")
        page.close()
    finally:
        settings.set_many({"default_ui_locale": "en"})
    print("  2. es: the Undo label is Spanish OK")


if __name__ == "__main__":
    run()
    print("REPEAT PROPAGATION UNDO UI 617 OK")
