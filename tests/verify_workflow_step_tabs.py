"""#594: the workflow builder and the drafted-workflow review show one tab per step.

Contract (anchored on ids and roles, never visible text):
  [T1] builder: a tablist with a Workflow tab, one tab per step (id wftab-step-<step id>,
       stable across a reorder) and an Add step tab; every tab controls a tabpanel that
       points back at it; ids are unique; each step's edit form sits inside its own panel.
  [T2] review: a tablist with a Workflow tab plus one tab per drafted step, all panels
       inside the ONE confirm form, field names s<i>_* unchanged.
  [T3] a POST built from the rendered review form creates a workflow identical to the same
       draft posted directly (hidden panels submit exactly as before).
  [T4] a POST built from the rendered builder edit form leaves the step unchanged.
  [T5] a server error that starts "Step N" marks tab N and opens it first.
  [T6] browser: arrow keys / Home / End move selection and focus, hidden panels still
       submit, an invalid field in a hidden panel switches to its tab and marks it, 390px
       has no horizontal page scroll, and with JavaScript off every panel is visible.
Reads app/static/wftabs.js, app/templates/workflow_builder.html and
app/templates/workflow_author_review.html so those files are owned by this suite.
"""

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright

from tests._ux_smoke import fresh_fixture, install_offline_route

JS = (ROOT / "app/static/wftabs.js").read_text(encoding="utf-8")
BUILDER = (ROOT / "app/templates/workflow_builder.html").read_text(encoding="utf-8")
REVIEW = (ROOT / "app/templates/workflow_author_review.html").read_text(encoding="utf-8")
assert "wftabs.js" in BUILDER and "wftabs.js" in REVIEW
assert 'role="tablist"' in BUILDER and 'role="tablist"' in REVIEW
assert "ArrowRight" in JS and "aria-selected" in JS


class Page(HTMLParser):
    """Collects elements (tag, attrs, parent chain of ids/forms) and form controls."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.els = []  # (tag, attrs dict, ancestors tuple of (tag, attrs))
        self.forms = []  # {"attrs":..., "controls": [...]}
        self._form = None
        self._select = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.els.append((tag, a, tuple(self.stack)))
        if tag not in ("input", "br", "meta", "link", "img", "hr", "source", "path", "rect"):
            self.stack.append((tag, a))
        if tag == "form":
            self._form = {"attrs": a, "controls": []}
            self.forms.append(self._form)
        elif self._form is not None:
            if tag == "input" and a.get("name"):
                self._form["controls"].append(("input", a, a.get("value", "")))
            elif tag == "select" and a.get("name"):
                self._select = [a, None, None]
                self._form["controls"].append(("select", a, self._select))
            elif tag == "option" and self._select is not None:
                if self._select[2] is None:
                    self._select[2] = a.get("value", "")  # first option = browser default
                if "selected" in a and "disabled" not in a:
                    self._select[1] = a.get("value", "")
            elif tag == "textarea" and a.get("name"):
                self._textarea = [a, ""]
                self._form["controls"].append(("textarea", a, self._textarea))

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def parse(html):
    p = Page()
    p.feed(html)
    return p


def form_data(form):
    """What a browser would submit for this form (no JS, defaults only)."""
    out = []
    for kind, a, extra in form["controls"]:
        if "disabled" in a:
            continue
        name = a["name"]
        if kind == "input":
            t = a.get("type", "text")
            if t in ("checkbox", "radio"):
                if "checked" in a:
                    out.append((name, a.get("value", "on")))
            elif t not in ("submit", "button", "file"):
                out.append((name, a.get("value", "")))
        elif kind == "select":
            v = extra[1] if extra[1] is not None else extra[2]
            if v is not None:
                out.append((name, v))
        else:
            out.append((name, extra[1].lstrip("\n")))
    merged = {}
    for k, v in out:
        merged.setdefault(k, []).append(v)
    return {k: (v if len(v) > 1 else v[0]) for k, v in merged.items()}


def tabs_and_panels(p):
    """The STEP tabs only: the page's top-level tabs (wfp-*, #630) have their own suite."""
    tabs = [(a, anc) for tag, a, anc in p.els if a.get("role") == "tab" and "wfp-" not in a["id"]]
    panels = {
        a["id"]: (a, anc)
        for tag, a, anc in p.els
        if a.get("role") == "tabpanel" and not a["id"].startswith("wfp-")
    }
    lists = [
        a
        for tag, a, anc in p.els
        if a.get("role") == "tablist" and a.get("aria-label") != "Workflow sections"
    ]
    return lists, tabs, panels


