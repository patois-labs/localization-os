"""Browser JS resilience pins (issue #545), run in real Chromium, offline.

  [1] jobs.js        a 404 (pruned or other-org job) or 401 (expired session) is NOT "Done.":
                     the label says why and polling STOPS; repeated server errors stop after a
                     bounded number of retries; a normal running -> completed run still works.
  [2] sse.js         the retry budget counts CONSECUTIVE failures: a stream that delivers
                     events and then drops many times never gives up, while a stream that never
                     connects still does.
  [3] queuedrun.js   a 404 or 401 run status stops polling and says why.
  [4] sidebar_ux.js  dragging a nav link outside edit mode never saves a new order; the same drag
                     in edit mode still does (non-vacuous).
  [5] connectors.js  a second "Load from Airtable" re-wires the pickers to the NEW payload (the
                     reused select keeps the latest pick handler).

Final line: "verify_browser_js_resilience OK".
"""

import os
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jsres_")

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from app.db import init_db
from app.main import app
from app.services import settings

init_db()
settings.set_many({"security_auth_required": "0"})
settings.invalidate_cache()

STATIC = Path("app/static")


def _src(name):
    return (STATIC / name).read_text(encoding="utf-8")


# setTimeout with the delay collapsed so retries/polls run at once; the real one is kept.
FAST_TIMERS = """
window.__realSetTimeout = window.setTimeout.bind(window);
window.setTimeout = (fn) => window.__realSetTimeout(fn, 0);
"""


def new_page(browser, html="<body></body>", storage=False):
    """Serve `html` from a fake http origin (about:blank denies localStorage); every other
    request is aborted so no asset or network call can leak out."""
    page = browser.new_page(viewport={"width": 1440, "height": 900})

    def handle(route):
        if route.request.url == "http://app.test/":
            route.fulfill(status=200, content_type="text/html", body=html)
        else:
            route.abort()

    page.route("**/*", handle)
    page.goto("http://app.test/")
    return page


def check_jobs(browser):
    html = '<div id="w"><div class="prog-bar"></div><span class="job-label"></span></div>'
    for status, needle in ((404, "no longer available"), (401, "session expired")):
        page = new_page(browser, html)
        page.evaluate(FAST_TIMERS)
        page.evaluate(
            """(st) => { window.__calls = 0;
            window.fetch = async () => { window.__calls++;
              return {ok: false, status: st, json: async () => ({error: 'x'})}; }; }""",
            status,
        )
        page.add_script_tag(content=_src("jobs.js"))
        page.evaluate("watchJob(1, document.querySelector('#w'), {reloadOnDone:false})")
        page.wait_for_function("document.querySelector('.job-label').textContent !== ''")
        page.wait_for_timeout(400)
        label = page.locator(".job-label").inner_text()
        assert needle in label, (status, label)
        assert "Done" not in label, (status, label)
        assert page.evaluate("window.__calls") == 1, ("kept polling", status)
        assert page.locator("#w.job-failed").count() == 1
        page.close()
    # transient server errors: bounded retries, then a message
    page = new_page(browser, html)
    page.evaluate(FAST_TIMERS)
    page.evaluate(
        """() => { window.__calls = 0;
        window.fetch = async () => { window.__calls++;
          return {ok: false, status: 500, json: async () => ({})}; }; }"""
    )
    page.add_script_tag(content=_src("jobs.js"))
    page.evaluate("watchJob(1, document.querySelector('#w'), {reloadOnDone:false})")
    page.wait_for_function("document.querySelector('.job-label').textContent !== ''")
    page.wait_for_timeout(400)
    calls = page.evaluate("window.__calls")
    assert 2 <= calls <= 10, calls
    assert "Done" not in page.locator(".job-label").inner_text()
    page.close()
    # a body with no status (e.g. an HTML login page parsed as {}) is not "Done." either
    page = new_page(browser, html)
    page.evaluate(FAST_TIMERS)
    page.evaluate(
        """() => { window.__calls = 0;
        window.fetch = async () => { window.__calls++;
          return {ok: true, status: 200, json: async () => ({})}; }; }"""
    )
    page.add_script_tag(content=_src("jobs.js"))
    page.evaluate("watchJob(1, document.querySelector('#w'), {reloadOnDone:false})")
    page.wait_for_timeout(500)
    assert "Done" not in page.locator(".job-label").inner_text()
    assert page.evaluate("window.__calls") <= 10
    page.close()
    # non-vacuous: running then completed still finishes with "Done."
    page = new_page(browser, html)
    page.evaluate(FAST_TIMERS)
    page.evaluate(
        """() => { window.__n = 0;
        window.fetch = async () => { window.__n++;
          const j = window.__n < 3
            ? {status: 'running', total: 4, done: 1, result: {}, message: 'Working'}
            : {status: 'completed', total: 4, done: 4, result: {}, message: 'ok'};
          return {ok: true, status: 200, json: async () => j}; }; }"""
    )
    page.add_script_tag(content=_src("jobs.js"))
    page.evaluate("watchJob(1, document.querySelector('#w'), {reloadOnDone:false})")
    page.wait_for_function("document.querySelector('.job-label').textContent.startsWith('Done')")
    assert page.evaluate("document.querySelector('.prog-bar').style.width") == "100%"
    page.close()
    print("  [1] jobs.js: 404/401 stop with a reason, errors bounded, normal run still Done OK")


