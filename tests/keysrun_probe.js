// Behaviour probe for app/static/keysrun.js -- the key-grid live progress widget
// (GitHub #47, and the silent-failure surface for #44).
//
// The widget's rules are decisions, not styling: WHEN it may reload the grid out
// from under the operator, WHETHER a failure or a "waiting on a human" survives on
// screen, what happens when the server's own observer window expires, and how many
// connections it is allowed to hold. None of that is visible in markup, so a
// template assertion cannot check any of it.
//
// This runs the REAL file (no copy, no re-implementation) inside a minimal DOM +
// a fake connectSSE, and drives the event sequences the server actually emits.
// Executed by tests/verify_run_recovery_progress.py when `node` is on PATH.
//
// Usage: node tests/keysrun_probe.js [path/to/keysrun.js]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC_PATH = process.argv[2]
  || path.join(__dirname, '..', 'app', 'static', 'keysrun.js');
const SRC = fs.readFileSync(SRC_PATH, 'utf8');

// --- the smallest DOM that keysrun.js actually touches ----------------------
function makeEl(tag) {
  const el = {
    tag, attrs: {}, children: [], _text: '', hidden: false,
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { if (k === 'hidden') { this.hidden = false; } delete this.attrs[k]; },
    appendChild(c) {
      this.children.push(c);
      this._text += (c._text || c.data || '');
      return c;
    },
    querySelector(sel) { return this._q(sel)[0] || null; },
    querySelectorAll(sel) { return this._q(sel); },
    _q(sel) {
      const want = sel.replace(/[[\]]/g, '');
      const out = [];
      (function walk(n) {
        for (const c of n.children) {
          if (c.attrs && want in c.attrs) { out.push(c); }
          walk(c);
        }
      })(this);
      return out;
    },
  };
  return el;
}

function buildStrip(runs) {
  const strip = makeEl('div');
  strip.setAttribute('data-keysrun', '');
  strip.setAttribute('data-stream-url', '/runs/stream');
  strip.setAttribute('data-grid-url', '/projects/1/keys');
  const foot = makeEl('p');
  foot.setAttribute('data-run-foot', '');
  for (const r of runs) {
    const row = makeEl('div');
    row.setAttribute('data-run-id', String(r.id));
    const st = makeEl('span');
    st.setAttribute('data-run-state', '');
    st.textContent = r.status;
    const steps = makeEl('span');
    steps.setAttribute('data-run-steps', '');
    const err = makeEl('div');
    err.setAttribute('data-run-error', '');
    err.hidden = true;
    err.attrs.hidden = '';
    row.appendChild(st);
    row.appendChild(steps);
    row.appendChild(err);
    strip.appendChild(row);
    r._row = row; r._state = st; r._steps = steps; r._err = err;
  }
  strip.appendChild(foot);
  strip._foot = foot;
  return strip;
}

function run(runs) {
  const strip = buildStrip(runs);
  const conns = {};          // run id -> newest handle
  const opened = [];         // every connect, in order (F12 slot accounting)
  let openCount = 0;
  let peakOpen = 0;
  let reloaded = 0;
  const sandbox = {
    document: {
      readyState: 'complete',
      querySelectorAll: (sel) => (sel === '[data-keysrun]' ? [strip] : []),
      addEventListener() {},
      createElement: makeEl,
      createTextNode: (t) => ({ _text: t, data: t, children: [] }),
    },
    window: { location: { pathname: '/projects/1/keys', reload() { reloaded += 1; } } },
    connectSSE(url, opts) {
      const id = decodeURIComponent(url.split('run_id=')[1]);
      openCount += 1;
      peakOpen = Math.max(peakOpen, openCount);
      const h = {
        id,
        closed: false,
        onMessage: opts.onMessage,
        onGiveUp: opts.onGiveUp,
        close() { if (!h.closed) { h.closed = true; openCount -= 1; } },
      };
      conns[id] = h;
      opened.push(h);
      return h;
    },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: path.basename(SRC_PATH) });
  return {
    strip,
    conns,
    opened,
    peakOpen: () => peakOpen,
    reloaded: () => reloaded,
    send(id, ev) { conns[String(id)].onMessage({ data: JSON.stringify(ev) }); },
    giveUp(id) { conns[String(id)].onGiveUp(); },
    row(i) {
      const r = strip.children[i];
      return {
        state: r.children[0].textContent,
        steps: r.children[1].textContent,
        err: r.children[2].textContent,
        errHidden: r.children[2].hidden,
        errChildren: r.children[2].children.length,
      };
    },
    foot: () => strip._foot.textContent,
  };
}

