"""Issues #600 and #602, executed under node plus a server render.

#600: the editor asked "approved but NOT saved to TM: placeholder differs" with the native
window.confirm(), which freezes the tab and automation and is not reliably exposed to
screen readers. It now asks in an in-page <dialog role="alertdialog"> (askInPage) and keeps
the SAME fail-closed behavior: only the yes button re-POSTs with the acknowledgement;
Escape, the no button, a browser without <dialog> support and an already-open dialog all
leave the placeholder-broken target out of TM.

#602: the live run page kept "Status: RUNNING" and the Cancel button after the run ended.
The stream's done / closed event now rewrites the status badge (localized labels ride in
data-labels) and removes the Cancel form; a paused run keeps Cancel.

No browser, no network: the REAL editor.html / run_view.html script text runs in node with
a small fake DOM, and the server render is checked for the ids the script relies on.
Run ONLY through tests/test_verify_suites.py. ASCII-only.
Final line: "EDITOR DIALOG 600 RUN PAGE 602 OK".
"""
# ruff: noqa: E501
# (The node scripts are JavaScript data inside Python strings; wrapping them hurts review.)

import json
import os
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_dialog600_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Document, Project, RunStatus, StepType, TargetLocale, WorkflowRun
from app.services import settings
from app.services import workflows as W

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), "rb") as fh:
        return fh.read().decode("utf-8").replace("\r\n", "\n")


def _node(script):
    node = shutil.which("node")
    if not node:
        _fail("this suite needs Node (it runs the real page scripts)")
    res = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    if res.returncode != 0:
        _fail((res.stdout[-2000:], res.stderr[-4000:]))
    return res.stdout


def _editor_functions(*names):
    src = _read("app", "templates", "editor.html")
    out = []
    for name in names:
        for prefix in ("async function ", "function "):
            marker = prefix + name + "("
            if marker in src:
                body = src.split(marker, 1)[1].split("\n}\n", 1)[0]
                out.append(marker + body + "\n}\n")
                break
        else:
            _fail(f"editor.html: function {name} not found")
    # #617 item 6: approve() reports propagated repeats through showPropagated (redraws other
    # rows; covered by verify_repeat_propagation_undo_ui_617). These harnesses have one row.
    if "approve" in names:
        out.append("function showPropagated() { return ''; }")
    return "\n".join(out)


_EDITOR_HARNESS = r"""
const assert = require('assert');
const posts = [];
const listeners = { yes: [], no: [] };
let dialogMode = 'yes';  // yes | no | escape | unsupported | open
const nodes = {};
function node(id) {
  if (!nodes[id]) nodes[id] = { id, textContent: '', isConnected: true, focus() { this.focused = true; },
    addEventListener(t, fn) { if (t === 'click') listeners[id === 'edAskYes' ? 'yes' : 'no'].push(fn); },
    removeEventListener(t, fn) { const l = listeners[id === 'edAskYes' ? 'yes' : 'no']; const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); } };
  return nodes[id];
}
const dlg = { open: false, closeFns: [],
  addEventListener(t, fn) { if (t === 'close') this.closeFns.push(fn); },
  showModal() {
    this.open = true;
    // the "user" answers on the next tick
    setImmediate(() => {
      if (dialogMode === 'yes') listeners.yes.slice().forEach((f) => f());
      else if (dialogMode === 'no') listeners.no.slice().forEach((f) => f());
      else this.close();  // Escape: the browser closes the dialog with no button pressed
    });
  },
  close() { this.open = false; const fns = this.closeFns.splice(0); fns.forEach((f) => f()); } };
const opener = { isConnected: true, focus() { this.focused = true; } };
const document = { activeElement: opener, getElementById: (id) => {
  if (id === 'edAsk') return dialogMode === 'unsupported' ? { open: false } : dlg;
  if (id.startsWith('tgt-')) return { value: 'Tienes articulos' };
  if (id.startsWith('st-')) return chip;
  if (id === 'approvedCount') return { textContent: '0' };
  return node(id);
} };
const chip = { textContent: '', classList: { add() {} }, className: '' };
const window = { CSRF_TOKEN: 't' };  // # pregate: allow-secret fake CSRF value for a node stub
const approvedIds = new Set(), TOTAL_SEGMENTS = 2;
const elapsed = () => 0, setStatusClass = () => {}, bumpProgress = () => {};
const save = async () => ({ ok: true, status: 'edited' });
const segmentNumber = () => 3;
const alerts = [], said = [];
const edAnnouncer = { say: (s) => said.push(s), alert: (s) => alerts.push(s), clearAlert() {} };
const fetch = async (url, opts) => {
  const body = opts && opts.body ? opts.body.toString() : '';
  posts.push(body);
  const acked = body.includes('acknowledge_placeholder_loss=1');
  return { ok: true, status: 200, json: async () => ({ ok: true, status: 'approved',
    tm_skipped_code: acked ? null : 'placeholder_integrity', tm_skipped: 'placeholder differs', tm_written: acked }) };
};
"""


