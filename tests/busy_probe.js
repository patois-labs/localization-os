// Behaviour probe for app/static/busy.js -- the app-wide busy indicators
// (feat/busy-indicators). Runs the REAL file (no copy, no re-implementation) inside
// a minimal fake DOM with a fake clock, and drives form submits and the LocBusy
// API the way a browser would. Executed by tests/verify_busy_indicators.py.
//
// Form data is checked the way the browser builds it: the HTML spec constructs the
// entry list AFTER the submit event has been dispatched, from the form's elements
// (only the clicked submit control is included, disabled controls are skipped), so
// entries() below reads name/value at exactly that point.
//
// Usage: node tests/busy_probe.js [path/to/busy.js]
'use strict';
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const SRC = fs.readFileSync(process.argv[2]
  || path.join(__dirname, '..', 'app', 'static', 'busy.js'), 'utf8');

// --- fake clock ---
let now = 0; let seq = 0; const timers = new Map();
function setTimeout(fn, ms) { const id = ++seq; timers.set(id, { fn, at: now + (ms || 0) }); return id; }
function clearTimeout(id) { timers.delete(id); }
function advance(ms) {
  const end = now + ms;
  for (;;) {
    let next = null;
    for (const [id, t] of timers) { if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t]; }
    if (!next) break;
    timers.delete(next[0]); now = next[1].at; next[1].fn();
  }
  now = end;
}

// --- minimal DOM ---
class ClassList {
  constructor() { this.s = new Set(); }
  add(c) { this.s.add(c); } remove(c) { this.s.delete(c); } contains(c) { return this.s.has(c); }
  toggle(c, on) {
    if (on === undefined) on = !this.s.has(c);
    if (on) this.s.add(c); else this.s.delete(c);
    return on;
  }
}
class TextNode {
  constructor(d) { this.nodeType = 3; this.data = d; this.parent = null; }
  get textContent() { return this.data; }
}
class El {
  constructor(tag, attrs, text) {
    this.tagName = tag.toUpperCase(); this.attrs = {}; this.classList = new ClassList();
    this.childNodes = []; this.style = { minWidth: '' }; this.disabled = false;
    this.parent = null; this.isConnected = true; this.nodeType = 1; this.elements = [];
    this.form = null; this._value = null; this.listeners = {}; this.open = false;
    for (const k of Object.keys(attrs || {})) this.setAttribute(k, attrs[k]);
    if (text !== undefined) this.appendChild(new TextNode(text));
  }
  closest(selector) {
    const attr = selector.slice(1, -1);
    return this.hasAttribute(attr) ? this : (this.parent ? this.parent.closest(selector) : null);
  }
  get dataset() {
    const data = {};
    for (const [key, value] of Object.entries(this.attrs)) {
      if (key.startsWith('data-')) data[key.slice(5)] = value;
    }
    return data;
  }
  get firstChild() { return this.childNodes[0] || null; }
  get parentNode() { return this.parent; }
  appendChild(n) {
    if (n.parent) n.parent.removeChild(n);
    this.childNodes.push(n); n.parent = this; return n;
  }
  removeChild(n) {
    const i = this.childNodes.indexOf(n);
    if (i >= 0) this.childNodes.splice(i, 1);
    n.parent = null; return n;
  }
  insertBefore(n, ref) {
    if (n.parent) n.parent.removeChild(n);
    const i = this.childNodes.indexOf(ref);
    this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, n); n.parent = this; return n;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  removeAttribute(k) { delete this.attrs[k]; }
  getBoundingClientRect() { return { width: 96.4 }; }
  addEventListener(t, fn, opts) { (this.listeners[t] = this.listeners[t] || []).push({ fn, once: !!(opts && opts.once) }); }
  removeEventListener(t, fn) {
    const l = this.listeners[t] || []; const i = l.findIndex(x => x.fn === fn);
    if (i >= 0) l.splice(i, 1);
  }
  fire(t) {
    for (const x of (this.listeners[t] || []).slice()) {
      if (x.once) this.removeEventListener(t, x.fn);
      x.fn({ type: t, target: this });
    }
  }
  focus() { document.activeElement = this; this.focused = true; }
  count(t) { return (this.listeners[t] || []).length; }
  // <dialog> (only present on elements created as 'dialog'): showModal records the question
  // and the "user" answers on a microtask, as dialogMode says (yes | no | escape).
  showModal() {
    this.open = true;
    const by = id => { const walk = n => { if (n.attrs && n.attrs.id === id) return n; for (const c of n.childNodes || []) { const r = walk(c); if (r) return r; } return null; }; return walk(this); };
    const q = { title: by('locAskTitle').textContent, message: by('locAskMsg').textContent };
    const btns = []; (function w(n) { if (n.tagName === 'BUTTON') btns.push(n); (n.childNodes || []).forEach(w); })(this);
    q.yes = btns[0].textContent; q.no = btns[1].textContent; q.yesBtn = btns[0]; q.noBtn = btns[1];
    dialogLog.push(q); confirmCalls.push(q.message);
    Promise.resolve().then(() => {
      if (dialogMode === 'yes') btns[0].fire('click');
      else if (dialogMode === 'no') btns[1].fire('click');
      else this.close();
    });
  }
  close() { if (!this.open) return; this.open = false; this.fire('close'); }
  get className() { return Array.from(this.classList.s).join(' '); }
  set className(v) { this.classList = new ClassList(); String(v).split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c)); }
  get type() { return this.attrs.type || (this.tagName === 'BUTTON' ? 'submit' : 'text'); }
  get value() { return this._value !== null ? this._value : (this.attrs.value || ''); }
  set value(v) { this._value = String(v); }
  get textContent() { return this.childNodes.map(n => n.textContent).join(''); }
  set textContent(v) { this.childNodes = []; if (v !== '') this.appendChild(new TextNode(String(v))); }
}
const dialogLog = []; let dialogMode = 'no';
function form(attrs, controls) {
  const f = new El('form', attrs);
  f.elements = controls;
  controls.forEach(c => { f.appendChild(c); c.form = f; });
  f.requestSubmitCalls = []; f.submitCalls = 0;
  f.requestSubmit = function (sub) { this.requestSubmitCalls.push(sub === undefined ? null : sub); };
  f.submit = function () { this.submitCalls += 1; };
  return f;
}
const isSubmit = el => (el.tagName === 'BUTTON' && el.type === 'submit')
  || (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'image'));