SSE_HARNESS = """
(mode) => new Promise((resolve) => {
  window.__realSetTimeout = window.setTimeout.bind(window);
  window.setTimeout = (fn) => window.__realSetTimeout(fn, 0);
  let connects = 0, messages = 0, gaveUp = 0, handle = null;
  window.fetch = async () => {
    connects++;
    if (mode === 'never') throw new Error('offline');
    if (connects > 9) { handle.close(); resolve({connects, messages, gaveUp}); }
    const enc = new TextEncoder();
    let sent = false;
    return { ok: true, headers: { get: () => 'text/event-stream' },
      body: { getReader: () => ({ read: async () => {
        if (sent) return { done: true };
        sent = true; return { done: false, value: enc.encode('data: hi\\n\\n') };
      } }) } };
  };
  handle = connectSSE('/x', { onMessage: () => { messages++; },
    onGiveUp: () => { gaveUp++; resolve({connects, messages, gaveUp}); } });
})
"""


def check_sse(browser):
    page = new_page(browser)
    page.add_script_tag(content=_src("sse.js"))
    out = page.evaluate(SSE_HARNESS, "flaky")
    assert out["gaveUp"] == 0, out
    assert out["messages"] >= 9, out
    page.close()
    page = new_page(browser)
    page.add_script_tag(content=_src("sse.js"))
    out = page.evaluate(SSE_HARNESS, "never")
    assert out["gaveUp"] == 1 and out["messages"] == 0, out
    assert out["connects"] == 6, out  # 1 + MAX_RETRIES failed attempts, then give up
    page.close()
    print("  [2] sse.js: good events reset the retry budget; a dead stream still gives up OK")


def check_queuedrun(browser):
    for status, needle in ((404, "no longer available"), (401, "session expired")):
        page = new_page(browser, '<p data-queued-run="5">Waiting for capacity.</p>')
        page.evaluate(FAST_TIMERS)
        page.evaluate(
            """(st) => { window.__calls = 0;
            window.fetch = async () => { window.__calls++;
              return {ok: false, status: st, json: async () => ({ok: false})}; }; }""",
            status,
        )
        page.add_script_tag(content=_src("queuedrun.js"))
        page.wait_for_function(
            "document.querySelector('[data-queued-run]').textContent.indexOf('Waiting') !== 0"
        )
        page.wait_for_timeout(300)
        assert needle in page.locator("[data-queued-run]").inner_text(), status
        assert page.evaluate("window.__calls") == 1, ("kept polling", status)
        page.close()
    print("  [3] queuedrun.js: 404/401 stop polling and say why OK")


