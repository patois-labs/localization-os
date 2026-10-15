"""A11y train T4: live status messages and the Editor approve bug, executed under node.

Run ONLY through tests/test_verify_suites.py (never hand this file to pytest). No server,
no browser, no network: each check runs the REAL shipped JavaScript (app/static/
announce.js, the Run view's script block, the Editor's save/approve functions) inside
node with a fake clock and a minimal fake DOM.

  1. announcer: polite coalescing (last wins, one pending), the minimum gap, the settle
     window, assertive bypass + 3 s de-dup, flush (with and without a settle window: a
     pending message is dropped), clearAlert, clear-then-set writes (every assignment
     to a region is recorded, clears included).
  2. run view: a page opened mid-run (and a reopen after the observer ceiling) that
     receives a burst of 20 replayed steps speaks ONE summary; a reopen that replays no
     steps does not repeat "Run started"; live steps, progress milestones, the failed
     close (assertive, with the error) and a lost connection; polite messages are never
     closer than 10 s; completed / cancelled / paused are flushed at once and a step
     still waiting in the gap is never spoken after them.
  3. editor approve bug: when save() returns null (HTTP error, network error, declined
     tag confirm) approve() never POSTs /approve, never clears the failure marker and
     says "Not approved: the edit was not saved."; a failed approve request is reported
     visibly and assertively; a success is announced with the confirmed count.
  4. editor save failures: failures inside one second are ONE alert; a blur save on an
     already-failed row is quiet, an Enter / Retry save is not; a recovered save says so.
  5. editor save order: outcomes apply in request order (a superseded save's late
     answer is ignored), a row edited while its save was in flight stays unsaved,
     approve refuses text that differs from the textarea, only `ok: true` is a save.
  6. editor shortcut: the Alt+A predicate never matches a keystroke that types a
     letter (Mac Option+A, Windows AltGr); Windows / Linux Alt+A and Mac Ctrl+Shift+A
     match.

Every source is read CRLF-safe (the maintainer's checkout has core.autocrlf=true).
"""
# ruff: noqa: E501
# (The node scripts are JavaScript data inside Python strings; wrapping them hurts review.)

import json
import os
import re
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TPL = os.path.join(_ROOT, "app", "templates")


def _read(*parts: str) -> str:
    with open(os.path.join(_ROOT, *parts), "rb") as fh:
        return fh.read().decode("utf-8").replace("\r\n", "\n")


