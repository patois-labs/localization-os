// Busy indicators (feat/busy-indicators): ONE app-wide "the app is working" signal,
// so a click that does real work (an LLM call, a run launch, an import) never looks
// dead and nobody clicks twice.
//
// Loaded in <head> on every page (base.html and the four standalone auth shells
// login/accept/setup/error), so window.LocBusy exists before any page script runs.
// The visual parts are a thin top progress bar ([data-busy-bar], rendered by
// _busy.html) and a CSS-only spinner on the busy button (style.css "Busy
// indicators" block). Two ways in:
//
//   1. FORM SUBMITS, automatically. One submit listener on `document` in the BUBBLE
//      phase, so every form-level handler (confirm() prompts, fetch interceptors)
//      runs first; a handler that cancels the submit (preventDefault) is respected
//      and the form is left alone. The clicked button (e.submitter) shows the
//      spinner + a label; every other submit button of that form is locked.
//
//   2. IN-PAGE ACTIONS (fetch buttons), explicitly:
//        var done = LocBusy.start(btn, { label: '...' });  ...  done();
//        LocBusy.wrap(btn, function () { return fetch(...); });   // always releases
//
// Markup contract (DESIGN.md, section 5, "Busy indicators"):
//   data-busy-label="..."   the button's label while busy (translated in the template)
//   data-busy-keep-label    keep the button's own content; spinner only (icon buttons)
//   data-busy-release="N"   on a form: release after N seconds (downloads, where the
//                           page never unloads)
//   data-busy="off"         on a form or a submit button: no busy state at all
//
// THE SUBMIT PATH NEVER SETS `disabled`. A disabled submitter is dropped from the
// form data set, which would break every form whose buttons carry name/value (e.g.
// status=completed vs status=closed), and an <input type=submit> is never
// relabelled (its value is the posted value). The submit path uses aria-disabled +
// aria-busy + a CSS class and a per-form flag: a second CLICKED submit of a busy form
// is dropped for the first few seconds (double click), and after that the user is asked
// whether to send it again (a navigation stopped with the browser's Stop button
// gives the page no event, so this is the way out). A script-driven submit with no
// submitter (a select's change handler) always goes through and restarts the busy
// state, so the newest filter/role choice wins.
//
// The JS API never sets a real `disabled` either: disabling the focused button drops
// keyboard focus to <body>. It uses aria-disabled + aria-busy, and a CAPTURE-phase
// click listener swallows clicks (mouse, Enter, Space, element.click()) on an element
// the JS API holds busy, so a busy action still cannot run twice.
//
// Screen readers: when work has been running for ANNOUNCE_AFTER_MS, the polite region
// _busy.html renders (#busyAnnounce) says the "working" label once, and "done" when the
// last action releases (announce.js). Quick actions stay silent; overlapping actions
// share one start and one finish. start/wrap with { announce: false } opt out (a page
// that announces its own outcome, like the Editor).
(function () {
  'use strict';
  if (window.LocBusy) { return; }

  var ELAPSED_AFTER_MS = 3000;
  var RESEND_GRACE_MS = 3000;
  var ANNOUNCE_AFTER_MS = 1000;
  var FORM_FLAG = '__locBusy';
  var JS_FLAG = '__locBusyJs';
  var labels = null;
  var active = [];
  var bar = null;
  var announcer = null;
  var announceTimer = null;
  var announced = false;
  var wasAnnounceable = false;

  // Translated strings: the JSON blob _busy.html renders ({working, elapsed, bar}).
  // English fallbacks keep the pattern working on a page that lacks the blob.
  function getLabels() {
    if (labels) { return labels; }
    labels = {
      working: 'Working...',
      done: 'Done',
      elapsed: '{s}s',
      bar: 'Working',
      resend: 'This is still working. If it seems stuck, you can send it again. Send again?'
    };
    try {
      var blob = document.querySelector('script[data-busy-labels]');
      if (blob) {
        var parsed = JSON.parse(blob.textContent || '{}');
        Object.keys(labels).forEach(function (k) {
          if (parsed && typeof parsed[k] === 'string' && parsed[k]) { labels[k] = parsed[k]; }
        });
      }
    } catch (e) { /* keep the English fallbacks */ }
    return labels;
  }

  function getBar() {
    if (bar && bar.isConnected) { return bar; }
    bar = document.querySelector('[data-busy-bar]');
    if (!bar && document.body) {
      bar = document.createElement('div');
      bar.className = 'busybar';
      bar.setAttribute('data-busy-bar', '');
      bar.setAttribute('role', 'progressbar');
      bar.setAttribute('aria-label', getLabels().bar);
      bar.setAttribute('aria-hidden', 'true');
      document.body.insertBefore(bar, document.body.firstChild);
    }
    return bar;
  }

  // The live region (announce.js, loaded by _busy.html). Absent on a page without
  // it: then there is nothing to announce into and the bar still works.
  function getAnnouncer() {
    if (announcer) { return announcer; }
    var region = document.getElementById ? document.getElementById('busyAnnounce') : null;
    if (!region || typeof makeAnnouncer !== 'function') { return null; }
    announcer = makeAnnouncer(region, null, { minGapMs: 1000 });
    return announcer;
  }

  // Start is spoken only once work has lasted ANNOUNCE_AFTER_MS (a fast save says
  // nothing); finish is spoken only when its start was.
  function syncAnnouncement() {
    var on = active.some(function (tok) { return tok.announce; });
    if (on === wasAnnounceable) { return; }
    wasAnnounceable = on;
    if (on) {
      clearTimeout(announceTimer);
      announceTimer = setTimeout(function () {
        announceTimer = null;
        var a = getAnnouncer();
        if (a && wasAnnounceable) { announced = true; a.say(getLabels().working); }
      }, ANNOUNCE_AFTER_MS);
      return;
    }
    clearTimeout(announceTimer);
    announceTimer = null;
    if (announced) {
      announced = false;
      var a = getAnnouncer();
      if (a) { a.say(getLabels().done); }
    }
  }

  function syncBar() {
    syncAnnouncement();
    var b = getBar();
    if (!b) { return; }
    var on = active.length > 0;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-hidden', on ? 'false' : 'true');
  }

  // A token is one live busy state. done() is idempotent and always re-syncs the bar.
  function addToken(release, isForm, quiet) {
    var tok = { released: false, started: Date.now(), form: !!isForm, announce: !quiet };
    tok.done = function () {
      if (tok.released) { return; }
      tok.released = true;
      var i = active.indexOf(tok);
      if (i !== -1) { active.splice(i, 1); }
      try { release(); } catch (e) { /* a restore must never throw into a caller */ }
      syncBar();
    };
    active.push(tok);
    syncBar();
    return tok;
  }

  function restoreAttr(el, name, prev) {
    if (prev === null) { el.removeAttribute(name); } else { el.setAttribute(name, prev); }
  }

  function widthOf(el) {
    return el.getBoundingClientRect ? el.getBoundingClientRect().width : 0;
  }

  // Put one button into the busy look; returns its restore(). The label is swapped
  // by MOVING the button's child nodes out (and back on restore), never by
  // rewriting innerHTML, so nested markup and any listeners on it survive intact.
  //
  // An <input type="submit"|"image"> is NEVER relabelled: its `value` is what the
  // browser serializes for the clicked submitter AFTER the submit event, so changing
  // it would post the busy label instead of the real value. Inputs cannot host the
  // ::before ring either, so they get the class, aria state and top bar only.
  function busyButton(btn, label, keepLabel) {
    var st = {
      nodes: null,
      minWidth: btn.style.minWidth,
      ariaBusy: btn.getAttribute('aria-busy'),
      ariaDisabled: btn.getAttribute('aria-disabled'),
      timer: null
    };
    // Pin the current width first so a shorter busy label never makes it shrink.
    var w = widthOf(btn);
    if (w > 0) { btn.style.minWidth = Math.ceil(w) + 'px'; }
    btn.setAttribute('aria-busy', 'true');
    btn.setAttribute('aria-disabled', 'true');
    btn.classList.add('btn-busy');
    var keep = keepLabel || btn.tagName === 'INPUT' || btn.hasAttribute('data-busy-keep-label');
    if (!keep) {
      var text = label || btn.getAttribute('data-busy-label') || getLabels().working;
      st.nodes = Array.prototype.slice.call(btn.childNodes);
      while (btn.firstChild) { btn.removeChild(btn.firstChild); }
      var span = document.createElement('span');
      span.className = 'busy-label';
      span.textContent = text;
      // The elapsed counter lives in its own span with a reserved, tabular width
      // (style.css .busy-elapsed), so ticking never changes the button's width.
      var clock = document.createElement('span');
      clock.className = 'busy-elapsed';
      clock.setAttribute('aria-hidden', 'true');
      btn.appendChild(span);
      btn.appendChild(clock);
      // The swap itself may grow the button once (a longer label); pin that width
      // so nothing after it (the counter) can move neighbouring controls.
      var w2 = widthOf(btn);
      if (w2 > 0) { btn.style.minWidth = Math.ceil(Math.max(w, w2)) + 'px'; }
      // A 60 s LLM wait must visibly keep going: after ~3 s show a live counter.
      var started = Date.now();
      var tick = function () {
        var secs = Math.round((Date.now() - started) / 1000);
        clock.textContent = getLabels().elapsed.replace('{s}', String(secs));
        st.timer = setTimeout(tick, 1000);
      };
      st.timer = setTimeout(tick, ELAPSED_AFTER_MS);
    }
    return function restore() {
      if (st.timer) { clearTimeout(st.timer); }
      if (st.nodes) {
        while (btn.firstChild) { btn.removeChild(btn.firstChild); }
        st.nodes.forEach(function (n) { btn.appendChild(n); });
      }
      btn.style.minWidth = st.minWidth;
      restoreAttr(btn, 'aria-busy', st.ariaBusy);
      restoreAttr(btn, 'aria-disabled', st.ariaDisabled);
      btn.classList.remove('btn-busy');
    };
  }

  function isSubmitControl(el) {
    if (!el) { return false; }
    if (el.tagName === 'BUTTON') { return el.type === 'submit'; }
    return el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'image');
  }

  // Every submit control that belongs to the form (form.elements includes buttons
  // outside the form that point at it with form="...").
  function submitControls(form) {
    return Array.prototype.filter.call(form.elements || [], isSubmitControl);
  }

  function startForm(form, submitter) {
    var controls = submitControls(form);
    var main = (submitter && controls.indexOf(submitter) !== -1) ? submitter : null;
    if (!main) {
      for (var i = 0; i < controls.length; i++) {
        if (!controls[i].disabled) { main = controls[i]; break; }
      }
    }
    var restores = [];
    controls.forEach(function (b) {
      if (b === main) { return; }
      var prev = b.getAttribute('aria-disabled');
      b.setAttribute('aria-disabled', 'true');
      b.classList.add('busy-locked');
      restores.push(function () {
        restoreAttr(b, 'aria-disabled', prev);
        b.classList.remove('busy-locked');
      });
    });
    if (main) {
      main.classList.add('busy-locked');
      var restoreMain = busyButton(main, null, false);
      restores.push(function () { restoreMain(); main.classList.remove('busy-locked'); });
    }
    var prevBusy = form.getAttribute('aria-busy');
    form.setAttribute('aria-busy', 'true');
    var tok = addToken(function () {
      restores.forEach(function (r) { r(); });
      restoreAttr(form, 'aria-busy', prevBusy);
      if (form[FORM_FLAG] === tok) { form[FORM_FLAG] = null; }
    }, true);
    form[FORM_FLAG] = tok;
    var secs = parseFloat(form.getAttribute('data-busy-release'));
    if (secs > 0) { setTimeout(tok.done, secs * 1000); }
    return tok;
  }

  // Send a form again after the user confirmed it. requestSubmit re-runs the form's
  // own handlers (a confirm() prompt is a genuine new send) and validation; it must
  // run after the current submit event has finished dispatching (the browser ignores
  // a submission started from inside one), hence the setTimeout at the call site.
  function resubmit(form, submitter) {
    if (form.requestSubmit) {
      try {
        if (submitter && submitter.form === form) { form.requestSubmit(submitter); }
        else { form.requestSubmit(); }
        return;
      } catch (err) { /* fall through to a plain submit */ }
    }
    form.submit();
  }

  // CAPTURE phase: a form that is already busy never sends a second POST by itself.
  // The submit is cancelled BEFORE the form's own handlers run (so a confirmation
  // is not asked twice). Within RESEND_GRACE_MS of the first submit it is a double
  // click or a double Enter and is dropped quietly. After that, the user is probably
  // re-trying because it looks stuck (the browser's Stop button or Esc cancels the
  // navigation but no event tells the page), so ask: OK releases the form and sends
  // it again, Cancel keeps waiting.
  //
  // A submit with NO submitter can be script-driven via requestSubmit(). The newest
  // submission must win, so it is let through (the new
  // navigation replaces the old one) and the busy state restarts from it.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    var tok = form && form[FORM_FLAG];
    if (!tok || tok.released) { return; }
    if (!e.submitter) { tok.done(); return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    if (Date.now() - tok.started < RESEND_GRACE_MS) { return; }
    // #623: asked in the page (window.LocAsk, handlers.js), never with the native
    // window.confirm. Only a yes releases and re-sends; no, Escape and a page without the
    // dialog keep waiting.
    var submitter = e.submitter || null;
    if (typeof window.LocAsk !== 'function') { return; }
    window.LocAsk(getLabels().resend, { opener: submitter }).then(function (ok) {
      if (!ok) { return; }
      tok.done();
      setTimeout(function () { resubmit(form, submitter); }, 0);
    });
  }, true);

  // Esc is the other way a user stops a navigation. If the browser hands the key to
  // the page, unlock every busy FORM (in-page fetch actions stay busy: their request
  // is still running and their own code releases them). An Esc that something else
  // owns (a handler that consumed it, an open <dialog>, the driver.js spotlight) is
  // closing THAT, not stopping the page load, so it leaves the forms locked.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' && e.key !== 'Esc') { return; }
    if (e.defaultPrevented) { return; }
    if (document.querySelector('dialog[open], .driver-popover')) { return; }
    active.slice().forEach(function (tok) { if (tok.form) { tok.done(); } });
  });

  // CAPTURE phase: an element the JS API holds busy keeps focus (no `disabled`), so
  // its activation is swallowed here, before any page handler, delegated or inline,
  // can run the action a second time.
  document.addEventListener('click', function (e) {
    for (var n = e.target; n; n = n.parentNode) {
      if (n[JS_FLAG]) {
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
    }
  }, true);

  // BUBBLE phase: every form-level handler has already had its say.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.tagName !== 'FORM') { return; }
    if (e.defaultPrevented) { return; }
    if (form.getAttribute('data-busy') === 'off') { return; }
    var submitter = e.submitter || null;
    if (submitter && submitter.getAttribute('data-busy') === 'off') { return; }
    // A dialog form closes its <dialog>; nothing navigates, so nothing to wait for.
    var method = ((submitter && submitter.getAttribute('formmethod')) || form.getAttribute('method') || '').toLowerCase();
    if (method === 'dialog') { return; }
    // A form that opens in another tab leaves this page as it is.
    var target = (submitter && submitter.getAttribute('formtarget')) || form.getAttribute('target') || '';
    if (target && target !== '_self' && target !== '_top' && target !== '_parent') { return; }
    var tok = startForm(form, submitter);
    // A listener registered later on window could still cancel this submit; if it
    // did, nothing will navigate, so release instead of leaving the form stuck.
    setTimeout(function () { if (e.defaultPrevented) { tok.done(); } }, 0);
  }, false);

  // Back button / bfcache: the page comes back exactly as it was left, busy state
  // included. Reset everything so the restored page is usable again.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) { api.reset(); }
  });

  var api = {
    // Busy state for an in-page action. `el` may be null (top bar only). Returns an
    // idempotent done(). Uses aria-disabled so keyboard focus stays on the element.
    start: function (el, opts) {
      opts = opts || {};
      var quiet = opts.announce === false;
      if (el && el.getAttribute && el.getAttribute('aria-busy') === 'true') {
        // Already busy: count the extra work on the bar, leave the button alone.
        return addToken(function () {}, false, quiet).done;
      }
      var restore = function () {};
      if (el) {
        var restoreButton = busyButton(el, opts.label, !!opts.keepLabel);
        el[JS_FLAG] = true;
        restore = function () { el[JS_FLAG] = false; restoreButton(); };
      }
      return addToken(restore, false, quiet).done;
    },
    // start(), await the promise (or the promise a function returns), always release.
    // A busy element is not started twice: the second call resolves without running fn.
    wrap: function (el, promiseOrFn, opts) {
      if (el && api.isBusy(el)) { return Promise.resolve(undefined); }
      var done = api.start(el, opts);
      var p;
      try {
        p = (typeof promiseOrFn === 'function') ? promiseOrFn() : promiseOrFn;
      } catch (err) {
        done();
        return Promise.reject(err);
      }
      return Promise.resolve(p).then(
        function (v) { done(); return v; },
        function (err) { done(); throw err; }
      );
    },
    // A script-driven form submit (e.g. a select's change handler) WITH the busy
    // state, keeping form.submit() semantics (no validation, no submit event).
    // Prefer form.requestSubmit() where validation cannot block; use this where the
    // old no-validation behaviour must stay. Like a submitter-less requestSubmit, a
    // newer change wins: an earlier busy state is released and restarted.
    submitForm: function (form) {
      if (!form) { return; }
      var tok = form[FORM_FLAG];
      if (tok && !tok.released) { tok.done(); }
      startForm(form, null);
      form.submit();
    },
    isBusy: function (el) {
      if (!el) { return active.length > 0; }
      return el.getAttribute && el.getAttribute('aria-busy') === 'true';
    },
    // Release every busy state on the page (bfcache restore; also callable by hand).
    reset: function () {
      active.slice().forEach(function (tok) { tok.done(); });
    }
  };

  window.LocBusy = api;
})();
