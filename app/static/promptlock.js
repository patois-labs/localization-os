// Locked, editable prompts (#630): the workflow System prompt heads and every step's Inline
// prompt are shown locked (a READONLY field, still focusable, read out and submitted). An
// Unlock button makes the section editable and shows Apply (a submit button of the section's
// own form, so Apply saves through the normal server route and the page comes back locked).
// "Restore default prompt" asks in the page (window.LocAsk, never the native confirm).
//
// An unlocked section with unapplied edits is "dirty". It is discarded and re-locked after 30
// minutes, and on refresh/close the browser's own prompt is shown (beforeunload); leaving by an
// in-app link, another form, or a tab switch asks "You will lose your changes" in the page.
//
// Markup contract (see templates/_prompt_lock.html):
//   [data-prompt-lock]            the section (optional data-lock-ms for the idle limit, data-lock-open
//                                 to start unlocked after a refused save)
//     [data-lock-field]           the readonly textarea
//     [data-lock-status]          role=status line: [data-lock-chip] (Locked/Unlocked), [data-lock-text]
//     [data-lock-unlock] [data-lock-apply][hidden] [data-lock-cancel][hidden]
//     [data-lock-restore-default] (step prompts only; head prompts use a server form)
// No inline handlers: every listener is attached here (CSP).
(function () {
  'use strict';

  var DEFAULT_MS = 30 * 60 * 1000;
  var sections = [];
  var leaving = false;   // an own Apply/Restore submit is on its way: do not warn on unload

  function I(key, fallback, vars) {
    var map = window.I18N || {};
    var s = (typeof map[key] === 'string' && map[key]) ? map[key] : fallback;
    if (vars) { Object.keys(vars).forEach(function (k) { s = s.split('{' + k + '}').join(vars[k]); }); }
    return s;
  }

  function dirty(sec) { return sec.unlocked && sec.field.value !== sec.original; }

  function say(sec, chip, text) {
    if (sec.chip) { sec.chip.textContent = chip; }
    if (sec.text) { sec.text.textContent = text; }
  }

  function setHidden(el, on) { if (el) { el.hidden = on; } }

  function clearTimer(sec) { if (sec.timer) { window.clearTimeout(sec.timer); sec.timer = null; } }

  function relock(sec, textKey, textFallback) {
    clearTimer(sec);
    sec.unlocked = false;
    sec.field.readOnly = true;
    setHidden(sec.unlockBtn, false);
    setHidden(sec.applyBtn, true);
    setHidden(sec.cancelBtn, true);
    say(sec, I('prompt_chip_locked', 'Locked'), I(textKey, textFallback));
  }

  function discard(sec, textKey, textFallback) {
    if (sec.unlocked) {
      sec.field.value = sec.discardTo;
      // keep any live character counter in step with the restored text
      sec.field.dispatchEvent(new Event('input', { bubbles: true }));
    }
    relock(sec, textKey || 'prompt_status_discarded',
      textFallback || 'Changes discarded. Locked.');
  }

  function unlock(sec, quiet) {
    sec.unlocked = true;
    sec.field.readOnly = false;
    // A refused save re-opens with the typed text: it counts as unapplied until applied.
    sec.original = sec.openDirty ? null : sec.field.value;
    sec.openDirty = false;
    sec.discardTo = sec.field.defaultValue;
    setHidden(sec.unlockBtn, true);
    setHidden(sec.applyBtn, false);
    setHidden(sec.cancelBtn, false);
    say(sec, I('prompt_chip_unlocked', 'Unlocked'),
      I('prompt_status_unlocked', 'Unlocked. Edit the prompt, then choose Apply to save it.'));
    clearTimer(sec);
    sec.timer = window.setTimeout(function () {
      if (sec.unlocked) {
        discard(sec, 'prompt_status_timeout',
          'Locked again after 30 minutes. Unapplied changes were discarded.');
      }
    }, sec.ms);
    if (!quiet) { sec.field.focus(); }
  }

  function dirtyList(scope) {
    return sections.filter(function (sec) {
      return dirty(sec) && (!scope || scope === sec.box || scope.contains(sec.box));
    });
  }

  function askLose(opener) {
    if (typeof window.LocAsk !== 'function') { return Promise.resolve(false); }
    return window.LocAsk(
      I('prompt_lose_msg', 'You have prompt changes that are not applied. If you leave now, they will be lost.'),
      {
        title: I('prompt_lose_title', 'You will lose your changes'),
        yes: I('prompt_lose_yes', 'Leave and lose changes'),
        no: I('prompt_lose_no', 'Stay'),
        opener: opener
      });
  }

  // Resolves true when it is fine to leave `scope` (nothing dirty, or the person chose to lose
  // the changes: those sections are then discarded and re-locked). False = stay.
  function confirmLeave(scope, opener) {
    var list = dirtyList(scope);
    if (!list.length) { return Promise.resolve(true); }
    return askLose(opener).then(function (yes) {
      if (yes) { list.forEach(function (sec) { discard(sec); }); }
      return !!yes;
    });
  }

  function bind(box) {
    if (box.dataset.lockReady === '1') { return; }
    var field = box.querySelector('[data-lock-field]');
    if (!field) { return; }
    box.dataset.lockReady = '1';
    var ms = parseInt(box.getAttribute('data-lock-ms'), 10);
    var sec = {
      box: box, field: field,
      chip: box.querySelector('[data-lock-chip]'),
      text: box.querySelector('[data-lock-text]'),
      unlockBtn: box.querySelector('[data-lock-unlock]'),
      applyBtn: box.querySelector('[data-lock-apply]'),
      cancelBtn: box.querySelector('[data-lock-cancel]'),
      restoreBtn: box.querySelector('[data-lock-restore-default]'),
      unlocked: false, original: field.value, discardTo: field.value, timer: null,
      ms: ms > 0 ? ms : DEFAULT_MS, openDirty: false
    };
    sections.push(sec);
    box.promptLock = sec;

    if (sec.unlockBtn) {
      sec.unlockBtn.addEventListener('click', function () { unlock(sec); });
    }
    if (sec.cancelBtn) {
      sec.cancelBtn.addEventListener('click', function () {
        if (!dirty(sec)) {
          discard(sec, 'prompt_status_locked_again', 'Locked again. Nothing was changed.');
          if (sec.unlockBtn) { sec.unlockBtn.focus(); }
          return;
        }
        askLose(sec.cancelBtn).then(function (yes) {
          if (!yes) { return; }
          discard(sec);
          if (sec.unlockBtn) { sec.unlockBtn.focus(); }
        });
      });
    }
    if (sec.applyBtn) {
      sec.applyBtn.addEventListener('click', function (e) {
        // Nothing changed: no new version, no reload. Say so and lock again.
        if (!dirty(sec)) {
          e.preventDefault();
          discard(sec, 'prompt_status_nochange', 'No changes to apply. Locked.');
          if (sec.unlockBtn) { sec.unlockBtn.focus(); }
        }
      });
    }
    if (sec.restoreBtn) {
      sec.restoreBtn.addEventListener('click', function (e) {
        var form = field.form;
        var action = sec.restoreBtn.getAttribute('data-restore-action');
        // Click guard: a restore already on its way (aria-disabled + busy) is not sent twice.
        if (sec.restoreBtn.getAttribute('aria-disabled') === 'true' ||
            (window.LocBusy && window.LocBusy.isBusy(sec.restoreBtn))) {
          e.preventDefault();
          return;
        }
        if (!form || !action || typeof window.LocAsk !== 'function') { return; }
        window.LocAsk(sec.restoreBtn.getAttribute('data-restore-message') || '', {
          opener: sec.restoreBtn
        }).then(function (yes) {
          if (!yes) { return; }
          // Restore resets ONLY the prompt: a separate POST to the step's restore-prompt route
          // that carries nothing but the CSRF token, never the step's edit form (other unsaved
          // edits in it are neither saved nor sent).
          var post = document.createElement('form');
          post.method = 'post';
          post.action = action;
          post.hidden = true;
          var tok = form.querySelector('input[name="csrf_token"]');
          if (tok) {
            var c = document.createElement('input');
            c.type = 'hidden'; c.name = 'csrf_token'; c.value = tok.value;
            post.appendChild(c);
          }
          document.body.appendChild(post);
          leaving = true;
          approvedForms.push(post);
          // The app's busy pattern: the Restore button shows its busy label and is
          // aria-disabled (focus stays on it) while the POST is on its way, and the submit
          // goes through requestSubmit() so the submit event runs (busy bar, double-submit
          // guard); a bare submit call would skip that event.
          if (window.LocBusy) {
            window.LocBusy.start(sec.restoreBtn, {
              label: sec.restoreBtn.getAttribute('data-busy-label') || undefined
            });
          }
          post.requestSubmit();
        });
      });
    }
    if (box.hasAttribute('data-lock-open')) {
      sec.openDirty = true;   // a refused save: the typed text is unapplied until applied
      unlock(sec, true);
    }
  }

  function init(root) {
    (root || document).querySelectorAll('[data-prompt-lock]').forEach(bind);
  }

  // --- leaving the page ---------------------------------------------------------------
  window.addEventListener('beforeunload', function (e) {
    if (leaving || !dirtyList().length) { return; }
    e.preventDefault();
    e.returnValue = '';   // the browser shows its own standard prompt; custom text is ignored
  });

  // Own form submits (Apply) are not "leaving with unapplied changes".
  document.addEventListener('submit', function (e) {
    if (e.defaultPrevented) { return; }
    var form = e.target;
    if (form && dirtyList().some(function (s) { return s.field.form === form || form.contains(s.box) || s.box.contains(form); })) {
      leaving = true;
    }
  });

  // Another form (rename, delete, move ...) while a section has unapplied edits.
  var approvedForms = [];
  document.addEventListener('submit', function (e) {
    var form = e.target;
    var at = approvedForms.indexOf(form);
    if (at >= 0) { approvedForms.splice(at, 1); return; }
    var others = dirtyList().filter(function (s) {
      return !(s.field.form === form || form.contains(s.box) || s.box.contains(form));
    });
    if (!others.length) { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    var submitter = e.submitter || null;
    askLose(submitter || document.activeElement).then(function (yes) {
      if (!yes) { return; }
      others.forEach(function (s) { discard(s); });
      approvedForms.push(form);
      if (typeof form.requestSubmit === 'function') {
        if (submitter) { form.requestSubmit(submitter); } else { form.requestSubmit(); }
      }
    });
  }, true);

  // In-app links.
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) { return; }
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.hasAttribute('download')) { return; }
    var href = a.getAttribute('href') || '';
    if (href.charAt(0) === '#' || /^(javascript|mailto|tel):/i.test(href)) { return; }
    if (a.target && a.target !== '_self') { return; }
    if (!dirtyList().length) { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    askLose(a).then(function (yes) {
      if (!yes) { return; }
      dirtyList().forEach(function (s) { discard(s); });
      window.location.href = a.href;
    });
  }, true);

  // Switching between the step tabs (wftabs.js) hides the panel that holds the edit: ask first.
  var passTab = null;
  function innerTabs(tab) {
    var list = tab.closest('[role="tablist"]');
    return list ? Array.prototype.slice.call(list.querySelectorAll('[role="tab"]')) : [];
  }
  function guardTab(e, target) {
    var tabs = innerTabs(target);
    var cur = tabs.filter(function (t) { return t.getAttribute('aria-selected') === 'true'; })[0];
    if (!cur || cur === target) { return false; }
    var panel = document.getElementById(cur.getAttribute('aria-controls'));
    if (!panel || !dirtyList(panel).length) { return false; }
    e.preventDefault();
    e.stopImmediatePropagation();
    confirmLeave(panel, cur).then(function (ok) {
      if (!ok) { return; }
      passTab = target;
      target.click();
      target.focus();
    });
    return true;
  }
  document.addEventListener('click', function (e) {
    var tab = e.target.closest && e.target.closest('[data-wf-tabs] [role="tab"]');
    if (!tab) { return; }
    if (passTab === tab) { passTab = null; return; }
    guardTab(e, tab);
  }, true);
  document.addEventListener('keydown', function (e) {
    var tab = e.target.closest && e.target.closest('[data-wf-tabs] [role="tab"]');
    if (!tab) { return; }
    var tabs = innerTabs(tab);
    var i = tabs.indexOf(tab);
    var n = null;
    if (e.key === 'ArrowRight') { n = tabs[(i + 1) % tabs.length]; }
    else if (e.key === 'ArrowLeft') { n = tabs[(i - 1 + tabs.length) % tabs.length]; }
    else if (e.key === 'Home') { n = tabs[0]; }
    else if (e.key === 'End') { n = tabs[tabs.length - 1]; }
    if (n && n !== tab) { guardTab(e, n); }
  }, true);

  window.LocPromptLock = {
    init: init,
    dirtyIn: dirtyList,
    hasDirty: function () { return dirtyList().length > 0; },
    confirmLeave: confirmLeave
  };

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', function () { init(); }); }
  else { init(); }
})();
