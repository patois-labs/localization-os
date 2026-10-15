/* Direction A: ephemeral enhancement of rendered sections. Documented in DESIGN.md. */
(function () {
  'use strict';
  if (document.documentElement.dataset.locComponentsReady) return;
  document.documentElement.dataset.locComponentsReady = 'true';
  document.querySelectorAll('[data-component-tabs]').forEach(function (root) {
    var list = root.querySelector(':scope > .component-tab-list');
    if (!list) return;
    var tabs = Array.from(list.querySelectorAll('[data-component-tab]'));
    var panels = tabs.map(function (tab) { return document.getElementById(tab.hash.slice(1)); });
    if (!tabs.length || panels.some(function (panel) { return !panel || !root.contains(panel); })) return;
    function activate(index, focus, updateHash) {
      tabs.forEach(function (tab, i) {
        tab.setAttribute('aria-selected', String(i === index));
        tab.tabIndex = i === index ? 0 : -1;
        panels[i].hidden = i !== index;
      });
      if (focus) tabs[index].focus();
      if (updateHash) history.replaceState(null, '', tabs[index].hash);
    }
    list.setAttribute('role', 'tablist');
    tabs.forEach(function (tab, i) {
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', panels[i].id);
      panels[i].setAttribute('role', 'tabpanel');
      panels[i].tabIndex = 0;
      tab.addEventListener('click', function (event) { event.preventDefault(); activate(i, false, true); });
      tab.addEventListener('keydown', function (event) {
        var next;
        var rtl = getComputedStyle(list).direction === 'rtl';
        if (event.key === 'ArrowRight') next = (i + (rtl ? -1 : 1) + tabs.length) % tabs.length;
        if (event.key === 'ArrowLeft') next = (i + (rtl ? 1 : -1) + tabs.length) % tabs.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = tabs.length - 1;
        if (next !== undefined) { event.preventDefault(); activate(next, true, true); }
      });
    });
    function reveal(target) {
      var index = panels.findIndex(function (panel) { return panel === target || panel.contains(target); });
      if (index >= 0) activate(index, false);
    }
    // Capture invalid before native validation or the existing error-summary focus.
    root.addEventListener('invalid', function (event) {
      var form = event.target.form;
      var first = form && Array.from(form.elements).find(function (field) {
        return field.willValidate && !field.validity.valid;
      });
      reveal(first || event.target);
    }, true);
    document.addEventListener('click', function (event) {
      var link = event.target.closest('a[href^="#"]');
      if (link && !list.contains(link)) {
        var target = document.getElementById(link.hash.slice(1));
        if (target) reveal(target);
      }
    }, true);
    function revealHash() {
      var target = document.getElementById(location.hash.slice(1));
      if (target) reveal(target);
    }
    activate(0, false);
    revealHash();
    window.addEventListener('hashchange', revealHash);
    var invalid = root.querySelector('[aria-invalid="true"]');
    if (invalid) reveal(invalid);
  });
  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-component-open]');
    if (trigger) {
      var dialog = document.getElementById(trigger.getAttribute('data-component-open'));
      if (dialog && dialog.matches('dialog[data-component-dialog]') && !dialog.open) {
        dialog._componentOpener = trigger;
        dialog.showModal();
      }
    }
    var close = event.target.closest('[data-component-close]');
    if (close) close.closest('dialog').close();
  });
  document.querySelectorAll('[data-component-dialog]').forEach(function (dialog) {
    dialog.addEventListener('close', function () {
      if (dialog._componentOpener && dialog._componentOpener.isConnected) dialog._componentOpener.focus();
    });
  });
})();
