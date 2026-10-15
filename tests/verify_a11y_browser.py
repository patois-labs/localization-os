"""Accessibility browser guard (a11y train T0, "guard first"). OPT-IN: skips cleanly.

Boots the real app on a throwaway data dir, seeds a small realistic install in-process,
signs in through the real login form in headless Chromium (Playwright sync API), and on
about a dozen pages runs:

  * axe-core 4.13.0 (vendored, tests/fixtures/axe/axe.min.js, size and sha256 pinned
    in verify_a11y_static) with tags wcag2a wcag2aa wcag21a wcag21aa wcag22aa, in the
    dark theme (plus the light theme on three pages, for contrast);
  * a 320 CSS px reflow check (1.4.10): how many px the whole page scrolls sideways
    (document scrollWidth minus clientWidth; a table or code block scrolling inside its
    own overflow-x:auto wrapper does not count), ratcheted per page so a fix visibly
    shrinks it;
  * on three pages a 25-stop Tab walk: focus must move (no trap), and each focused
    element must look different from its unfocused self (2.4.7); Projects also runs
    at 683x384, with sidebar open/close focus and inertness checks;
  * on the Editor (T4), after the measurements: a keystroke that types a letter (Mac
    Option+A, Windows AltGr+A) is not prevented and opens nothing, while Mac
    Ctrl+Shift+A (not VoiceOver's Ctrl+Option, T11) and Windows Alt+A open the alternates
    popover; the popover by keyboard
    (arrows, Home/End, Escape / Enter / Alt+A returning focus to the textarea, Tab out
    closing it, axe-clean while open) and its FIRST placement attached to its trigger
    (below, or flipped above at the bottom edge, also when a long status message pushed
    the row off screen while the alternates loaded), and still attached 2.5 s later,
    after its own status text reflowed the sticky toolbar; row state as text with Retry, the
    approve-after-failed-save bug (no approve POST, failure kept, "Not approved" shown),
    a busy button keeping keyboard focus; and at 320 px the table scrolls inside its
    wrapper while the popover opened from its last column is fully on screen, and closes
    (keeping the scroll position) when its trigger is scrolled out of the wrapper;
  * T8 (2.5.7 / 2.1.1 / 1.1.1 / 2.5.3): on Projects, sidebar edit mode by keyboard (move
    buttons with disabled edges, kept focus and a polite announcement, the width stepper,
    the keyboard separator, axe-clean while editing, Done returning focus); on the workflow
    builder, the diagram described by the step list and axe's experimental
    label-content-name-mismatch run by id with zero findings; on the Run view, the
    "Diagram as text" disclosure by keyboard, not live;
  * on the project page with a SAVED 3-across layout (train L1, "project-layout"; train
    L2 adds the same case on Dashboard, Setup AI and Users, whose edit mode must also
    keep a card's nested heading visible, "dashboard-layout" etc.): the
    cards are really moved (DOM order = the saved rows), Tab visits the cards in that
    order, the edit mode opens from the keyboard with focus on Done, is axe-clean, a
    Move button keeps focus after moving its card, Escape restores the layout, and at
    320 px the 3-across row stacks with 0 px sideways overflow;
  * the seed carries QA flags and TM locks (soft and hard) on a document segment and on
    key values, so the Editor and the key grid are measured WITH their disclosures:
    every summary is a 24 x 24 px target and axe is clean at 1366 and 320 px;

with prefers-reduced-motion emulated. Results are compared with the BASELINE RATCHET in
tests/fixtures/a11y_baseline.json (page -> {axe rule: node count, overflow_px: int,
tabwalk: {...}}). The suite FAILS when any count rises, a new axe rule appears, a page's
sideways overflow grows by more than 4 px, or a Tab walk gets a trap or more problems.
When something falls it prints a "tighten the baseline" note (by design it does not
fail, so a fix never breaks the suite on its own); regenerate the file with
LOC_TMS_A11Y_BASELINE_WRITE=1 in the same run that fixed it, so every later train (T1 to
T10) visibly lowers the numbers. The goal is a baseline with no violations left.

Skip rule: no playwright package, or Chromium will not launch -> prints a SKIP line,
`collected ids: (skipped)` and still ends in OK, so CI and dev boxes without browsers stay
green. Set LOC_TMS_A11Y_BROWSER_REQUIRED=1 to turn that skip into a FAILURE (use it on the
owner's box and before any VPAT reissue): a green run must never silently mean "never ran".

Install once per machine:
    .venv\\Scripts\\python.exe -m pip install -r requirements-dev.txt
    .venv\\Scripts\\python.exe -m playwright install chromium
"""

import contextlib
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# The axe pin lives in ONE place, the static suite (normal sweep, no browser needed),
# so a modified axe.min.js fails there even where this suite skips.
from tests.verify_a11y_static import AXE_PATH, AXE_SIZE, axe_findings

_BASELINE_PATH = os.path.join(_ROOT, "tests", "fixtures", "a11y_baseline.json")
_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
_TAB_STOPS = 25
# Local deadlines, well inside the wrapper's 600 s cap, so one stuck page fails clearly.
_PAGE_DEADLINE_S = 90.0
_SUITE_DEADLINE_S = 420.0
_ACTION_TIMEOUT_MS = 30000
_AXE_TIMEOUT_MS = 45000
# Sideways overflow at 320 px is ratcheted in pixels; growth beyond this fails.
_OVERFLOW_TOLERANCE_PX = 4
_REQUIRED = os.environ.get("LOC_TMS_A11Y_BROWSER_REQUIRED", "").strip() == "1"
_WRITE = os.environ.get("LOC_TMS_A11Y_BASELINE_WRITE", "").strip() == "1"

_ADMIN_EMAIL = "a11y-admin@example.com"
_ADMIN_PW = "a11y-browser-pw-2026"  # pregate: allow-secret throwaway fixture login


def _finish_skip(reason: str) -> int:
    print(f"  SKIP: {reason}")
    if _REQUIRED:
        print("  LOC_TMS_A11Y_BROWSER_REQUIRED=1 is set, so a skip is a failure")
        print("A11Y BROWSER FAILED")
        return 1
    print("collected ids: (skipped)")
    print("A11Y BROWSER OK")
    return 0


def _check_axe_bytes() -> str:
    with open(AXE_PATH, "rb") as fh:
        data = fh.read()
    problems = axe_findings(data)
    assert not problems, "; ".join(v for v, _w in problems)
    return data.decode("utf-8")


# --------------------------------------------------------------------------- #
# In-page probes (adapted from the 09-23 audit scanner, HECVAT/_work/a11y-tools/scan.py)
# --------------------------------------------------------------------------- #
_JS_REFLOW = r"""
() => {
  const vw = document.documentElement.clientWidth;
  const sw = document.documentElement.scrollWidth;
  let off = 0;
  for (const e of document.querySelectorAll('body *')) {
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.right <= vw + 2) continue;
    let p = e.parentElement, scroller = false;
    while (p && p !== document.body) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll') { scroller = true; break; }
      p = p.parentElement;
    }
    if (!scroller) off++;
  }
  return { overflowPx: Math.max(0, sw - vw), offenders: off };
}
"""

# The throwaway data dir's path is printed on some pages (Settings shows the database
# path) and its length depends on where the harness puts temp files, which made the 320 px
# overflow differ between a standalone and a wrapper run. Before measuring reflow, every
# occurrence of it in the page text becomes one fixed placeholder.
_JS_NORMALIZE_PATHS = r"""
(needles) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const lower = needles.map(n => n.toLowerCase());
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    let text = node.nodeValue;
    for (const n of lower) {
      let i = text.toLowerCase().indexOf(n);
      while (i >= 0) {
        text = text.slice(0, i) + '<data-dir>' + text.slice(i + n.length);
        i = text.toLowerCase().indexOf(n, i + 10);
      }
    }
    if (text !== node.nodeValue) node.nodeValue = text;
  }
}
"""


def _path_needles(data_dir: str) -> list[str]:
    forms = {data_dir, os.path.realpath(data_dir), os.path.abspath(data_dir)}
    out = set()
    for f in forms:
        out.update({f, f.replace("\\", "/"), f.replace("/", "\\")})
    return sorted((n for n in out if n), key=len, reverse=True)


_JS_FOCUS_STYLE = r"""
() => {
  const e = document.activeElement;
  if (!e || e === document.body || e === document.documentElement) return null;
  const cs = getComputedStyle(e);
  const r = e.getBoundingClientRect();
  return {
    desc: e.tagName + (e.id ? '#' + e.id : '') + '|' + (e.getAttribute('name') || '') + '|'
      + (e.getAttribute('href') || '') + '|' + (e.innerText || '').trim().slice(0, 30),
    outline: cs.outlineStyle + ' ' + cs.outlineWidth + ' ' + cs.outlineColor,
    shadow: cs.boxShadow, bg: cs.backgroundColor, border: cs.borderColor,
    color: cs.color, deco: cs.textDecorationLine,
    inView: r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth,
    visible: r.width > 0 && r.height > 0,
  };
}
"""

_JS_BLURRED_STYLE = r"""
() => {
  const e = document.querySelector('[data-a11y-probe]');
  if (!e) return null;
  const cs = getComputedStyle(e);
  return {
    outline: cs.outlineStyle + ' ' + cs.outlineWidth + ' ' + cs.outlineColor,
    shadow: cs.boxShadow, bg: cs.backgroundColor, border: cs.borderColor,
    color: cs.color, deco: cs.textDecorationLine,
  };
}
"""

_STYLE_KEYS = ("outline", "shadow", "bg", "border", "color", "deco")


def _settle(page) -> None:
    # A page that polls never goes idle; the short sleep below still settles it.
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=5000)
    time.sleep(0.3)


def _run_axe(page, axe_src: str) -> dict:
    # DevTools evaluation installs the audit tool without weakening the site's CSP.
    page.evaluate(axe_src)
    res = page.evaluate(
        "async ([tags, ms]) => await Promise.race(["
        " axe.run(document, {runOnly: {type: 'tag', values: tags}}),"
        " new Promise((_, reject) => setTimeout("
        " () => reject(new Error('axe.run did not finish in ' + ms + ' ms')), ms))])",
        [_TAGS, _AXE_TIMEOUT_MS],
    )
    return {v["id"]: len(v["nodes"]) for v in res["violations"]}


def _tab_walk(page) -> dict:
    """Tab through the page; for each stop compare the focused style with the same
    element's style after focus moves back off it (Shift+Tab)."""
    page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
    stops, no_indicator, out_of_view = [], 0, 0
    trap = False
    for _ in range(_TAB_STOPS):
        page.keyboard.press("Tab")
        time.sleep(0.05)
        st = page.evaluate(_JS_FOCUS_STYLE)
        if st is None:
            stops.append("<body>")
            continue
        page.evaluate("() => document.activeElement.setAttribute('data-a11y-probe', '1')")
        page.keyboard.press("Shift+Tab")
        time.sleep(0.05)
        base = page.evaluate(_JS_BLURRED_STYLE)
        page.keyboard.press("Tab")
        page.evaluate(
            "() => { const e = document.querySelector('[data-a11y-probe]');"
            " if (e) e.removeAttribute('data-a11y-probe'); }"
        )
        stops.append(st["desc"])
        if st["visible"]:
            if base is not None and all(st[k] == base[k] for k in _STYLE_KEYS):
                no_indicator += 1
            if not st["inView"]:
                out_of_view += 1
        if len(stops) > 6 and len(set(stops[-6:])) <= 2:
            trap = True
            break
    return {
        "trap": trap,
        "no_indicator": no_indicator,
        "out_of_view": out_of_view,
        "distinct_stops": len(set(stops)),
    }


# --------------------------------------------------------------------------- #
# Server + seeding
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UvicornThread:
    """Run the real app in a background uvicorn Server on a free port (verify_mcp)."""

    def __init__(self, app, port: int):
        import uvicorn

        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, timeout: float = 20.0) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise AssertionError("uvicorn did not start in time")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _post(client, url: str, data: dict, files=None) -> str:
    r = client.post(url, data=data, files=files, follow_redirects=False)
    assert r.status_code == 303, f"seed POST {url} -> {r.status_code}: {r.text[:300]}"
    return r.headers["location"]


def _tail_id(location: str) -> int:
    return int(location.split("?")[0].rstrip("/").split("/")[-1])