def _node(script: str) -> str:
    node = shutil.which("node")
    assert node, "verify_a11y_live_status requires Node (it runs the real page scripts)"
    result = subprocess.run(
        [node, "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-4000:])
    return result.stdout


# A fake clock shared by every node script: setTimeout/clearTimeout/Date.now under the
# test's control, tick(ms) runs due timers in order.
_CLOCK = """
const assert = require('assert');
let now = 1000000, seq = 0;
const timers = new Map();
Date = { now: () => now };
const setTimeout = (fn, ms) => { timers.set(++seq, { fn, at: now + (ms || 0) }); return seq; };
const clearTimeout = (id) => { timers.delete(id); };
const setInterval = () => 0, clearInterval = () => {};
function tick(ms) {
  const end = now + ms;
  for (;;) {
    let next = null;
    for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
    if (!next) break;
    timers.delete(next[0]); now = next[1].at; next[1].fn();
  }
  now = end;
}
// A live region that records every non-empty text it is given (writes), and EVERY
// assignment including the clears (log), with the time.
function region(name) {
  const el = { name, writes: [], log: [], _t: '' };
  Object.defineProperty(el, 'textContent', {
    get() { return this._t; },
    set(v) {
      this._t = String(v);
      this.log.push([now, this._t]);
      if (this._t) this.writes.push([now, this._t]);
    },
  });
  return el;
}
// Clear-then-set: every non-empty write directly follows a clear of the region, so a
// repeated message is a change a screen reader reads again.
function assertClearThenSet(el) {
  el.log.forEach((w, i) => {
    if (w[1]) assert(i > 0 && el.log[i - 1][1] === '', el.name + ' set without a clear first: ' + JSON.stringify(el.log.slice(Math.max(0, i - 2), i + 1)));
  });
}
"""


def _announce_js() -> str:
    return _read("app", "static", "announce.js")


def test_announcer() -> None:
    _node(
        _CLOCK
        + _announce_js()
        + r"""
const polite = region('polite'), urgent = region('alert');
const a = makeAnnouncer(polite, urgent, { minGapMs: 10000, settleMs: 2000 });
// First message: region cleared, then set ~100 ms later.
a.say('first'); assert.equal(polite.textContent, ''); tick(100);
assert.equal(polite.textContent, 'first');
// Inside the gap: coalesce, last wins, one write when the gap ends.
a.say('old'); a.say('older'); a.say('latest');
tick(9800); assert.equal(polite.writes.length, 1, 'spoke inside the gap');
tick(200); assert.equal(polite.textContent, 'latest');
assert.equal(polite.writes.length, 2, 'more than one pending message was kept');
// The same text twice is announced twice (clear-then-set).
tick(10000); a.say('latest'); tick(100);
assert.equal(polite.writes.length, 3, 'a repeated message was swallowed');
// Assertive bypasses the gap and drops the pending polite message.
a.say('pending polite'); a.alert('Save failed'); tick(100);
assert.equal(urgent.textContent, 'Save failed');
// De-dup: the same alert inside 3 s is spoken once, again after 3 s.
const before = urgent.writes.length;
a.alert('Save failed'); tick(100);
assert.equal(urgent.writes.length, before, 'duplicate alert inside 3 s');
tick(3000); a.alert('Save failed'); tick(100);
assert.equal(urgent.writes.length, before + 1, 'alert after 3 s swallowed');
tick(20000);
assert(!polite.writes.some(w => w[1] === 'pending polite'), 'stale polite spoken after an alert');
// clearAlert empties the visible alert and forgets the de-dup.
a.clearAlert(); assert.equal(urgent.textContent, '');
a.alert('Save failed'); tick(100); assert.equal(urgent.textContent, 'Save failed');
// Settle window: a burst is held and only the last message is spoken, once.
tick(20000);
const n = polite.writes.length;
a.settle(); assert(a.settling());
for (let i = 1; i <= 20; i++) a.say('Run in progress: ' + i + ' of 20 steps done.');
tick(1999); assert.equal(polite.writes.length, n, 'spoke inside the settle window');
// An alert inside the window does not end it.
a.alert('Connection hiccup'); a.say('Run in progress: 20 of 20 steps done.');
tick(1); assert(!a.settling());
tick(100);
assert.equal(polite.writes.length, n + 1, 'a replay burst was not ONE message');
assert.equal(polite.textContent, 'Run in progress: 20 of 20 steps done.');
// flush speaks now, drops the pending one and ends a settle window.
a.say('queued'); a.settle(); a.flush('Run completed.'); assert(!a.settling());
tick(100); assert.equal(polite.textContent, 'Run completed.');
tick(30000); assert(!polite.writes.some(w => w[1] === 'queued'));
// flush with NO settle window: a message pending in the gap is dropped, never spoken
// after the terminal one.
a.say('Step 3 of 3 finished: Review.'); tick(100);
a.say('Step 3: Review, 75% done.');
a.flush('Run completed.');
tick(100); assert.equal(polite.textContent, 'Run completed.');
tick(30000);
assert.equal(polite.writes.at(-1)[1], 'Run completed.', 'a stale pending message was spoken after flush');
assert(!polite.writes.some(w => w[1] === 'Step 3: Review, 75% done.'), 'a pending message outlived flush');
// keep: an OUTCOME waiting in the gap is not dropped by an alert (it is spoken when the
// gap ends, after the alert) nor replaced by a later plain say(). Outcomes coalesce last
// wins among themselves: what waits is the LATEST outcome plus the LATEST plain message
// said after it; an alert drops only that plain one.
a.say('Step 1 of 2 finished.'); tick(100);
a.say('Loading alternates for segment 3.');
a.say('Segment 4 approved.', { keep: true });
a.say('Loading alternates for segment 2.');
a.alert('Segment 2 was not saved.'); tick(100);
assert.equal(urgent.textContent, 'Segment 2 was not saved.');
a.say('Segment 4 approved.', { keep: true });
a.say('Segment 5 approved.', { keep: true });
a.say('3 alternates.'); a.say('No unconfirmed segments left.');
tick(9700); assert.equal(polite.textContent, 'Step 1 of 2 finished.', 'kept message spoke inside the gap');
tick(200);
assert.equal(polite.textContent, 'Segment 5 approved. No unconfirmed segments left.',
  'kept outcome lost or messages piled up: ' + JSON.stringify(polite.writes.slice(-2)));
// Once spoken the mark is gone: a plain pending message is dropped by an alert again,
// and flush / cancel still drop a kept one.
a.say('Loading alternates.'); a.alert('Could not load alternates.'); tick(30000);
assert(!polite.writes.some(w => w[1] === 'Loading alternates.'), 'a plain pending message outlived an alert');
a.say('Opening a gap.'); tick(100);
a.say('Kept then flushed.', { keep: true }); a.flush('Run completed.'); tick(100);
a.say('Kept then cancelled.', { keep: true }); a.cancel(); tick(30000);
assert(!polite.writes.some(w => /^Kept then/.test(w[1])), 'flush or cancel left a kept message pending');
// N kept outcomes inside one gap (rapid Ctrl+Enter) are ONE write naming only the last:
// the gap never turns into a growing speech backlog.
a.say('Opening a burst gap.'); tick(100);
const burstFrom = polite.writes.length;
for (let i = 1; i <= 12; i++) { a.say('Segment ' + i + ' approved. ' + i + ' of 12 confirmed.', { keep: true }); tick(60); }
tick(20000);
const burst = polite.writes.slice(burstFrom).map(w => w[1]);
assert.deepEqual(burst, ['Segment 12 approved. 12 of 12 confirmed.'],
  'kept outcomes inside one gap were not one write naming only the last: ' + JSON.stringify(burst));
// Every write in this whole sequence cleared the region first (both regions).
assertClearThenSet(polite);
assertClearThenSet(urgent);
assert(polite.log.filter(w => w[1] === 'latest').length === 2 && polite.log.some((w, i) => w[1] === 'latest' && polite.log[i - 1][1] === ''));
// No regions (a page without them): nothing throws.
const quiet = makeAnnouncer(null, null, { minGapMs: 1000 });
quiet.say('x'); quiet.alert('y'); quiet.flush('z'); quiet.clearAlert(); tick(5000);
console.log('ok');
"""
    )
    print("  announcer: coalesce, 10 s gap, settle burst -> one, alert bypass + 3 s de-dup OK")


def _run_view_script() -> str:
    src = _read("app", "templates", "run_view.html")
    start = src.index('<script src="/static/sse.js')
    block = src[start:]
    opening = '<script nonce="{{ csp_nonce() }}">\n'
    block = block[block.index(opening) + len(opening) : block.index("</script>\n{% endif %}")]
    block = block.replace("{{ run.id if run else 'null' }}", "7").replace(
        "{{ run.id if run else '' }}", "7"
    )
    block = block.replace(
        "{% include '_html_escape.js' %}", _read("app", "templates", "_html_escape.js")
    )
    assert "{{" not in block and "{%" not in block, (
        "run_view script has new Jinja: extend this harness"
    )
    return block


def test_run_view_replay() -> None:
    _node(
        _CLOCK
        + _announce_js()
        + r"""
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, dataset: {}, style: {}, hidden: false, innerHTML: '',
    textContent: '', appendChild() {}, classList: { add() {}, remove() {} },
    querySelector: () => null, getAttribute: () => null, setAttribute() {}, remove() {} };
  return els[id];
}
els.runAnnounce = region('polite');
els.runAlert = region('alert');
const document = {
  visibilityState: 'visible',
  getElementById: (id) => el(id),
  createElement: () => ({ className: '', innerHTML: '', classList: { add() {} } }),
  addEventListener() {},
};
const window = { location: { href: '' } };
const fetch = () => Promise.reject(new Error('offline'));
const opens = [];
function connectSSE(url, opts) { opens.push(opts); return { close() {} }; }
"""
        + _run_view_script()
        + r"""
const polite = els.runAnnounce, urgent = els.runAlert;
const send = (ev) => opens[opens.length - 1].onMessage({ data: JSON.stringify(ev) });
const step = (i, extra) => send(Object.assign({ type: 'step', step_index: i, step_name: 'Step ' + (i + 1),
  step_type: 'translate', chunks: 1, output: 'x' }, extra || {}));
// 1. Page opened mid-run: connect, then the observer replays 20 recorded steps at once.
opens[0].onConnect();
send({ type: 'run_started', run_id: 7, steps: 24 });
for (let i = 0; i < 20; i++) step(i);
tick(2200);
assert.equal(polite.writes.length, 1, 'replay burst: ' + JSON.stringify(polite.writes));
assert.equal(polite.textContent, 'Run in progress: 20 of 24 steps done.');
// 2. A live step after the window: 1-based numbering, spoken (after the 10 s gap).
tick(10000);
step(20);
tick(100);
assert.equal(polite.textContent, 'Step 21 of 24 finished: Step 21.');
// A failed step is polite (a loop may recover it), not an alert.
tick(10000);
step(21, { error: 'judge below gate' });
tick(100);
assert.equal(polite.textContent, 'Step 22 of 24 failed: Step 22.');
assert.equal(urgent.writes.length, 0, 'a failed step raised an alert');
// 3. Progress every second for 3 minutes: the step change, 25/50/75 % crossings,
//    never two polite messages closer than 10 s.
for (let s = 0; s <= 180; s++) {
  send({ type: 'progress', run_id: 7, done: s, total: 180, unit: 'segments', step: 'Translate', step_index: 22, at: '' });
  tick(1000);
}
const texts = polite.writes.map(w => w[1]);
assert(texts.includes('Step 23: Translate, 0 of 180 segments.'), texts.join(' | '));
for (const pct of [25, 50, 75]) {
  assert(texts.some(t => t === 'Step 23: Translate, ' + pct + '% done.'), 'missing ' + pct + '%: ' + texts.join(' | '));
}
for (let i = 1; i < polite.writes.length; i++) {
  assert(polite.writes[i][0] - polite.writes[i - 1][0] >= 10000, 'polite messages closer than 10 s');
}
// 4. Observer ceiling: closed with a LIVE status reopens; the new replay is summarized.
const spoken = polite.writes.length;
send({ type: 'closed', run_id: 7, status: 'running' });
assert.equal(opens.length, 2, 'the ceiling close did not reopen');
tick(20000);
opens[1].onConnect();
send({ type: 'run_started', run_id: 7, steps: 24 });
for (let i = 0; i < 23; i++) step(i);
tick(2200);
assert.equal(polite.writes.length, spoken + 1, 'reopen replay was not one message');
assert.equal(polite.textContent, 'Run in progress: 23 of 24 steps done.');
assert(!polite.writes.some(w => /^Run started/.test(w[1]) && w[0] > 1000000 + 60000), 'Run started spoken on a reopen');
// 4b. A reopen whose replay carries NO recorded steps (the run is between steps): the
//     settle window closes with nothing but run_started in it, and "Run started" must
//     still not be said a second time.
const beforeEmpty = polite.writes.length;
send({ type: 'closed', run_id: 7, status: 'running' });
assert.equal(opens.length, 3, 'the second ceiling close did not reopen');
opens[2].onConnect();
send({ type: 'run_started', run_id: 7, steps: 24 });
tick(30000);
assert(!polite.writes.slice(beforeEmpty).some(w => /^Run started/.test(w[1])),
  'Run started spoken again on a reopen with no steps: ' + JSON.stringify(polite.writes.slice(beforeEmpty)));
// 5. The run fails: assertive, carrying the error text; the visible line shows it too.
send({ type: 'closed', run_id: 7, status: 'failed', error: 'Provider refused the request.' });
tick(100);
assert.equal(urgent.textContent, 'Run failed: Provider refused the request.');
assert(els.runStatus.textContent.startsWith('Run failed: Provider refused the request.'), els.runStatus.textContent);
// 6. Connection lost after retries: assertive.
opens[opens.length - 1].onGiveUp();
tick(100);
assert.equal(urgent.textContent, 'Connection lost. Refresh the page to see the latest status.');
assertClearThenSet(polite);
assertClearThenSet(urgent);
console.log('ok');
"""
    )
    print(
        "  run view: 20-step replay -> 1 summary (page open and reopen), 10 s gap, milestones, alerts OK"
    )


_RUN_VIEW_TERMINAL = r"""
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, dataset: {}, style: {}, hidden: false, innerHTML: '',
    textContent: '', appendChild() {}, classList: { add() {}, remove() {} },
    querySelector: () => null, getAttribute: () => null, setAttribute() {}, remove() {} };
  return els[id];
}
els.runAnnounce = region('polite');
els.runAlert = region('alert');
const document = { visibilityState: 'visible', getElementById: (id) => el(id),
  createElement: () => ({ classList: { add() {} } }), addEventListener() {} };
const window = { location: { href: '' } };
const fetch = () => Promise.reject(new Error('offline'));
const opens = [];
function connectSSE(url, opts) { opens.push(opts); return { close() {} }; }
"""


def test_run_view_terminal_states() -> None:
    """done, cancelled and paused are flushed at once (inside the 10 s gap), and a live
    step still waiting in the gap is DROPPED: nothing stale is said after them."""
    for ending, spoken in (
        ("{ type: 'done', run_id: 7, text: 'ok' }", "Run completed."),
        ("{ type: 'closed', run_id: 7, status: 'cancelled' }", "Run cancelled."),
        ("{ type: 'closed', run_id: 7, status: 'paused' }", "Paused for human review."),
    ):
        _node(
            _CLOCK
            + _announce_js()
            + _RUN_VIEW_TERMINAL
            + _run_view_script()
            + f"\nconst ENDING = {ending}, SPOKEN = {json.dumps(spoken)};\n"
            + r"""
const send = (ev) => opens[0].onMessage({ data: JSON.stringify(ev) });
const polite = els.runAnnounce;
opens[0].onConnect();
send({ type: 'run_started', run_id: 7, steps: 3 });
tick(2200);
assert.equal(polite.textContent, 'Run started, 3 steps.');
// A live step inside the 10 s gap waits (pending), then the run ends.
tick(1000);
send({ type: 'step', step_index: 0, step_name: 'Translate', step_type: 'translate', chunks: 1, output: 'x' });
tick(100);
assert.equal(polite.textContent, 'Run started, 3 steps.', 'the gap was not kept');
send(ENDING);
tick(100);
assert.equal(polite.textContent, SPOKEN, 'not flushed at once');
if (ENDING.type === 'done') send({ type: 'closed', run_id: 7, status: 'completed' });
tick(30000);
assert.equal(polite.writes.at(-1)[1], SPOKEN, 'stale message after the ending: ' + JSON.stringify(polite.writes));
assert(!polite.writes.some(w => /^Step 1 of 3/.test(w[1])), 'the pending step was spoken: ' + JSON.stringify(polite.writes));
assert.equal(els.runAlert.writes.length, 0);
assertClearThenSet(polite);
console.log('ok');
"""
        )
    print("  run view: completed / cancelled / paused flushed at once, pending step dropped OK")


def _editor_functions(*names: str) -> str:
    src = _read("app", "templates", "editor.html")
    out = []
    # ux/review-editor: save/approve now share the shipped human-label formatter.
    # #623: the tag-loss question is asked by the in-page dialog helper.
    names = ("statusLabel", "askInPage", *names)
    for name in names:
        for prefix in ("async function ", "function "):
            marker = prefix + name + "("
            if marker in src:
                body = src.split(marker, 1)[1].split("\n}\n", 1)[0]
                out.append(marker + body + "\n}\n")
                break
        else:
            raise AssertionError(f"editor.html: function {name} not found")
    # #617 item 6: approve() reports propagated repeats through showPropagated (redraws other
    # rows; covered by verify_repeat_propagation_undo_ui_617). These harnesses have one row.
    if "approve" in names:
        out.append("function showPropagated() { return ''; }")
    return "\n".join(out)


# Fakes for the Editor harness: one row (segment id 12, displayed as number 12 of 3).
_EDITOR_FAKES = r"""
let state = 'failed', mode = 'ok', posts = [], rowStates = [];
// #623: the tag-loss question is the in-page <dialog> (askInPage). dialogMode is what the
// "user" does with it: yes | no | escape | unsupported | open. confirmCalls must stay empty:
// no window.confirm any more.
let dialogMode = 'no';
const confirmCalls = [], asked = [], askNodes = {};
const askListeners = { edAskYes: [], edAskNo: [] };
function askNode(id) {
  if (!askNodes[id]) askNodes[id] = { id, textContent: '', isConnected: true,
    focus() { this.focused = true; },
    addEventListener(t, fn) { if (t === 'click') askListeners[id].push(fn); },
    removeEventListener(t, fn) { const l = askListeners[id], i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); } };
  return askNodes[id];
}
const opener = { isConnected: true, focus() { this.focused = true; } };
const dlg = { open: false, closeFns: [],
  addEventListener(t, fn) { if (t === 'close') this.closeFns.push(fn); },
  showModal() {
    this.open = true;
    asked.push({ title: askNode('edAskTitle').textContent, message: askNode('edAskMsg').textContent,
      yes: askNode('edAskYes').textContent, no: askNode('edAskNo').textContent });
    setImmediate(() => {
      if (dialogMode === 'yes') askListeners.edAskYes.slice().forEach((f) => f());
      else if (dialogMode === 'no') askListeners.edAskNo.slice().forEach((f) => f());
      else this.close();  // Escape closes with no button pressed
    });
  },
  close() { this.open = false; this.closeFns.splice(0).forEach((f) => f()); } };
const approvedIds = new Set(), TOTAL_SEGMENTS = 3;
const row = { classList: { contains: (c) => (c === 'save-failed' && state === 'failed') ||
  (c === 'dirty' && state !== '') } };
const tas = {};
function ta(id) {
  if (!tas[id]) tas[id] = { value: 'my edit ' + id, dataset: { number: String(id) }, closest: () => row };
  return tas[id];
}
// `dataset` like a real element: sendSave drops data-tm-fill on a changed save (TM label fix).
const chip = { textContent: 'draft', className: 'chip st-draft', classList: { add() {} }, dataset: {} };
const count = { textContent: '0' };
const LIVE = {};  // live regions (edStatus / edAlert) for a harness using the real announcer
const document = { activeElement: opener, getElementById: (id) => {
  if (LIVE[id]) return LIVE[id];
  if (id === 'edAsk') return dialogMode === 'unsupported' ? { open: false } : (dialogMode === 'open' ? { open: true, showModal() {} } : dlg);
  if (id === 'edAskTitle' || id === 'edAskMsg' || id === 'edAskYes' || id === 'edAskNo') return askNode(id);
  if (id === 'approvedCount') return count;
  if (id.startsWith('tgt-')) return ta(Number(id.slice(4)));
  return chip;
} };
const window = { confirm: (m) => { confirmCalls.push(m); return false; }, CSRF_TOKEN: 't' };  // # pregate: allow-secret fake CSRF value for a node stub
const elapsed = () => 0, focusAt = {};
const setRowState = (id, next) => { state = next; rowStates.push(next); };
const setStatusClass = () => {}, renderQA = () => {};
const bumpProgress = () => { count.textContent = String(Number(count.textContent) + 1); };
const polite = [], alerts = [];
let cleared = 0;
const edAnnouncer = { say: (s) => polite.push(s), alert: (s) => alerts.push(s), clearAlert: () => { cleared++; } };
// mode 'manual': each save waits in `inflight` until the test settles it (ok / fail),
// so responses can arrive in any order.
const inflight = [];
const SAVED = { ok: true, status: 'edited', qa: [] };
const fetch = async (url, opts) => {
  posts.push(url);
  if (url.endsWith('/approve')) {
    if (mode === 'approve-network') throw new Error('offline');
    return { ok: mode !== 'approve-http', status: 503,
      json: async () => ({ ok: true, status: 'approved', tm_written: false }) };
  }
  if (mode === 'manual') {
    const text = opts.body.get('target_text');
    return new Promise((resolve, reject) => inflight.push({
      text,
      ok: (data) => resolve({ ok: true, status: 200, json: async () => data || SAVED }),
      fail: () => reject(new Error('offline')),
    }));
  }
  if (mode === 'save-network') throw new Error('offline');
  return { ok: mode !== 'save-http', status: 500,
    json: async () => (mode === 'decline' ? { ok: false, needs_confirm: true, message: 'tags?' }
      : (mode === 'tagloss' && opts.body.get('confirm') !== '1') ? { ok: false, needs_confirm: true, message: 'Tags will be lost. Save anyway?' }
      : mode === 'save-not-ok' ? { ok: false } : SAVED) };
};
const settled = () => new Promise((r) => setImmediate(r));
const saveSeq = {}, savesInFlight = {};
"""


def test_failed_save_never_approves() -> None:
    _node(
        _CLOCK
        + _EDITOR_FAKES
        + _editor_functions("segmentNumber", "saveFailure", "save", "sendSave", "approve")
        + r"""
const failedSaves = new Set();
let failureTimer = null;
(async () => {
  for (const failing of ['save-http', 'save-network', 'decline']) {
    mode = failing; state = 'failed'; posts = []; rowStates = []; alerts.length = 0;
    const result = await approve(12);
    assert.equal(result, null, failing + ': approve returned a result');
    assert(!posts.some(u => u.endsWith('/approve')), failing + ': approve was POSTed: ' + posts);
    assert.equal(state, 'failed', failing + ': the failure marker was cleared');
    assert(!rowStates.includes(''), failing + ': row state was cleared');
    assert.equal(ta(12).value, 'my edit 12', failing + ': the edit was lost');
    assert.deepEqual(alerts, ['Not approved: the edit was not saved.'], failing + ': ' + alerts);
    tick(2000);
    assert.deepEqual(alerts, ['Not approved: the edit was not saved.'], failing + ': a second alert');
    assert.equal(count.textContent, '0');
  }
  // An unsaved (dirty, not failed) row whose tag confirm is declined stays unsaved.
  mode = 'decline'; state = 'dirty'; posts = [];
  assert.equal(await approve(12), null);
  assert.equal(state, 'dirty');
  assert(!posts.some(u => u.endsWith('/approve')));
  // #623: every decline above went through the in-page dialog, never window.confirm.
  assert.equal(confirmCalls.length, 0, 'window.confirm was called');
  assert(asked.length === 2 && asked.every(a => a.message === 'tags?'), JSON.stringify(asked));
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    print(
        "  editor: save failed / network / declined -> approve never POSTed, marker kept, told OK"
    )


def test_tag_loss_dialog() -> None:
    """#623: the editor's tag-loss question is the in-page dialog (askInPage), fail closed."""
    send = _editor_functions("sendSave")
    assert "window.confirm(" not in send, "sendSave must not call window.confirm"
    for mode, saved in (
        ("yes", True),
        ("no", False),
        ("escape", False),
        ("unsupported", False),
        ("open", False),
    ):
        _node(
            _CLOCK
            + _EDITOR_FAKES
            + _editor_functions("segmentNumber", "saveFailure", "save", "sendSave")
            + f"""
const failedSaves = new Set();
let failureTimer = null;
(async () => {{
  mode = 'tagloss'; state = 'dirty'; dialogMode = {json.dumps(mode)};
  const result = await save(12, 0, false, true);
  const confirmedPosts = posts.length;
  if ({json.dumps(saved)}) {{
    assert(result && result.ok === true, 'yes must save');
    assert.equal(confirmedPosts, 2, 'yes re-POSTs once with confirm=1');
    assert.equal(rowStates.at(-1), '');
  }} else {{
    assert.equal(result, null, 'not confirmed must save nothing');
    assert.equal(confirmedPosts, 1, 'no second POST');
    assert.equal(rowStates.at(-1), 'dirty', 'row must stay unsaved');
  }}
  assert.equal(confirmCalls.length, 0, 'window.confirm was called');
  if (dialogMode !== 'unsupported' && dialogMode !== 'open') {{
    assert.equal(asked.length, 1);
    assert.equal(asked[0].message, 'Tags will be lost. Save anyway?');
    assert.equal(asked[0].yes, 'Save anyway');
    assert.equal(asked[0].no, 'Do not save');
    assert(!dlg.open, 'dialog left open');
    assert(opener.focused, 'focus was not returned');
    assert.equal(askListeners.edAskYes.length + askListeners.edAskNo.length, 0, 'listeners leaked');
    assert(askNode('edAskNo').focused, 'focus must start on the safe button');
  }}
  console.log('ok');
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        )
    print(
        "  editor: tag-loss confirm is the in-page dialog; no / Escape / unsupported fail closed OK"
    )


def test_approve_outcomes() -> None:
    _node(
        _CLOCK
        + _EDITOR_FAKES
        + _editor_functions("segmentNumber", "saveFailure", "save", "sendSave", "approve")
        + r"""
const failedSaves = new Set();
let failureTimer = null;
(async () => {
  for (const failing of ['approve-http', 'approve-network']) {
    mode = failing; state = ''; alerts.length = 0;
    assert.equal(await approve(12), null);
    assert.deepEqual(alerts, ['Segment 12 was not approved. Try again.'], failing + ': ' + alerts);
    assert.equal(count.textContent, '0', failing + ': counted');
    assert.equal(chip.textContent, 'Edited', failing + ': chip claims approved');
  }
  mode = 'ok'; state = ''; polite.length = 0;
  const before = cleared;
  chip.dataset.tmFill = '1';
  const data = await approve(12);
  assert.equal(data.status, 'approved');
  assert(!('tmFill' in chip.dataset), 'a changed save must drop the TM fill marker');
  assert.equal(count.textContent, '1');
  assert.equal(polite.at(-1), 'Segment 12 approved. 1 of 3 confirmed.');
  assert(cleared > before, 'a stale alert stayed visible after a success');
  await approve(12);
  assert.equal(count.textContent, '1', 'a re-approve counted twice');
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    print("  editor: failed approve request visible + assertive; success says n of m OK")


def test_save_failures() -> None:
    _node(
        _CLOCK
        + _EDITOR_FAKES
        + _editor_functions("segmentNumber", "saveFailure", "save", "sendSave")
        + r"""
const failedSaves = new Set();
let failureTimer = null;
(async () => {
  // Three rows fail inside one second (a network drop mid-burst): ONE alert.
  mode = 'save-network'; state = '';
  await save(1); state = ''; await save(2); state = ''; await save(3);
  assert.equal(alerts.length, 0, 'alert before the 1 s window closed');
  tick(1000);
  assert.deepEqual(alerts, ['3 segments were not saved. Your text is kept; press Enter in each to retry.']);
  // A blur save on a row that already failed is quiet; an explicit one is not.
  alerts.length = 0; state = 'failed';
  await save(12); tick(1000);
  assert.equal(alerts.length, 0, 'a blur save on a failed row spoke');
  await save(12, 0, false, true); tick(1000);
  assert.deepEqual(alerts, ['Segment 12 was not saved. Your text is kept; press Enter to retry.']);
  // The row recovers: polite, and the visible alert is cleared.
  mode = 'ok'; state = 'failed'; polite.length = 0;
  const before = cleared;
  assert((await save(12)).status === 'edited');
  assert.equal(state, '');
  assert.deepEqual(polite, ['Segment 12 saved.']);
  assert(cleared > before);
  // A routine save is silent.
  polite.length = 0; await save(12);
  assert.equal(polite.length, 0, 'a routine save spoke');
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    print("  editor: failures aggregated in 1 s, failed-row blur quiet, recovery announced OK")


def test_approval_heard_despite_other_row_alert() -> None:
    """The a11y browser flake of 2026-09-24, as a product bug: the REAL announcer and the
    REAL save / approve code. Row 4's blur save fails (its "not saved" alert is due one
    second later); row 12 recovers ("Segment 12 saved." opens the 1 s polite gap); the
    approve button then approves row 12, whose announcement waits out that gap; row 4's
    alert fires inside the wait. Before the fix the alert dropped the approval: it was
    never spoken and the visible status stayed "Segment 12 saved.". Also Ctrl+Enter on
    the last unconfirmed row: "No unconfirmed segments left" joins the approval instead
    of replacing it."""
    ed = _read("app", "templates", "editor.html")
    real = re.search(r"^const edAnnouncer = makeAnnouncer\(.*\);$", ed, re.M)
    assert real, "editor.html: the edAnnouncer construction line moved; extend this harness"
    stub = "const edAnnouncer = { say: (s) => polite.push(s), alert: (s) => alerts.push(s), clearAlert: () => { cleared++; } };"
    assert stub in _EDITOR_FAKES
    fakes = _EDITOR_FAKES.replace(
        stub, "LIVE.edStatus = region('polite'); LIVE.edAlert = region('alert');\n" + real.group(0)
    )
    _node(
        _CLOCK
        + _announce_js()
        + fakes
        + _editor_functions(
            "segmentNumber", "saveFailure", "save", "sendSave", "approve", "focusNextUnconfirmed"
        )
        + r"""
const failedSaves = new Set();
let failureTimer = null;
const textareas = () => [];  // no row left after this one: nothing unconfirmed
const status = LIVE.edStatus, urgent = LIVE.edAlert;
const ROW4 = 'Segment 4 was not saved. Your text is kept; press Enter to retry.';
const APPROVED = 'Segment 12 approved. 1 of 3 confirmed.';
(async () => {
  const t0 = Date.now();
  // Row 4's blur save fails: its alert is due at t0 + 1 s.
  mode = 'save-network'; state = 'dirty';
  assert.equal(await save(4), null);
  // t0 + 300 ms: row 12's Retry succeeds, said politely (the 1 s gap starts).
  tick(300);
  mode = 'ok'; state = 'failed';
  assert(await save(12, 0, false, true));
  tick(100);
  assert.equal(status.textContent, 'Segment 12 saved.');
  // t0 + 500 ms: the approve button. Approved; the message waits for the gap.
  tick(100);
  state = '';
  assert.equal((await approve(12)).status, 'approved');
  assert.equal(count.textContent, '1');
  // t0 + 1 s: row 4's alert, inside that wait.
  tick(600);
  assert.equal(urgent.textContent, ROW4);
  assert.equal(status.textContent, 'Segment 12 saved.', 'the approval was spoken inside the 1 s gap');
  tick(2000);
  assert.equal(status.textContent, APPROVED,
    "another row's alert dropped the approval: " + JSON.stringify(status.writes));
  const w = status.writes;
  assert(w.at(-1)[0] - w.at(-2)[0] >= 1000, 'the polite gap was not kept: ' + JSON.stringify(w));
  assert.equal(urgent.textContent, ROW4, 'the other row\'s alert was cleared');
  // Ctrl+Enter on the last unconfirmed row, again inside a gap: the approval and
  // "No unconfirmed segments left" are ONE message, neither replaces the other.
  tick(5000);
  mode = 'ok'; state = 'failed';
  assert(await save(12, 0, false, true));
  tick(200);
  state = '';
  const again = await approve(12);
  assert(again && !focusNextUnconfirmed(0));
  tick(2000);
  assert.equal(status.textContent, APPROVED + ' No unconfirmed segments left on this page.',
    'Ctrl+Enter on the last row lost a message: ' + JSON.stringify(status.writes.slice(-2)));
  assert.equal(count.textContent, '1', 'a re-approve counted twice');
  assertClearThenSet(status);
  assertClearThenSet(urgent);
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    print("  editor: an approval waiting out the gap survives another row's alert OK")


def test_save_order_and_ok() -> None:
    """Gate round 1, findings 4 and 5: save outcomes are applied in REQUEST order (a
    superseded save's late answer is ignored), a row whose text changed while its save
    was in flight stays unsaved, approve never approves text that differs from the
    textarea, and only `ok: true` counts as saved."""
    _node(
        _CLOCK
        + _EDITOR_FAKES
        + _editor_functions("segmentNumber", "saveFailure", "save", "sendSave", "approve")
        + r"""
const failedSaves = new Set();
let failureTimer = null;
(async () => {
  // (a) A blur save is in flight; the linguist types on and presses Ctrl+Enter: the
  //     newer save and the approve succeed, THEN the old blur save fails. The approved
  //     row must not turn "Save failed" and nothing may be announced.
  mode = 'manual'; state = 'dirty'; alerts.length = 0; posts = []; inflight.length = 0;
  ta(12).value = 'first';
  const blur = save(12);
  ta(12).value = 'first and more';
  const approving = approve(12);
  await settled();
  assert.equal(inflight.length, 2);
  assert.equal(inflight[1].text, 'first and more');
  inflight[1].ok();
  const approved = await approving;
  assert(approved && approved.status === 'approved', 'the approve did not go through');
  inflight[0].fail();
  assert.equal(await blur, null, 'a superseded save returned a result');
  tick(2000);
  assert.equal(state, '', 'a late failure of an older save marked the approved row: ' + state);
  assert.deepEqual(alerts, [], 'a superseded failure was announced: ' + alerts);
  assert.equal(chip.textContent, 'Approved', 'a superseded save rewrote the chip');
  // (b) The NEWER save (with the extra text) fails, then the older one succeeds: the
  //     failure stays, the older answer changes nothing.
  state = 'dirty'; alerts.length = 0; inflight.length = 0; chip.textContent = 'draft';
  ta(12).value = 'one';
  const older = save(12);
  ta(12).value = 'one two';
  const newer = save(12, 0, false, true);
  await settled();
  inflight[1].fail();
  assert.equal(await newer, null);
  assert.equal(state, 'failed');
  inflight[0].ok();
  assert.equal(await older, null, 'an older save claimed the row after a newer one');
  assert.equal(state, 'failed', 'an older success cleared the newer failure');
  assert.equal(chip.textContent, 'draft', 'an older success rewrote the chip');
  tick(2000);
  assert.deepEqual(alerts, ['Segment 12 was not saved. Your text is kept; press Enter to retry.']);
  // (c) The text changes while its save is in flight (no newer save yet): saved, but
  //     the row stays UNSAVED because the textarea no longer matches the server.
  state = 'dirty'; inflight.length = 0;
  ta(12).value = 'three';
  const typing = save(12);
  await settled();
  ta(12).value = 'three and four';
  inflight[0].ok();
  assert(await typing, 'the save itself succeeded');
  assert.equal(state, 'dirty', 'row shown as saved although its text differs from what was saved');
  // (d) Approve while typing: the save lands, but the textarea moved on, so approve
  //     does not approve the older text.
  state = 'dirty'; inflight.length = 0; alerts.length = 0; posts = [];
  ta(12).value = 'five';
  const ap = approve(12);
  await settled();
  ta(12).value = 'five six';
  inflight[0].ok();
  assert.equal(await ap, null, 'approved text that differs from the textarea');
  assert(!posts.some(u => u.endsWith('/approve')), 'approve POSTed for stale text');
  assert.deepEqual(alerts, ['Not approved: the edit was not saved.']);
  assert.equal(state, 'dirty');
  // (e) A failure and then a recovery inside the 1 s window: the stale "not saved"
  //     alert is never spoken.
  mode = 'save-network'; state = 'dirty'; alerts.length = 0; polite.length = 0;
  await save(12);
  assert.equal(state, 'failed');
  mode = 'ok';
  assert(await save(12, 0, false, true));
  tick(2000);
  assert.deepEqual(alerts, [], 'a recovered failure was still announced: ' + alerts);
  assert.deepEqual(polite, ['Segment 12 saved.']);
  // (e2) Enter on a row that already failed (the retry), then focus moves on and the
  //      textarea's blur save fires with the SAME text: the blur joins the Enter save
  //      (one request), so the retry's failure is still announced.
  mode = 'manual'; state = 'failed'; alerts.length = 0; inflight.length = 0;
  ta(12).value = 'retry me';
  const enter = save(12, 0, false, true);
  const blurred = save(12);
  await settled();
  assert.equal(inflight.length, 1, 'a blur save with the same text sent a second request');
  inflight[0].fail();
  assert.equal(await enter, null);
  assert.equal(await blurred, null);
  tick(2000);
  assert.deepEqual(alerts, ['Segment 12 was not saved. Your text is kept; press Enter to retry.'],
    'the retry failure was not announced: ' + alerts);
  assert.equal(state, 'failed');
  //      ...and when the blur carries NEWER text it is a real save of its own.
  state = 'dirty'; inflight.length = 0;
  ta(12).value = 'a';
  const first = save(12, 0, false, true);
  ta(12).value = 'ab';
  const second = save(12);
  await settled();
  assert.equal(inflight.length, 2, 'a blur save with newer text was swallowed');
  inflight[1].ok(); inflight[0].ok();
  assert(await second);
  assert.equal(await first, null);
  assert.equal(state, '');
  // (f) Only ok === true is a save: a 200 {ok:false} is a failure, and approve stops.
  for (const failing of ['save-not-ok']) {
    mode = failing; state = ''; posts = []; alerts.length = 0; chip.textContent = 'draft';
    assert.equal(await save(12, 0, false, false), null, 'ok:false counted as saved');
    assert.equal(state, 'failed');
    assert.equal(chip.textContent, 'draft', 'ok:false rewrote the chip');
    state = 'dirty';
    assert.equal(await approve(12), null);
    assert(!posts.some(u => u.endsWith('/approve')), 'approve POSTed after ok:false');
    assert.equal(state, 'failed');
    assert.equal(alerts.at(-1), 'Not approved: the edit was not saved.');
  }
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    )
    print(
        "  editor: superseded saves ignored, typed-on row stays unsaved, stale approve refused,"
        " ok:false is a failure OK"
    )


def test_alt_a_never_types_over_a_letter() -> None:
    """Gate round 1, finding 1: the alternates shortcut never swallows a typed letter.
    Windows / Linux: Alt+A by the typed letter a/A with no Ctrl, Meta or AltGr. macOS:
    Option+A TYPES a letter (a-ring on US / German, a-ogonek on Polish Pro), so the shortcut there is
    Ctrl+Shift+A (types nothing, and not VoiceOver's Ctrl+Option), matched by the physical key."""
    _node(
        "const assert = require('assert');\n"
        + "let platform = 'Win32', uaPlatform = 'Windows';\n"
        + "const navigator = { get platform() { return platform; },"
        + " get userAgentData() { return uaPlatform === null ? undefined : { platform: uaPlatform }; } };\n"
        + _editor_functions("onMac", "isAltA")
        + r"""
const ev = (o) => Object.assign({ key: 'a', code: 'KeyA', altKey: false, ctrlKey: false, metaKey: false,
  shiftKey: false, getModifierState: (m) => m === 'AltGraph' && !!o.altGraph }, o);
const cases = [
  // [platform, uaPlatform, event, expected, label]
  ['Win32', 'Windows', { altKey: true }, true, 'Windows Alt+A'],
  ['Win32', 'Windows', { altKey: true, shiftKey: true, key: 'A' }, true, 'Windows Alt+Shift+A'],
  ['Win32', 'Windows', { altKey: true, code: 'KeyQ' }, true, 'Windows AZERTY Alt+A (letter a)'],
  ['Win32', 'Windows', { altKey: true, ctrlKey: true, key: '\u0105' }, false, 'Windows AltGr+A (Polish)'],
  ['Win32', 'Windows', { altKey: true, ctrlKey: true }, false, 'Windows Ctrl+Alt+A'],
  ['Win32', 'Windows', { altKey: true, key: '\u0105', altGraph: true }, false, 'AltGraph reported'],
  ['Win32', 'Windows', { altKey: true, key: '\u0105' }, false, 'Alt with a typed letter'],
  ['Win32', 'Windows', { altKey: true, metaKey: true }, false, 'Win+Alt+A'],
  ['Win32', 'Windows', { key: 'a' }, false, 'plain a'],
  ['Linux x86_64', 'Linux', { altKey: true }, true, 'Linux Alt+A'],
  ['Linux x86_64', 'Linux', { key: '\u0105', altGraph: true }, false, 'Linux AltGr+A'],
  ['MacIntel', 'macOS', { altKey: true, key: '\u00e5' }, false, 'Mac Option+A (US, \u00e5)'],
  ['MacIntel', 'macOS', { altKey: true, key: '\u0105' }, false, 'Mac Option+A (Polish Pro, \u0105)'],
  ['MacIntel', 'macOS', { altKey: true, key: 'a' }, false, 'Mac Option+A (any layout)'],
  ['MacIntel', 'macOS', { ctrlKey: true, shiftKey: true, key: 'A' }, true, 'Mac Ctrl+Shift+A'],
  ['MacIntel', 'macOS', { ctrlKey: true, shiftKey: true, key: '\u0001' }, true, 'Mac Ctrl+Shift+A reporting a control char'],
  ['MacIntel', 'macOS', { altKey: true, ctrlKey: true }, false, 'Mac Ctrl+Option+A (VoiceOver VO+A)'],
  ['MacIntel', 'macOS', { altKey: true, ctrlKey: true, shiftKey: true }, false, 'Mac Ctrl+Option+Shift+A (VoiceOver)'],
  ['MacIntel', 'macOS', { ctrlKey: true, shiftKey: true, code: 'KeyQ', key: 'Q' }, false, 'Mac Ctrl+Shift+Q'],
  ['MacIntel', 'macOS', { ctrlKey: true, shiftKey: true, metaKey: true }, false, 'Mac Cmd+Ctrl+Shift+A'],
  ['MacIntel', 'macOS', { ctrlKey: true }, false, 'Mac Ctrl+A'],
  ['MacIntel', 'macOS', { shiftKey: true, key: 'A' }, false, 'Mac Shift+A (a capital letter)'],
  ['MacIntel', null, { ctrlKey: true, shiftKey: true }, true, 'Mac without userAgentData'],
  ['MacIntel', null, { altKey: true, key: '\u00e5' }, false, 'Mac without userAgentData, Option+A'],
  ['iPad', null, { altKey: true, key: '\u00e5' }, false, 'iPad Option+A'],
];
for (const [p, ua, e, want, label] of cases) {
  platform = p; uaPlatform = ua;
  assert.equal(!!isAltA(ev(e)), want, label);
}
console.log('ok');
"""
    )
    print("  editor: Alt+A / Ctrl+Shift+A never match a keystroke that types a letter OK")


def test_static_contract() -> None:
    """The markup half the node checks rely on (C21 in verify_a11y_static covers the rest)."""
    ed = _read("app", "templates", "editor.html")
    rv = _read("app", "templates", "run_view.html")
    assert 'id="approvedCount">{{ approved_count }}</b> of {{ total_segments }} confirmed' in ed
    assert not re.search(r'id="approvedCount"[^>]*aria-live', ed), (
        "#approvedCount must stay non-live"
    )
    # The Editor never re-adds a real alert() for save/approve outcomes.
    approve = _editor_functions("approve")
    assert "window.alert" not in approve and not re.search(r"(?<![.\w])alert\(", approve)
    # run_view: announcer calls sit AFTER freezeProgress( in the closed branch.
    closed = rv.split("} else if (ev.type === 'closed') {", 1)[1]
    order = [
        closed.index(s)
        for s in ("STILL_LIVE_STATUSES", "reopenStream()", "return;", "freezeProgress(")
    ]
    assert order == sorted(order), order
    assert closed.index("freezeProgress(") < closed.index("announcer."), (
        "announcer before freezeProgress"
    )
    print("  static: approvedCount pin untouched and non-live, closed-branch order kept OK")


def main() -> None:
    tests = (
        test_static_contract,
        test_announcer,
        test_run_view_replay,
        test_run_view_terminal_states,
        test_failed_save_never_approves,
        test_approve_outcomes,
        test_tag_loss_dialog,
        test_save_failures,
        test_approval_heard_despite_other_row_alert,
        test_save_order_and_ok,
        test_alt_a_never_types_over_a_letter,
    )
    for test in tests:
        test()
    print(json.dumps({"checks": len(tests)}))
    print(f"A11Y LIVE STATUS ({len(tests)} checks) OK")


if __name__ == "__main__":
    sys.exit(main())