def check_tab_wiring(p, expect_tabs):
    lists, tabs, panels = tabs_and_panels(p)
    assert len(lists) == 1 and lists[0].get("aria-label"), "one labelled tablist"
    assert len(tabs) == expect_tabs, (len(tabs), expect_tabs)
    ids = [a["id"] for _, a, _ in p.els if a.get("id")]
    assert len(ids) == len(set(ids)), "duplicate ids"
    selected = [a for a, _ in tabs if a.get("aria-selected") == "true"]
    assert len(selected) == 1, "exactly one selected tab"
    for a, _ in tabs:
        assert a["id"].startswith("wftab-"), a["id"]
        panel = panels[a["aria-controls"]]
        assert panel[0]["aria-labelledby"] == a["id"], (a["id"], "panel must point back")
        assert a.get("tabindex") == ("0" if a.get("aria-selected") == "true" else "-1")
    assert len(panels) == expect_tabs, (len(panels), expect_tabs)
    return tabs, panels


def completed_runs():
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import RunStatus, WorkflowRun

    with Session(engine) as session:
        for run in session.exec(select(WorkflowRun)).all():
            run.status = RunStatus.COMPLETED
            session.add(run)
        session.commit()


def step_rows(workflow_id):
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import WorkflowStep

    with Session(engine) as session:
        rows = session.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == workflow_id)
            .order_by(WorkflowStep.order_index)
        ).all()
        skip = {"id", "workflow_id", "created_at", "updated_at", "adapted_from_id"}
        return [{k: v for k, v in r.model_dump().items() if k not in skip} for r in rows]


DRAFT = {
    "description": "Translate then check.",
    "step_count": "2",
    "s0_name": "Translate",
    "s0_step_type": "llm_agent",
    "s0_prompt_text": "Translate {text} into {target_locale}.",
    "s0_tm_examples": "1",
    "s0_tm_examples_window": "3",
    "s1_name": "Check",
    "s1_step_type": "lqa",
    "s1_lqa_threshold": "90",
}