def _seed() -> dict:
    """A small install: customer, document project with a parsed document, key project
    with two keys, glossary with two pairs and a term, a workflow with a human step, and
    an admin. Seeded with auth OFF through the real web routes (the same forms a user
    submits) plus the key service; then auth is switched ON for the browser."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.main import app
    from app.models import (
        Project,
        ProjectType,
        Role,
        RunStatus,
        Segment,
        StepRun,
        WorkflowRun,
        WorkflowStep,
    )
    from app.services import content, security, settings
    from app.services import keys as keys_svc

    init_db()
    settings.set_many({"key_projects_enabled": "1"})
    settings.invalidate_cache()
    c = TestClient(app)
    _post(c, "/customers", {"name": "Acme Outdoor"})
    cust_id = 1
    doc_pid = _tail_id(
        _post(
            c,
            "/projects",
            {
                "name": "Spring catalog",
                "source_locale": "en-US",
                "target_locales": "fr-FR",
                "default_provider": "claude",
                "customer_id": str(cust_id),
            },
        )
    )
    text = (
        b"Welcome to the spring catalog. Our new tents are lighter than ever.\n\n"
        b"Every sleeping bag is tested at minus ten degrees. Free returns within 30 days."
    )
    doc_id = _tail_id(
        _post(
            c,
            f"/projects/{doc_pid}/documents",
            {"mode": "segmented"},
            files={"file": ("catalog.txt", text, "text/plain")},
        )
    )
    with Session(engine) as s:
        kp = content.project_create(
            s,
            "App strings",
            source_locale="en-US",
            target_locales="de-DE, fr-FR",
            customer_id=str(cust_id),
            project_type=ProjectType.KEY_BASED,
        )
        key_pid = kp.id
        project = s.get(Project, key_pid)
        for name, base in (("checkout.title", "Checkout"), ("checkout.pay", "Pay now")):
            keys_svc.create_key(s, project, name, base_value=base, actor="a11y-fixture")
    gid = _tail_id(_post(c, "/glossaries", {"name": "Outdoor terms", "customer_id": str(cust_id)}))
    _post(
        c,
        f"/glossaries/{gid}/pairs",
        {"source_locale": ["en-US", "en-US"], "target_locale": ["fr-FR", "de-DE"]},
    )
    _post(
        c,
        f"/glossaries/{gid}/terms/add",
        {
            "source_term": "sleeping bag",
            "target_term": "sac de couchage",
            "source_locale": "en-US",
            "target_locale": "fr-FR",
        },
    )
    wf_id = _tail_id(_post(c, "/workflows", {"name": "Review flow"}))
    _post(c, f"/workflows/{wf_id}/steps", {"name": "Human review", "step_type": "human_review"})
    _post(c, f"/workflows/{wf_id}/steps", {"name": "Quality check", "step_type": "lqa"})
    with Session(engine) as s:
        now = datetime(2026, 9, 24, 12)
        run = WorkflowRun(
            workflow_id=wf_id,
            document_id=doc_id,
            target_locale="fr-FR",
            status=RunStatus.FAILED,
            started_at=now,
            finished_at=now + timedelta(seconds=20),
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        run_id = run.id
        steps = s.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf_id)
            .order_by(WorkflowStep.order_index)
        ).all()
        for step, status, note in zip(
            steps,
            (RunStatus.FAILED, RunStatus.COMPLETED),
            (
                "Provider unavailable: https://api.example-provider.com/v1/chat/completions?deployment="
                + "long-deployment-" * 20,
                "LQA score 96 passed <img src=x>",
            ),
            strict=True,
        ):
            s.add(
                StepRun(
                    workflow_run_id=run_id,
                    step_id=step.id,
                    status=status,
                    error=note,
                    started_at=now,
                    finished_at=now + timedelta(seconds=10),
                )
            )
        s.commit()
    # T4 fix round 1 (gate MEDIUM): QA flags and TM locks (fuzzy soft lock and hard lock)
    # on a document segment and on key values, so axe meets the Editor's and the key
    # grid's QA / TM lock disclosures (a flag message is markup-like on purpose: it must
    # stay text). The Editor keyboard block edits the FIRST row, so the hard lock sits on
    # the last one.
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id).order_by(Segment.order_index)
        ).all()
        assert len(segs) >= 2, f"the seeded document parsed into {len(segs)} segment(s)"
        segs[0].qa_flags = [
            {
                "type": "number_integrity",
                "message": "Number 30 changed <b>in</b> target",
                "severity": "major",
            },
            {"type": "terminology", "message": "Glossary term not used", "severity": "minor"},
        ]
        segs[0].lock_band = "fuzzy"
        segs[0].tm_match_pct = 87
        segs[-1].lock_band = "exact"
        segs[-1].lock_hard = True
        values = s.exec(
            select(Segment).where(Segment.key_id.is_not(None)).order_by(Segment.id)  # type: ignore[union-attr]
        ).all()
        assert len(values) >= 3, f"the seeded keys have {len(values)} value row(s)"
        for i, v in enumerate(values[:3]):
            v.qa_flags = [
                {"type": "char_limit", "message": "Too long <i>by</i> 4", "severity": "minor"}
            ]
            v.lock_band = "fuzzy" if i == 0 else "exact"
            v.tm_match_pct = 91 if i == 0 else 100
            v.lock_hard = i == 2
            v.target_text = v.target_text or "Kasse"
        s.add_all([*segs, *values[:3]])
        s.commit()
    security.create_user(_ADMIN_EMAIL, "A11y Admin", Role.ADMIN, _ADMIN_PW)
    # SPEC-expired-invite-delete: an expired row puts Resend + Delete on the Users walk.
    security.create_invite("lapsed@example.com", Role.TRANSLATOR, _ADMIN_EMAIL, days=-1)
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    return {
        "cust_id": cust_id,
        "doc_pid": doc_pid,
        "doc_id": doc_id,
        "key_pid": key_pid,
        "gid": gid,
        "run_id": run_id,
        "wf_id": wf_id,
        "app": app,
    }


def _pages(ids: dict) -> list[tuple[str, str, bool, bool]]:
    """(name, path, light theme too, tab walk). Login is fetched signed out."""
    return [
        ("login", "/auth/login", False, True),
        ("dashboard", "/", True, False),
        ("projects", "/projects", False, True),
        ("customer-detail", f"/customers/{ids['cust_id']}", False, False),
        ("project", f"/projects/{ids['doc_pid']}", True, False),
        # Train L1: the same project page with a SAVED 3-across layout (see _layout_setup).
        ("project-layout", f"/projects/{ids['doc_pid']}", False, False),
        # Train L2: the same saved-layout case on three more hub pages (_LAYOUT_CASES).
        ("dashboard-layout", "/", False, False),
        ("setup-ai", "/setup-ai", False, False),
        ("setup-ai-layout", "/setup-ai", False, False),
        ("users-layout", "/users", False, False),
        ("document-layout", f"/documents/{ids['doc_id']}", False, False),
        ("project-keys", f"/projects/{ids['key_pid']}/keys", False, False),
        ("editor", f"/documents/{ids['doc_id']}/editor", True, True),
        ("glossary-detail", f"/glossaries/{ids['gid']}", False, False),
        ("queries", "/queries", False, False),
        ("workflows", "/workflows", False, False),
        ("workflow-new", "/workflows/new", False, False),
        ("workflow", f"/workflows/{ids['wf_id']}", False, False),
        ("run-view", f"/runs/{ids['run_id']}", False, False),
        ("users", "/users", False, False),
        ("settings", "/settings", False, False),
        # Password policy train: the Security page (with its Passwords card) and the
        # forced password change page (a standalone sign-in style page).
        ("security", "/security", False, True),
        ("password-change", "/auth/password-change", False, True),
        # Security review 2026-09 (#251): the sign-out confirmation page (GET /auth/logout).
        ("sign-out-confirm", "/auth/logout", False, True),
        ("help", "/help", False, False),
    ]


# --------------------------------------------------------------------------- #
# Browser walk
# --------------------------------------------------------------------------- #
def _login(ctx, base: str) -> None:
    p = ctx.new_page()
    p.goto(base + "/auth/login")
    p.fill("form[action='/auth/login'] input[name=email]", _ADMIN_EMAIL)
    p.fill("form[action='/auth/login'] input[name=password]", _ADMIN_PW)
    p.click("form[action='/auth/login'] button[type=submit]")
    p.wait_for_load_state("load")
    assert "/auth/login" not in p.url, f"sign-in through the real form failed: {p.url}"
    p.close()


def _set_theme(page, theme: str) -> None:
    """The product's own switch (ADR 0063): a signed-in page stores the choice on the
    account (POST /account/theme, what the account menu sends) and the head bootstrap reads
    it from <html data-theme-pref>; a signed-out page falls back to localStorage loc_theme."""
    status = page.evaluate(
        """async (t) => {
            localStorage.setItem('loc_theme', t);
            if (!document.documentElement.hasAttribute('data-theme-pref') || !window.CSRF_TOKEN) {
                return 0;
            }
            const r = await fetch('/account/theme', {
                method: 'POST', credentials: 'same-origin',
                headers: {'Content-Type': 'application/x-www-form-urlencoded',
                          'Accept': 'application/json', 'X-CSRF-Token': window.CSRF_TOKEN},
                body: 'theme=' + encodeURIComponent(t)});
            return r.status;
        }""",
        theme,
    )
    assert status in (0, 200), f"saving the {theme} theme failed: HTTP {status}"


def _visit(page, base: str, path: str, theme: str = "dark") -> None:
    violations = []

    def on_console(message):
        if "content security policy" in message.text.lower():
            violations.append(message.text)

    page.on("console", on_console)
    resp = page.goto(base + path)
    assert resp is not None and resp.status == 200, f"{path}: HTTP {resp.status if resp else None}"
    policy = resp.headers.get("content-security-policy", "")
    assert "script-src 'self' 'nonce-" in policy, (path, policy)
    nonces = page.evaluate(
        "() => Array.from(document.querySelectorAll('script:not([src])'), s => s.nonce)"
    )
    assert all(n and f"'nonce-{n}'" in policy for n in nonces), (path, nonces)
    final = page.url[len(base) :].split("?")[0]
    assert final == path, f"{path}: landed on {final} (signed out or redirected?)"
    got = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert (got == "light") == (theme == "light"), f"{path}: wanted {theme} theme, data-theme={got}"
    _settle(page)
    page.remove_listener("console", on_console)
    assert not violations, (path, violations)


def _check_script_csp(ctx, base: str) -> None:
    """A real escaping slip must be blocked, while shared accessible handlers still run."""
    page = ctx.new_page()
    try:
        page.goto(base + "/projects")
        page.evaluate("""() => {
          window.__cspViolations = [];
          document.addEventListener('securitypolicyviolation', e => {
            window.__cspViolations.push(e.effectiveDirective);
          });
          const script = document.createElement('script');
          script.textContent = 'window.__untrustedScriptRan = true';
          document.body.appendChild(script);
          const button = document.createElement('button');
          button.setAttribute('onclick', 'window.__untrustedHandlerRan = true');
          document.body.appendChild(button);
          button.click();
          button.remove();
        }""")
        page.wait_for_function("() => window.__cspViolations.length >= 2")
        assert page.evaluate("() => !window.__untrustedScriptRan && !window.__untrustedHandlerRan")
        page.evaluate("""() => {
          // #623: the native dialog must never be used; the question is the in-page one.
          window.__confirmCount = 0;
          window.confirm = () => { window.__confirmCount++; return true; };
          const form = document.createElement('form');
          form.id = 'csp-confirm-probe';
          form.setAttribute('data-action-confirm', 'Cancel this test action?');
          form.innerHTML = '<input aria-label="CSP probe"><button>Submit probe</button>';
          document.body.appendChild(form);
          window.__submits = [];
          window.addEventListener('submit', e => {
            window.__submits.push(e.defaultPrevented);
            e.preventDefault();
          });
          const input = document.createElement('input');
          input.id = 'csp-select-probe';
          input.value = 'copy this link';
          input.setAttribute('data-select-on-click', '');
          document.body.appendChild(input);
        }""")
        ask = page.locator("dialog[open][role=alertdialog]")
        page.locator("#csp-confirm-probe input").focus()
        page.keyboard.press("Enter")
        ask.wait_for(state="visible")
        assert "Cancel this test action?" in ask.inner_text()
        assert page.evaluate("() => document.activeElement.textContent") == "Cancel"
        assert page.evaluate("() => window.__submits.join()") == "true"
        # Escape is a no: the form is not sent and focus returns to where it was.
        page.keyboard.press("Escape")
        ask.wait_for(state="hidden")
        assert (
            page.evaluate("() => document.activeElement.getAttribute('aria-label')") == "CSP probe"
        )
        page.locator("#csp-confirm-probe button").click()
        ask.wait_for(state="visible")
        ask.get_by_role("button", name="Cancel", exact=True).click()
        ask.wait_for(state="hidden")
        assert page.evaluate("() => window.__submits.join()") == "true,true"
        # Only the confirm button sends it (the re-send is a submit nobody cancelled).
        page.locator("#csp-confirm-probe button").click()
        ask.wait_for(state="visible")
        ask.get_by_role("button", name="Confirm", exact=True).click()
        ask.wait_for(state="hidden")
        page.wait_for_function("() => window.__submits.length >= 4")
        assert page.evaluate("() => window.__submits.join()") == "true,true,true,false"
        assert page.evaluate("() => window.__confirmCount") == 0
        page.locator("#csp-select-probe").click()
        assert page.locator("#csp-select-probe").evaluate(
            "el => el.selectionStart === 0 && el.selectionEnd === el.value.length"
        )
        print("  CSP: injected script/handler blocked; confirmation click/Enter and selection work")
    finally:
        page.close()


class _Watchdog:
    """Fails the suite clearly (exit 1) when one page or the whole walk overruns its
    deadline, even if a browser call hangs where no Playwright timeout applies."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.server = None
        self.t_suite = time.time()
        self.page = "(setup)"
        self.t_page = time.time()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def on_page(self, name: str) -> None:
        self.page, self.t_page = name, time.time()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            now = time.time()
            why = None
            if now - self.t_page > _PAGE_DEADLINE_S:
                why = f"page '{self.page}' exceeded {_PAGE_DEADLINE_S:.0f}s"
            elif now - self.t_suite > _SUITE_DEADLINE_S:
                why = f"the walk exceeded {_SUITE_DEADLINE_S:.0f}s (at page '{self.page}')"
            if why:
                print(f"  TIMEOUT: {why}", flush=True)
                self._cleanup()
                print("A11Y BROWSER FAILED", flush=True)
                os._exit(1)

    def _cleanup(self) -> None:
        """os._exit skips atexit, so remove the throwaway data dir (keys, db) here."""
        with contextlib.suppress(Exception):
            if self.server is not None:
                self.server.server.should_exit = True
                self.server.thread.join(timeout=5)
        with contextlib.suppress(Exception):
            from tests._bootstrap import _cleanup

            _cleanup(self.data_dir)