def check_sidebar(browser, client):
    html = client.get("/jobs").text
    page = new_page(browser, html, storage=True)
    page.add_script_tag(content=_src("sidebar_ux.js"))
    drag = """(sel) => {
      // two links of ONE section (an item drag only reorders inside its own section)
      const sec = Array.from(document.querySelectorAll('.side-nav .nav-section'))
        .find((s) => s.querySelectorAll(':scope > a').length >= 2);
      const links = sec.querySelectorAll(':scope > a');
      const a = links[0], b = links[1];
      const dt = new DataTransfer();
      a.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
      b.dispatchEvent(new DragEvent('dragover', {bubbles: true, cancelable: true,
        clientY: b.getBoundingClientRect().bottom + 5, dataTransfer: dt}));
      // a bare DataTransfer reports 'none' (a cancelled drag, rolled back); report a real drop
      Object.defineProperty(dt, 'dropEffect', { get: () => 'move' });
      a.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
      return localStorage.getItem('lsSidebarOrder');
    }"""
    sel = ".side-nav .nav-section > a"
    assert page.locator(sel).count() >= 2
    assert page.evaluate(drag, sel) is None, "a drag outside edit mode saved an order"
    page.locator("button.sb-edit, [aria-label^='Edit sidebar layout']").first.click()
    assert page.evaluate("document.querySelector('.sidebar').classList.contains('editing')")
    assert page.evaluate(drag, sel) is not None, "edit-mode drag no longer saves (vacuous test)"
    page.close()
    print("  [4] sidebar_ux.js: no reorder outside edit mode; edit mode still saves OK")


CONNECTOR_HTML = """
<form data-kind="airtable" data-browse="settings" cid="7">
  <label>Base <input name="base_id" type="text"></label>
  <label>Table <input name="table" type="text"></label>
  <label>Source field <input name="source_field" type="text"></label>
  <button type="button" data-connector-browse>Load from Airtable</button>
  <span class="browse-out"></span>
</form>"""

CONNECTOR_DRIVER = """
async () => {
  const pays = [
    [{id: 'b1', name: 'One', tables: [{name: 'T-one'}]}],
    [{id: 'b2', name: 'Two', tables: [{name: 'T-two'}]}],
  ];
  let n = 0;
  window.CSRF_TOKEN = 'tok';
  window.fetch = async (url) => {
    const body = url.endsWith('/browse/containers')
      ? {ok: true, containers: pays[n++]} : {ok: true, fields: ['f']};
    return {status: 200, text: async () => JSON.stringify(body)};
  };
  const btn = document.querySelector('[data-connector-browse]');
  const settle = () => new Promise((r) => setTimeout(r, 50));
  btn.click(); await settle();
  btn.click(); await settle();
  const base = document.querySelector('select[data-for="base_id"]');
  base.value = 'b2';
  base.dispatchEvent(new Event('change', {bubbles: true}));
  await settle();
  const tables = Array.from(document.querySelectorAll('select[data-for="table"] option'))
    .map((o) => o.value).filter(Boolean);
  return tables;
}
"""


def check_connectors(browser):
    page = new_page(browser, CONNECTOR_HTML)
    page.add_script_tag(content=_src("connectors.js"))
    tables = page.evaluate(CONNECTOR_DRIVER)
    assert tables == ["T-two"], tables
    page.close()
    print("  [5] connectors.js: a second Load re-wires the pickers to the new payload OK")


if __name__ == "__main__":
    client = TestClient(app)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        try:
            check_jobs(browser)
            check_sse(browser)
            check_queuedrun(browser)
            check_sidebar(browser, client)
            check_connectors(browser)
        finally:
            browser.close()
    print("verify_browser_js_resilience OK")
