"""Issue #115: provider model lists are fetched only after user intent.

This suite executes the shipped JavaScript under Node with a deliberately small
DOM stub.  It guards the interaction contract that source-only assertions miss:

* loading an ordinary page (and initializing workflow model controls) performs
  no ``POST /workflows/models``;
* opening New Project performs the first fetch, while reopening it reuses the
  browser-session/provider cache;
* selecting a different provider fetches that provider once, selecting a cached
  provider reuses its result, and the explicit Fetch button forces a refresh;
* workflow-create and workflow-builder controls are lazy too, but their provider
  change and explicit-fetch paths remain live;
* the cache is scoped to the signed-in user/org context, so one org's model names
  cannot be replayed into another org's UI from sessionStorage.

Fail-pre-fix proof: ``model_catalog.js`` does not exist and each affected surface
contains an eager ``fetchModels(true)`` call at baseline 2a2476b3.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_lazy_models_")


def _inline_script(path: Path, marker: str) -> str:
    """Return the inline script containing *marker* from a Jinja template."""
    source = path.read_text(encoding="utf-8")
    for chunk in source.split('<script nonce="{{ csp_nonce() }}">')[1:]:
        body, sep, _rest = chunk.partition("</script>")
        if sep and marker in body:
            assert "{{" not in body and "{%" not in body, path
            return body
    raise AssertionError(f"inline script containing {marker!r} not found in {path}")


def test_builder_renders_saved_manual_model() -> None:
    """A saved custom model must reach the enabled no-JS/manual control."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Workflow
    from app.services import workflows

    init_db()
    with Session(engine) as session:
        workflow = Workflow(name="Issue 115 saved model", scope="global")
        session.add(workflow)
        session.commit()
        session.refresh(workflow)
        step = workflows.add_step(
            session,
            workflow.id,
            name="Saved model step",
            provider="local",
            model="saved-custom-model",
            prompt_text="Translate: {text}",
        )
        workflow_id = workflow.id
        step_id = step.id

    body = TestClient(app).get(f"/workflows/{workflow_id}").text
    marker = f'data-provider="wfeProvider-{step_id}"'
    start = body.index(marker)
    picker = body[start : start + 900]
    assert 'class="wfmodel-manual" value="saved-custom-model"' in picker, picker
    assert 'class="wfmodel-select" disabled' in picker, picker
    print("  issue #115: rendered builder keeps saved model in enabled manual control OK")