def _is_focused(locator) -> bool:
    return bool(locator.evaluate("e => e === document.activeElement"))


def _label_in_name(page) -> list[str]:
    """axe's label-content-name-mismatch (2.5.3) is tagged experimental, so the tag run
    skips it: run it by id and require that it actually ran (passes > 0)."""
    res = page.evaluate(
        "async () => await axe.run(document,"
        " {runOnly: {type: 'rule', values: ['label-content-name-mismatch']}})"
    )
    ran = sum(len(r["nodes"]) for r in res["passes"] + res["violations"])
    assert ran > 0, "label-content-name-mismatch matched nothing: the check did not run"
    return [n["html"][:200] for v in res["violations"] for n in v["nodes"]]


_JS_FIRST_ITEM_FOCUSED = (
    "() => document.activeElement === "
    "document.querySelector('details.usermenu [role=\"menuitem\"]')"
)


def _usermenu_focus(page) -> None:
    """nvda-fixes (4.1.2 / 2.1.1 / 2.4.3, WAI-ARIA APG menu button): the user menu moves
    focus to its first item on EVERY open. NVDA's browse mode turns Enter on the summary
    into a synthetic click with no keydown, so element.click() must do it too; so must a
    real Enter and a real mouse click (no visible ring then). Arrows, typeahead on the
    label (not the aria-hidden glyph), Escape back to the summary, outside click and Tab
    out close it, and aria-expanded follows every open and close."""
    menu = page.locator("details.usermenu")
    summary = menu.locator("summary")
    items = menu.locator('[role="menuitem"], [role="menuitemradio"]')
    n = items.count()
    assert n >= 4, f"user menu should list several items, got {n}"
    assert summary.get_attribute("aria-expanded") == "false"
    assert not items.evaluate_all(
        "els => els.filter(e => !e.firstElementChild"
        " || e.firstElementChild.getAttribute('aria-hidden') !== 'true')"
        ".map(e => e.textContent.trim())"
    ), "every user menu item leads with an aria-hidden icon glyph"

    def is_open() -> bool:
        return bool(menu.evaluate("e => e.open"))

    # 1. The screen reader path: a click with no keydown before it.
    summary.evaluate("el => el.click()")
    page.wait_for_function(_JS_FIRST_ITEM_FOCUSED, timeout=_ACTION_TIMEOUT_MS)
    assert is_open() and summary.get_attribute("aria-expanded") == "true"
    page.keyboard.press("End")
    assert _is_focused(items.nth(n - 1))
    page.keyboard.press("ArrowDown")
    assert _is_focused(items.first), "ArrowDown from the last item wraps to the first"
    page.keyboard.press("ArrowUp")
    assert _is_focused(items.nth(n - 1)), "ArrowUp from the first item wraps to the last"
    page.keyboard.press("Home")
    assert _is_focused(items.first)
    page.keyboard.press("h")
    assert page.evaluate(
        "() => document.activeElement.getAttribute('href') === '/help'"
        " && document.activeElement.textContent.includes('Help')"
    ), "typeahead 'h' should reach Help (the glyph before the label is skipped)"
    page.keyboard.press("Escape")
    assert not is_open() and summary.get_attribute("aria-expanded") == "false"
    assert _is_focused(summary), "Escape returns focus to the summary"

    # 2. The keyboard path: Enter on the focused summary.
    summary.focus()
    page.keyboard.press("Enter")
    page.wait_for_function(_JS_FIRST_ITEM_FOCUSED, timeout=_ACTION_TIMEOUT_MS)
    assert summary.get_attribute("aria-expanded") == "true"
    page.keyboard.press("End")
    page.keyboard.press("Tab")
    page.wait_for_function(
        "() => !document.querySelector('details.usermenu').open", timeout=_ACTION_TIMEOUT_MS
    )
    assert summary.get_attribute("aria-expanded") == "false", "Tab out of the menu closes it"

    # 3. The mouse path: focus moves too, without a focus ring.
    summary.click()
    page.wait_for_function(_JS_FIRST_ITEM_FOCUSED, timeout=_ACTION_TIMEOUT_MS)
    assert not items.first.evaluate("e => e.matches(':focus-visible')"), (
        "a mouse-opened menu must not show a focus ring on its first item"
    )
    # 4. An outside click closes it (the crumbs are plain text, nothing navigates).
    page.locator(".crumbs").click()
    assert not is_open() and summary.get_attribute("aria-expanded") == "false"
    # 5. A scripted open (open = true) takes the same path.
    menu.evaluate("e => { e.open = true; }")
    page.wait_for_function(_JS_FIRST_ITEM_FOCUSED, timeout=_ACTION_TIMEOUT_MS)
    assert summary.get_attribute("aria-expanded") == "true"
    page.keyboard.press("Escape")
    assert not is_open() and _is_focused(summary)


def _sidebar_keyboard(page, axe_src: str) -> None:
    """a11y T8 (2.5.7 / 2.1.1): the sidebar's reorder and resize work without dragging.
    Edit mode by keyboard, move buttons (edges disabled, focus kept, announced), the width
    stepper and the keyboard separator, axe-clean while editing, Done restores focus.
    Leaves localStorage as it found it (later pages share this browser context)."""
    status = page.locator(".sidebar .sb-status")
    assert status.get_attribute("role") == "status"
    assert status.get_attribute("aria-live") == "polite"
    handle = page.locator(".sb-resize")
    assert (
        handle.get_attribute("aria-hidden") == "true" and handle.get_attribute("tabindex") is None
    )
    edit = page.locator(".sb-edit")
    edit.focus()
    page.keyboard.press("Enter")
    assert edit.get_attribute("aria-pressed") == "true"
    groups = page.locator(".side-nav .sb-move-group")
    n = page.locator(".side-nav .nav-section").count()
    assert groups.count() == n > 1
    assert groups.first.locator(".sb-move-up").is_disabled()
    assert groups.last.locator(".sb-move-down").is_disabled()
    # Item move: the first item of the first group moves down one place.
    sec = page.locator(".side-nav .nav-section").first
    first = sec.locator(":scope > a").first
    key = first.get_attribute("data-nav-key")
    label = first.evaluate(
        "e => (e.getAttribute('aria-label') || e.textContent).replace(/\\s+/g, ' ').trim()"
    )
    down = sec.locator(f'a[data-nav-key="{key}"] + .sb-move .sb-move-down')
    assert down.get_attribute("aria-label") == "Move down: " + label
    assert sec.locator(f'a[data-nav-key="{key}"] + .sb-move .sb-move-up').is_disabled()
    down.focus()
    page.keyboard.press("Enter")
    items = sec.locator(":scope > a")
    assert items.nth(1).get_attribute("data-nav-key") == key, "the item did not move down"
    assert page.evaluate(
        "(k) => document.activeElement.closest('.sb-move').previousElementSibling"
        ".getAttribute('data-nav-key') === k",
        key,
    ), "focus left the moved item's buttons"
    page.wait_for_timeout(150)
    m = sec.locator(":scope > a").count()
    assert status.text_content() == f"{label}, position 2 of {m}", status.text_content()
    # Keyboard Up again: back to the top, and focus falls to the enabled twin (down).
    sec.locator(f'a[data-nav-key="{key}"] + .sb-move .sb-move-up').focus()
    page.keyboard.press("Enter")
    assert sec.locator(":scope > a").first.get_attribute("data-nav-key") == key
    assert page.evaluate("() => document.activeElement.classList.contains('sb-move-down')")
    # Group move: the first group moves down.
    gkey = sec.get_attribute("data-nav-key")
    groups.first.locator(".sb-move-down").click()
    assert page.locator(".side-nav .nav-section").nth(1).get_attribute("data-nav-key") == gkey
    stored = page.evaluate("() => JSON.parse(localStorage.getItem('lsSidebarOrder'))")
    assert stored["groups"][1] == gkey, stored
    # Width stepper (20 px steps through the same clamp + storage key as the drag).
    wider = page.locator(".sb-width-row .sb-wider")
    wider.click()
    assert page.evaluate("() => localStorage.getItem('lsSidebarWidth')") == "272"
    assert page.locator(".sb-width-now").text_content() == "272 px"
    page.locator(".sb-width-row .sb-narrower").click()
    page.locator(".sb-width-row .sb-narrower").click()
    assert page.evaluate("() => localStorage.getItem('lsSidebarWidth')") == "232"
    page.locator(".sb-width-row .sb-width-reset").click()
    assert page.evaluate("() => localStorage.getItem('lsSidebarWidth')") is None
    # The keyboard window splitter, exposed only while editing.
    assert (
        handle.get_attribute("role") == "separator" and handle.get_attribute("aria-hidden") is None
    )
    assert handle.get_attribute("tabindex") == "0"
    handle.focus()
    for press, want in (
        ("ArrowRight", "272"),
        ("End", "420"),
        ("ArrowRight", "420"),
        ("Home", "180"),
    ):
        page.keyboard.press(press)
        assert handle.get_attribute("aria-valuenow") == want, (
            press,
            handle.get_attribute("aria-valuenow"),
        )
    page.keyboard.press("Enter")
    assert handle.get_attribute("aria-valuenow") == "252"
    assert page.evaluate("() => localStorage.getItem('lsSidebarWidth')") is None
    for b in page.locator(".sb-move button, .sb-width-row button").all():
        box = b.bounding_box()
        assert box and box["width"] >= 24 and box["height"] >= 24, box
    found = _run_axe(page, axe_src)
    assert not found, f"sidebar edit mode must be axe-clean: {found}"
    page.locator(".sb-done").click()
    assert _is_focused(edit) and edit.get_attribute("aria-pressed") == "false"
    assert page.locator(".sb-move").count() == 0
    assert handle.get_attribute("aria-hidden") == "true" and handle.get_attribute("role") is None
    keys = page.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('lsSidebar'))")
    assert set(keys) <= {"lsSidebarOrder", "lsSidebarWidth", "lsSidebarCollapsed"}, keys
    page.evaluate(
        "() => { localStorage.removeItem('lsSidebarOrder');"
        " localStorage.removeItem('lsSidebarWidth'); }"
    )
    print("  projects: sidebar move buttons, width stepper and separator work by keyboard")


def _check_disclosures(page, label: str) -> None:
    """The seeded QA flag and TM lock disclosures are on the page, and every summary is
    a 24 x 24 px target (WCAG 2.5.8; axe then meets them too)."""
    for kind in ("qa-flag", "lockinfo"):
        summaries = page.locator(f"details.{kind} > summary")
        assert summaries.count(), f"{label}: the seed shows no {kind} disclosure"
        for summary in summaries.all():
            box = summary.bounding_box()
            assert box and box["width"] >= 24 and box["height"] >= 24, (label, kind, box)
    assert not page.locator("details.qa-flag b, details.qa-flag i").count(), (
        f"{label}: a QA flag message rendered as markup"
    )