let failures = 0;
function ok(cond, msg) {
  if (cond) { console.log('  ok: ' + msg); } else { failures += 1; console.log('  FAIL: ' + msg); }
}

// --- 1. every run finishes cleanly -> exactly one reload --------------------
(function allClean() {
  const t = run([{ id: 1, status: 'running' }, { id: 2, status: 'running' }]);
  t.send(1, { type: 'run_started', run_id: 1, steps: 3 });
  t.send(1, { type: 'step', step_index: 0, step_name: 'a' });
  ok(t.row(0).steps === '1 of 3 steps', 'step counter reads "1 of 3 steps"');
  t.send(1, { type: 'done' });
  t.send(1, { type: 'closed', status: 'completed' });
  ok(t.reloaded() === 0, 'no reload while a sibling run is still live');
  t.send(2, { type: 'run_started', run_id: 2, steps: 1 });
  t.send(2, { type: 'done' });
  t.send(2, { type: 'closed', status: 'completed' });
  ok(t.reloaded() === 1, 'all-clean reloads the grid exactly once');
})();

// --- 2. a FAILED run keeps its error on screen, and blocks the reload -------
(function failedRun() {
  const t = run([{ id: 7, status: 'running' }]);
  const boom = "step 'fix': APIError - Context size exceeded <img src=x onerror=alert(1)>";
  t.send(7, { type: 'run_started', run_id: 7, steps: 2 });
  t.send(7, { type: 'closed', status: 'failed', error: boom });
  ok(t.reloaded() === 0, 'a FAILED run is never reloaded away');
  ok(t.row(0).err === boom, 'the step error is rendered verbatim');
  ok(t.row(0).errChildren === 0, 'the error went in as text, never parsed as markup');
  ok(t.row(0).errHidden === false, 'the error row is revealed');
  ok(t.row(0).state === 'FAILED', 'the row reads FAILED');
  ok(t.foot().indexOf('1 failed') !== -1, 'the footer counts the failure');
})();

// --- 3. F5: a PAUSED run must block the reload too --------------------------
(function pausedRun() {
  const t = run([{ id: 11, status: 'running' }, { id: 12, status: 'running' }]);
  t.send(11, { type: 'run_started', run_id: 11, steps: 2 });
  t.send(11, { type: 'paused' });
  t.send(11, { type: 'closed', status: 'paused' });
  t.send(12, { type: 'run_started', run_id: 12, steps: 2 });
  t.send(12, { type: 'done' });
  t.send(12, { type: 'closed', status: 'completed' });
  ok(t.row(0).state === 'paused for review', 'a paused run says "paused for review"');
  ok(t.reloaded() === 0,
    'F5: a fan-out containing a PAUSED run must NOT auto-reload -- the reload would '
    + 'erase "waiting on human review", which is #44 all over again');
  ok(t.foot().indexOf('still needs you') !== -1,
    'the footer says a run still needs the operator');
})();

// --- 4. F6: a run that outlives the observer window -------------------------
(function observerExpiry() {
  const t = run([{ id: 21, status: 'running' }]);
  t.send(21, { type: 'run_started', run_id: 21, steps: 4 });
  t.send(21, { type: 'step', step_index: 0, step_name: 'a' });
  // First observer window expires with the run still going.
  t.send(21, { type: 'closed', status: 'running' });
  ok(t.opened.length === 2, 'F6: a non-terminal close reconnects once');
  ok(t.row(0).state === 'running', 'the row still reads running after the reconnect');
  // Second window expires too -> stop, and SAY so.
  t.send(21, { type: 'closed', status: 'running' });
  ok(t.opened.length === 2, 'it does not reconnect a third time');
  ok(t.row(0).steps === 'still running - reload to check',
    'F6: the row says the run is still going and hands over, instead of freezing '
    + 'silently on "running"');
  ok(t.reloaded() === 0, 'a still-running run is not reloaded away');
  ok(t.foot().indexOf('still needs you') !== -1, 'the footer flags it');
})();

