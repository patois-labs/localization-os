// Shared screen reader announcer (WCAG 4.1.3 status messages). Loaded once per page
// from _busy.html; it defines one global function and touches no DOM until a caller
// hands it the live regions that page rendered at load (regions inserted later are
// often missed by screen readers).
//
// makeAnnouncer(politeEl, alertEl, { minGapMs, settleMs }) returns:
//   say(msg, { keep })
//                polite. Coalesces: while the gap since the last polite message is
//                running (or a settle window is open) only the LATEST message waits,
//                never more than one pending, spoken when the wait ends (keep below
//                adds at most one outcome in front of it).
//                keep: true marks an OUTCOME the user asked for ("Segment 4 approved."):
//                while it waits, an alert does not drop it and a later plain say() does
//                not replace it. Outcomes coalesce last wins among themselves, so what
//                waits is at most two messages: the latest kept outcome followed by the
//                latest plain message said after it, if any. A plain message said
//                before an outcome is older news and is dropped.
//   flush(msg)   polite, now: drops any pending message (kept or not) and ends a settle
//                window. For terminal states ("Run completed.").
//   alert(msg)   assertive: bypasses the gap and cancels a pending plain polite
//                message; a kept outcome still pending is spoken after the gap, as
//                planned. The same text within 3 s is spoken once.
//   clearAlert() empties the assertive region (a visible alert that is no longer true).
//   settle()     opens a settle window of settleMs: say() calls inside it are held and
//                only the last one is spoken when it closes (a replay burst after a
//                reconnect becomes one summary). It drops what was waiting, kept or not.
//   settling()   true while that window is open.
//   cancel()     drops the pending polite message (kept or not) and any write under way.
// Writes clear the region first and set the text about 100 ms later, so the same
// message twice is announced twice.
function makeAnnouncer(politeEl, alertEl, options) {
  'use strict';
  options = options || {};
  var gap = options.minGapMs || 0;
  var settleMs = options.settleMs || 0;
  var WRITE_DELAY_MS = 100;
  var ALERT_DEDUP_MS = 3000;
  var last = -Infinity;
  var until = 0;
  var kept = '';     // the latest pending outcome (say with keep; last wins)
  var pending = '';  // the latest pending plain message (last wins)
  var timer = null;
  var politeWrite = null;
  var alertWrite = null;
  var alerts = Object.create(null);

  function dropPending() {
    clearTimeout(timer);
    timer = null;
    pending = '';
    kept = '';
  }
  function cancel() {
    dropPending();
    clearTimeout(politeWrite);
    politeWrite = null;
  }
  function writePolite(message) {
    clearTimeout(politeWrite);
    last = Date.now();
    if (!politeEl) { return; }
    politeEl.textContent = '';
    politeWrite = setTimeout(function () {
      politeWrite = null;
      politeEl.textContent = message;
    }, WRITE_DELAY_MS);
  }
  function drain() {
    timer = null;
    var message = kept && pending ? kept + ' ' + pending : (kept || pending);
    kept = '';
    pending = '';
    if (message) { writePolite(message); }
  }
  function say(message, opts) {
    if (!message) { return; }
    message = String(message);
    if (opts && opts.keep) {
      // An outcome: replaces an older waiting outcome (last wins, as for plain
      // messages, so rapid approvals never pile up into a backlog); a plain message
      // said before it is older news and is dropped, as a newer say would.
      kept = message;
      pending = '';
    } else {
      pending = message;
    }
    clearTimeout(timer);
    timer = null;
    var now = Date.now();
    var delay = Math.max(0, last + gap - now, until - now);
    if (delay > 0) { timer = setTimeout(drain, delay); } else { drain(); }
  }
  function flush(message) {
    cancel();
    until = 0;
    if (message) { writePolite(String(message)); }
  }
  function alert(message) {
    if (!message) { return; }
    message = String(message);
    // A pending plain polite message is stale next to an error ("Loading
    // alternates..."), so it is dropped; a KEPT outcome already happened (a segment was
    // approved), which an error about something else does not make untrue, so it
    // waits out the gap and is spoken. The settle window is left alone so a replay
    // that is still arriving stays summarized.
    if (kept) { pending = ''; } else { dropPending(); }
    var now = Date.now();
    Object.keys(alerts).forEach(function (text) {
      if (now - alerts[text] >= ALERT_DEDUP_MS) { delete alerts[text]; }
    });
    if (alerts[message] !== undefined) { return; }
    alerts[message] = now;
    clearTimeout(alertWrite);
    if (!alertEl) { return; }
    alertEl.textContent = '';
    alertWrite = setTimeout(function () {
      alertWrite = null;
      alertEl.textContent = message;
    }, WRITE_DELAY_MS);
  }
  function clearAlert() {
    clearTimeout(alertWrite);
    alertWrite = null;
    alerts = Object.create(null);
    if (alertEl) { alertEl.textContent = ''; }
  }
  function settle() {
    // Drop what was waiting (the replay re-derives it); a write already under way
    // finishes.
    dropPending();
    until = Date.now() + settleMs;
  }
  return {
    say: say,
    flush: flush,
    alert: alert,
    clearAlert: clearAlert,
    settle: settle,
    settling: function () { return Date.now() < until; },
    cancel: cancel
  };
}
