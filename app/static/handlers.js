// Values from templates are HTML data, never JavaScript source. Load before
// busy.js: this submit listener cancels before its bubble listener starts busy.
(function () {
  'use strict';

  // Issue #623: the in-page, focus-trapped question that replaces the native window.confirm
  // (which freezes the tab and automation and is not reliably exposed to screen readers).
  // window.LocAsk(message, opts) resolves true ONLY when the yes button is pressed; Escape,
  // the no button, a browser without <dialog>.showModal and a dialog that is already open
  // all resolve false, so every caller fails closed. All text is set with textContent
  // (never markup). Focus starts on the no button (the safe answer) and returns to the
  // opener when the dialog closes. opts: title, yes, no (default to the localized
  // js.confirm_* strings), opener (element to refocus), safeFirst (default true).
  var askDialog = null;
  function i18n(key, fallback) {
    var I = window.I18N || {};
    return (typeof I[key] === 'string' && I[key]) ? I[key] : fallback;
  }
  function buildAskDialog() {
    var dlg = document.createElement('dialog');
    dlg.className = 'modal session-dialog';
    dlg.setAttribute('role', 'alertdialog');
    dlg.setAttribute('aria-labelledby', 'locAskTitle');
    dlg.setAttribute('aria-describedby', 'locAskMsg');
    var card = document.createElement('div');
    card.className = 'card modal-card';
    var h = document.createElement('h2'); h.setAttribute('id', 'locAskTitle');
    var p = document.createElement('p'); p.setAttribute('id', 'locAskMsg');
    var actions = document.createElement('div'); actions.className = 'session-actions';
    var yes = document.createElement('button'); yes.setAttribute('type', 'button');
    var no = document.createElement('button'); no.setAttribute('type', 'button');
    no.className = 'ghost';
    actions.appendChild(yes); actions.appendChild(no);
    card.appendChild(h); card.appendChild(p); card.appendChild(actions);
    dlg.appendChild(card);
    document.body.appendChild(dlg);
    return { dlg: dlg, title: h, msg: p, yes: yes, no: no };
  }
  function ask(message, opts) {
    opts = opts || {};
    var d;
    try {
      if (!document.body || typeof document.createElement !== 'function') { return Promise.resolve(false); }
      if (!askDialog) { askDialog = buildAskDialog(); }
      d = askDialog;
      if (typeof d.dlg.showModal !== 'function' || d.dlg.open) { return Promise.resolve(false); }
    } catch (err) { return Promise.resolve(false); }
    d.title.textContent = opts.title || i18n('confirm_title', 'Please confirm');
    d.msg.textContent = String(message);
    d.yes.textContent = opts.yes || i18n('confirm_yes', 'Confirm');
    d.no.textContent = opts.no || i18n('confirm_no', 'Cancel');
    var opener = opts.opener || document.activeElement;
    return new Promise(function (resolve) {
      var answer = false;
      function onYes() { answer = true; d.dlg.close(); }
      function onNo() { d.dlg.close(); }
      d.dlg.addEventListener('close', function () {
        d.yes.removeEventListener('click', onYes);
        d.no.removeEventListener('click', onNo);
        if (opener && opener.isConnected && typeof opener.focus === 'function') { opener.focus(); }
        resolve(answer);
      }, { once: true });
      d.yes.addEventListener('click', onYes);
      d.no.addEventListener('click', onNo);
      try { d.dlg.showModal(); } catch (err) {
        d.yes.removeEventListener('click', onYes);
        d.no.removeEventListener('click', onNo);
        resolve(false);
        return;
      }
      d.no.focus();
    });
  }
  window.LocAsk = ask;

  // data-action-confirm: the click or submit is cancelled at once, the question is asked in
  // the page, and only a yes sends it again (flagged so it is not asked twice). Anything
  // but a yes leaves the action undone.
  var approved = [];
  // guard(e, el, message): cancel the event `e` on `el`, ask `message`, and on a yes send it
  // again once. Returns false (event left alone) only for that approved re-send. Shared by
  // data-action-confirm and by page scripts through window.LocAsk.guard (#623).
  function guard(e, el, message) {
    var at = approved.indexOf(el);
    if (at >= 0) { approved.splice(at, 1); return false; }
    e.preventDefault();
    var submitter = e.type === 'submit' ? (e.submitter || null) : null;
    // Focus returns to where the person was (the field they pressed Enter in, or the
    // button they clicked); when nothing real has focus (some browsers do not focus a
    // clicked button) the submit button or the element itself.
    var focused = document.activeElement;
    var opener = (focused && focused !== document.body) ? focused : (submitter || el);
    ask(message, { opener: opener }).then(function (yes) {
      if (!yes) { return; }
      approved.push(el);
      var form = el.tagName === 'FORM' ? el : el.form;
      var viaForm = form && (el.tagName === 'FORM' || (el.type || '').toLowerCase() === 'submit');
      if (viaForm && typeof form.requestSubmit === 'function') {
        if (el.tagName === 'FORM') { submitter ? form.requestSubmit(submitter) : form.requestSubmit(); }
        else { form.requestSubmit(el); }
      } else if (el.tagName === 'FORM') {
        // No requestSubmit: LocBusy.submitForm starts the busy state and sends the form
        // (a bare submit() would bypass the busy guard). Without LocBusy nothing is sent.
        if (window.LocBusy && typeof window.LocBusy.submitForm === 'function') { window.LocBusy.submitForm(form); }
      } else {
        el.click();
      }
      // A resend that never reached the listener must not leave a stale approval behind.
      setTimeout(function () {
        var i = approved.indexOf(el);
        if (i >= 0) { approved.splice(i, 1); }
      }, 0);
    });
    return true;
  }
  // For a page script's own submit listener: window.LocAsk.guard(e, message). The submit is
  // cancelled and the question asked in the page; only a yes sends it again. Never throws.
  ask.guard = function (e, message) {
    try { return guard(e, e.currentTarget || e.target, String(message)); }
    catch (err) { e.preventDefault(); return true; }
  };
  function confirmAction(e) {
    var el = e.type === 'submit' ? e.target : e.target.closest('[data-action-confirm]');
    if (!el || e.defaultPrevented) { return; }
    // Forms confirm on submit (including Enter), never twice on button clicks.
    if (e.type === 'click' && el.tagName === 'FORM') { return; }
    var message = el.getAttribute('data-action-confirm');
    if (message === null) { return; }
    guard(e, el, message);
  }
  document.addEventListener('submit', confirmAction);
  document.addEventListener('click', confirmAction);

  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-select-on-click]');
    if (el) { el.select(); }
  });

  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-action]');
    if (!el || e.defaultPrevented) { return; }
    var id = Number(el.dataset.id);
    // Explicit dispatch preserves argument types without eval or dynamic lookup.
    switch (el.dataset.action) {
      case 'translate': window.startTranslate(id, el.dataset.locale); break;
      case 'test-engine': window.testEngine(el.dataset.provider, el); break;
      case 'test-engine-row': window.testEngineRow(id, el); break;
      case 'accept-tm': window.acceptTM(id, el); break;
      case 'alternates': window.alternates(id, el); break;
      case 'repair-tags': window.repairTags(id, el); break;
      case 'approve': window.approveBtn(el, id); break;
      case 'propagation': window.togglePropagation(el, id); break;
      case 'undo-propagation': window.undoPropagation(el, id); break;
      case 'copy-source': window.copySrc(id); break;
      case 'save-paragraph': window.savePara(id, false, el); break;
      case 'revert-paragraph': window.revertPara(id, el); break;
    }
  });
})();