# Dispatch one keydown on a textarea, optionally as a Mac (navigator.platform and
# userAgentData overridden for the call only); returns whether it was prevented.
_JS_KEYDOWN = """([id, init, mac]) => {
  const ta = document.getElementById(id);
  if (mac) {
    const own = (get) => ({ get, configurable: true });
    Object.defineProperty(navigator, 'platform', own(() => 'MacIntel'));
    Object.defineProperty(navigator, 'userAgentData', own(() => ({ platform: 'macOS' })));
  }
  try {
    const opts = { bubbles: true, cancelable: true, code: 'KeyA' };
    const ev = new KeyboardEvent('keydown', Object.assign(opts, init));
    ta.dispatchEvent(ev);
    return ev.defaultPrevented;
  } finally {
    delete navigator.platform;
    delete navigator.userAgentData;
  }
}"""

# Record where the alternates popover is FIRST placed: a MutationObserver callback runs
# right after the task that inserted and positioned it, before any scroll or resize
# event could re-place it.
_JS_WATCH_FIRST_PLACEMENT = """() => {
  window.__altFirst = null;
  const obs = new MutationObserver(() => {
    const m = document.querySelector('.altmenu');
    if (!m) return;
    obs.disconnect();
    const box = (r) => ({ x: r.left, y: r.top, w: r.width, h: r.height });
    const t = document.querySelector('[aria-controls="' + m.id + '"]');
    window.__altFirst = { m: box(m.getBoundingClientRect()), t: box(t.getBoundingClientRect()),
      vw: document.documentElement.clientWidth, vh: window.innerHeight };
  });
  obs.observe(document.body, { childList: true, subtree: true });
}"""


def _assert_first_placement(page, label: str) -> str:
    """The popover's FIRST placement is attached to its trigger: 4 px below it or 4 px
    above it (flipped), right edges aligned unless clamped 8 px inside the viewport."""
    page.wait_for_function("() => window.__altFirst")
    return _assert_attached(page.evaluate("() => window.__altFirst"), label, "first placed")


# Count the sticky toolbar's size changes from now on (a ResizeObserver's first callback
# only reports the size it started with).
_JS_WATCH_TOOLBAR = """() => {
  window.__barResized = 0;
  let first = true;
  new ResizeObserver(() => { if (first) { first = false; return; } window.__barResized++; })
    .observe(document.querySelector('.editor-toolbar'));
}"""
_JS_PLACED_NOW = """(id) => {
  const m = document.getElementById(id);
  const t = document.querySelector('[aria-controls="' + id + '"]');
  const box = (r) => ({ x: r.left, y: r.top, w: r.width, h: r.height });
  return { m: box(m.getBoundingClientRect()), t: box(t.getBoundingClientRect()),
    vw: document.documentElement.clientWidth, vh: window.innerHeight,
    resized: window.__barResized };
}"""


def _assert_still_attached(page, seg: str, label: str) -> str:
    """2.5 s after opening, the popover's own "N alternates" status (clear, then set) has
    reflowed the sticky toolbar and moved the row; the popover followed it (fix round 1,
    LOW 2: it used to re-place only on scroll or resize and overlapped its trigger)."""
    page.wait_for_timeout(2500)
    g = page.evaluate(_JS_PLACED_NOW, f"alts-{seg}")
    assert g["resized"], f"{label}: the toolbar never reflowed, this check is vacuous {g}"
    return _assert_attached(g, label, "2.5 s after opening")


def _assert_attached(g: dict, label: str, when: str) -> str:
    m, t = g["m"], g["t"]
    below = abs(m["y"] - (t["y"] + t["h"] + 4)) <= 1
    above = abs(m["y"] + m["h"] - (t["y"] - 4)) <= 1
    flip = t["y"] + t["h"] + 4 + m["h"] > g["vh"] - 8 and t["y"] - m["h"] - 4 >= 8
    assert above if flip else below, (
        f"{label}: popover {when} away from its trigger (flip={flip}) {g}"
    )
    right = abs(m["x"] + m["w"] - (t["x"] + t["w"])) <= 1
    clamped = abs(m["x"] - 8) <= 1 or abs(m["x"] + m["w"] - (g["vw"] - 8)) <= 1
    assert right or clamped, f"{label}: popover {when} off its trigger's edge {g}"
    return "below" if below else "above"


def _editor_keyboard(page, axe_src: str) -> None:
    """T4 in a real browser: the alternates popover by keyboard (open, arrows, Home/End,
    Escape and Enter returning focus to the textarea), axe-clean while open; row state
    as text; the approve-after-failed-save bug; busy buttons keep focus."""
    ta = page.locator("textarea.tgt-edit").first
    seg = ta.get_attribute("data-seg")
    trigger = page.locator(f'button[data-action="alternates"][data-id="{seg}"]')
    menu = page.locator(f"#alts-{seg}")
    items = menu.locator("button.altitem")
    assert trigger.get_attribute("aria-expanded") == "false"
    assert trigger.get_attribute("aria-controls") == f"alts-{seg}"
    ta.focus()
    # Gate round 1 (HIGH): a keystroke that TYPES a letter is never taken as the
    # shortcut: Mac Option+A ("å" on US / German, "ą" on Polish Pro) and Windows AltGr+A
    # ("ą", reported as Ctrl+Alt) are not prevented and open nothing.
    alt_requests: list[str] = []

    def on_request(r, seen=alt_requests):
        if r.url.endswith("/alternates"):
            seen.append(r.url)

    page.on("request", on_request)
    for label, init, mac in (
        ("Mac Option+A typing a-ring", {"key": "å", "altKey": True}, True),
        ("Mac Option+A typing a-ogonek", {"key": "ą", "altKey": True}, True),
        ("Mac Option+Shift+A", {"key": "Å", "altKey": True, "shiftKey": True}, True),
        ("Windows AltGr+A", {"key": "ą", "altKey": True, "ctrlKey": True}, False),
        # T11: VoiceOver's own modifier is never the page's shortcut.
        ("Mac Ctrl+Option+A (VoiceOver VO+A)", {"key": "a", "altKey": True, "ctrlKey": True}, True),
    ):
        prevented = page.evaluate(_JS_KEYDOWN, [f"tgt-{seg}", init, mac])
        assert not prevented, f"{label}: the keystroke was prevented (its letter would be lost)"
    page.wait_for_timeout(300)
    assert menu.count() == 0 and not alt_requests, (
        f"a typed letter opened alternates {alt_requests}"
    )
    assert trigger.get_attribute("aria-busy") != "true"
    # The Mac chord (Ctrl+Shift+A, T11) and Windows / Linux Alt+A do open it.
    for label, init, mac in (
        ("Mac Ctrl+Shift+A", {"key": "A", "shiftKey": True, "ctrlKey": True}, True),
        ("Windows Alt+A", {"key": "a", "altKey": True}, False),
    ):
        assert page.evaluate(_JS_KEYDOWN, [f"tgt-{seg}", init, mac]), f"{label} not taken"
        menu.wait_for(state="visible")
        page.keyboard.press("Escape")
        assert menu.count() == 0 and _is_focused(ta), f"{label}: Escape did not return focus"
    page.remove_listener("request", on_request)
    assert len(alt_requests) >= 1, alt_requests
    # First placement is attached to the trigger (gate round 1, LOW 3).
    page.evaluate(_JS_WATCH_FIRST_PLACEMENT)
    page.evaluate(_JS_WATCH_TOOLBAR)
    page.keyboard.press("Alt+KeyA")
    menu.wait_for(state="visible")
    _assert_first_placement(page, "first row")
    _assert_still_attached(page, seg, "first row")
    n = items.count()
    assert n >= 2, f"the offline engine should propose alternates ({n})"
    assert trigger.get_attribute("aria-expanded") == "true"
    assert menu.get_attribute("role") == "group" and menu.get_attribute("aria-label")
    assert _is_focused(items.first), "focus did not move into the popover"
    for key, index in (
        ("End", n - 1),
        ("Home", 0),
        ("ArrowUp", n - 1),
        ("ArrowDown", 0),
        ("ArrowDown", 1),
    ):
        page.keyboard.press(key)
        assert _is_focused(items.nth(index)), f"{key} did not focus item {index}"
    assert not _run_axe(page, axe_src), "the open alternates popover must be axe-clean"
    page.keyboard.press("Escape")
    assert menu.count() == 0 and trigger.get_attribute("aria-expanded") == "false"
    assert _is_focused(ta), "Escape did not return focus to the textarea"
    # Alt+A inside the popover closes it too (toggle); focus returns to the text.
    page.keyboard.press("Alt+KeyA")
    menu.wait_for(state="visible")
    page.keyboard.press("Alt+KeyA")
    assert menu.count() == 0 and _is_focused(ta), "Alt+A in the popover did not close it"
    # Enter inserts the focused choice, saves, and returns focus to the textarea.
    page.keyboard.press("Alt+KeyA")
    menu.wait_for(state="visible")
    choice = items.nth(1).inner_text()
    page.keyboard.press("ArrowDown")
    with page.expect_response(lambda r: r.url.endswith(f"/segments/{seg}/save")):
        page.keyboard.press("Enter")
    assert menu.count() == 0 and _is_focused(ta), "Enter did not return focus to the textarea"
    assert ta.input_value() == choice, (ta.input_value(), choice)
    page.wait_for_function(f"() => !document.getElementById('rs-{seg}')")
    # Tab out of the popover closes it where focus went.
    page.keyboard.press("Alt+KeyA")
    menu.wait_for(state="visible")
    for _ in range(n):
        page.keyboard.press("Tab")
    assert menu.count() == 0, "focus left the popover but it stayed open"
    # The last row at the bottom edge of the viewport: first placement flips above the
    # trigger and sits 4 px from it (round 1 measured it floating ~45 px higher).
    last = page.locator('button[data-action="alternates"]').last
    last_id = last.get_attribute("data-id")
    last.evaluate("e => e.scrollIntoView({ block: 'end' })")
    last.focus()
    page.evaluate(_JS_WATCH_FIRST_PLACEMENT)
    # 2026-09-24 flake, made deterministic: the status line above the table is written
    # asynchronously (1 s gap, clear then set), and a long message wraps the sticky
    # toolbar, pushing this bottom row BELOW the viewport while its alternates load.
    # Round 1 then clamped the popover to the viewport edge, away from its off-screen
    # trigger. Hold the alternates answer; once "Loading..." has landed, empty the
    # status line (the announcer's clear), put the row at the bottom edge, write a long
    # status (the announcer's set, a moment later in real use), check the row really
    # was pushed out (else this block proves nothing), then release the answer.
    held: list = []

    def hold(route):  # one parameter: Playwright passes (route, request) to two
        held.append(route)

    page.route("**/segments/*/alternates", hold)
    with page.expect_request(lambda r: r.url.endswith(f"/segments/{last_id}/alternates")):
        page.keyboard.press("Enter")
    number = page.locator(f"#tgt-{last_id}").get_attribute("data-number")
    page.wait_for_function(
        "() => document.getElementById('edStatus').textContent"
        f".includes('Loading alternates for segment {number}.')"
    )
    # ux/review-editor: Chrome's scroll anchoring now keeps the row still when the status
    # grows (the grid is no longer inside a scroll wrapper), which would hide the worst
    # case this block constructs. Switch anchoring off everywhere, not just on the root:
    # with the compact rows Chrome still anchored (re-gate 2 measured the window scroll
    # following the status growth with only the root off, likely the focused "alt" button).
    # The layout must still push the row (the assert below); anchoring is restored after
    # the popover check so later steps run as on main.
    page.evaluate(
        "() => { document.documentElement.style.overflowAnchor = 'none';"
        " const st = document.createElement('style'); st.id = 'rg-no-anchor';"
        " st.textContent = '*{overflow-anchor:none !important}'; document.head.append(st);"
        " document.getElementById('edStatus').textContent = ''; }"
    )
    last.evaluate("e => e.scrollIntoView({ block: 'end' })")
    page.evaluate(
        "(n) => { document.getElementById('edStatus').textContent = 'Alternates for segment '"
        " + n + ' are ready. Press Alt+A in that segment to see them. 3 alternates. Use"
        " arrow keys to choose, Escape to go back. ' + 'Alternates are ready. '.repeat(8); }",
        number,
    )
    pushed = last.evaluate("e => e.getBoundingClientRect().bottom > innerHeight")
    assert pushed, "a long status line no longer moves the table: this check is vacuous, revisit it"
    assert len(held) == 1, held
    page.evaluate(_JS_WATCH_TOOLBAR)
    held[0].continue_()
    page.locator("#alts-" + last_id).wait_for(state="visible")
    page.unroute("**/segments/*/alternates")
    print(
        f"  editor: last-row popover first placed {_assert_first_placement(page, 'last row')},"
        f" 2.5 s later {_assert_still_attached(page, last_id, 'last row')}"
    )
    page.keyboard.press("Escape")
    assert page.locator(".altmenu").count() == 0
    page.evaluate(
        "() => { document.getElementById('rg-no-anchor').remove();"
        " document.documentElement.style.overflowAnchor = ''; }"
    )
    # Row state as text + the approve bug: a failed save shows "Save failed" and a Retry
    # button in the row; Ctrl+Enter then must NOT approve (the old text) and must say so.
    posted: list[str] = []
    page.on("request", lambda r: posted.append(r.url) if r.method == "POST" else None)
    fail = {"save": True}

    def save_route(route):
        if fail["save"]:
            route.abort()
        else:
            route.continue_()

    page.route("**/segments/*/save", save_route)
    ta.focus()
    page.keyboard.type(" edit")
    rowstate = page.locator(f"#rs-{seg}")
    assert "Unsaved" in rowstate.inner_text()
    page.keyboard.press("Control+Enter")
    page.wait_for_function(
        "() => (document.getElementById('edAlert').textContent || '').includes('Not approved')"
    )
    assert not any(u.endswith("/approve") for u in posted), "approve POSTed after a failed save"
    assert "Save failed" in rowstate.inner_text()
    row = page.locator(f'tr[data-seg="{seg}"]')
    assert "save-failed" in (row.get_attribute("class") or "")
    assert _is_focused(ta), "a refused approve moved focus"
    retry = rowstate.locator("button")
    assert "saving segment" in retry.evaluate("e => e.textContent")
    # Moving focus to Retry blurs the textarea, whose save still fails (quietly: the
    # row already failed); a focused Retry survives that and keeps focus.
    retry.focus()
    page.wait_for_timeout(300)
    assert _is_focused(retry), "a repeated failure rebuilt the Retry button and dropped focus"
    fail["save"] = False
    page.keyboard.press("Enter")
    page.wait_for_function(f"() => !document.getElementById('rs-{seg}')")
    assert "save-failed" not in (row.get_attribute("class") or "")
    assert _is_focused(ta), "a successful Retry dropped focus instead of returning to the text"
    page.wait_for_function(
        "() => (document.getElementById('edStatus').textContent || '').includes('saved')"
    )
    # Busy JS path keeps keyboard focus (no real `disabled` on the approve button).
    approve = page.locator(f'button[data-action="approve"][data-id="{seg}"]')
    approve.focus()
    with page.expect_response(lambda r: r.url.endswith(f"/segments/{seg}/approve")):
        page.keyboard.press("Enter")
    page.wait_for_function(
        f'() => document.querySelector(\'button[data-action="approve"][data-id="{seg}"]\')'
        ".getAttribute('aria-busy') !== 'true'"
    )
    assert _is_focused(approve), "the busy approve button lost keyboard focus"
    # The route above also failed the last row's blur save (ta.focus() left it), so
    # "Segment 4 was not saved" is alerted about a second later, often while this
    # approval waits out the 1 s polite gap. It used to drop the approval (the
    # 2026-09-24 flake); a kept outcome must survive it (node: verify_a11y_live_status).
    page.wait_for_function(
        "() => /approved\\. \\d+ of \\d+ confirmed/"
        ".test(document.getElementById('edStatus').textContent)"
    )
    page.unroute("**/segments/*/save")