with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
    # ---- [T1] builder markup -------------------------------------------------------
    html = client.get(pages["workflow"]).text
    p = parse(html)
    step_ids = re.findall(r"/steps/(\d+)/edit", html)
    assert len(step_ids) >= 2, step_ids
    tabs, panels = check_tab_wiring(p, len(step_ids) + 1)
    tab_ids = [a["id"] for a, _ in tabs]
    assert tab_ids[0] == "wftab-settings", tab_ids
    assert "wftab-add" not in tab_ids, "Add a step is a top-level page tab now (#630)"
    assert tab_ids[1:] == [f"wftab-step-{i}" for i in step_ids], "one tab per step, in order"
    for sid in step_ids:
        panel_id = f"wftabpanel-step-{sid}"
        edit = [f for f in p.forms if f["attrs"].get("action", "").endswith(f"/steps/{sid}/edit")]
        assert len(edit) == 1
        names = {a["name"] for _, a, _ in edit[0]["controls"]}
        assert {"csrf_token", "name", "step_type", "step_key", "run_condition_ref"} <= names, names
        # the edit form is a descendant of its own tabpanel (found via the ancestor chain)
        form_els = [
            anc
            for tag, a, anc in p.els
            if tag == "form" and a.get("action", "").endswith(f"/steps/{sid}/edit")
        ]
        assert any(x[1].get("id") == panel_id for x in form_els[0]), "edit form inside its panel"
    add_forms = [f for f in p.forms if f["attrs"].get("id") == "wf-add-step-form"]
    assert len(add_forms) == 1 and add_forms[0]["attrs"]["action"].endswith("/steps")
    add_anc = next(
        anc for tag, a, anc in p.els if tag == "form" and a.get("id") == "wf-add-step-form"
    )
    assert any(x[1].get("id") == "wfp-panel-add" for x in add_anc)
    assert 'id="wfStepList"' in html and "wf-steptabs" in html
    # reorder keeps the step ids (and therefore the tab ids) stable
    completed_runs()
    first = step_ids[0]
    mv = client.post(
        f"/workflows/1/steps/{first}/move",
        data={"position": "1"},
        follow_redirects=False,
    )
    assert mv.status_code == 303, mv.status_code
    again = re.findall(r"/steps/(\d+)/edit", client.get(pages["workflow"]).text)
    assert sorted(again) == sorted(step_ids) and again != step_ids, "ids stable, order changed"
    print("  [T1] builder: one tab per step, stable ids, forms inside panels OK")

    # ---- [T4] edit round trip from the rendered form ----------------------------------
    before = step_rows(1)
    html = client.get(pages["workflow"]).text
    p = parse(html)
    for f in [f for f in p.forms if f["attrs"].get("action", "").endswith("/edit")]:
        r = client.post(f["attrs"]["action"], data=form_data(f), follow_redirects=False)
        assert r.status_code == 303, (f["attrs"]["action"], r.status_code, r.text[:300])
    assert step_rows(1) == before, "re-saving the rendered step forms must not change a step"
    print("  [T4] builder: re-posting the rendered step forms changes nothing OK")

    # ---- [T2][T3][T5] review page ------------------------------------------------------
    bad = client.post("/workflows/author/confirm", data={**DRAFT, "name": ""})
    assert bad.status_code in (200, 400, 422), bad.status_code
    rp = parse(bad.text)
    tabs, panels = check_tab_wiring(rp, 3)
    assert [a["id"] for a, _ in tabs] == ["wftab-settings", "wftab-step-1", "wftab-step-2"]
    confirm = [f for f in rp.forms if f["attrs"].get("action") == "/workflows/author/confirm"]
    assert len(confirm) == 1
    names = {a["name"] for _, a, _ in confirm[0]["controls"]}
    for i in (0, 1):
        assert {f"s{i}_name", f"s{i}_step_type", f"s{i}_prompt_text"} <= names, names
    assert {"name", "customer_id", "step_count", "csrf_token"} <= names
    # every step field is inside the one form AND inside a tabpanel
    for _tag, a, anc in rp.els:
        n = a.get("name", "")
        if re.match(r"s\d+_", n):
            kinds = [x[0] for x in anc]
            assert "form" in kinds and any(x[1].get("role") == "tabpanel" for x in anc), n
    # [T3] the rendered form, completed with a name, equals the direct post
    data = form_data(confirm[0])
    data["name"] = "Tabbed review"
    r1 = client.post("/workflows/author/confirm", data=data, follow_redirects=False)
    assert r1.status_code == 303, (r1.status_code, re.findall(r"<li>(.*?)</li>", r1.text)[:6])
    direct = {**DRAFT, "name": "Direct review"}
    r2 = client.post("/workflows/author/confirm", data=direct, follow_redirects=False)
    assert r2.status_code == 303, (r2.status_code, r2.text[:400])
    wf1 = int(r1.headers["location"].rstrip("/").split("/")[-1].split("?")[0])
    wf2 = int(r2.headers["location"].rstrip("/").split("/")[-1].split("?")[0])
    assert step_rows(wf1) == step_rows(wf2) and len(step_rows(wf1)) == 2, "identical steps"
    print("  [T2][T3] review: one tab per draft step, rendered form posts identically OK")
    # [T5]
    err = client.post(
        "/workflows/author/confirm",
        data={**DRAFT, "name": "X", "s1_provider": "no-such-provider"},
    )
    ep = parse(err.text)
    _, etabs, _ = tabs_and_panels(ep)
    flagged = [a["id"] for a, _ in etabs if a.get("data-invalid")]
    assert flagged == ["wftab-step-2"], flagged
    root = next(a for _tag, a, _anc in ep.els if "data-wf-tabs" in a)
    assert root["data-initial"] == "wftab-step-2"
    assert [a["id"] for a, _ in etabs if a.get("aria-selected") == "true"] == ["wftab-step-2"]
    print("  [T5] review: a 'Step 2' error marks and opens tab 2 OK")

    # ---- [T6] browser -----------------------------------------------------------------
    review_html = bad.text
    browser = pw.chromium.launch(channel="chrome")
    try:
        ctx = browser.new_context(viewport={"width": 390, "height": 800})
        page = ctx.new_page()
        install_offline_route(ctx, client, documents={"/rev": review_html})
        page.goto("http://ux.local/rev")
        assert page.locator(".wf-steptabs.wf-tabbed").count() == 1
        sel = "[role=tab][aria-selected=true]"
        assert page.locator(sel).get_attribute("id") == "wftab-settings"
        assert page.locator("#wftabpanel-step-1").is_hidden()
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        page.locator("#wftab-settings").focus()
        page.keyboard.press("ArrowRight")
        assert page.locator(sel).get_attribute("id") == "wftab-step-1"
        assert page.evaluate("document.activeElement.id") == "wftab-step-1"
        assert page.locator("#wftabpanel-step-1").is_visible()
        page.keyboard.press("End")
        assert page.locator(sel).get_attribute("id") == "wftab-step-2"
        page.keyboard.press("ArrowRight")
        assert page.locator(sel).get_attribute("id") == "wftab-settings", "wraps"
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("Home")
        assert page.locator(sel).get_attribute("id") == "wftab-settings"
        # hidden panels still submit
        page.locator("#wftab-step-2").click()
        names = page.evaluate(
            "Array.from(new FormData("
            "document.querySelector('form[action=\"/workflows/author/confirm\"]')).keys())"
        )
        assert "s0_name" in names and "name" in names, "hidden panel fields must submit"
        # an invalid control in a hidden panel switches to its tab and marks it
        page.locator("#wftab-step-2").click()
        page.locator('form[action="/workflows/author/confirm"] button[type=submit]').first.click()
        assert page.locator(sel).get_attribute("id") == "wftab-settings"
        assert page.locator("#wftab-settings").get_attribute("data-invalid") == "true"
        # several invalid controls in different hidden panels: the tab of the FIRST invalid
        # control (document order) must stay open, because that is the one the browser
        # focuses; every offending tab is still marked.
        page.evaluate(
            "document.querySelector('[name=s0_tm_examples_window]').value = '99';"
            "document.querySelector('[name=s1_lqa_threshold]').value = '500';"
        )
        page.locator("#wftab-step-2").click()
        page.locator('form[action="/workflows/author/confirm"] button[type=submit]').first.click()
        assert page.locator(sel).get_attribute("id") == "wftab-settings", "first invalid wins"
        page.fill('form[action="/workflows/author/confirm"] input[name=name]', "Fixed name")
        page.locator("#wftab-step-2").click()
        page.locator('form[action="/workflows/author/confirm"] button[type=submit]').first.click()
        assert page.locator(sel).get_attribute("id") == "wftab-step-1", "first invalid wins"
        assert page.evaluate("document.activeElement.name") == "s0_tm_examples_window"
        marked = page.evaluate(
            "[...document.querySelectorAll('[role=tab][data-invalid]')].map(t => t.id)"
        )
        assert marked == ["wftab-step-1", "wftab-step-2"], marked  # Name was fixed above
        # fixing the field clears the mark on that tab only
        page.fill("[name=s0_tm_examples_window]", "3")
        assert page.locator("#wftab-step-1").get_attribute("data-invalid") is None
        assert page.locator("#wftab-step-2").get_attribute("data-invalid") == "true"
        ctx.close()
        # a malformed URL hash must not break the tab wiring
        ctx = browser.new_context(viewport={"width": 390, "height": 800})
        page = ctx.new_page()
        install_offline_route(ctx, client, documents={"/rev": review_html})
        page.goto("http://ux.local/rev#%E0%A4%A")
        assert page.locator("[role=tabpanel]:visible").count() == 1
        assert page.locator(sel).get_attribute("id") == "wftab-settings"
        ctx.close()
        # the builder has no data-initial, so its hash path really runs: a malformed hash on
        # load or on a later hashchange must leave exactly one panel showing, and the tabs
        # must still switch.
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        install_offline_route(ctx, client)
        page.goto("http://ux.local" + pages["workflow"] + "#%E0%A4%A")
        # the step tabs sit inside the page's Steps tab (#630): one page panel, one step panel
        assert page.locator(".wf-pagetabs > [role=tabpanel]:visible").count() == 1
        assert page.locator("[data-wf-tabs] [role=tabpanel]:visible").count() == 1
        assert page.locator("[data-wf-tabs] " + sel).get_attribute("id") == "wftab-settings"
        page.evaluate("location.hash = '%E0%A4%A'")
        page.wait_for_timeout(100)
        page.locator("#wfp-tab-add").click()
        assert page.locator("#wfp-panel-add").is_visible()
        assert page.locator("#wf-add-step-form").is_visible()
        assert page.locator(".wf-pagetabs > [role=tabpanel]:visible").count() == 1
        assert not errors, errors
        ctx.close()
        # step names are data: a hostile name is text in the tab, never markup
        evil = '<img src=x onerror="window.__xss=1">'
        xr = client.post(
            "/workflows/author/confirm",
            data={**DRAFT, "name": "", "s0_name": evil},
        )
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        install_offline_route(ctx, client, documents={"/rev": xr.text})
        page.goto("http://ux.local/rev")
        page.evaluate(
            "const n = document.querySelector('[name=s0_name]'); n.value = '<b id=pwn>x</b>';"
            "n.dispatchEvent(new Event('input', {bubbles: true}));"
        )
        assert page.locator("#pwn, img[src=x]").count() == 0, "tab label must be text"
        assert not page.evaluate("window.__xss")
        assert "<b id=pwn>x</b>" in page.locator("#wftab-step-1").inner_text()
        ctx.close()
        # no JS: every panel visible, tab bar hidden
        ctx = browser.new_context(viewport={"width": 390, "height": 800}, java_script_enabled=False)
        page = ctx.new_page()
        install_offline_route(ctx, client, documents={"/rev": review_html})
        page.goto("http://ux.local/rev")
        assert page.locator("[role=tabpanel]").evaluate_all(
            "els => els.every(e => e.offsetParent !== null)"
        )
        assert page.locator("[role=tablist]").is_hidden()
        ctx.close()
        # builder at 390 and desktop: no horizontal scroll, tab switch shows only its panel
        for width in (390, 1280):
            ctx = browser.new_context(viewport={"width": width, "height": 800})
            page = ctx.new_page()
            install_offline_route(ctx, client)
            page.goto("http://ux.local" + pages["workflow"])
            assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth"), (
                width
            )
            page.locator("#wfp-tab-add").click()
            assert page.locator("#wfp-panel-add").is_visible()
            assert page.locator("#wftabpanel-settings").is_hidden()
            visible = page.locator(".wf-pagetabs > [role=tabpanel]:visible").count()
            assert visible == 1, visible
            ctx.close()
        # prompt-authoring assistant + presets target the ADD STEP form's Prompt box, never
        # the first step's edit textarea (the old unscoped selector hit that one), and bring
        # the Add step tab forward.
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        install_offline_route(
            ctx,
            client,
            stubs={f"{pages['workflow']}/author.json": {"ok": True, "draft": "DRAFTED PROMPT"}},
        )
        page.goto("http://ux.local" + pages["workflow"])
        edit_boxes = page.evaluate(
            "[...document.querySelectorAll('textarea[name=prompt_text]')]"
            ".filter(t => !t.closest('#wf-add-step-form')).map(t => t.value)"
        )
        assert edit_boxes, "step edit forms carry their own prompt_text"
        page.locator("#wfp-tab-author").click()  # the assistant is its own page tab (#630)
        page.fill("#author-form textarea[name=description]", "a strict reviewer")
        page.locator("#author-form button[type=submit]").click()
        # Locator assertion, not wait_for_function("<js>"): the string form is eval'd by the
        # polling and the page CSP (no unsafe-eval) rejects it whenever the first check is false.
        # 30 s like wait_for_function's default (the draft is an LLM-stub round trip).
        expect(page.locator("#wf-add-step-form textarea[name=prompt_text]")).to_have_value(
            "DRAFTED PROMPT", timeout=30000
        )
        assert page.locator("#wfp-tab-add").get_attribute("aria-selected") == "true"
        assert page.locator("#wfp-panel-add").is_visible()
        assert page.evaluate("document.activeElement.name") == "prompt_text"
        assert (
            page.evaluate(
                "[...document.querySelectorAll('textarea[name=prompt_text]')]"
                ".filter(t => !t.closest('#wf-add-step-form')).map(t => t.value)"
            )
            == edit_boxes
        ), "no step edit textarea may be overwritten"
        page.locator("#wfp-tab-steps").click()
        page.locator("#wftab-settings").click()
        page.evaluate("document.querySelector('a.preset')?.click()")
        body = page.evaluate("document.querySelector('a.preset')?.dataset.body")
        if body is not None:
            assert (
                page.evaluate(
                    "document.querySelector('#wf-add-step-form textarea[name=prompt_text]').value"
                )
                == body
            )
        ctx.close()
        # the no-JS authoring fallback re-renders the builder with the draft in the Add step
        # panel; the tab bar must open that panel, not hide the draft behind Workflow.
        fb = client.post(f"{pages['workflow']}/author", data={"description": "a strict reviewer"})
        assert fb.status_code == 200, fb.status_code
        fbp = parse(fb.text)
        froot = next(a for _tag, a, _anc in fbp.els if "data-wf-pagetabs" in a)
        assert froot.get("data-initial") == "wfp-tab-add", froot
    finally:
        browser.close()
    print("  [T6] browser: keys, hidden-panel submit, invalid switch, 390px, no-JS OK")
print("VERIFY WORKFLOW STEP TABS OK")