// The entry list the browser builds after the submit event (HTML "constructing the
// entry list"): skip disabled controls and every submit control but the submitter.
function entries(f, submitter) {
  const out = [];
  for (const el of f.elements) {
    if (el.disabled) continue;
    if (isSubmit(el) && el !== submitter) continue;
    const name = el.getAttribute('name');
    if (!name) continue;
    out.push(name + '=' + el.value);
  }
  return out.join('&');
}

const docListeners = { capture: [], bubble: [], keydown: [], click: [], clickCapture: [] };
const winListeners = {};
const bar = new El('div', { 'data-busy-bar': '', role: 'progressbar', 'aria-hidden': 'true' });
const labelsBlob = new El('script', { 'data-busy-labels': '' },
  JSON.stringify({ working: 'Arbeit...', done: 'Fertig', elapsed: '{s} s', bar: 'Bitte warten', resend: 'Nochmal senden?' }));
const document = {
  getElementById(id) { return id === 'busyAnnounce' ? busyStatus : null; },
  body: new El('body'), activeElement: null,
  addEventListener(type, fn, capture) {
    if (type === 'submit') docListeners[capture ? 'capture' : 'bubble'].push(fn);
    if (type === 'click') docListeners[capture ? 'clickCapture' : 'click'].push(fn);
    if (type === 'keydown') docListeners.keydown.push(fn);
  },
  querySelector(sel) {
    if (sel === '[data-busy-bar]') return bar;
    if (sel === 'script[data-busy-labels]') return labelsBlob;
    if (sel === 'dialog[open], .driver-popover') return overlayOpen ? overlayEl : null;
    return null;
  },
  createElement(tag) {
    const e = new El(tag);
    if (tag !== 'dialog') { e.showModal = undefined; e.close = undefined; }
    return e;
  },
};
let overlayOpen = false; const overlayEl = new El('dialog', { open: '' });
const confirmCalls = []; let nativeConfirms = 0;
const settle = () => new Promise(r => setImmediate(r));
const window = {
  addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
  confirm() { nativeConfirms += 1; return true; },  // the native dialog must never be used
};
const ctx = vm.createContext({ window, document, setTimeout, clearTimeout, Date: { now: () => now },
  Promise, JSON, Object, Array, Math, String, parseFloat, Error });
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'handlers.js'), 'utf8'), ctx);
const busyStatus = new El('p');
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'announce.js'), 'utf8'), ctx);
vm.runInContext(SRC, ctx, { filename: 'busy.js' });
const LocBusy = window.LocBusy;
assert(LocBusy && typeof LocBusy.start === 'function', 'window.LocBusy not exposed');

function submit(f, submitter, formHandler) {
  const ev = { type: 'submit', target: f, submitter: submitter || null, defaultPrevented: false,
    stopped: false, handlerRan: false,
    preventDefault() { this.defaultPrevented = true; },
    stopImmediatePropagation() { this.stopped = true; } };
  for (const fn of docListeners.capture) { fn(ev); if (ev.stopped) return ev; }
  if (formHandler) { ev.handlerRan = true; formHandler(ev); }
  for (const fn of docListeners.bubble) { fn(ev); if (ev.stopped) return ev; }
  return ev;
}
function key(k, prevented) {
  docListeners.keydown.forEach(fn => fn({ key: k, defaultPrevented: !!prevented }));
}
const barOn = () => bar.classList.contains('is-active') && bar.getAttribute('aria-hidden') === 'false';

