// Per-step model dropdowns (shared by workflow_builder.html and
// workflow_author_review.html). Each .wfmodel block holds a select+manual
// pair sharing one name (only the ENABLED one submits). data-provider names
// the id of the provider <select> that drives it: on change or explicit fetch we
// POST /workflows/models for that ref, rebuild the option list ("(default)"
// first), and switch to list mode. An empty list (keyless provider,
// non-listable engine kind, or the "(default)" provider) falls back to the
// free-text input. Options are built via createElement only (no innerHTML -
// XSS). Without JS, the manual text input submits (list stays
// disabled/hidden) - graceful degradation.
//
// initWfModels({ lazyDetails }) - pass lazyDetails: true (workflow_builder's
// per-step edit forms live inside a collapsed <details>; fetching on load for
// every one of them would fire 2xN silent POSTs to /workflows/models on every
// page view) to defer each block's initial fetch until its nearest <details>
// ancestor is opened. All other controls stay quiet on page load and fetch only
// after a provider change or an explicit Fetch-models click.
function initWfModels(opts) {
  opts = opts || {};
  var lazyDetails = !!opts.lazyDetails;

  function els(wrap) {
    return { sel: wrap.querySelector('.wfmodel-select'),
             man: wrap.querySelector('.wfmodel-manual'),
             out: wrap.querySelector('.wfmodel-out'),
             lbl: wrap.querySelector('.wfmodel-toggle-label'),
             prov: document.getElementById(wrap.dataset.provider) };
  }
  // The toggle label: its icon glyph in an aria-hidden span (a screen reader must
  // not read the symbol), then the words.
  function setLabel(lbl, glyph, text) {
    var g = document.createElement('span');
    g.setAttribute('aria-hidden', 'true');
    g.textContent = glyph;
    lbl.replaceChildren(g, ' ' + text);
  }
  function useList(wrap) {
    var e = els(wrap); if (!e.sel) return;
    e.man.disabled = true; e.man.style.display = 'none';
    e.sel.disabled = false; e.sel.style.display = '';
    if (e.lbl) setLabel(e.lbl, '✎', 'Type id manually');
  }
  function useManual(wrap) {
    var e = els(wrap); if (!e.sel) return;
    e.sel.disabled = true; e.sel.style.display = 'none';
    e.man.disabled = false; e.man.style.display = '';
    if (e.lbl) setLabel(e.lbl, '☰', 'Pick from list');
  }
  function toggle(wrap) {
    var e = els(wrap);
    if (e.man.disabled) { e.man.value = e.sel.value || e.man.value; useManual(wrap); }
    // Quiet initialization leaves the list empty. Do not disable a populated
    // saved/manual value until a deliberate list request has succeeded.
    else { fetchModels(wrap, false); }
  }
  function requestIsCurrent(wrap, ref, generation) {
    var e = els(wrap);
    return wrap._wfModelGeneration === generation &&
      ((e.prov && e.prov.value) || '') === ref;
  }
  async function fetchModels(wrap, silent, refresh) {
    var e = els(wrap);
    var generation = (wrap._wfModelGeneration || 0) + 1;
    wrap._wfModelGeneration = generation;
    var ref = e.prov ? e.prov.value : '';
    if (!ref) {  // "(default)" provider - no specific model list
      if (!silent) e.out.textContent = 'provider default - leave blank, or type an id manually.';
      useManual(wrap); return;
    }
    if (!silent) e.out.textContent = 'fetching models…';
    var d;
    try {
      d = { models: await window.LocModelCatalog.get(ref, { refresh: !!refresh }) };
    } catch (err) {
      if (!requestIsCurrent(wrap, ref, generation)) return;
      e.out.textContent = 'could not fetch models - type an id manually.';
      useManual(wrap); return;
    }
    if (!requestIsCurrent(wrap, ref, generation)) return;
    if (!d.models || !d.models.length) {
      e.out.textContent = 'no model list for this provider - type an id manually (blank = provider default).';
      useManual(wrap); return;
    }
    var cur = ((e.man.value || e.sel.value) || '').trim();
    e.sel.textContent = '';
    var def = document.createElement('option'); def.value = ''; def.textContent = '(default)'; e.sel.appendChild(def);
    d.models.forEach(function (m) { var o = document.createElement('option'); o.value = m; o.textContent = m; e.sel.appendChild(o); });
    if (cur && d.models.indexOf(cur) === -1) {  // keep a typed/custom id not in the list
      var o = document.createElement('option'); o.value = cur; o.textContent = cur + ' (current)';
      e.sel.insertBefore(o, e.sel.children[1] || null);
    }
    e.sel.value = cur;  // '' selects "(default)"; a known id selects that option
    useList(wrap);
    e.out.textContent = d.models.length + ' model(s) - pick one, or (default).';
  }
  document.querySelectorAll('.wfmodel').forEach(function (wrap) {
    var e = els(wrap);
    wrap.querySelector('.wfmodel-fetch').addEventListener('click', function () {
      // Busy indicators: a deliberate Fetch shows the shared busy state (the silent
      // on-load / provider-change fetches below stay quiet).
      var p = fetchModels(wrap, false, true);
      if (window.LocBusy) { window.LocBusy.wrap(this, p); }
    });
    wrap.querySelector('.wfmodel-toggle').addEventListener('click', function () { toggle(wrap); });
    if (e.prov) e.prov.addEventListener('change', function () { fetchModels(wrap); });
    if (lazyDetails) {
      // Per-step edit forms live inside a collapsed <details> (one pair of .wfmodel
      // blocks per step) - wait for the first user open. The shared provider cache
      // coalesces both model controls in a step and bounds repeat opens.
      var host = wrap.closest('details');
      if (host && !host.open) {
        var fetchOnFirstOpen = function () {
          host.removeEventListener('toggle', fetchOnFirstOpen);
          if (host.open) fetchModels(wrap, true);
        };
        host.addEventListener('toggle', fetchOnFirstOpen);
        return;
      }
    }
  });
}