def test_lazy_model_probe_javascript() -> None:
    node = shutil.which("node")
    assert node, "issue #115 regression requires Node to execute the shipped browser JavaScript"

    base = (_ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    catalog_path = _ROOT / "app/static/model_catalog.js"
    wfmodel_path = _ROOT / "app/static/wfmodel.js"
    dialog_js = _inline_script(
        _ROOT / "app/templates/_new_project_dialog.html", "async function fetchModels"
    )
    workflow_new_js = _inline_script(
        _ROOT / "app/templates/workflows_new.html", "async function fetchModels"
    )

    assert catalog_path.is_file(), "shared session/provider model cache is missing"
    assert base.index("/static/model_catalog.js") < base.index("_new_project_dialog.html"), (
        "the shared catalog must load before the global dialog's inline script"
    )

    harness = r"""
const fs = require('fs');
const vm = require('vm');
const catalogSource = fs.readFileSync(process.argv[2], 'utf8');
const wfmodelSource = fs.readFileSync(process.argv[3], 'utf8');
const dialogSource = fs.readFileSync(process.argv[4], 'utf8');
const workflowNewSource = fs.readFileSync(process.argv[5], 'utf8');

function assert(ok, message) { if (!ok) throw new Error(message); }
function target(extra) {
  const listeners = {};
  const element = {
    addEventListener: function (name, fn) { (listeners[name] ||= []).push(fn); },
    removeEventListener: function (name, fn) {
      listeners[name] = (listeners[name] || []).filter(function (x) { return x !== fn; });
    },
    fire: function (name, event) {
      (listeners[name] || []).slice().forEach(function (fn) {
        fn(event || { target: this });
      }, this);
    },
  };
  Object.defineProperties(element, Object.getOwnPropertyDescriptors(extra || {}));
  return element;
}
function select(value) {
  let text = '';
  return target({
    value: value || '', disabled: false, hidden: false, style: {}, children: [],
    appendChild: function (node) { this.children.push(node); },
    insertBefore: function (node, before) {
      const i = this.children.indexOf(before);
      this.children.splice(i < 0 ? this.children.length : i, 0, node);
    },
    get textContent() { return text; },
    set textContent(v) { text = v; if (v === '') this.children = []; },
  });
}
function button() { return target({ disabled: false }); }
function flush() { return new Promise(function (resolve) { setImmediate(resolve); }); }

const stored = {};
global.window = global;
global.sessionStorage = {
  getItem: function (key) {
    return Object.prototype.hasOwnProperty.call(stored, key) ? stored[key] : null;
  },
  setItem: function (key, value) { stored[key] = String(value); },
};
global[String.fromCharCode(67, 83, 82, 70, 95, 84, 79, 75, 69, 78)] =
  String.fromCharCode(99, 115, 114, 102);
global.LOC_MODEL_CACHE_SCOPE = ['org-1', 'user-1'];
let calls = [];
let deferredFetches = null;
function response(models, status) {
  return { ok: !status || status < 400, status: status || 200,
    json: async function () { return { models: models }; } };
}
global.fetch = function (url, opts) {
  assert(url === '/workflows/models', 'unexpected fetch: ' + url);
  const ref = String(opts.body.get('provider'));
  calls.push(ref);
  if (deferredFetches !== null) {
    return new Promise(function (resolve, reject) {
      deferredFetches.push({ ref: ref, resolve: resolve, reject: reject });
    });
  }
  return Promise.resolve(response([ref + '-model']));
};

vm.runInThisContext(catalogSource, { filename: 'model_catalog.js' });
assert(calls.length === 0, 'loading the shared catalog must not probe models');

// Shared cache contract: provider-keyed, session-persistent, explicit refresh,
// and caller-scope confinement.
(async function () {
  await window.LocModelCatalog.get('local');
  await window.LocModelCatalog.get('local');
  assert(calls.join(',') === 'local', 'same-provider reads must be cached: ' + calls);
  await window.LocModelCatalog.get('openai');
  await window.LocModelCatalog.get('local');
  assert(calls.join(',') === 'local,openai', 'provider cache must be independent: ' + calls);
  await window.LocModelCatalog.get('local', { refresh: true });
  assert(calls.join(',') === 'local,openai,local', 'explicit refresh must hit the endpoint');

  // Re-evaluate the module to discard its in-memory map; sessionStorage must
  // still satisfy this browser-session/provider lookup without another POST.
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-reload.js' });
  await window.LocModelCatalog.get('local');
  assert(calls.length === 3, 'sessionStorage cache did not survive page-module reload');
  window.LOC_MODEL_CACHE_SCOPE = ['org-2', 'user-2'];
  await window.LocModelCatalog.get('local');
  assert(calls.length === 4, 'a different org/user scope must not reuse cached model names');

  // Fresh logical browser scope for the New Project behavior.
  window.LOC_MODEL_CACHE_SCOPE = ['org-dialog', 'user-dialog'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-dialog.js' });
  calls = [];
  const opener = button();
  const closer = button();
  const provider = select('local');
  const modelSelect = select(''); modelSelect.disabled = true;
  const manual = target({ value: '', disabled: false, hidden: false });
  const out = target({ textContent: '' });
  const fetchButton = button();
  const describeMap = new Map([
    ['[data-np-desc-provider]', provider],
    ['[data-np-desc-model-select]', modelSelect],
    ['[data-np-desc-model-manual]', manual],
    ['[data-np-desc-model-out]', out],
    ['[data-np-desc-model-fetch]', fetchButton],
  ]);
  const describe = target({
    querySelector: function (sel) { return describeMap.get(sel) || null; },
  });
  const dialog = target({
    open: false,
    showModal: function () { this.open = true; },
    close: function () { this.open = false; },
    querySelector: function (sel) {
      if (sel === '[data-np-describe]') return describe;
      return null;
    },
    querySelectorAll: function (sel) { return sel === '[data-np-close]' ? [closer] : []; },
  });
  global.document = {
    getElementById: function (id) { return id === 'new-project-dialog' ? dialog : null; },
    querySelectorAll: function (sel) { return sel === '[data-np-open]' ? [opener] : []; },
    createElement: function (tag) { return { tagName: tag, value: '', textContent: '' }; },
  };
  global.location = { hash: '' };
  vm.runInThisContext(dialogSource, { filename: '_new_project_dialog.inline.js' });
  await flush();
  assert(calls.length === 0, 'ordinary page load eagerly probed models: ' + calls);
  opener.fire('click', { preventDefault: function () {} });
  await flush(); await flush();
  assert(calls.join(',') === 'local', 'first dialog open must fetch selected provider: ' + calls);
  assert(modelSelect.children.map(function (o) { return o.value; }).join(',') === ',local-model',
    'dialog catalog options were not replaced exactly: ' + modelSelect.children.length);
  closer.fire('click');
  opener.fire('click', { preventDefault: function () {} });
  await flush(); await flush();
  assert(calls.length === 1, 'repeat dialog open must reuse provider cache: ' + calls);
  provider.value = 'openai'; provider.fire('change');
  await flush(); await flush();
  assert(calls.join(',') === 'local,openai',
    'provider change must fetch the new provider: ' + calls);
  assert(modelSelect.children.map(function (o) { return o.value; }).join(',') === ',openai-model',
    'dialog provider change must replace, not append, options');
  provider.value = 'local'; provider.fire('change');
  await flush(); await flush();
  assert(calls.length === 2, 'returning to a cached provider must not reprobe: ' + calls);
  assert(modelSelect.children.map(function (o) { return o.value; }).join(',') === ',local-model',
    'dialog cached provider must replace the visible catalog');
  fetchButton.fire('click');
  await flush(); await flush();
  assert(calls.join(',') === 'local,openai,local', 'explicit dialog fetch must refresh: ' + calls);

  // Reopening while an explicit same-provider refresh is in flight must join
  // that refresh, not render the older cached catalog and invalidate the fresh
  // request's UI generation.
  window.LOC_MODEL_CACHE_SCOPE = ['org-dialog-reopen-refresh', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-dialog-reopen-refresh.js' });
  await window.LocModelCatalog.get('local');
  provider.value = 'local'; provider.fire('change');
  await flush(); await flush();
  assert(modelSelect.children[1].value === 'local-model',
    'reopen-refresh setup did not render the cached Local catalog');
  deferredFetches = [];
  fetchButton.fire('click');
  assert(deferredFetches.length === 1 && deferredFetches[0].ref === 'local',
    'explicit Local refresh did not start exactly one request');
  closer.fire('click');
  opener.fire('click', { preventDefault: function () {} });
  await flush(); await flush();
  assert(deferredFetches.length === 1,
    'dialog reopen did not coalesce with the pending explicit refresh');
  deferredFetches[0].resolve(response(['local-fresh']));
  deferredFetches = null;
  await flush(); await flush();
  assert(modelSelect.children.map(function (o) { return o.value; }).join(',') === ',local-fresh',
    'dialog reopen discarded the fresh explicit-refresh catalog');

  // A non-refresh reopen must retain its selected model if Fetch fails.
  deferredFetches = [];
  fetchButton.fire('click');
  deferredFetches[0].resolve(response(['local-a', 'local-b']));
  await flush(); await flush();
  modelSelect.value = 'local-b';
  fetchButton.fire('click');
  closer.fire('click');
  opener.fire('click', { preventDefault: function () {} });
  assert(deferredFetches.length === 2, 'reopen must join the explicit refresh');
  deferredFetches[1].reject(new Error('refresh connection dropped'));
  deferredFetches = null;
  await flush(); await flush();
  assert(!modelSelect.disabled && manual.disabled && modelSelect.value === 'local-b',
    'MED-1: failed joined refresh lost the selected model or enabled manual input');
  assert(modelSelect.children.map(function (o) { return o.value; }).join(',') ===
    ',local-a,local-b', 'MED-1: failed joined refresh lost the cached list');
  modelSelect.value = '';

  // A late provider response (success or failure) must not overwrite the
  // currently selected provider's cached catalog.
  window.LOC_MODEL_CACHE_SCOPE = ['org-dialog-stale-success', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-dialog-stale-success.js' });
  await window.LocModelCatalog.get('local');
  deferredFetches = [];
  provider.value = 'openai'; provider.fire('change');
  provider.value = 'local'; provider.fire('change');
  await flush(); await flush();
  assert(modelSelect.children[1].value === 'local-model', 'cached Local catalog was not restored');
  const dialogLateSuccess = deferredFetches[0];
  assert(dialogLateSuccess.ref === 'openai', 'wrong deferred dialog provider');
  dialogLateSuccess.resolve(response(['openai-late']));
  deferredFetches = null;
  await flush(); await flush();
  assert(modelSelect.children[1].value === 'local-model',
    'late dialog success overwrote the current Local catalog');

  window.LOC_MODEL_CACHE_SCOPE = ['org-dialog-stale-failure', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-dialog-stale-failure.js' });
  await window.LocModelCatalog.get('local');
  deferredFetches = [];
  provider.value = 'openai'; provider.fire('change');
  provider.value = 'local'; provider.fire('change');
  await flush(); await flush();
  const dialogLateFailure = deferredFetches[0];
  dialogLateFailure.reject(new Error('late dialog failure'));
  deferredFetches = null;
  await flush(); await flush();
  assert(!modelSelect.disabled && modelSelect.children[1].value === 'local-model',
    'late dialog failure replaced the current Local list with manual fallback');

  // Non-ok responses must populate neither cache tier; reopening retries.
  for (const status of [403, 500]) {
    window.LOC_MODEL_CACHE_SCOPE = ['org-dialog-http-' + status, 'user'];
    const storageBefore = JSON.stringify(stored);
    calls = []; deferredFetches = [];
    closer.fire('click');
    opener.fire('click', { preventDefault: function () {} });
    deferredFetches[0].resolve(response([], status));
    await flush(); await flush();
    assert(modelSelect.disabled && !manual.disabled, 'HTTP error must use manual fallback');
    assert(JSON.stringify(stored) === storageBefore,
      'LOW-3: HTTP ' + status + ' was cached in sessionStorage');
    closer.fire('click');
    opener.fire('click', { preventDefault: function () {} });
    assert(calls.length === 2, 'LOW-3: HTTP error was cached in memory; reopen must retry');
    deferredFetches[1].resolve(response(['local-recovered']));
    deferredFetches = null;
    await flush(); await flush();
    assert(!modelSelect.disabled && manual.disabled &&
      modelSelect.children[1].value === 'local-recovered', 'HTTP retry did not render models');
  }

  // Failed-create redirects open before picker initialization.
  window.LOC_MODEL_CACHE_SCOPE = ['org-dialog-hash-new', 'user'];
  calls = []; dialog.close(); modelSelect.value = ''; modelSelect.textContent = '';
  modelSelect.disabled = true; manual.disabled = false;
  global.location = { hash: '#new' };
  vm.runInThisContext(dialogSource, { filename: '_new_project_dialog.hash-new.js' });
  await flush(); await flush();
  assert(dialog.open && calls.join(',') === 'local',
    'LOW-4: #new must open and request exactly once: ' + calls);
  assert(!modelSelect.disabled && manual.disabled &&
    modelSelect.children.map(function (o) { return o.value; }).join(',') === ',local-model',
    'LOW-4: #new did not render the model list');

  // Workflow creation page: initialization is quiet; provider change is lazy;
  // explicit fetch refreshes even when that provider is cached.
  window.LOC_MODEL_CACHE_SCOPE = ['org-workflow-new', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-workflow-new.js' });
  calls = [];
  const wfProvider = select('local');
  const wfSelect = select(''); wfSelect.disabled = true;
  const wfManual = target({ value: '', disabled: false, style: {} });
  const wfOut = target({ textContent: '' });
  const wfFetch = button();
  const wfToggle = button();
  const wfWrap = target({
    dataset: { provider: 'wf-describe-provider' },
    querySelector: function (sel) {
      return ({ '.wfmodel-select': wfSelect, '.wfmodel-manual': wfManual,
        '.wfmodel-out': wfOut, '.wfmodel-fetch': wfFetch,
        '.wfmodel-toggle': wfToggle })[sel] || null;
    },
  });
  global.document = {
    querySelector: function () { return wfWrap; },
    getElementById: function (id) { return id === 'wf-describe-provider' ? wfProvider : null; },
    createElement: function (tag) { return { tagName: tag, value: '', textContent: '' }; },
  };
  vm.runInThisContext(workflowNewSource, { filename: 'workflows_new.inline.js' });
  await flush();
  assert(calls.length === 0, 'workflow-create page load eagerly probed models: ' + calls);
  wfProvider.value = '';
  wfManual.value = 'my-typed-model';
  wfToggle.fire('click');
  assert(wfSelect.disabled && !wfManual.disabled && wfManual.value === 'my-typed-model',
    'LOW-2: empty list toggle disabled the typed manual model');
  assert(calls.length === 0, 'empty provider toggle must not request a catalog');
  wfProvider.value = 'local';
  deferredFetches = [];
  wfToggle.fire('click');
  assert(wfSelect.disabled && !wfManual.disabled && deferredFetches.length === 1,
    'LOW-2: list toggle must fetch lazily and keep manual mode while pending');
  deferredFetches[0].resolve(response([]));
  await flush(); await flush();
  assert(wfSelect.disabled && !wfManual.disabled, 'empty catalog must keep manual mode');
  wfProvider.value = 'openai';
  wfToggle.fire('click');
  deferredFetches[1].resolve(response(['my-typed-model']));
  deferredFetches = null;
  await flush(); await flush();
  assert(!wfSelect.disabled && wfManual.disabled && wfSelect.value === 'my-typed-model',
    'LOW-2: successful list toggle must preserve the typed model');
  window.LOC_MODEL_CACHE_SCOPE = ['org-workflow-new-after-toggle', 'user'];
  wfProvider.value = 'local'; wfManual.value = ''; wfSelect.value = ''; calls = [];
  wfProvider.fire('change'); await flush(); await flush();
  assert(calls.join(',') === 'local', 'workflow-create provider change must fetch: ' + calls);
  wfFetch.fire('click'); await flush(); await flush();
  assert(calls.join(',') === 'local,local',
    'workflow-create explicit fetch must refresh: ' + calls);
  assert(wfSelect.children.map(function (o) { return o.value; }).join(',') === ',local-model',
    'workflow-create options were not replaced exactly');

  window.LOC_MODEL_CACHE_SCOPE = ['org-workflow-new-stale-success', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-workflow-new-stale-1.js' });
  await window.LocModelCatalog.get('local');
  deferredFetches = [];
  wfProvider.value = 'openai'; wfProvider.fire('change');
  wfProvider.value = 'local'; wfProvider.fire('change');
  await flush(); await flush();
  const wfNewLateSuccess = deferredFetches[0];
  wfNewLateSuccess.resolve(response(['openai-late']));
  deferredFetches = null;
  await flush(); await flush();
  assert(wfSelect.children[1].value === 'local-model',
    'late workflow-create success overwrote Local');

  window.LOC_MODEL_CACHE_SCOPE = ['org-workflow-new-stale-failure', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-workflow-new-stale-2.js' });
  await window.LocModelCatalog.get('local');
  deferredFetches = [];
  wfProvider.value = 'openai'; wfProvider.fire('change');
  wfProvider.value = 'local'; wfProvider.fire('change');
  await flush(); await flush();
  const wfNewLateFailure = deferredFetches[0];
  wfNewLateFailure.reject(new Error('late workflow-create failure'));
  deferredFetches = null;
  await flush(); await flush();
  assert(!wfSelect.disabled && wfSelect.children[1].value === 'local-model',
    'late workflow-create failure replaced Local with manual fallback');

  // Shared workflow-builder/review control: init is quiet, provider change fetches,
  // a second control with the same provider reuses the cache, and explicit fetch
  // forces one refresh.
  window.LOC_MODEL_CACHE_SCOPE = ['org-builder', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-builder.js' });
  calls = [];
  const builderProvider = select('local');
  const detailsProvider = select('openai');
  const savedProvider = select('saved-provider');
  const detailsHost = target({ open: false });
  function modelWrap(providerId, host, savedModel) {
    const sel = select(''); sel.disabled = true;
    const man = target({ value: savedModel || '', disabled: false, style: {} });
    const fetchBtn = button();
    const toggleBtn = button();
    const outEl = target({ textContent: '' });
    return target({
      dataset: { provider: providerId },
      closest: function () { return host || null; },
      querySelector: function (query) {
        return ({ '.wfmodel-select': sel, '.wfmodel-manual': man,
          '.wfmodel-fetch': fetchBtn, '.wfmodel-toggle': toggleBtn,
          '.wfmodel-out': outEl })[query] || null;
      },
      _fetch: fetchBtn,
      _toggle: toggleBtn,
      _sel: sel,
      _man: man,
    });
  }
  const wrapA = modelWrap('builder-provider', null);
  const wrapB = modelWrap('builder-provider', null);
  const wrapInDetails = modelWrap('details-provider', detailsHost);
  const wrapSaved = modelWrap('saved-provider', null, 'saved-custom-model');
  global.document = {
    querySelectorAll: function (sel) {
      return sel === '.wfmodel' ? [wrapA, wrapB, wrapInDetails, wrapSaved] : [];
    },
    getElementById: function (id) {
      if (id === 'builder-provider') return builderProvider;
      if (id === 'details-provider') return detailsProvider;
      if (id === 'saved-provider') return savedProvider;
      return null;
    },
    createElement: function (tag) { return { tagName: tag, value: '', textContent: '' }; },
  };
  vm.runInThisContext(wfmodelSource, { filename: 'wfmodel.js' });
  initWfModels({ lazyDetails: true });
  await flush();
  assert(calls.length === 0, 'workflow-builder initialization eagerly probed models: ' + calls);
  wrapSaved._toggle.fire('click');
  assert(!wrapSaved._man.disabled,
    'list toggle disabled the saved manual model before a catalog was available');
  await flush(); await flush();
  assert(wrapSaved._man.disabled && !wrapSaved._sel.disabled,
    'successful lazy toggle did not switch to list mode');
  assert(wrapSaved._sel.value === 'saved-custom-model',
    'lazy list toggle did not preserve the saved model selection');
  assert(wrapSaved._sel.children.map(function (o) { return o.value; }).join(',') ===
      ',saved-custom-model,saved-provider-model',
    'saved custom model was not retained as the current option');
  calls = [];
  detailsHost.open = true; detailsHost.fire('toggle');
  await flush(); await flush();
  assert(calls.join(',') === 'openai', 'opening a step editor must fetch lazily: ' + calls);
  detailsHost.open = false; detailsHost.fire('toggle');
  detailsHost.open = true; detailsHost.fire('toggle');
  await flush(); await flush();
  assert(calls.length === 1, 'repeat step-editor opens must stay bounded: ' + calls);
  builderProvider.fire('change'); await flush(); await flush();
  assert(calls.join(',') === 'openai,local',
    'same-provider controls should share one cached fetch: ' + calls);
  wrapA._fetch.fire('click'); await flush(); await flush();
  assert(calls.join(',') === 'openai,local,local',
    'builder explicit fetch must refresh: ' + calls);

  window.LOC_MODEL_CACHE_SCOPE = ['org-builder-stale-success', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-builder-stale-1.js' });
  await window.LocModelCatalog.get('local');
  deferredFetches = [];
  builderProvider.value = 'openai'; builderProvider.fire('change');
  builderProvider.value = 'local'; builderProvider.fire('change');
  await flush(); await flush();
  const builderLateSuccess = deferredFetches[0];
  builderLateSuccess.resolve(response(['openai-late']));
  deferredFetches = null;
  await flush(); await flush();
  assert(wrapA._sel.children[1].value === 'local-model',
    'late builder success overwrote Local');

  window.LOC_MODEL_CACHE_SCOPE = ['org-builder-stale-failure', 'user'];
  vm.runInThisContext(catalogSource, { filename: 'model_catalog-builder-stale-2.js' });
  await window.LocModelCatalog.get('local');
  deferredFetches = [];
  builderProvider.value = 'openai'; builderProvider.fire('change');
  builderProvider.value = 'local'; builderProvider.fire('change');
  await flush(); await flush();
  const builderLateFailure = deferredFetches[0];
  builderLateFailure.reject(new Error('late builder failure'));
  deferredFetches = null;
  await flush(); await flush();
  assert(!wrapA._sel.disabled && wrapA._sel.children[1].value === 'local-model',
    'late builder failure replaced Local with manual fallback');

  console.log('lazy model probe JavaScript behavior OK');
})().catch(function (err) { console.error(err.stack || err); process.exit(1); });
"""

    with tempfile.TemporaryDirectory(prefix="loctms_lazy_models_") as tmp:
        tmp_path = Path(tmp)
        dialog_path = tmp_path / "dialog.js"
        workflow_new_path = tmp_path / "workflow_new.js"
        harness_path = tmp_path / "harness.js"
        dialog_path.write_text(dialog_js, encoding="utf-8")
        workflow_new_path.write_text(workflow_new_js, encoding="utf-8")
        harness_path.write_text(harness, encoding="utf-8")
        run = subprocess.run(
            [
                node,
                str(harness_path),
                str(catalog_path),
                str(wfmodel_path),
                str(dialog_path),
                str(workflow_new_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert run.returncode == 0, (run.stdout, run.stderr)
    assert "lazy model probe JavaScript behavior OK" in run.stdout, run.stdout
    print("  issue #115: lazy dialog/workflow probes + scoped session/provider cache OK")


def test_setup_refusal_javascript():
    node = shutil.which("node")
    assert node, "Setup AI refusal regression requires Node"
    source = (_ROOT / "app/templates/setup_ai.html").read_text(encoding="utf-8")
    functions = []
    for name in ("fetchModels", "fetchEngineModels"):
        start = source.index("async function " + name + "(")
        functions.append(source[start : source.index("\n}", start) + 2])
    harness = r"""
const assert = require('assert');
const out = {textContent: ''};
const sel = {value: 'saved-model'};
global.window = {};
global.document = {getElementById: id => id === 'localout' ? out :
  id === 'lmodelSelect' ? sel : {value: 'http://metadata'}};
global.localBody = () => new URLSearchParams();
global.engModelEls = () => ({out, sel});
const btn = {closest: () => ({querySelector: () => ({value: 'http://metadata'})})};
(async () => {
  for (const data of [{blocked: true, error: 'Blocked target: metadata'},
                      {blocked: true}, {error: '<b>custom refusal</b>'}]) {
    global.fetch = async () => ({json: async () => ({models: [], ...data})});
    for (const call of [() => fetchModels(false), () => fetchModels(true),
                        () => fetchEngineModels(btn)]) {
      await call();
      assert(out.textContent.includes(data.error || 'refused'), out.textContent);
      assert(!out.textContent.includes('no models'), out.textContent);
      assert.equal(sel.value, 'saved-model');
    }
  }
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    run = subprocess.run(
        [node, "-e", "\n".join(functions) + harness],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    print("  Setup AI local/Add/Edit pickers render refusals as text OK")


def test_setup_forbidden_is_not_unreachable():
    """Issue #546: a 403 from the admin-only models endpoint (a linguist opening the page)
    is not 'server unreachable'; the silent page-load probe says nothing at all."""
    node = shutil.which("node")
    assert node, "Setup AI 403 regression requires Node"
    source = (_ROOT / "app/templates/setup_ai.html").read_text(encoding="utf-8")
    functions = []
    for name in ("fetchModels", "fetchEngineModels"):
        start = source.index("async function " + name + "(")
        functions.append(source[start : source.index("\n}", start) + 2])
    harness = r"""
const assert = require('assert');
const out = {textContent: ''};
const sel = {value: 'saved-model'};
global.window = {};
global.document = {getElementById: id => id === 'localout' ? out :
  id === 'lmodelSelect' ? sel : {value: 'http://127.0.0.1:1234'}};
global.localBody = () => new URLSearchParams();
global.engModelEls = () => ({out, sel});
const btn = {closest: () => ({querySelector: () => ({value: 'http://127.0.0.1:1234'})})};
(async () => {
  // the real 403 is an HTML error page, so .json() throws
  global.fetch = async () => ({status: 403, ok: false,
    json: async () => { throw new SyntaxError('Unexpected token <'); }});
  out.textContent = '';
  await fetchModels(true);
  assert.equal(out.textContent, '', 'silent probe must stay quiet on 403: ' + out.textContent);
  for (const call of [() => fetchModels(false), () => fetchEngineModels(btn)]) {
    out.textContent = '';
    await call();
    assert(/administrator/i.test(out.textContent), out.textContent);
    assert(!/unreachable|could not reach/i.test(out.textContent), out.textContent);
  }
  // a genuine network failure still says so
  global.fetch = async () => { throw new TypeError('network down'); };
  out.textContent = '';
  await fetchModels(true);
  assert(/unreachable/.test(out.textContent), out.textContent);
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    run = subprocess.run(
        [node, "-e", "\n".join(functions) + harness],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    print("  Setup AI: 403 is not 'server unreachable' (silent on load) OK")


def main() -> None:
    test_setup_forbidden_is_not_unreachable()
    test_setup_refusal_javascript()
    test_builder_renders_saved_manual_model()
    test_lazy_model_probe_javascript()
    print("verify_lazy_model_probe: OK")


if __name__ == "__main__":
    main()
