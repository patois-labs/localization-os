/* Onboarding spotlight wiring (SPEC-onboarding-tour Phase 2) -- the optional
 * driver.js garnish over the Phase-1 "Get started" checklist.
 *
 * Progressive enhancement: this file is served ONLY to eligible (admin/linguist)
 * users, and even then does nothing unless driver.js loaded. The "Show me"
 * buttons ship [hidden]; we unhide them only when driver.js is present, so with
 * JS (or the vendored lib) unavailable they stay invisible/inert and the plain
 * Phase-1 links remain the whole feature.
 *
 * Two entry points, both driven by server-rendered data-attributes (no hardcoded
 * English here -- all strings come from page.onboarding.* via those attrs / the
 * labels blob):
 *   1. Checklist "Show me": spotlight the step's CTA, then continue to the target
 *      page carrying ?spotlight=<step>.
 *   2. Target page: if ?spotlight=<step> is set and the real control carries
 *      [data-spotlight-anchor="<step>"], spotlight it. Missing anchor -> no-op.
 */
(function () {
  'use strict';

  // driver.js IIFE exposes the factory at window.driver.js.driver.
  var Driver = window.driver && window.driver.js && window.driver.js.driver;
  if (typeof Driver !== 'function') { return; }  // lib absent -> leave links as-is

  // Translated button labels (rendered by base.html; page.onboarding.spot_*).
  var labels = { go: 'Next', done: 'Done', close: 'Close' };
  try {
    var blob = document.querySelector('script[data-spotlight-labels]');
    if (blob) {
      var parsed = JSON.parse(blob.textContent || '{}');
      if (parsed.go) { labels.go = parsed.go; }
      if (parsed.done) { labels.done = parsed.done; }
      if (parsed.close) { labels.close = parsed.close; }
    }
  } catch (e) { /* keep fallbacks */ }

  var reduce = false;
  try {
    reduce = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { reduce = false; }

  function makeDriver() {
    // A fresh instance per spotlight; single-element highlight() shows the
    // popover with a labeled action button (Esc / overlay-click also close it).
    return Driver({
      animate: !reduce,          // honor prefers-reduced-motion (no fade)
      allowClose: true,          // Esc + overlay click exit
      overlayColor: '#000',
      overlayOpacity: 0.6,
      showProgress: false,
      popoverClass: 'ob-spotlight'
    });
  }

  // driver.js 1.8.0 ships a generic English aria-label="Close" on the x button;
  // overwrite it UNCONDITIONALLY with the locale-correct label (Schliessen /
  // Cerrar / Fermer) once the popover mounts, so the accessible name matches the
  // page language (a plain guard on "no aria-label" would never fire here).
  function labelCloseBtn() {
    var apply = function () {
      var x = document.querySelector('.driver-popover-close-btn');
      if (x) {
        x.setAttribute('aria-label', labels.close);
        x.setAttribute('type', 'button');
      }
    };
    if (window.requestAnimationFrame) { window.requestAnimationFrame(apply); }
    else { setTimeout(apply, 0); }
  }

  function plainText(value) {
    var node = document.createElement('span');
    node.textContent = value || '';
    return node.innerHTML;
  }

  function spotlight(element, title, description, btnText, onNext) {
    if (!element) { return null; }
    var d = makeDriver();
    d.highlight({
      element: element,
      popover: {
        title: plainText(title),
        description: plainText(description),
        showButtons: ['next', 'close'],
        nextBtnText: plainText(btnText),
        onNextClick: function () { onNext(d); },
        onCloseClick: function () { d.destroy(); }
      }
    });
    labelCloseBtn();
    return d;
  }

  // --- 1. Checklist "Show me" affordances --------------------------------
  var showmes = document.querySelectorAll('.ob-showme[data-spotlight-step]');
  Array.prototype.forEach.call(showmes, function (btn) {
    btn.hidden = false;                 // reveal now that driver.js is present
    btn.addEventListener('click', function () {
      var url = btn.getAttribute('data-spotlight-url');
      var li = btn.closest ? btn.closest('.ob-step') : null;
      var cta = li ? li.querySelector('.ob-cta') : null;
      var go = function () { if (url) { window.location.href = url; } };
      if (!cta || !url) { go(); return; }  // no CTA/url -> just navigate
      spotlight(
        cta,
        btn.getAttribute('data-spotlight-title'),
        btn.getAttribute('data-spotlight-desc'),
        labels.go,
        function (d) { d.destroy(); go(); }
      );
    });
  });

  // --- 2. Target-page continuation (?spotlight=<step>) -------------------
  var step = null;
  try { step = new URLSearchParams(window.location.search).get('spotlight'); }
  catch (e) { step = null; }
  if (step) {
    // Attribute-value selector; guard against a garbage param (no CSS.escape in
    // older engines, so validate the token shape ourselves).
    var anchor = /^[a-z_-]+$/i.test(step)
      ? document.querySelector('[data-spotlight-anchor="' + step + '"]')
      : null;
    if (anchor) {
      spotlight(
        anchor,
        anchor.getAttribute('data-spotlight-title'),
        anchor.getAttribute('data-spotlight-desc'),
        labels.done,
        function (d) { d.destroy(); }
      );
    }
    // Missing anchor (control not on this page / hidden by RBAC): degrade to
    // nothing -- the user still landed on the right page from the plain link.
  }
})();