def _editor_popover_reflow(page) -> None:
    """T4 at 320 px: the Editor table scrolls inside its own wrapper, and the alternates
    popover opened from the far-right actions column is fully on screen, not clipped."""
    assert page.locator(".tablewrap > table.editor").count() == 1, "Editor table not wrapped"
    # ux/review-editor: first the real 320 px layout (the grid stacks): the popover from the
    # last row's actions is fully on screen and not covered, and Escape returns focus.
    real = page.locator('button[data-action="alternates"]').last
    real_seg = real.get_attribute("data-id")
    real.focus()
    page.keyboard.press("Enter")
    real_menu = page.locator(f"#alts-{real_seg}")
    real_menu.wait_for(state="visible")
    rbox = real_menu.bounding_box()
    rwidth = page.evaluate("() => document.documentElement.clientWidth")
    assert rbox and rbox["x"] >= 0 and rbox["x"] + rbox["width"] <= rwidth, (rbox, rwidth)
    ritem = real_menu.locator("button.altitem").first.bounding_box()
    assert page.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y);"
        " return !!(e && e.closest('.altmenu')); }",
        [ritem["x"] + ritem["width"] / 2, ritem["y"] + ritem["height"] / 2],
    ), "the popover is clipped or covered in the stacked 320 px layout"
    page.keyboard.press("Escape")
    page.wait_for_function(f"() => !document.getElementById('alts-{real_seg}')")
    assert _is_focused(page.locator(f"#tgt-{real_seg}")), "Escape did not return focus"
    # ux/review-editor: the segment grid now stacks at 320 px, so this wrapper no longer
    # overflows by itself. Rebuild the premise this test is about (an actions column
    # past the right edge of a scrolling wrapper) with CSSOM style properties, which a
    # strict CSP allows, then scroll the trigger into view before opening the popover.
    page.evaluate(
        "() => { const t = document.querySelector('table.editor');"
        " t.style.setProperty('min-width', '900px', 'important');"
        " document.querySelectorAll('table.editor td.acts').forEach(td => {"
        " td.style.setProperty('justify-content', 'flex-end'); }); }"
    )
    assert page.locator(".tablewrap:has(> table.editor)").evaluate(
        "e => e.scrollWidth > e.clientWidth + 1"
    ), "the Editor wrapper must scroll sideways for this check to mean anything"
    trigger = page.locator('button[data-action="alternates"]').last
    seg = trigger.get_attribute("data-id")
    trigger.focus()
    page.keyboard.press("Enter")
    menu = page.locator(f"#alts-{seg}")
    menu.wait_for(state="visible")
    box = menu.bounding_box()
    width = page.evaluate("() => document.documentElement.clientWidth")
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, (box, width)
    first = menu.locator("button.altitem").first
    fb = first.bounding_box()
    hit = page.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y);"
        " return !!(e && e.closest('.altmenu')); }",
        [fb["x"] + fb["width"] / 2, fb["y"] + fb["height"] / 2],
    )
    assert hit, "the popover is clipped or covered at 320 px"
    # Scrolling the wrapper back left takes the trigger out of its scroller: the popover
    # closes (round 1 left it clamped to the viewport edge, detached from its row) and
    # the scroll position the user chose is kept (focus goes back to the row's text
    # without scrolling).
    wrap = page.locator(".tablewrap:has(> table.editor)")
    assert first.evaluate("e => e === document.activeElement")
    wrap.evaluate("e => { e.scrollLeft = 0; }")
    page.wait_for_function(f"() => !document.getElementById('alts-{seg}')")
    assert trigger.get_attribute("aria-expanded") == "false"
    page.wait_for_timeout(200)
    assert wrap.evaluate("e => e.scrollLeft") == 0, "closing the popover scrolled the table back"
    assert _is_focused(page.locator(f"#tgt-{seg}")), "focus was lost with the closed popover"
    # Escape closes it too and returns focus to the text, wherever the wrapper is scrolled.
    trigger.focus()
    page.keyboard.press("Enter")
    menu.wait_for(state="visible")
    page.keyboard.press("Escape")
    page.wait_for_function(f"() => !document.getElementById('alts-{seg}')")
    assert trigger.get_attribute("aria-expanded") == "false"
    assert _is_focused(page.locator(f"#tgt-{seg}")), "Escape did not return focus to the text"


# Train L1 (editable page layouts): the saved layout the "project-layout" entry measures.
# A 3-across row first (TM, Glossary, Target languages), so the reading order visibly
# differs from the default markup order.
_LAYOUT_ROWS = [
    ["tm", "glossary", "langs"],
    ["overview", "documents", "upload"],
    ["budget"],
    ["members"],
]
# Train L2: the same saved-layout case on Dashboard, Setup AI and Users (the maintainer named
# them). entry name -> (page key, saved rows). Each starts with a 3-across row whose
# middle card is the one the keyboard moves; on Dashboard and Setup AI that card's h2
# sits inside a .row-between next to a link or badge, so the edit mode must keep that
# nested heading visible while it hides its neighbours.
_LAYOUT_CASES = {
    "project-layout": ("project", _LAYOUT_ROWS),
    "dashboard-layout": ("dashboard", [["quick", "running", "activity"]]),
    "setup-ai-layout": (
        "setup_ai",
        [
            ["egress", "local", "cloud"],
            ["tm_match"],
            ["defaults", "spend"],
            ["engines"],
            ["assist_limits"],
        ],
    ),
    "users-layout": ("users", [["members", "add", "invite"], ["expired"]]),
    # The Chunk plan card has no h2 (its name is a <summary>): the edit mode names the
    # tile from its data-card-title with a visible .layout-tile-title.
    "document-layout": ("document", [["chunks", "context", "segments"], ["workflow"]]),
}
_JS_LAYOUT_FETCH = r"""
async ([method, rows, pageKey]) => {
  const init = {method, headers: {'X-CSRF-Token': window.CSRF_TOKEN}};
  if (rows) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify({v: 1, rows});
  }
  const r = await fetch('/account/layouts/' + pageKey, init);
  return r.status;
}
"""
_JS_TILE_NAMES = r"""
() => Array.from(document.querySelectorAll('#page-layout section[data-card]')).map(s => {
  const h = s.querySelector('h2') || s.querySelector('.layout-tile-title');
  const r = h ? h.getBoundingClientRect() : null;
  return [s.dataset.card, !!(h && r.width > 0 && r.height > 0 && !h.closest('[inert]'))];
}).filter(x => !x[1]).map(x => x[0])
"""
_JS_LAYOUT_ROWS = r"""
() => Array.from(document.querySelectorAll('#page-layout > .layout-row')).map(
  r => Array.from(r.querySelectorAll(':scope > section[data-card]')).map(s => s.dataset.card))
"""
_JS_CARD_OF_FOCUS = r"""
() => { const s = document.activeElement && document.activeElement.closest('[data-card]');
        return s ? s.dataset.card : null; }
"""
_JS_TABBABLE_CARDS = r"""
() => Array.from(document.querySelectorAll('#page-layout section[data-card]')).filter(s =>
  Array.from(s.querySelectorAll('a[href], button, input, select, textarea, summary'))
    .some(e => !e.disabled && e.tabIndex >= 0 && e.getClientRects().length > 0)
).map(s => s.dataset.card)
"""


def _layout_setup(page, base: str, path: str, axe_src: str, name: str) -> None:
    """Save the case's layout (a 3-across row first) through the page's own session
    (CSRF header), reload, and prove it is applied by MOVING the cards (DOM order =
    visual order), that Tab follows it, and that the edit mode is keyboard-operable,
    names every tile by a visible heading and is axe-clean."""
    page_key, rows = _LAYOUT_CASES[name]
    status = page.evaluate(_JS_LAYOUT_FETCH, ["PUT", rows, page_key])
    assert status == 200, f"{name}: saving the layout through the page failed: HTTP {status}"
    _visit(page, base, path)
    got = page.evaluate(_JS_LAYOUT_ROWS)
    assert got == rows, f"{name}: saved layout not applied: {got}"
    cols = page.locator("#page-layout > .layout-row").first.evaluate(
        "e => getComputedStyle(e).gridTemplateColumns.split(' ').length"
    )
    assert cols == 3, f"{name}: a 3-across row must render 3 columns at 1366 px, got {cols}"
    # Tab order follows the layout: walk forward from the Edit layout button and record
    # the card each stop lands in; the cards must come in the saved order.
    expected = page.evaluate(_JS_TABBABLE_CARDS)
    flat = [c for row in rows for c in row]
    assert expected == [c for c in flat if c in expected], (expected, flat)
    page.locator("[data-layout-edit]").focus()
    seen: list = []
    for _ in range(1200):
        page.keyboard.press("Tab")
        card = page.evaluate(_JS_CARD_OF_FOCUS)
        if card and (not seen or seen[-1] != card):
            seen.append(card)
        if seen and seen[-1] == expected[-1]:
            break
    assert seen == expected, f"{name}: Tab order does not follow the layout: {seen} != {expected}"
    # The edit mode: keyboard-reachable, focus on Done, axe-clean while open, every tile
    # named by a visible heading, a Move button keeps focus after moving its card, Escape
    # cancels and restores the layout.
    page.locator("[data-layout-edit]").focus()
    page.keyboard.press("Enter")
    assert page.evaluate("() => document.activeElement.dataset.layoutBar") == "done"
    assert page.locator("#page-layout.layout-editing").count() == 1
    assert not _run_axe(page, axe_src), f"{name}: the layout edit mode must be axe-clean"
    unnamed = page.evaluate(_JS_TILE_NAMES)
    assert not unnamed, f"{name}: tiles without a visible heading in edit mode: {unnamed}"
    for target in page.locator(".layout-tool, .layout-wopt input").all()[:12]:
        box = target.bounding_box()
        assert box and box["width"] >= 24 and box["height"] >= 24, box
    card = rows[0][1]
    # The card's name is its heading WITHOUT decorative aria-hidden icons (L2 gate LOW 3,
    # fixed in T11), so the Move button's name starts with words.
    title = page.locator(f'#page-layout section[data-card="{card}"] h2').first.evaluate(
        "e => { const c = e.cloneNode(true);"
        " c.querySelectorAll('[aria-hidden=\"true\"]').forEach((n) => n.remove());"
        " return c.textContent.replace(/\\s+/g, ' ').trim(); }"
    )
    move = page.locator(f'[data-layout-card="{card}"][data-layout-action="down"]')
    label = move.evaluate("e => e.textContent")
    assert title and title in label and "Move down" in label, (label, title)
    assert re.search(r"Move down: \w", label), f"{name}: the Move name must start with words"
    move.focus()
    page.keyboard.press("Enter")
    assert move.evaluate("e => e === document.activeElement"), "focus must stay on the button"
    assert page.evaluate(_JS_LAYOUT_ROWS)[1] == [card], page.evaluate(_JS_LAYOUT_ROWS)
    page.keyboard.press("Escape")
    assert page.locator("#page-layout.layout-editing").count() == 0
    assert page.evaluate(_JS_LAYOUT_ROWS) == rows, f"{name}: Escape must restore the layout"
    assert page.evaluate("() => document.activeElement.hasAttribute('data-layout-edit')")
    # Train L2 x T9: neither the session warning nor the page error summary may sit
    # inside the movable area (a saved layout rebuilds it from its cards only).
    assert page.evaluate(
        "() => !document.querySelector('#page-layout #session-dialog, #page-layout .error-summary')"
    ), f"{name}: session dialog or error summary inside #page-layout"
    if name == "project-layout":
        # Gate L1 round 2: engine behaviour, measured once on the project page's rows.
        _layout_inflight(page)
        _layout_under_session_dialog(page)


