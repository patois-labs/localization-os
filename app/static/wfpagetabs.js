// Workflow page top-level tabs (#630): Steps, System prompt, Add a step, Prompt-authoring
// assistant. Each is a real tab that SWITCHES the panel (it does not scroll). The System prompt
// is its own tab and is never one of the step tabs inside Steps (those belong to wftabs.js).
//
// PROGRESSIVE ENHANCEMENT: the server renders the tablist and every panel; without this script
// the bar stays hidden (CSS) and the panels show stacked. This file adds `wf-tabbed`, hides the
// unselected panels with `hidden`, and wires the WAI-ARIA tabs pattern: role=tablist/tab/tabpanel,
// roving tabindex, ArrowLeft/ArrowRight/Home/End, aria-selected.
//
// The chosen tab lives in the URL hash (each tab carries data-hash, an anchor that already
// existed on the page: #wf-steps, #system-prompt, #wf-add-step, #wf-author), so a link or a
// reload keeps the tab. A hash that points INSIDE a panel (#wsp-step_head, #wftab-step-7) opens
// the panel that holds it. Leaving a panel that holds unapplied prompt edits asks first
// (window.LocPromptLock, in the page, "You will lose your changes").
(function () {
  'use strict';

  function init(root) {
    if (root.dataset.wfPageTabsReady === '1') { return; }
    var list = root.querySelector(':scope > [role="tablist"]');
    if (!list) { return; }
    var tabs = Array.prototype.slice.call(list.querySelectorAll(':scope > [role="tab"]'));
    if (!tabs.length) { return; }
    var panels = {};
    tabs.forEach(function (t) { panels[t.id] = document.getElementById(t.getAttribute('aria-controls')); });
    root.dataset.wfPageTabsReady = '1';
    root.classList.add('wf-tabbed');

    function current() {
      return tabs.filter(function (t) { return t.getAttribute('aria-selected') === 'true'; })[0] || tabs[0];
    }
    function tabFor(node) {
      for (var i = 0; i < tabs.length; i++) {
        var p = panels[tabs[i].id];
        if (p && (p === node || p.contains(node))) { return tabs[i]; }
      }
      return null;
    }
    function hashNode() {
      if (location.hash.length < 2) { return null; }
      try { return document.getElementById(decodeURIComponent(location.hash.slice(1))); }
      catch (e) { return null; }   // a malformed %-escape: ignore the hash
    }

    function show(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.setAttribute('tabindex', on ? '0' : '-1');
        t.classList.toggle('active', on);
        if (panels[t.id]) { panels[t.id].hidden = !on; }
      });
      if (focus) { tab.focus(); }
    }

    // User-driven switch: ask first when the panel being left holds unapplied prompt edits.
    // Resolves true when the tab is (now) shown, false when the person chose to stay.
    function select(tab, focus, keepHash) {
      if (!tab || tab === current()) { if (tab && focus) { tab.focus(); } return Promise.resolve(true); }
      var L = window.LocPromptLock;
      var leaving = panels[current().id];
      var gate = (L && leaving) ? L.confirmLeave(leaving, current()) : Promise.resolve(true);
      return gate.then(function (ok) {
        if (!ok) { return false; }
        show(tab, focus);
        if (!keepHash && tab.dataset.hash && window.history && history.replaceState) {
          // replaceState: no scroll jump, no extra back-button stops
          history.replaceState(null, '', location.pathname + location.search + '#' + tab.dataset.hash);
        }
        return true;
      });
    }

    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(t, false); });
      t.addEventListener('keydown', function (ev) {
        var n = null;
        if (ev.key === 'ArrowRight') { n = tabs[(i + 1) % tabs.length]; }
        else if (ev.key === 'ArrowLeft') { n = tabs[(i - 1 + tabs.length) % tabs.length]; }
        else if (ev.key === 'Home') { n = tabs[0]; }
        else if (ev.key === 'End') { n = tabs[tabs.length - 1]; }
        if (n) { ev.preventDefault(); select(n, true); }
      });
    });

    // "Add step" lands on the new step after the page reloads: wftabs.js reads this key. The
    // form now lives outside its root, so it is written here (same key and value).
    root.querySelectorAll('form[data-wf-tab-store]').forEach(function (f) {
      f.addEventListener('submit', function () {
        try { window.sessionStorage.setItem('wftabs:' + location.pathname, '__' + f.getAttribute('data-wf-tab-store') + '__'); }
        catch (e) { /* storage blocked: the step tabs still work */ }
      });
    });

    // Initial tab: server (data-initial) > URL hash > first.
    var initial = null;
    var want = root.getAttribute('data-initial');
    if (want) { initial = document.getElementById(want); }
    if (!initial) {
      var h = hashNode();
      if (h) { initial = tabFor(h); }
    }
    show(initial || tabs[0], false);
    if (!want) {
      var hn = hashNode();
      if (hn && tabFor(hn) && hn.scrollIntoView) { hn.scrollIntoView(); }
    }

    // In-page links and manual hash edits. The hash has ALREADY changed when this fires, so if
    // the person chooses Stay on "You will lose your changes", put the previous hash back
    // (replaceState: no new hashchange, no history entry) so the URL and the visible tab agree.
    window.addEventListener('hashchange', function (ev) {
      var n = hashNode();
      var t = n && tabFor(n);
      if (!t) { return; }
      var old = (ev && ev.oldURL) || '';
      var at = old.indexOf('#');
      var prevHash = at >= 0 ? old.slice(at) : '';
      select(t, false, true).then(function (ok) {
        if (ok) {
          if (n.scrollIntoView) { n.scrollIntoView(); }
        } else if (window.history && history.replaceState) {
          history.replaceState(null, '', location.pathname + location.search + prevHash);
        }
      });
    });

    root.wfPageTabsSelect = function (anchorId) {
      var n = document.getElementById(anchorId);
      var t = n && tabFor(n);
      return t ? select(t, false) : Promise.resolve();
    };
  }

  window.WfPageTabs = {
    init: init,
    // select the tab that holds the element with this id (an anchor id or any element in a panel)
    select: function (id) {
      var r = document.querySelector('[data-wf-pagetabs]');
      return (r && r.wfPageTabsSelect) ? r.wfPageTabsSelect(id) : Promise.resolve();
    }
  };

  function boot() { document.querySelectorAll('[data-wf-pagetabs]').forEach(init); }
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', boot); }
  else { boot(); }
})();