// --- 4b. NEW-2: a bare `closed` (no status) must degrade honestly -----------
(function bareCloseAfterPause() {
  const t = run([{ id: 25, status: 'running' }, { id: 26, status: 'running' }]);
  t.send(25, { type: 'run_started', run_id: 25, steps: 2 });
  t.send(25, { type: 'paused' });
  // An older server (or the pre-fix live-pause branch) closes with NO status here.
  t.send(25, { type: 'closed' });
  ok(t.opened.filter((c) => c.id === '25').length === 1,
    'NEW-2: a bare close on a row we already know is paused does not reconnect');
  // Whatever it did, replay what a SECOND observer window really delivers for a
  // paused run -- the full replay, then another bare close. This is the sequence
  // that made the pre-fix widget exhaust its reconnect budget and relabel the row.
  t.send(25, { type: 'run_started', run_id: 25, steps: 2 });
  t.send(25, { type: 'paused' });
  t.send(25, { type: 'closed' });
  ok(t.row(0).state === 'paused for review',
    'NEW-2: a bare close must NOT relabel a known-PAUSED run (got "'
    + t.row(0).state + '")');
  ok(t.row(0).steps.indexOf('still running') === -1,
    'NEW-2: a known-paused row is not described as still running');
  t.send(26, { type: 'run_started', run_id: 26, steps: 1 });
  t.send(26, { type: 'done' });
  t.send(26, { type: 'closed', status: 'completed' });
  ok(t.reloaded() === 0, 'the paused run still blocks the auto-reload');
})();

(function bareCloseWithNoVerdictAtAll() {
  // Nothing was ever learned about this run: a bare close IS ambiguous here, so the
  // F6 reconnect-once path is the right degradation.
  const t = run([{ id: 27, status: 'running' }]);
  t.send(27, { type: 'run_started', run_id: 27, steps: 3 });
  t.send(27, { type: 'closed' });
  ok(t.opened.length === 2,
    'a bare close with no verdict yet still reconnects once (F6 path intact)');
  t.send(27, { type: 'closed' });
  ok(t.row(0).steps === 'still running - reload to check',
    'and then hands over honestly');
})();

// --- 5. F11: replayed steps must not inflate the counter --------------------
(function replayedSteps() {
  const t = run([{ id: 31, status: 'running' }]);
  t.send(31, { type: 'run_started', run_id: 31, steps: 3 });
  t.send(31, { type: 'step', step_index: 0, step_name: 'a' });
  t.send(31, { type: 'step', step_index: 1, step_name: 'b' });
  ok(t.row(0).steps === '2 of 3 steps', 'two distinct steps read "2 of 3 steps"');
  // observe_run REPLAYS every recorded step on a reconnect.
  t.send(31, { type: 'closed', status: 'running' });
  t.send(31, { type: 'run_started', run_id: 31, steps: 3 });
  t.send(31, { type: 'step', step_index: 0, step_name: 'a' });
  t.send(31, { type: 'step', step_index: 1, step_name: 'b' });
  t.send(31, { type: 'step', step_index: 2, step_name: 'c' });
  ok(t.row(0).steps === '3 of 3 steps',
    'F11: a full replay after a reconnect counts DISTINCT steps, not events '
    + '(got ' + t.row(0).steps + ')');
})();

// --- 6. F12: at most 5 streams open at once ---------------------------------
(function connectionBudget() {
  const runs = [];
  for (let i = 41; i <= 48; i += 1) { runs.push({ id: i, status: 'running' }); }
  const t = run(runs);
  ok(t.peakOpen() === 5,
    'F12: at most 5 concurrent EventSources (browsers cap ~6 per host and each one '
    + 'parks a server thread) -- peak was ' + t.peakOpen());
  ok(t.row(5).state === 'waiting for a slot', 'a queued row says so honestly');
  // Finishing one run must hand its slot to a waiting row.
  t.send(41, { type: 'closed', status: 'completed' });
  ok(t.conns['46'] !== undefined, 'a freed slot starts the next queued stream');
  ok(t.peakOpen() === 5, 'the budget is never exceeded while draining the queue');
})();

// --- 7. an already-finished strip must not reload ---------------------------
(function nothingWasLive() {
  const t = run([{ id: 51, status: 'completed' }]);
  t.send(51, { type: 'run_started', run_id: 51, steps: 1 });
  t.send(51, { type: 'closed', status: 'completed' });
  ok(t.reloaded() === 0, 'nothing was in flight, so nothing is reloaded');
})();

// --- 8. a dropped connection is reported, not swallowed ---------------------
(function connectionLost() {
  const t = run([{ id: 61, status: 'running' }]);
  t.send(61, { type: 'run_started', run_id: 61, steps: 2 });
  t.giveUp(61);
  ok(t.row(0).steps === 'connection lost - reload to see the latest',
    'a give-up says the connection was lost');
  ok(t.reloaded() === 0, 'a lost connection does not trigger a reload');
})();

if (failures) {
  console.log('KEYSRUN WIDGET PROBE FAILED (' + failures + ')');
  process.exit(1);
}
console.log('KEYSRUN WIDGET PROBE OK');