def _layout_under_session_dialog(page) -> None:
    """Gate L1 round 2 (T9 x L1): with edit mode open and an UNSAVED move, the session
    warning dialog opens. Escape there (which the warning refuses on purpose) must not
    cancel edit mode underneath: the rows stay as moved, and "Stay signed in" returns
    focus to Done. The warning is opened through its real path: /auth/session reports 90 s
    left, and a visibilitychange makes session_timeout.js ask."""
    assert page.locator("#session-dialog").count() == 1, "the session warning must be on the page"
    page.route(
        "**/auth/session",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"signed_in": true, "remaining_seconds": 90, "ending": "idle"}',
        ),
    )
    page.route(
        "**/auth/keepalive",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"signed_in": true, "remaining_seconds": 3600, "ending": "idle"}',
        ),
    )
    try:
        page.locator("[data-layout-edit]").click()
        page.locator('[data-layout-card="tm"][data-layout-action="down"]').click()
        moved = page.evaluate(_JS_LAYOUT_ROWS)
        page.locator('[data-layout-bar="done"]').focus()
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_selector("#session-dialog[open]")
        assert page.evaluate("() => document.activeElement.id") == "session-stay"
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert page.locator("#session-dialog[open]").count() == 1, "the warning refuses Escape"
        assert page.locator("#page-layout.layout-editing").count() == 1, (
            "Escape inside the session dialog cancelled layout edit mode underneath it"
        )
        assert page.evaluate(_JS_LAYOUT_ROWS) == moved, "the unsaved move was thrown away"
        page.locator("#session-stay").click()
        page.wait_for_selector("#session-dialog[open]", state="detached")
        page.wait_for_function(
            "() => document.activeElement && "
            "document.activeElement.getAttribute('data-layout-bar') === 'done'"
        )
        assert page.evaluate(_JS_LAYOUT_ROWS) == moved
        # Escape outside any dialog still cancels, as before.
        page.keyboard.press("Escape")
        assert page.locator("#page-layout.layout-editing").count() == 0
    finally:
        page.unroute("**/auth/session")
        page.unroute("**/auth/keepalive")


_JS_HOLD_FETCH = r"""
() => { window.__realFetch = window.__realFetch || window.fetch;
  window.fetch = (u, i) => new Promise(res => {
    window.__release = () => res(window.__realFetch(u, i)); }); }
"""


def _layout_inflight(page) -> None:
    """Gate L1 LOW-6: while Done's PUT is in flight the layout is frozen. Escape, Cancel
    and Move buttons do nothing, and when the save lands the page shows exactly what the
    server stored and says so (screen, announcement and server agree)."""
    page.locator("[data-layout-edit]").click()
    page.locator('[data-layout-card="glossary"][data-layout-action="down"]').click()
    moved = page.evaluate(_JS_LAYOUT_ROWS)
    assert moved != _LAYOUT_ROWS, moved
    page.evaluate(_JS_HOLD_FETCH)
    page.locator('[data-layout-bar="done"]').click()
    page.keyboard.press("Escape")
    page.locator('[data-layout-bar="cancel"]').click()
    page.locator('[data-layout-card="tm"][data-layout-action="down"]').click()
    assert page.locator("#page-layout.layout-editing").count() == 1, "Escape/Cancel ran mid-save"
    assert page.evaluate(_JS_LAYOUT_ROWS) == moved, "a move ran mid-save"
    page.evaluate("() => { window.__release(); window.fetch = window.__realFetch; }")
    page.wait_for_function(
        "() => !document.getElementById('page-layout').classList.contains('layout-editing')"
    )
    page.wait_for_timeout(400)
    assert page.evaluate(_JS_LAYOUT_ROWS) == moved, "the screen must show what was saved"
    assert "saved" in (page.locator("#layoutAnnounce").text_content() or "").lower()
    server = page.evaluate(
        "async () => (await (await fetch('/account/layouts/project')).json()).rows"
    )
    assert server[: len(moved)] == moved, (server, moved)
    # Back to the measured layout for the rest of this page's checks.
    assert page.evaluate(_JS_LAYOUT_FETCH, ["PUT", _LAYOUT_ROWS, "project"]) == 200