(async () => {
// Delegated confirmation runs before busy activation, including Enter submits.
const poison = "' + (globalThis.__PWNED = true) + '";
const bConfirm = new El('button', { name: 'go', value: 'yes' }, 'Delete');
const fConfirm = form({ 'data-action-confirm': poison }, [bConfirm]);
// #623: the question is the in-page dialog (window.LocAsk). The submit is cancelled at
// once; only a yes sends the form again (requestSubmit with the same submitter, flagged
// so it is not asked twice). No, Escape and a browser without showModal send nothing.
for (const mode of ['no', 'escape']) {
  dialogMode = mode;
  const n0 = dialogLog.length;
  let confirmEvent = submit(fConfirm, bConfirm);
  assert(confirmEvent.defaultPrevented && !barOn() && !LocBusy.isBusy(fConfirm));
  await settle();
  assert(dialogLog.length === n0 + 1, mode + ': the dialog was not asked');
  assert(fConfirm.requestSubmitCalls.length === 0 && fConfirm.submitCalls === 0, mode + ': a refused confirm sent the form');
  assert(!barOn() && !bConfirm.hasAttribute('aria-busy') && bConfirm.textContent === 'Delete');
  assert(bConfirm.focused, mode + ': focus did not return to the submitter');
  assert(submit(fConfirm, null).defaultPrevented, 'Enter bypassed confirmation');
  await settle();
  assert(fConfirm.requestSubmitCalls.length === 0 && fConfirm.submitCalls === 0);
}
// Focus returns to where the person was: the field they pressed Enter in, not the button.
{
  const field = new El('input', { type: 'text' });
  field.focus();
  dialogMode = 'no';
  submit(fConfirm, bConfirm);
  await settle();
  assert(document.activeElement === field, 'focus did not return to the field the person was in');
}
const dlgEl = document.body.childNodes.find(n => n.tagName === 'DIALOG');
assert(dlgEl && dlgEl.attrs.role === 'alertdialog', 'the dialog needs role=alertdialog');
assert(dlgEl.attrs['aria-labelledby'] === 'locAskTitle' && dlgEl.attrs['aria-describedby'] === 'locAskMsg');
const last = dialogLog[dialogLog.length - 1];
assert(last.title === 'Please confirm' && last.yes === 'Confirm' && last.no === 'Cancel', [last.title, last.yes, last.no].join('|'));
assert(last.noBtn.focused, 'focus must start on the safe (no) button');
assert(last.yesBtn.count('click') === 0 && last.noBtn.count('click') === 0, 'dialog listeners leaked');
assert(!dlgEl.open, 'dialog left open');
dialogMode = 'yes';
let confirmEvent = submit(fConfirm, bConfirm);
assert(confirmEvent.defaultPrevented && !barOn(), 'a confirmed submit must still be cancelled until the dialog answers');
await settle();
assert(fConfirm.requestSubmitCalls.length === 1 && fConfirm.requestSubmitCalls[0] === bConfirm, 'yes did not re-send with the submitter');
confirmEvent = submit(fConfirm, bConfirm);  // the browser dispatches the re-send as a normal submit
assert(!confirmEvent.defaultPrevented && barOn() && LocBusy.isBusy(bConfirm));
assert(confirmCalls.every(message => message === poison) && !ctx.__PWNED);
const confirmCount = confirmCalls.length;
submit(fConfirm, bConfirm);
await settle();
assert(confirmCalls.length === confirmCount, 'busy double-click asked again');
LocBusy.reset();
// Fail closed when the dialog cannot be shown (no showModal) or one is already open.
{
  dialogMode = 'yes';
  const saved = dlgEl.showModal;
  dlgEl.showModal = undefined;
  const e1 = submit(fConfirm, bConfirm);
  await settle();
  assert(e1.defaultPrevented && fConfirm.requestSubmitCalls.length === 1, 'unsupported dialog must not confirm');
  dlgEl.showModal = saved;
  dlgEl.open = true;
  const e2 = submit(fConfirm, bConfirm);
  await settle();
  assert(e2.defaultPrevented && fConfirm.requestSubmitCalls.length === 1, 'an already-open dialog must not confirm');
  dlgEl.open = false;
  dialogMode = 'no';
}
// Capture listeners run first, as in a browser; stopImmediatePropagation ends dispatch.
function click(target) {
  const e = { type: 'click', target, defaultPrevented: false, stopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopImmediatePropagation() { this.stopped = true; } };
  for (const fn of docListeners.clickCapture.concat(docListeners.click)) {
    fn(e);
    if (e.stopped) break;
  }
  return e;
}
dialogMode = 'no';
const remove = new El('button', { 'data-action-confirm': poison });
let removeClicks = 0;
remove.click = () => { const ev = click(remove); removeClicks += 1; remove.lastClick = ev; };
assert(click(remove).defaultPrevented, 'button confirm cancel ignored');
await settle();
assert(removeClicks === 0, 'a refused button confirm acted');
dialogMode = 'yes';
assert(click(remove).defaultPrevented, 'a button confirm must be cancelled until the dialog answers');
await settle();
assert(removeClicks === 1 && !remove.lastClick.defaultPrevented, 'button confirm accept ignored');
assert(!click(bConfirm).defaultPrevented, 'form confirmed on click instead of submit');
// A submit button that carries the attribute itself (formaction buttons): yes re-sends
// through the form with that button as the submitter; no sends nothing.
const bFormAction = new El('button', { type: 'submit', formaction: '/x/delete', 'data-action-confirm': poison }, 'Remove');
const fFormAction = form({ method: 'post' }, [bFormAction]);
dialogMode = 'no';
assert(click(bFormAction).defaultPrevented);
await settle();
assert(fFormAction.requestSubmitCalls.length === 0);
dialogMode = 'yes';
assert(click(bFormAction).defaultPrevented);
await settle();
assert(fFormAction.requestSubmitCalls.length === 1 && fFormAction.requestSubmitCalls[0] === bFormAction, 'formaction button not re-sent');
// A browser without requestSubmit: a yes sends the form through LocBusy.submitForm (the busy
// state starts; a bare form.submit() would bypass it).
const bNoRs = new El('button', { name: 'go', value: 'yes' }, 'Remove');
const fNoRs = form({ 'data-action-confirm': poison }, [bNoRs]);
fNoRs.requestSubmit = undefined;
assert(submit(fNoRs, bNoRs).defaultPrevented);
await settle();
assert(fNoRs.submitCalls === 1 && LocBusy.isBusy(fNoRs), 'no-requestSubmit fallback did not send through LocBusy');
LocBusy.reset();
// #623: page scripts with their own submit listener (delete forms) call
// window.LocAsk.guard(e, message): cancelled at once, no / Escape / unsupported send nothing,
// only yes re-sends (the listener then lets the approved re-send through, asked once).
{
  const bDel = new El('button', { type: 'submit' }, 'Delete');
  const fDel = form({ method: 'post' }, [bDel]);
  const pageListener = ev => { window.LocAsk.guard(ev, 'Delete this thing?'); };
  const n0 = dialogLog.length;
  for (const mode of ['no', 'escape']) {
    dialogMode = mode;
    const ev = submit(fDel, bDel, pageListener);
    assert(ev.defaultPrevented, mode + ': the page listener submit was not cancelled');
    await settle();
    assert(fDel.requestSubmitCalls.length === 0 && fDel.submitCalls === 0, mode + ': guard sent the form');
  }
  assert(dialogLog.length === n0 + 2 && dialogLog[n0].message === 'Delete this thing?', 'guard did not ask');
  dialogMode = 'yes';
  assert(submit(fDel, bDel, pageListener).defaultPrevented, 'guard must cancel until answered');
  await settle();
  assert(fDel.requestSubmitCalls.length === 1 && fDel.requestSubmitCalls[0] === bDel, 'guard yes did not re-send');
  const again = submit(fDel, bDel, pageListener);  // the re-send: not cancelled, not asked again
  assert(!again.defaultPrevented && dialogLog.length === n0 + 3, 'approved re-send was cancelled or asked twice');
  const ev3 = submit(fDel, bDel, pageListener);    // a later submit asks again
  assert(ev3.defaultPrevented);
  await settle();
  dialogMode = 'no';
  LocBusy.reset();
}
for (const [action, fn, expected] of [
  ['translate', 'startTranslate', [7, poison]],
  ['test-engine', 'testEngine', [poison, 'button']],
  ['test-engine-row', 'testEngineRow', [7, 'button']],
  ['accept-tm', 'acceptTM', [7, 'button']],
  ['alternates', 'alternates', [7, 'button']],
  ['repair-tags', 'repairTags', [7, 'button']],
  ['approve', 'approveBtn', ['button', 7]],
  ['copy-source', 'copySrc', [7]],
  ['save-paragraph', 'savePara', [7, false, 'button']],
  ['revert-paragraph', 'revertPara', [7, 'button']],
]) {
  const button = new El('button', { 'data-action': action, 'data-id': '7',
    'data-locale': poison, 'data-provider': poison });
  const child = new El('span'); button.appendChild(child);
  let args;
  window[fn] = (...values) => { args = values.map(v => v === button ? 'button' : v); };
  click(child);
  assert.deepStrictEqual(args, expected, action + ' arguments changed');
}
assert(!ctx.__PWNED, 'data became executable');
confirmCalls.length = 0;
dialogMode = 'no';

// (a) named submit buttons: the clicked one goes busy, NOTHING is disabled, and the
//     entry list the browser builds after the event still carries status=closed.
const keep = new El('b', {}, 'Mark completed');
const bDone = new El('button', { type: 'submit', name: 'status', value: 'completed' });
bDone.appendChild(keep);
const bClose = new El('button',
  { type: 'submit', name: 'status', value: 'closed', 'data-busy-label': 'Closing...' }, 'Close');
const fStatus = form({ method: 'post', action: '/projects/1/status' }, [bDone, bClose]);
let ev = submit(fStatus, bClose);
assert(!ev.defaultPrevented, '(a) the first submit must go through');
assert(entries(fStatus, bClose) === 'status=closed', '(a) posted data changed: ' + entries(fStatus, bClose));
assert(bClose.disabled === false && bDone.disabled === false, '(a) a submit button was disabled');
assert(bClose.getAttribute('aria-busy') === 'true' && bClose.classList.contains('btn-busy'), '(a) not busy');
assert(bClose.classList.contains('busy-locked'), '(a) submitter not locked');
const label = () => bClose.childNodes[0];
const clock = () => bClose.childNodes[1];
assert(label().textContent === 'Closing...', '(a) data-busy-label not shown: ' + bClose.textContent);
assert(clock().classList.contains('busy-elapsed') && clock().textContent === '', '(a) no reserved clock span');
assert(bClose.style.minWidth === '97px', '(a) width not pinned: ' + bClose.style.minWidth);
assert(bDone.getAttribute('aria-disabled') === 'true' && bDone.classList.contains('busy-locked'), '(a) sibling');
assert(!bDone.classList.contains('btn-busy'), '(a) only the clicked button shows the spinner');
assert(fStatus.getAttribute('aria-busy') === 'true' && barOn(), '(a) form/bar not busy');
advance(2999); assert(clock().textContent === '', '(a) counter too early');
advance(1); assert(clock().textContent === '3 s', '(a) elapsed counter: ' + clock().textContent);
assert(label().textContent === 'Closing...', '(a) the counter must not rewrite the label span');
advance(9000); assert(clock().textContent === '12 s', '(a) counter not live: ' + clock().textContent);
// (b1) past the grace window a second submit is NOT sent by itself: it is cancelled
//      before the form's own handler and the user is asked; Cancel keeps waiting.
ev = submit(fStatus, bDone, () => { throw new Error('form handler must not run on a busy form'); });
assert(ev.defaultPrevented && ev.stopped && !ev.handlerRan, '(b1) second submit not blocked');
await settle();
assert(confirmCalls.length === 1 && confirmCalls[0] === 'Nochmal senden?', '(b1) no translated re-send prompt');
advance(0);
assert(fStatus.requestSubmitCalls.length === 0 && bClose.getAttribute('aria-busy') === 'true',
  '(b1) Cancel must keep the form busy and send nothing');

// (a2) an <input type=submit> keeps its value: it is serialized AFTER the event.
const note = new El('input', { type: 'text', name: 'note', value: 'x' });
const inApprove = new El('input',
  { type: 'submit', name: 'decision', value: 'approve', 'data-busy-label': 'Approving...' });
const inReject = new El('input', { type: 'submit', name: 'decision', value: 'reject' });
const fIn = form({ method: 'post' }, [note, inApprove, inReject]);
ev = submit(fIn, inApprove);
assert(!ev.defaultPrevented, '(a2) input submit blocked');
assert(entries(fIn, inApprove) === 'note=x&decision=approve', '(a2) posted data changed: ' + entries(fIn, inApprove));
assert(inApprove.value === 'approve' && inApprove.disabled === false, '(a2) input value/disabled touched');
assert(inApprove.getAttribute('aria-busy') === 'true' && inApprove.classList.contains('btn-busy') && barOn(),
  '(a2) input submitter not marked busy');
assert(inReject.getAttribute('aria-disabled') === 'true', '(a2) sibling input not locked');
LocBusy.reset();
assert(inApprove.value === 'approve' && !inApprove.hasAttribute('aria-busy'), '(a2) input not restored');

// (b2) within the grace window a second submit (double click / double Enter) is
//      dropped quietly: no prompt, no handler, no second POST.
const bQ = new El('button', { type: 'submit', name: 'go', value: '1' }, 'Go');
const fQ = form({ method: 'post' }, [bQ]);
submit(fQ, bQ);
advance(1000);
const before = confirmCalls.length;
ev = submit(fQ, bQ, () => { throw new Error('handler ran'); });
assert(ev.defaultPrevented && !ev.handlerRan && confirmCalls.length === before, '(b2) double click not dropped');
// (b3) past the grace window, OK releases the form and sends it again with the same
//      submitter (after the current event has finished dispatching).
advance(3000);
dialogMode = 'yes';
ev = submit(fQ, bQ);
assert(ev.defaultPrevented, '(b3) the busy submit must be cancelled');
await settle();
assert(confirmCalls.length === before + 1, '(b3) no prompt');
assert(!bQ.hasAttribute('aria-busy') && !fQ.hasAttribute('aria-busy'), '(b3) OK did not release the form');
assert(fQ.requestSubmitCalls.length === 0, '(b3) resubmitted inside the submit event');
advance(0);
assert(fQ.requestSubmitCalls.length === 1 && fQ.requestSubmitCalls[0] === bQ, '(b3) not re-sent with submitter');
ev = submit(fQ, bQ);  // the browser dispatches the re-send as a normal submit
assert(!ev.defaultPrevented && bQ.getAttribute('aria-busy') === 'true', '(b3) the re-send must go busy again');
dialogMode = 'no';
LocBusy.reset();

// (c) default label from the translated blob; a cancelled confirm() leaves the form alone.
const bSave = new El('button', { type: 'submit' }, 'Save');
const fSave = form({ method: 'post' }, [bSave]);
ev = submit(fSave, bSave, e => e.preventDefault());
assert(ev.defaultPrevented && !bSave.hasAttribute('aria-busy') && bSave.textContent === 'Save', '(c) went busy');
ev = submit(fSave, bSave);
assert(bSave.childNodes[0].textContent === 'Arbeit...', '(c) default label: ' + bSave.textContent);

// (d) opt-outs: data-busy="off" on the form or the submitter; dialog + new-tab forms.
const bOff = new El('button', { type: 'submit' }, 'Go');
submit(form({ 'data-busy': 'off' }, [bOff]), bOff);
assert(!bOff.hasAttribute('aria-busy'), '(d) form data-busy=off ignored');
const bOff2 = new El('button', { type: 'submit', 'data-busy': 'off' }, 'Go');
submit(form({}, [bOff2]), bOff2);
assert(!bOff2.hasAttribute('aria-busy'), '(d) submitter data-busy=off ignored');
const bDlg = new El('button', { type: 'submit' }, 'OK');
submit(form({ method: 'dialog' }, [bDlg]), bDlg);
assert(!bDlg.hasAttribute('aria-busy'), '(d) method=dialog went busy');
const bTab = new El('button', { type: 'submit' }, 'Open');
submit(form({ target: '_blank' }, [bTab]), bTab);
assert(!bTab.hasAttribute('aria-busy'), '(d) target=_blank went busy');

// (e) data-busy-release: a download form releases by itself.
const bDl = new El('button', { type: 'submit', 'data-busy-label': 'Preparing download...' }, 'Download');
const fDl = form({ method: 'get', 'data-busy-release': '3' }, [bDl]);
submit(fDl, bDl);
assert(bDl.childNodes[0].textContent === 'Preparing download...', '(e) download form not busy');
advance(2999); assert(bDl.getAttribute('aria-busy') === 'true', '(e) released too early');
advance(1);
assert(!bDl.hasAttribute('aria-busy') && bDl.textContent === 'Download' && bDl.style.minWidth === ''
  && !fDl.hasAttribute('aria-busy') && !bDl.classList.contains('busy-locked'), '(e) not released');
ev = submit(fDl, bDl); assert(!ev.defaultPrevented, '(e) a released form must submit again');
advance(3000);

// (f) a later listener that cancels the submit (after ours) releases on the next tick.
const bLate = new El('button', { type: 'submit' }, 'Late');
ev = submit(form({}, [bLate]), bLate);
ev.preventDefault(); advance(0);
assert(!bLate.hasAttribute('aria-busy'), '(f) a submit cancelled after us stayed busy');

// (g) Back button: pageshow(persisted) restores every busy form exactly.
(winListeners.pageshow || []).forEach(fn => fn({ persisted: true }));
assert(bClose.textContent === 'Close' && !bClose.hasAttribute('aria-busy') && !bClose.hasAttribute('aria-disabled')
  && !bClose.classList.contains('btn-busy') && !bClose.classList.contains('busy-locked'), '(g) not restored');
assert(bDone.childNodes[0] === keep && !bDone.hasAttribute('aria-disabled'), '(g) sibling not restored');
assert(bSave.textContent === 'Save' && !fStatus.hasAttribute('aria-busy') && !barOn(), '(g) page not reset');
ev = submit(fStatus, bDone); assert(!ev.defaultPrevented, '(g) restored form must submit again');
LocBusy.reset();

// (h) child nodes survive the label swap (moved out and back, never re-created).
assert(bDone.childNodes.length === 1 && bDone.childNodes[0] === keep, '(h) child element replaced');

// (i) data-busy-keep-label: spinner only, content untouched.
const bIcon = new El('button', { type: 'submit', 'data-busy-keep-label': '' }, 'x');
const iconChild = bIcon.childNodes[0];
submit(form({}, [bIcon]), bIcon);
assert(bIcon.classList.contains('btn-busy') && bIcon.childNodes[0] === iconChild, '(i) keep-label swapped');
LocBusy.reset();

// (l) Esc (a stopped navigation, if the browser hands the key to the page) unlocks
//     every busy FORM but leaves an in-flight JS-API action busy.
const bEsc = new El('button', { type: 'submit' }, 'Send');
const fEsc = form({ method: 'post' }, [bEsc]);
submit(fEsc, bEsc);
const apiBtn = new El('button', { type: 'button' }, 'Fetch');
const apiDone = LocBusy.start(apiBtn);
key('Enter');
assert(bEsc.getAttribute('aria-busy') === 'true', '(l) a non-Esc key released the form');
// An Esc something else owns (consumed by a handler, an open <dialog>, the driver.js
// spotlight) closes THAT and must leave the form locked.
key('Escape', true);
assert(bEsc.getAttribute('aria-busy') === 'true', '(l) a consumed Esc released the form');
overlayOpen = true;
key('Escape');
assert(bEsc.getAttribute('aria-busy') === 'true', '(l) Esc with an open dialog/popover released the form');
overlayOpen = false;
key('Escape');
assert(!bEsc.hasAttribute('aria-busy') && !fEsc.hasAttribute('aria-busy'), '(l) Esc did not release the form');
assert(apiBtn.getAttribute('aria-busy') === 'true' && apiBtn.disabled === false && barOn(), '(l) Esc hit a JS action');
ev = submit(fEsc, bEsc); assert(!ev.defaultPrevented, '(l) released form must submit again');
apiDone(); LocBusy.reset();

// (o) T4: the JS API keeps keyboard focus (no real `disabled`) yet a busy action still
//     cannot run twice: the capture-phase click guard swallows the activation (mouse,
//     Enter/Space and element.click() all arrive as a click) before any page handler.
{
  const gBtn = new El('button', { type: 'button', 'data-action': 'alternates', 'data-id': '9' });
  const gChild = new El('span'); gBtn.appendChild(gChild);
  let runs = 0;
  window.alternates = () => { runs += 1; };
  click(gChild);
  assert(runs === 1, '(o) an idle action did not run');
  const gDone = LocBusy.start(gBtn, { keepLabel: true });
  assert(gBtn.disabled === false && gBtn.getAttribute('aria-disabled') === 'true', '(o) focus-dropping disabled');
  const blocked = click(gChild);
  assert(runs === 1 && blocked.defaultPrevented && blocked.stopped, '(o) a busy action ran again');
  gDone();
  click(gChild);
  assert(runs === 2, '(o) a released action stayed blocked');
  // A form-path submitter is NOT guarded here: its second click must reach the submit
  // path (double-click drop, then the resend prompt).
  const fBtn = new El('button', { type: 'submit' }, 'Go');
  submit(form({ method: 'post' }, [fBtn]), fBtn);
  assert(!click(fBtn).stopped, '(o) the click guard swallowed a form submitter');
  LocBusy.reset();
}

// (m) LocBusy.submitForm: the busy state for a script-driven form.submit() (keeps its
//     no-validation semantics); a newer change wins, as it did before (sent again,
//     busy restarted).
const fAuto = form({ method: 'get' }, [new El('input', { type: 'checkbox', name: 'x', value: '1' })]);
LocBusy.submitForm(fAuto);
assert(fAuto.submitCalls === 1 && fAuto.getAttribute('aria-busy') === 'true' && barOn(), '(m) not busy/sent');
advance(500);
LocBusy.submitForm(fAuto);
assert(fAuto.submitCalls === 2 && fAuto.getAttribute('aria-busy') === 'true', '(m) newer change dropped');
LocBusy.reset();

// (n) a submit with NO submitter (requestSubmit() from a select's change handler) is
//     never dropped and never prompted: inside the grace window and after it, it goes
//     through (form handler runs, nothing prevented) and the busy state restarts. A
//     CLICKED double submit right after it is still dropped quietly.
const selRole = new El('select', { name: 'role' });
const fRole = form({ method: 'post' }, [selRole]);
selRole.value = 'viewer';
ev = submit(fRole, null);
assert(!ev.defaultPrevented && fRole.getAttribute('aria-busy') === 'true', '(n) first change not busy');
advance(500);
selRole.value = 'reviewer';
const asked0 = confirmCalls.length;
ev = submit(fRole, null, () => {});
assert(!ev.defaultPrevented && !ev.stopped && ev.handlerRan, '(n) change within the grace window dropped');
assert(entries(fRole, null) === 'role=reviewer' && fRole.getAttribute('aria-busy') === 'true', '(n) not re-busy');
advance(5000);
selRole.value = 'manager';
ev = submit(fRole, null, () => {});
assert(!ev.defaultPrevented && ev.handlerRan && confirmCalls.length === asked0, '(n) late change prompted/dropped');
const bRole = new El('button', { type: 'submit' }, 'Save');
const fRole2 = form({ method: 'post' }, [new El('select', { name: 'r' }), bRole]);
submit(fRole2, null);
advance(200);
ev = submit(fRole2, bRole, () => { throw new Error('handler ran'); });
assert(ev.defaultPrevented && !ev.handlerRan && confirmCalls.length === asked0, '(n) clicked double submit not dropped');
LocBusy.reset();

(async () => {
  // (j) LocBusy.wrap: focusable while running, ALWAYS released, error propagates.
  const b = new El('button', { type: 'button', 'data-busy-label': 'Testing...' }, 'Test');
  let seen = null;
  let rejectIt;
  const p = LocBusy.wrap(b, () => {
    seen = [b.disabled, b.childNodes[0].textContent, barOn()];
    return new Promise((_, rej) => { rejectIt = rej; });
  });
  assert(seen && seen[0] === false && seen[1] === 'Testing...' && seen[2] === true, '(j) not busy: ' + seen);
  assert(b.getAttribute('aria-disabled') === 'true', '(j) missing aria-disabled');
  let calledTwice = false;
  await LocBusy.wrap(b, () => { calledTwice = true; });
  assert(!calledTwice, '(j) a busy element was started twice');
  rejectIt(new Error('network down'));
  let caught = null;
  try { await p; } catch (e) { caught = e; }
  assert(caught && caught.message === 'network down', '(j) the error must propagate');
  assert(b.disabled === false && b.textContent === 'Test' && !b.hasAttribute('aria-busy') && !barOn(),
    '(j) not released on error');
  // A button that was disabled before stays disabled after.
  const pre = new El('button', { type: 'button' }, 'Pre'); pre.disabled = true;
  await LocBusy.wrap(pre, Promise.resolve(1));
  assert(pre.disabled === true, '(j) prior disabled state not restored');
  // A synchronous throw releases too.
  let sync = null;
  try { await LocBusy.wrap(b, () => { throw new Error('boom'); }); } catch (e) { sync = e; }
  assert(sync && !b.hasAttribute('aria-busy') && !barOn(), '(j) sync throw left it busy');
  // (k) start(null): top bar only; done() is idempotent.
  const done = LocBusy.start(null);
  assert(barOn() && LocBusy.isBusy(), '(k) bar-only start');
  done(); done();
  assert(!barOn() && !LocBusy.isBusy(), '(k) bar-only done');
  // (p) T4 live region: work that lasts ANNOUNCE_AFTER_MS (1 s) says the translated
  //     "working" label once; the translated "done" label follows when the LAST action
  //     releases. A quick action and an { announce: false } action say nothing.
  advance(2000);
  busyStatus.textContent = '';
  const quick = LocBusy.start(b);
  advance(500); quick(); advance(3000);
  assert(busyStatus.textContent === '', '(p) a quick action was announced: ' + busyStatus.textContent);
  const silent = LocBusy.start(b, { announce: false });
  advance(3000);
  assert(busyStatus.textContent === '', '(p) announce:false was announced');
  silent(); advance(3000);
  assert(busyStatus.textContent === '', '(p) announce:false announced its finish');
  const releaseFirst = LocBusy.start(b);
  advance(1100);
  assert(busyStatus.textContent === 'Arbeit...', '(p) start not announced');
  const releaseSecond = LocBusy.start(null);
  releaseFirst(); advance(1100);
  assert(busyStatus.textContent === 'Arbeit...', '(p) finished before all actions released');
  releaseSecond(); advance(1100);
  assert(busyStatus.textContent === 'Fertig', '(p) finish not announced');
  // Overlapping actions share one start: a second start while announced says nothing new.
  busyStatus.textContent = '';
  const one = LocBusy.start(null); advance(1500);
  const two = LocBusy.start(null); advance(1500);
  assert(busyStatus.textContent === 'Arbeit...', '(p) overlapping start');
  busyStatus.textContent = 'x'; one(); advance(1500);
  assert(busyStatus.textContent === 'x', '(p) finish spoken while work remains');
  two(); advance(1500);
  assert(busyStatus.textContent === 'Fertig', '(p) shared finish');
  assert(nativeConfirms === 0, 'window.confirm was used');
  console.log('busy.js behaviour OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
