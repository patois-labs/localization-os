// Workflow step tabs (#594): one tab per step, workflow settings on the first tab.
//
// PROGRESSIVE ENHANCEMENT. The server renders the tablist AND every panel; without this
// script the tab bar stays hidden (CSS) and every panel shows as its own stacked card. This
// file only adds the `wf-tabbed` class, hides the unselected panels with the `hidden`
// attribute (hidden controls still submit with their form), and wires the WAI-ARIA tabs
// pattern: roving tabindex, arrow/Home/End keys, aria-selected.
//
// Contract with the markup:
//   .wf-steptabs[data-wf-tabs][data-initial="<panel id>"]   wrapper (data-initial optional)
//     [role=tablist] > [role=tab][aria-controls=<panel id>][data-invalid]?
//     [role=tabpanel][id][aria-labelledby=<tab id>]     one per tab, anywhere inside wrapper
//   data-wf-tab-store="last"   on a form: after it submits, the NEXT page load selects the
//                              last step tab (used by "Add step" so the new step is shown)
(function () {
  'use strict';

  var TYPE_LABELS = { tm_match: 'TM match', lqa: 'LQA', human_review: 'Review',
                      external: 'External', llm_agent: 'Agent' };

  function storeKey() { return 'wftabs:' + location.pathname; }
  function readStore() { try { return window.sessionStorage.getItem(storeKey()); } catch (e) { return null; } }
  function writeStore(v) {
    try {
      if (v) window.sessionStorage.setItem(storeKey(), v);
      else window.sessionStorage.removeItem(storeKey());
    } catch (e) { /* storage blocked: tabs still work */ }
  }

  function hashTarget() {
    if (location.hash.length < 2) return null;
    try { return document.getElementById(decodeURIComponent(location.hash.slice(1))); }
    catch (e) { return null; }  // malformed %-escape: ignore the hash, keep the tabs working
  }

  function init(root) {
    if (root.dataset.wfTabsReady === '1') return;
    var list = root.querySelector('[role="tablist"]');
    if (!list) return;
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    if (!tabs.length) return;
    var panels = {};
    tabs.forEach(function (t) {
      var p = document.getElementById(t.getAttribute('aria-controls'));
      if (p) panels[t.id] = p;
    });
    root.dataset.wfTabsReady = '1';
    root.classList.add('wf-tabbed');

    function panelOf(tab) { return panels[tab.id]; }
    function tabForPanel(el) {
      for (var i = 0; i < tabs.length; i++) {
        var p = panelOf(tabs[i]);
        if (p && (p === el || p.contains(el))) return tabs[i];
      }
      return null;
    }

    function select(tab, focus) {
      if (!tab) return;
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.setAttribute('tabindex', on ? '0' : '-1');
        t.classList.toggle('active', on);
        var p = panelOf(t);
        if (p) {
          p.hidden = !on;
          // A panel inside a list item (the builder's <ol>) hides its wrapper too, so no
          // empty list row is left behind.
          var w = p.parentElement;
          if (w && w.hasAttribute('data-wf-panel-wrap')) w.hidden = !on;
        }
      });
      if (focus) tab.focus();
      if (tab.scrollIntoView && list.scrollWidth > list.clientWidth) {
        // Keep the selected tab visible inside the (horizontally scrolling) tab bar only.
        var left = tab.offsetLeft - list.offsetLeft;
        if (left < list.scrollLeft || left + tab.offsetWidth > list.scrollLeft + list.clientWidth) {
          list.scrollLeft = Math.max(0, left - 16);
        }
      }
    }

    function reveal(el) {
      var t = el && tabForPanel(el);
      if (t && t.getAttribute('aria-selected') !== 'true') select(t, false);
    }

    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(t, false); writeStore(t.id); });
      t.addEventListener('keydown', function (ev) {
        var n = null;
        if (ev.key === 'ArrowRight') n = tabs[(i + 1) % tabs.length];
        else if (ev.key === 'ArrowLeft') n = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (ev.key === 'Home') n = tabs[0];
        else if (ev.key === 'End') n = tabs[tabs.length - 1];
        if (n) { ev.preventDefault(); select(n, true); writeStore(n.id); }
      });
    });

    // A browser blocks submit on an invalid control it cannot focus (inside a hidden
    // panel). `invalid` does not bubble, so listen in the capture phase and reveal the
    // panel first; the tab is also marked so the author sees where the problem is.
    // One validation pass fires `invalid` for EVERY bad control, in document order, and the
    // browser then focuses the FIRST one. So only the first event of a pass may switch tabs
    // (otherwise the last bad panel wins and the focused control ends up hidden); the later
    // ones still mark their own tab. A new pass starts on the next task.
    var passClaimed = false;
    var clientMarked = [];
    root.addEventListener('invalid', function (ev) {
      var t = tabForPanel(ev.target);
      if (!t) return;
      t.setAttribute('data-invalid', 'true');
      if (clientMarked.indexOf(t) < 0) clientMarked.push(t);
      if (!passClaimed) {
        passClaimed = true;
        window.setTimeout(function () { passClaimed = false; }, 0);
        if (t.getAttribute('aria-selected') !== 'true') select(t, false);
      }
    }, true);
    // Fixing the last bad control in a panel clears the mark that validation put on its tab
    // (a mark the SERVER rendered stays until the page is submitted again).
    function clearFixed(ev) {
      var t = tabForPanel(ev.target);
      var i = t ? clientMarked.indexOf(t) : -1;
      if (i < 0) return;
      var p = panelOf(t);
      if (p && !p.querySelector('input:invalid, select:invalid, textarea:invalid')) {
        t.removeAttribute('data-invalid');
        clientMarked.splice(i, 1);
      }
    }
    root.addEventListener('input', clearFixed);
    root.addEventListener('change', clearFixed);

    // Live tab label: a step's tab follows its Type select and Name box (review page).
    root.querySelectorAll('select.step-type-select').forEach(function (sel) {
      var tab = tabForPanel(sel);
      if (!tab) return;
      var out = tab.querySelector('.wf-tab-type');
      var panel = panelOf(tab);
      var name = panel && panel.querySelector('input[name$="_name"]');
      function refresh() {
        if (!out) return;
        var ty = sel.value;
        var txt = TYPE_LABELS[ty] || ty || 'Step';
        if (ty === 'llm_agent' && name && name.value.trim()) txt = name.value.trim();
        out.textContent = txt;
        tab.title = tab.querySelector('.wf-tab-num').textContent + ' ' + txt;
      }
      sel.addEventListener('change', refresh);
      if (name) name.addEventListener('input', refresh);
    });

    // "Add step" lands on the new step after the page reloads.
    root.querySelectorAll('form[data-wf-tab-store]').forEach(function (f) {
      f.addEventListener('submit', function () { writeStore('__' + f.getAttribute('data-wf-tab-store') + '__'); });
    });

    // Initial tab: server error > URL hash > remembered tab > first.
    var initial = null;
    var want = root.getAttribute('data-initial');
    if (want) initial = document.getElementById(want);
    if (initial && initial.getAttribute('role') !== 'tab') initial = tabForPanel(initial);
    if (!initial) {
      var h = hashTarget();
      if (h) initial = tabForPanel(h);
    }
    if (!initial) {
      var s = readStore();
      if (s === '__last__') {
        var steps = tabs.filter(function (t) { return /wftab-step-/.test(t.id); });
        initial = steps.length ? steps[steps.length - 1] : null;
        writeStore(null);
      } else if (s) {
        initial = tabs.filter(function (t) { return t.id === s; })[0] || null;
      }
    }
    select(initial || tabs[0], false);

    // In-page links (the section nav, "Add a step") that point into a hidden panel.
    window.addEventListener('hashchange', function () {
      var h = hashTarget();
      if (h) { reveal(h); h.scrollIntoView(); }
    });
    root.wfTabsSelectPanel = function (id) { reveal(document.getElementById(id)); };
  }

  window.WfTabs = {
    init: init,
    selectPanel: function (id) {
      var el = document.getElementById(id);
      var root = el && el.closest('[data-wf-tabs]');
      if (root && root.wfTabsSelectPanel) root.wfTabsSelectPanel(id);
    }
  };
  function boot() { document.querySelectorAll('[data-wf-tabs]').forEach(init); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