def _walk(browser, base: str, ids: dict, axe_src: str, dog: _Watchdog, data_dir: str) -> dict:
    results: dict = {}
    opts = {"viewport": {"width": 1366, "height": 900}, "reduced_motion": "reduce"}
    # Exercise the production CSP. Axe is installed through DevTools evaluation.
    anon = browser.new_context(**opts)
    auth = browser.new_context(**opts)
    _login(auth, base)
    _check_script_csp(auth, base)
    for name, path, light, walk in _pages(ids):
        ctx = anon if name == "login" else auth
        page = ctx.new_page()
        page.set_default_timeout(_ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(_ACTION_TIMEOUT_MS)
        dog.on_page(name)
        t0 = time.time()
        try:
            page.goto(base + ("/auth/login" if name == "login" else "/"))
            _set_theme(page, "dark")
            _visit(page, base, path)
            if name in _LAYOUT_CASES:
                _layout_setup(page, base, path, axe_src, name)
            if name == "customer-detail":
                # Open the page's own disclosures (Rename customer), not the user menu:
                # its open panel is a dropdown that covers the Delete button and was the
                # one target-size node here (T11, WCAG 2.5.8), a test artifact.
                page.locator("details:not(.usermenu)").evaluate_all(
                    "els => els.forEach(el => el.open = true)"
                )
                assert not page.locator("details.usermenu[open]").count()
                targets = page.locator(".danger-btn")
                assert targets.count(), "seed must exercise customer detail delete controls"
                for target in targets.all():
                    box = target.bounding_box()
                    assert box and box["width"] >= 24 and box["height"] >= 24, box
            if name == "help":
                assert (
                    page.locator("main h1, main h2, main h3").first.evaluate("el => el.tagName")
                    == "H1"
                )
            if name == "projects":
                page.locator("[data-np-open]").first.click()
                radios = page.locator('#new-project-dialog input[name="project_type"]')
                assert radios.count() == 3  # Documents, Keys, Connected
                for radio in radios.all():
                    box = radio.bounding_box()
                    assert box and box["width"] >= 24 and box["height"] >= 24, box
                assert not _run_axe(page, axe_src), "open New project dialog must be axe-clean"
                page.locator("#new-project-dialog").evaluate("el => el.close()")
            if name == "workflow":
                # a11y T8: the diagram is described by the step list; move / remove
                # buttons are named by their visible words plus the step.
                assert (
                    page.locator("svg.wf-graph-svg").first.get_attribute("aria-describedby")
                    == "wfStepList"
                )
                assert page.locator("ol#wfStepList.steplist > li").count() == 2
                # Chrome blockifies the absolutely positioned sr-only suffix, so its name
                # reads "move down : Human review"; either way it STARTS with the visible
                # words (2.5.3), which is what is asserted.
                # #594: each step is its own tab, so only the selected step's buttons are
                # in the accessibility tree; select the step's tab first (tab 0 is Workflow).
                for words, step, tab in (
                    ("move down", "Human review", 1),
                    ("move up", "Quality check", 2),
                    ("remove", "Human review", 1),
                    ("remove", "Quality check", 2),
                ):
                    page.locator("[data-wf-tabs] [role=tab]").nth(tab).click()
                    pattern = re.compile("^" + re.escape(words) + r" ?: " + re.escape(step) + "$")
                    assert page.get_by_role("button", name=pattern).count() == 1, (
                        words,
                        step,
                        page.locator("ol#wfStepList").aria_snapshot(),
                    )
            if name == "run-view":
                svg = page.locator("#runGraphCard svg.wf-graph-svg")
                assert svg.get_attribute("aria-label") == "Workflow run diagram"
                assert svg.get_attribute("aria-describedby") == "wfgText"
                text = page.locator("#runGraphCard details.wfg-text")
                assert text.get_attribute("open") is None
                text.locator("summary").focus()
                page.keyboard.press("Enter")
                assert text.get_attribute("open") is not None
                lines = page.locator("#wfgText > li").all_inner_texts()
                assert len(lines) == 2 and lines[0].startswith("Step 1: Human review."), lines
                assert "Status: failed." in lines[0] and "Status: done" in lines[1], lines
                assert page.locator("#runGraphCard [aria-live]").count() == 0
                text.locator("summary").click()  # closed again: measured as a user finds it
            if name == "workflow-new":
                tabs = page.locator('.wf-tab[role="tab"]')
                assert tabs.count() == 5
                selected = page.locator('.wf-tab[aria-selected="true"]')
                selected.focus()
                for key, index in (("Home", 0), ("ArrowLeft", 4), ("ArrowRight", 0), ("End", 4)):
                    page.keyboard.press(key)
                    tab = tabs.nth(index)
                    assert tab.evaluate("e => e === document.activeElement && e.tabIndex === 0")
                    assert tab.get_attribute("aria-selected") == "true"
                    panel = page.locator("#" + tab.get_attribute("aria-controls"))
                    assert panel.is_visible() and panel.get_attribute("role") == "tabpanel"
                    assert panel.get_attribute("aria-labelledby") == tab.get_attribute("id")
                    assert page.locator('.wf-tab[tabindex="0"]').count() == 1
                    assert page.locator('[role="tabpanel"]:visible').count() == 1
                tabs.nth(1).click()
                insert = page.locator('[role="tabpanel"]:visible .wf-ins').first
                text = insert.get_attribute("data-insert")
                insert.click()
                assert text in page.locator("#wf-describe-desc").input_value()
            if name == "projects":
                groups = page.locator('.side-nav [role="group"]')
                assert groups.count() == page.locator(".side-nav .nav-group").count() > 0
                assert groups.evaluate_all("""els => els.every(e => {
                    const heading = document.getElementById(e.getAttribute('aria-labelledby'));
                    return heading && e.contains(heading) && heading.textContent.trim();
                })""")
                assert page.locator(".side-nav .nav-glyph").evaluate_all(
                    "els => els.length > 0 && "
                    "els.every(e => e.getAttribute('aria-hidden') === 'true')"
                )
                assert page.locator(".side-nav .badge.beta").evaluate_all("""els => els.every(e =>
                    e.closest('a').getAttribute('aria-label').endsWith(' ' + e.textContent.trim())
                )""")
                # Console: Light / Dark / Match my computer are menuitemradio choices in the
                # account menu (the top-bar toggle is gone). Each click applies at once and
                # moves aria-checked; Match my computer follows the OS setting, live.
                page.locator("details.usermenu summary").click()
                choice = {
                    k: page.locator(f'[data-theme-set="{k}"]') for k in ("light", "dark", "system")
                }
                for k, el in choice.items():
                    assert el.get_attribute("role") == "menuitemradio", k
                for pick in ("light", "dark"):
                    choice[pick].click()
                    assert (
                        page.evaluate("document.documentElement.getAttribute('data-theme')") == pick
                    )
                    for k, el in choice.items():
                        assert el.get_attribute("aria-checked") == (
                            "true" if k == pick else "false"
                        ), (pick, k)
                # ADR 0063: the click is saved on the ACCOUNT, so the next page render
                # (any browser) carries it on <html> before first paint.
                pref = ""
                for _ in range(40):
                    pref = page.evaluate(
                        "async () => (await (await fetch('/account')).text())"
                        ".match(/<html[^>]*data-theme-pref=.([a-z]+)./)?.[1] || ''"
                    )
                    if pref == "dark":
                        break
                    time.sleep(0.05)
                assert pref == "dark", f"the menu choice must be stored per account: {pref!r}"
                page.emulate_media(color_scheme="light")
                choice["system"].click()
                assert (
                    page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
                )
                assert choice["system"].get_attribute("aria-checked") == "true"
                page.emulate_media(color_scheme="dark")
                for _ in range(40):  # poll: the page CSP forbids wait_for_function's eval
                    if (
                        page.evaluate("document.documentElement.getAttribute('data-theme')")
                        == "dark"
                    ):
                        break
                    time.sleep(0.05)
                assert (
                    page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
                )
                page.emulate_media(color_scheme=None)
                page.keyboard.press("Escape")
                _usermenu_focus(page)
                print("  user menu: focus moves in on every open (click, Enter, mouse) OK")
            if name == "users":
                for field in ("role", "department", "customer"):
                    form = page.locator('form[action$="/' + field + '"]').first
                    button = form.locator('button[type="submit"]')
                    assert button.inner_text() == "Save"
                    assert button.get_attribute("aria-label").startswith("Save " + field + " for ")
                    box = button.bounding_box()
                    assert box["height"] >= 24 and box["width"] >= 24
                    control = form.locator("select, input:not([type=hidden])").first
                    navigations = []

                    def listener(frame, events=navigations):
                        events.append(frame.url)

                    page.on("framenavigated", listener)
                    control.dispatch_event("change")
                    page.wait_for_timeout(100)
                    page.remove_listener("framenavigated", listener)
                    assert not navigations, "changing a user field must wait for Save"
            if name in ("editor", "project-keys"):
                _check_disclosures(page, name)
            rec: dict = {"axe": _run_axe(page, axe_src)}
            if name == "workflow":
                # a11y T8 (2.5.3): axe is injected now; the move / remove names start
                # with their visible words, so the experimental rule finds nothing.
                bad = _label_in_name(page)
                assert not bad, f"workflow builder: label not in name {bad}"
            if name == "projects":
                _visit(page, base, path)  # fresh page: axe's injected script is gone
                _sidebar_keyboard(page, axe_src)
            if walk:
                _visit(page, base, path)  # fresh page: axe's injected script is gone
                rec["tabwalk"] = _tab_walk(page)
            if name == "projects":
                # Closed off-canvas links used to remain in the keyboard sequence.
                page.set_viewport_size({"width": 683, "height": 384})
                _visit(page, base, path)
                results[name + "@narrow"] = {"axe": {}, "tabwalk": _tab_walk(page)}
                print(f"  {name}@narrow (683x384): {results[name + '@narrow']['tabwalk']}")
                page.locator("[data-nav-toggle]").click()
                assert page.locator(".main").get_attribute("inert") is not None
                assert page.evaluate("() => !!document.activeElement.closest('.side-nav')")
                page.keyboard.press("Escape")
                assert page.locator(".main").get_attribute("inert") is None
                assert page.locator("[data-nav-toggle]").evaluate(
                    "e => e === document.activeElement"
                )
                page.locator("[data-nav-toggle]").click()
                close = page.locator("[data-nav-close]")
                assert close.is_visible()
                box = close.bounding_box()
                assert box["width"] >= 24 and box["height"] >= 24
                close.focus()
                page.keyboard.press("Enter")
                assert page.locator(".main").get_attribute("inert") is None
                assert page.locator("[data-nav-toggle]").evaluate(
                    "e => e === document.activeElement"
                )
                page.locator("[data-nav-toggle]").click()
                page.locator("[data-nav-backdrop]").click(position={"x": 650, "y": 200})
                assert page.locator(".main").get_attribute("inert") is None
                assert page.locator("[data-nav-toggle]").evaluate(
                    "e => e === document.activeElement"
                )
            page.set_viewport_size({"width": 320, "height": 800})
            _visit(page, base, path)
            page.evaluate(_JS_NORMALIZE_PATHS, _path_needles(data_dir))
            rec["overflow_px"] = int(page.evaluate(_JS_REFLOW)["overflowPx"])
            if name in ("editor", "project-keys"):
                # The disclosures at 320 px too (gate round 1 measured target-size here).
                _check_disclosures(page, name + "@320")
                assert not _run_axe(page, axe_src), f"{name}: axe at 320 px must be clean"
                _visit(page, base, path)  # fresh page: axe's injected script is gone
            if name == "editor":
                _editor_popover_reflow(page)
            if name in _LAYOUT_CASES:
                assert rec["overflow_px"] == 0, "a saved 3-across layout must reflow at 320 px"
                assert (
                    page.locator("#page-layout > .layout-row").first.evaluate(
                        "e => getComputedStyle(e).gridTemplateColumns.split(' ').length"
                    )
                    == 1
                ), "a 3-across row must stack at 320 px"
            if name == "run-view":
                assert rec["overflow_px"] == 0, "long provider URL must reflow at 320px"
                assert page.locator(".step-head .step-note").count() == 1
                assert page.locator(".step-head .error-inline").count() == 1
                gantt = page.locator(".rv-gantt")
                assert gantt.evaluate("e => e.scrollWidth > e.clientWidth")
                assert gantt.get_attribute("tabindex") == "0"
            if name == "help":
                labels = page.locator('main [role="region"][tabindex="0"]').evaluate_all(
                    "els => els.map(e => e.getAttribute('aria-label'))"
                )
                assert all(labels) and len(labels) == len(set(labels)), labels
            if name == "projects":
                assert page.locator(".topactions").evaluate(
                    "e => !!(e.compareDocumentPosition(document.querySelector('.topsearch-form'))"
                    " & Node.DOCUMENT_POSITION_FOLLOWING)"
                )
            page.set_viewport_size({"width": 1366, "height": 900})
            if name == "help":
                _settle(page)
                page.locator(".docs-toc a").last.focus()
                assert page.locator(".docs-toc a").last.evaluate(
                    "e => { const r = e.getBoundingClientRect();"
                    " return r.top >= 0 && r.bottom <= innerHeight; }"
                )
            results[name] = rec
            if light:
                _set_theme(page, "light")
                _visit(page, base, path, theme="light")
                results[name + "@light"] = {"axe": _run_axe(page, axe_src)}
                _set_theme(page, "dark")
            if name in _LAYOUT_CASES:
                # Last: back to the default, so later pages see the page as seeded.
                reset = ["DELETE", None, _LAYOUT_CASES[name][0]]
                assert page.evaluate(_JS_LAYOUT_FETCH, reset) == 200
                print(f"  {name}: saved 3-across applied, Tab follows it, edit mode OK")
            if name == "editor":
                # Last, because it saves and approves a segment (the measurements above
                # must see the page as seeded).
                _visit(page, base, path)
                _editor_keyboard(page, axe_src)
                print(
                    "  editor: alternates popover keyboard, row state, approve bug, busy focus OK"
                )
        finally:
            page.close()
        axe_total = sum(results[name]["axe"].values())
        print(
            f"  {name}: axe {axe_total} node(s) in {len(results[name]['axe'])} rule(s), "
            f"overflow at 320 px {results[name]['overflow_px']} px"
            + (f", tabwalk {results[name]['tabwalk']}" if walk else "")
            + f" ({time.time() - t0:.1f}s)"
        )
    dog.on_page("page-errors")
    _page_errors(anon, auth, base, ids, axe_src)
    dog.on_page("session-announce")
    _session_announce_exposed(auth, base)
    print("  session warning: announcements exposed while the dialog is modal OK")
    anon.close()
    auth.close()
    return results


def _ax_region(cdp, selector: str) -> dict:
    """The accessibility node of `selector` in real Chromium (CDP), with its text."""
    doc = cdp.send("DOM.getDocument", {"depth": -1})
    nid = cdp.send("DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": selector})
    assert nid.get("nodeId"), f"{selector} is not on the page"
    tree = cdp.send(
        "Accessibility.getPartialAXTree", {"nodeId": nid["nodeId"], "fetchRelatives": False}
    )
    node = tree["nodes"][0]
    return {
        "ignored": bool(node.get("ignored")),
        "reasons": [r.get("name") for r in node.get("ignoredReasons", [])],
        "role": (node.get("role") or {}).get("value"),
        "live": {
            p["name"]: (p.get("value") or {}).get("value")
            for p in node.get("properties", [])
            if p["name"] in ("live", "atomic", "busy")
        },
    }


def _session_announce_exposed(ctx, base: str) -> None:
    """T11 gate round 1 MEDIUM 1 (4.1.3): announcements made while the session warning is
    open must reach the accessibility tree. The dialog is modal (showModal), so Chromium
    ignores everything outside it (activeModalDialog); the region announce() writes into
    must be inside it, exposed as a polite status, and carry the text. Driven through the
    real path: /auth/session reports 45 s left (warning open, the 60 s message spoken),
    then reports signed out (the sign-out message spoken in the open dialog)."""
    page = ctx.new_page()
    page.set_default_timeout(_ACTION_TIMEOUT_MS)
    state = {"body": '{"signed_in": true, "remaining_seconds": 45, "ending": "idle"}'}
    page.route(
        "**/auth/session",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=state["body"]
        ),
    )
    try:
        page.goto(base + "/projects")
        _settle(page)
        assert page.locator("#session-dialog").count() == 1, (
            "the session warning must be on the page"
        )
        cdp = ctx.new_cdp_session(page)
        cdp.send("DOM.enable")
        cdp.send("Accessibility.enable")
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_selector("#session-dialog[open]")
        assert page.evaluate("() => document.activeElement.id") == "session-stay", (
            "focus must land on Stay signed in"
        )
        region_js = (
            "() => { const r = ['session-dialog-announce', 'session-announce']"
            ".map(id => document.getElementById(id)).find(el => el && el.textContent.trim());"
            " return r ? {id: r.id, text: r.textContent.trim(),"
            " inDialog: !!r.closest('#session-dialog')} : null; }"
        )
        page.wait_for_function(region_js)
        spoken = page.evaluate(region_js)
        assert spoken["inDialog"], f"the 60 s message went to a region outside the dialog: {spoken}"
        assert "1 minute" in spoken["text"], spoken
        node = _ax_region(cdp, "#" + spoken["id"])
        assert (
            not node["ignored"]
            and node["role"] == "status"
            and node["live"].get("live") == "polite"
        ), f"the countdown announcer is not exposed while the dialog is modal: {node}"
        # Now the server says the session is gone: the open warning switches to signed out.
        state["body"] = '{"signed_in": false}'
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_selector("#session-sign-in:not([hidden])")
        page.wait_for_function(
            "() => { const r = ['session-dialog-announce', 'session-announce']"
            ".map(id => document.getElementById(id)).find(el => el && el.textContent.trim());"
            " return r && /signed out/i.test(r.textContent); }"
        )
        spoken = page.evaluate(region_js)
        assert spoken["inDialog"], f"the sign-out went to a region outside the dialog: {spoken}"
        assert spoken["text"] == "You have been signed out", spoken
        assert page.evaluate("() => document.getElementById('session-dialog').open"), (
            "the dialog must still be open (modal)"
        )
        node = _ax_region(cdp, "#" + spoken["id"])
        assert (
            not node["ignored"]
            and node["role"] == "status"
            and node["live"].get("live") == "polite"
        ), f"the sign-out announcer is ignored while the dialog is modal: {node}"
        # Announced exactly once: no other region carries it.
        count = page.evaluate(
            "() => ['session-dialog-announce', 'session-announce'].filter(id => {"
            " const el = document.getElementById(id);"
            " return el && /signed out/i.test(el.textContent); }).length"
        )
        assert count == 1, f"the sign-out is in {count} live regions"
        assert page.evaluate("() => document.activeElement.id") == "session-sign-in", (
            "focus must land on Sign in again"
        )
        # Security review 2026-09 (#260): the signed-out state cannot be dismissed, not even
        # by a repeated Escape (which a browser may refuse to let a page cancel), and the
        # page behind it is no longer readable.
        for _ in range(3):
            page.keyboard.press("Escape")
        page.wait_for_timeout(100)
        assert page.evaluate("() => document.getElementById('session-dialog').open"), (
            "#260: Escape closed the signed-out dialog"
        )
        assert page.evaluate("() => document.activeElement.id") == "session-sign-in"
        assert not page.locator("#main-content").is_visible(), (
            "#260: the page content is still readable after the session ended"
        )
        assert page.locator("#session-sign-in").is_visible()
    finally:
        page.unroute("**/auth/session")
        page.close()