def test_placeholder_dialog():
    src = _read("app", "templates", "editor.html")
    if (
        "window.confirm(`Segment" in src
        or "tm_skipped_code === 'placeholder_integrity') {\n      if (window.confirm" in src
    ):
        _fail("#600: the placeholder acknowledge must not use window.confirm")
    if '<dialog class="modal session-dialog" id="edAsk" role="alertdialog"' not in src:
        _fail("#600: the editor needs the in-page alertdialog #edAsk")
    for needle in (
        'aria-labelledby="edAskTitle"',
        'aria-describedby="edAskMsg"',
        "dlg.showModal()",
    ):
        if needle not in src:
            _fail(f"#600: missing {needle}")
    if "innerHTML" in _editor_functions("askInPage"):
        _fail("#600: askInPage must set text with textContent only")
    for mode, expect_ack in (
        ("yes", True),
        ("no", False),
        ("escape", False),
        ("unsupported", False),
    ):
        _node(
            _EDITOR_HARNESS
            + _editor_functions("statusLabel", "askInPage", "approve")
            + f"""
dialogMode = {json.dumps(mode)};
(async () => {{
  const data = await approve(5);
  assert(data, 'approve returned nothing');
  const acked = posts.some((b) => b.includes('acknowledge_placeholder_loss=1'));
  assert.equal(acked, {json.dumps(expect_ack)}, 'mode ' + dialogMode + ': posts=' + JSON.stringify(posts));
  assert.equal(data.tm_written, {json.dumps(expect_ack)});
  assert.equal(posts.length, {2 if expect_ack else 1});
  if (dialogMode !== 'unsupported') {{
    assert.equal(nodes.edAskTitle.textContent, 'Save it to TM anyway?');
    assert(nodes.edAskMsg.textContent.startsWith('Segment 3 is approved but was NOT saved to TM: placeholder differs'));
    assert(!dlg.open, 'dialog left open');
    assert(opener.focused, 'focus was not returned');
    assert.equal(listeners.yes.length + listeners.no.length, 0, 'listeners leaked');
  }}
  console.log('ok');
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        )
    print("  1. #600: yes acknowledges; no / Escape / no <dialog> support fail closed OK")


def _run_view_script():
    src = _read("app", "templates", "run_view.html")
    block = src[src.index('<script src="/static/sse.js') :]
    opening = '<script nonce="{{ csp_nonce() }}">\n'
    block = block[block.index(opening) + len(opening) : block.index("</script>\n{% endif %}")]
    block = block.replace("{{ run.id if run else 'null' }}", "7").replace(
        "{{ run.id if run else '' }}", "7"
    )
    block = block.replace(
        "{% include '_html_escape.js' %}", _read("app", "templates", "_html_escape.js")
    )
    if "{{" in block or "{%" in block:
        _fail("run_view script has new Jinja: extend this harness")
    return block


_RUN_HARNESS = r"""
const assert = require('assert');
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, dataset: {}, style: {}, hidden: false, innerHTML: '', textContent: '',
    appendChild() {}, classList: { add() {}, remove() {} }, querySelector: () => null,
    getAttribute: () => null, setAttribute() {}, removed: false, remove() { this.removed = true; } };
  return els[id];
}
const badge = { textContent: 'running', title: 'running', className: 'badge run' };
const wrap = { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
els.runStatusBadge = Object.assign(el('runStatusBadge'), {
  getAttribute: (k) => (k === 'data-labels' ? JSON.stringify({ completed: 'Fertig', failed: 'Fehler', failed_budget: 'Budget', cancelled: 'Abgebrochen' }) : null),
  querySelector: (sel) => (sel === '.badge' ? badge : sel === '.status-text' ? wrap : null) });
const document = { visibilityState: 'visible', getElementById: (id) => el(id),
  createElement: () => ({ classList: { add() {} } }), addEventListener() {} };
const window = { location: { href: '' } };
const fetch = () => Promise.reject(new Error('offline'));
const opens = [];
function connectSSE(url, opts) { opens.push(opts); return { close() {} }; }
const noop = () => {};
function makeAnnouncer() { return { say: noop, alert: noop, flush: noop, settle: noop, settling: () => false, clearAlert: noop }; }
"""


def test_run_page_settles():
    for ending, final, label, cls, cancel_removed in (
        ("{ type: 'done', run_id: 7, text: 'ok' }", "completed", "Fertig", "badge exact", True),
        (
            "{ type: 'closed', run_id: 7, status: 'completed' }",
            "completed",
            "Fertig",
            "badge exact",
            True,
        ),
        (
            "{ type: 'closed', run_id: 7, status: 'failed', error: 'boom' }",
            "failed",
            "Fehler",
            "badge danger",
            True,
        ),
        (
            "{ type: 'closed', run_id: 7, status: 'cancelled' }",
            "cancelled",
            "Abgebrochen",
            "badge status",
            True,
        ),
        ("{ type: 'closed', run_id: 7, status: 'paused' }", "", "running", "badge run", False),
    ):
        _node(
            _RUN_HARNESS
            + _run_view_script()
            + f"""
const send = (ev) => opens[0].onMessage({{ data: JSON.stringify(ev) }});
opens[0].onConnect();
send({{ type: 'run_started', run_id: 7, steps: 1 }});
assert.equal(els.runCancelForm, undefined, 'cancel form touched before the end');
send({ending});
const cancel = els.runCancelForm;
assert.equal(!!(cancel && cancel.removed), {json.dumps(cancel_removed)}, 'cancel form state for ' + {json.dumps(ending)});
assert.equal(badge.textContent, {json.dumps(label)});
assert.equal(badge.className, {json.dumps(cls)});
assert.equal(wrap.attrs['data-status'], {json.dumps(final) if final else "undefined"});
console.log('ok');
"""
        )
    print("  2. #602: done / closed rewrite the badge and remove Cancel; paused keeps it OK")


def test_run_page_render():
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    with Session(engine) as s:
        p = Project(name="d600", source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        d = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        wf = W.create_workflow(s, "wf602")
        W.add_step(
            s,
            wf.id,
            name="T",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            model="m1",
            prompt_text="T {text}",
            key="t",
        )
        r = WorkflowRun(
            workflow_id=wf.id, document_id=d.id, target_locale="de-DE", status=RunStatus.RUNNING
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        rid = r.id
    page = TestClient(app).get(f"/runs/{rid}").text
    for needle in ('id="runStatusBadge"', "data-labels=", 'id="runCancelForm"', "settleRunChrome("):
        if needle not in page:
            _fail(f"#602: the live run page must contain {needle}")
    print("  3. #602: the live run page carries the badge slot, labels and Cancel id OK")


def main():
    test_placeholder_dialog()
    test_run_page_settles()
    test_run_page_render()
    print("EDITOR DIALOG 600 RUN PAGE 602 OK")


if __name__ == "__main__":
    main()