def _page_errors(anon, auth, base: str, ids: dict, axe_src: str) -> None:
    """T6 (3.3.1 / 3.3.3): a page error is focused on load, marks its field, and the
    errored page is as axe-clean as the page itself. Not baselined: every assertion is
    absolute."""
    page = anon.new_page()
    page.set_default_timeout(_ACTION_TIMEOUT_MS)
    try:
        page.goto(base + "/auth/login")
        page.fill("#login-username", _ADMIN_EMAIL)
        page.fill(
            "#login-password", "not-the-a11y-password"
        )  # pregate: allow-secret wrong on purpose
        page.click("form[action='/auth/login'] button[type=submit]")
        page.wait_for_load_state("load")
        _settle(page)
        assert page.evaluate("() => document.activeElement.id") == "page-error", (
            "a failed sign-in must move focus to the error summary"
        )
        assert page.title().startswith("Error: "), page.title()
        assert page.input_value("#login-username") == _ADMIN_EMAIL, "username must be kept"
        assert page.input_value("#login-password") == "", "the password must not be echoed"
        assert page.get_attribute("#login-password", "aria-invalid") == "true"
        assert not _run_axe(page, axe_src), "the failed sign-in page must be axe-clean"
    finally:
        page.close()
    page = auth.new_page()
    page.set_default_timeout(_ACTION_TIMEOUT_MS)
    try:
        page.goto(base + f"/projects/{ids['doc_pid']}?err=format")
        _settle(page)
        # #564: the upload field is below the fold, so focus lands ON the field (on
        # screen, WCAG 2.2 SC 2.4.11) instead of a summary scrolled off the top; the
        # summary sentence joins the field's description so it is still read out.
        assert page.evaluate("() => document.activeElement.id") == "doc-upload-file"
        field = page.locator("#doc-upload-file")
        assert field.get_attribute("aria-invalid") == "true"
        assert "page-error-msg" in (field.get_attribute("aria-describedby") or "").split()
        assert field.evaluate(
            "e => { const r = e.getBoundingClientRect();"
            " return r.top >= 0 && r.bottom <= innerHeight; }"
        ), "the focused field must be on screen"
        page.locator("#page-error a[href='#doc-upload-file']").click()
        assert _is_focused(field), "Go to the field must focus the field"
        assert not _run_axe(page, axe_src), "an errored project page must be axe-clean"
        # "Go to the section" (an anchor, not a form control) must move focus too: the
        # handler cancels the native fragment jump, so a target that is not focusable on
        # its own gets tabindex="-1" and takes focus.
        page.goto(base + f"/glossaries/{ids['gid']}?err=candidate_not_pending")
        _settle(page)
        assert page.evaluate("() => document.activeElement.id") == "page-error"
        page.locator("#page-error a[href='#candidates']").click()
        assert page.evaluate("() => document.activeElement.id") == "candidates", (
            "Go to the section must move focus to the section"
        )
        # The same for a section anchor that is a bare element (the project lifecycle card).
        page.goto(base + f"/projects/{ids['doc_pid']}?err=status")
        _settle(page)
        page.locator("#page-error a[href='#project-lifecycle']").click()
        assert page.evaluate("() => document.activeElement.id") == "project-lifecycle", (
            "Go to the section must move focus to the lifecycle section"
        )
        # The focusability test itself: an <a> without href (tabIndex 0 in Chromium, yet
        # not focusable) must get tabindex="-1" too. form_errors.js is loaded again over
        # a planted summary, so the probe does not depend on any template's markup.
        page.goto(base + f"/projects/{ids['doc_pid']}")
        _settle(page)
        page.evaluate(
            """() => {
              document.getElementById('main-content').insertAdjacentHTML('afterbegin',
                '<div id="page-error" tabindex="-1"><p><a href="#probe-anchor">Go</a></p></div>'
                + '<a id="probe-anchor"></a>');
            }"""
        )
        page.add_script_tag(url=base + "/static/form_errors.js?probe=anchor")
        page.locator("#page-error a[href='#probe-anchor']").click()
        assert page.evaluate("() => document.activeElement.id") == "probe-anchor", (
            "an anchor target without href must still take focus"
        )
        # A section link whose target is not on the page is dropped, not left dead.
        page.goto(base + f"/projects/{ids['key_pid']}?err=export")
        _settle(page)
        assert page.locator("#download").count() == 0, "a key project has no download card"
        assert page.locator("#page-error").count() == 1
        assert page.locator("#page-error a").count() == 0, "dead section link must be dropped"
        # A modal the error belongs to keeps focus: the summary does not steal it. The error
        # comes from a real refused create: since the 2026-09 security review (#248) free
        # text in ?err= is shown only after the server redirected THIS session there.
        target = page.evaluate(
            """async () => {
              const body = new URLSearchParams({name: 'x', segmentation_profile_id: '999999'});
              const r = await fetch('/projects', {method: 'POST', body,
                                                  headers: {'X-CSRF-Token': window.CSRF_TOKEN}});
              const u = new URL(r.url); return u.pathname + u.search; }"""
        )
        assert target.startswith("/projects?err="), target
        page.goto(base + target + "#new")
        _settle(page)
        assert page.locator("#new-project-dialog[open]").count() == 1
        assert page.locator("#np-error").count() == 1, "the recorded error must render"
        assert page.evaluate("() => document.activeElement.id") != "page-error"
        # A client-side check: the upload size guard marks the field through LocA11y.
        page.goto(base + f"/projects/{ids['doc_pid']}")
        _settle(page)
        page.evaluate(
            "() => { const f = document.getElementById('doc-upload-file');"
            " const m = document.getElementById('doc-upload-size-error');"
            " window.LocA11y.fieldError(f, 'Too big.', m); }"
        )
        msg = page.locator("#doc-upload-size-error")
        assert msg.get_attribute("role") == "alert" and msg.is_visible()
        assert "doc-upload-size-error" in (field.get_attribute("aria-describedby") or "")
        assert field.get_attribute("aria-invalid") == "true"
        page.evaluate(
            "() => window.LocA11y.clearFieldError(document.getElementById('doc-upload-file'),"
            " document.getElementById('doc-upload-size-error'))"
        )
        assert field.get_attribute("aria-invalid") is None and not msg.is_visible()
    finally:
        page.close()
    print(
        "  page errors: focus on load, field marked, go-to field and section links,"
        " modal kept, LocA11y, axe OK"
    )


# --------------------------------------------------------------------------- #
# Baseline ratchet
# --------------------------------------------------------------------------- #
def compare(baseline: dict, results: dict) -> tuple[list[str], list[str]]:
    """Return (failures, tighten notes)."""
    fails, tighten = [], []
    for page in sorted(set(baseline) | set(results)):
        if page not in results:
            fails.append(f"{page}: in the baseline but no longer walked (remove it)")
            continue
        if page not in baseline:
            fails.append(f"{page}: walked but has no baseline entry (regenerate)")
            continue
        got, want = results[page], baseline[page]
        for rule in sorted(set(got.get("axe", {})) | set(want.get("axe", {}))):
            g, w = got.get("axe", {}).get(rule, 0), want.get("axe", {}).get(rule, 0)
            if g > w:
                what = "NEW rule" if w == 0 else "count rose"
                fails.append(f"{page}: axe {rule} {what}: {w} -> {g}")
            elif g < w:
                tighten.append(f"{page}: axe {rule} {w} -> {g}")
        if "overflow_px" in want or "overflow_px" in got:
            g, w = int(got.get("overflow_px", 0)), int(want.get("overflow_px", 0))
            if g > w + _OVERFLOW_TOLERANCE_PX:
                fails.append(f"{page}: sideways overflow at 320 px grew: {w} -> {g} px")
            elif g < w - _OVERFLOW_TOLERANCE_PX:
                tighten.append(f"{page}: overflow at 320 px {w} -> {g} px")
        if "tabwalk" in want or "tabwalk" in got:
            g, w = got.get("tabwalk", {}), want.get("tabwalk", {})
            if g.get("trap") and not w.get("trap"):
                fails.append(f"{page}: Tab walk now traps focus")
            elif w.get("trap") and not g.get("trap"):
                tighten.append(f"{page}: Tab walk trap gone")
            for k in ("no_indicator", "out_of_view"):
                gv, wv = g.get(k, 0), w.get(k, 0)
                if gv > wv:
                    fails.append(f"{page}: Tab walk {k} rose: {wv} -> {gv}")
                elif gv < wv:
                    tighten.append(f"{page}: Tab walk {k} {wv} -> {gv}")
    return fails, tighten


def _selftest_compare() -> None:
    tw = {"trap": False, "no_indicator": 1}
    base = {"p": {"axe": {"label": 2}, "overflow_px": 400, "tabwalk": tw}}
    same = {"p": {"axe": {"label": 2}, "overflow_px": 402, "tabwalk": dict(tw)}}
    worse = {
        "p": {
            "axe": {"label": 3, "region": 1},
            "overflow_px": 450,
            "tabwalk": {"trap": True, "no_indicator": 2},
        }
    }
    better = {"p": {"axe": {}, "overflow_px": 0, "tabwalk": {"trap": False, "no_indicator": 0}}}
    assert compare(base, same) == ([], []), compare(base, same)
    f, _t = compare(base, worse)
    assert len(f) == 5, f
    f, t = compare(base, better)
    assert not f and len(t) == 3, (f, t)
    f, _t = compare(base, {**same, "q": {"axe": {}, "overflow_px": 0}})
    assert f and "no baseline entry" in f[0], f


def _write_baseline(results: dict) -> None:
    body = json.dumps(results, indent=2, sort_keys=True) + "\n"
    with open(_BASELINE_PATH, "wb") as fh:
        fh.write(body.encode("utf-8"))  # LF on every platform


def _strip_diagnostics(results: dict) -> dict:
    """distinct_stops is printed for humans but not ratcheted (it moves with content)."""
    out = {}
    for page, rec in results.items():
        rec = dict(rec)
        if "tabwalk" in rec:
            rec["tabwalk"] = {k: v for k, v in rec["tabwalk"].items() if k != "distinct_stops"}
        out[page] = rec
    return out


def main() -> int:
    t_start = time.time()
    _selftest_compare()
    print("  selftest: baseline comparator flags rises, new rules, traps; notes falls")
    axe_src = _check_axe_bytes()
    print(f"  vendored axe-core 4.13.0: {AXE_SIZE} bytes, sha256 pinned OK")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return _finish_skip(
            f"playwright not installed ({exc}); pip install -r requirements-dev.txt; "
            "python -m playwright install chromium"
        )

    from tests._bootstrap import init_env

    data_dir = init_env("loctms_a11y_")
    # Validate the throwaway fixture even when Chromium is unavailable locally.
    ids = _seed()

    pw = sync_playwright().start()
    try:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
            return _finish_skip(
                f"chromium will not launch ({first[:160]}); python -m playwright install chromium"
            )
        print(f"  browser: chromium {browser.version} headless, reduced motion emulated")
        dog = _Watchdog(data_dir)
        dog.start()
        port = _free_port()
        server = _UvicornThread(ids["app"], port)
        server.start()
        dog.server = server
        base = f"http://127.0.0.1:{port}"
        try:
            results = _strip_diagnostics(_walk(browser, base, ids, axe_src, dog, data_dir))
        finally:
            dog.stop()
            server.stop()
            browser.close()
    finally:
        pw.stop()

    walked = time.time() - t_start
    if _WRITE:
        _write_baseline(results)
        print(
            f"  baseline WRITTEN: {os.path.relpath(_BASELINE_PATH, _ROOT)} ({len(results)} pages)"
        )
    if not os.path.isfile(_BASELINE_PATH):
        print("  no baseline file; run once with LOC_TMS_A11Y_BASELINE_WRITE=1")
        print("A11Y BROWSER FAILED")
        return 1
    with open(_BASELINE_PATH, "rb") as fh:
        baseline = json.loads(fh.read().decode("utf-8"))
    fails, tighten = compare(baseline, results)
    for note in tighten:
        print(f"  tighten the baseline: {note} (regenerate with LOC_TMS_A11Y_BASELINE_WRITE=1)")
    for f in fails:
        print(f"  REGRESSION: {f}")
    totals = {p: sum(r["axe"].values()) for p, r in results.items()}
    print(f"  axe node totals: {json.dumps(totals, sort_keys=True)}")
    print(f"  runtime: {walked:.1f}s for {len(results)} page checks")
    print("collected ids: " + " ".join(sorted(results)))
    if fails:
        print("A11Y BROWSER FAILED")
        return 1
    print("A11Y BROWSER OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
