"""#630: the workflow page has real top-level tabs, and its prompts are locked until unlocked.

Reads app/static/wfpagetabs.js, app/static/promptlock.js, app/static/wfprompts.css,
app/templates/workflow_builder.html, app/templates/_prompt_lock.html and
app/templates/_workflow_system_prompts.html so those files are owned by this suite. The
server half (permissions, audit, parity) is verify_workflow_prompt_lock_api.

Contract (anchored on ids, roles and data- attributes, never visible text):
  [L1] markup: one labelled top-level tablist (Steps, System prompt, Add a step, Prompt-authoring
       assistant), every tab controls a tabpanel that points back, roving tabindex, one selected;
       the System prompt is its own tab and never one of the step tabs; the old scroll-only
       section nav is gone; no inline on* handlers.
  [L2] switching: a click shows only that panel (the others are hidden, nothing scrolls), the
       choice is written to the URL hash, a reload or a link with the hash keeps it, a hash
       that points inside a panel opens its tab, arrow keys / Home / End move selection.
  [L3] locked by default: every prompt field is readonly; Unlock is per section, shows Apply and
       announces the state; one unlocked section leaves the others locked.
  [L4] unapplied edits: leaving by an in-app link, a tab switch or a step-tab switch asks
       "You will lose your changes" in the page (window.LocAsk, never a native dialog); Stay
       keeps the edit, Leave discards it and re-locks; a reload / close shows the browser's
       beforeunload prompt only while an edit is unapplied; Apply sends the edit without it.
  [L5] re-lock: after 30 minutes the section locks again and the edit is discarded.
  [L6] Restore default prompt asks in the page, never with the native confirm.
  [L7] no JS: every panel shows, the tab bar is hidden, the fields stay locked; 390px has no
       horizontal scroll; es / fr / de render translated labels.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright

from tests._ux_smoke import fresh_fixture, install_offline_route

PAGETABS = (ROOT / "app/static/wfpagetabs.js").read_text(encoding="utf-8")
LOCK = (ROOT / "app/static/promptlock.js").read_text(encoding="utf-8")
BUILDER = (ROOT / "app/templates/workflow_builder.html").read_text(encoding="utf-8")
for name in (
    "wfprompts.css",
    "../templates/_prompt_lock.html",
    "../templates/_workflow_system_prompts.html",
):
    assert (ROOT / "app/static" / name).exists(), name
assert "wfpagetabs.js" in BUILDER and "promptlock.js" in BUILDER
assert (
    "ArrowRight" in PAGETABS and "aria-selected" in PAGETABS and "history.replaceState" in PAGETABS
)
assert "beforeunload" in LOCK and "LocAsk" in LOCK and "confirm(" not in LOCK.replace("LocAsk", "")

AGENT_STEP = (
    "[...document.querySelectorAll('[id^=wftabpanel-step-]')]"
    ".find(p => p.querySelector('select[name=step_type]')?.value === 'llm_agent')"
    "?.id.replace('wftabpanel-step-', '')"
)
TABS = ["wfp-tab-steps", "wfp-tab-system", "wfp-tab-add", "wfp-tab-author"]
HASH = {
    "wfp-tab-steps": "wf-steps",
    "wfp-tab-system": "system-prompt",
    "wfp-tab-add": "wf-add-step",
    "wfp-tab-author": "wf-author",
}

with fresh_fixture() as (client, pages, _), sync_playwright() as pw:
    from app.services import settings

    settings.set_many({"system_prompt_overrides_enabled": "1"})
    settings.invalidate_cache()
    WF = pages["workflow"]
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import RunStatus, WorkflowRun

    with Session(engine) as session:  # the shared seed leaves a run in flight: finish it
        for run in session.exec(select(WorkflowRun)).all():
            run.status = RunStatus.COMPLETED
            session.add(run)
        session.commit()
    # the seeded workflow only has review steps (their prompt field is hidden): add an AI step
    added = client.post(
        WF + "/steps",
        data={"step_type": "llm_agent", "name": "Agent", "prompt_text": "Translate: {text}"},
        follow_redirects=False,
    )
    assert added.status_code == 303, (added.status_code, added.text[:300])
    html = client.get(WF).text

    # ---- [L1] markup ----------------------------------------------------------------------
    assert "wf-section-nav" not in html, "the scroll-only section nav is replaced by real tabs"
    tablists = re.findall(r'<div[^>]*role="tablist"[^>]*>', html)
    assert len(tablists) == 2, "page tabs + the step tabs inside Steps"
    assert 'aria-label="Workflow sections"' in tablists[0]
    ids = re.findall(r'<button[^>]*role="tab"[^>]*id="(wfp-tab-[a-z]+)"', html)
    assert ids == TABS, ids
    for tab in TABS:
        panel = tab.replace("-tab-", "-panel-")
        assert f'id="{panel}"' in html and f'aria-controls="{panel}"' in html
        assert re.search(rf'id="{panel}"[^>]*aria-labelledby="{tab}"', html), tab
    assert html.count('role="tab" id="wfp-tab-') == 4
    assert len(re.findall(r'role="tab"[^>]*aria-selected="true"[^>]*tabindex="0"', html)) >= 1
    # the System prompt is its own tab, never a step tab
    inner = html[html.index('aria-label="Workflow settings and steps"') :]
    inner = inner[: inner.index("</div>")]
    assert "system" not in inner.lower() and "wftab-add" not in inner
    assert not re.search(r"<[a-z][^>]*\son[a-z]+=", html), "no inline on* handlers (CSP)"
    print("  [L1] markup: four labelled top-level tabs, System prompt separate from step tabs OK")

    browser = pw.chromium.launch(channel="chrome")
    try:
        # ---- [L2] switching, hash, keys ---------------------------------------------------
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        dialogs = []  # every browser-native dialog: only beforeunload is ever expected
        page.on("dialog", lambda d: (dialogs.append(d.type), d.accept()))
        install_offline_route(ctx, client)
        page.goto("http://ux.local" + WF)
        top = ".wf-pagetabs > [role=tablist] > [role=tab]"
        assert page.locator(".wf-pagetabs.wf-tabbed").count() == 1
        assert page.locator(top).count() == 4
        assert page.locator("#wfp-tab-steps").get_attribute("aria-selected") == "true"
        assert page.locator("#wfp-panel-steps").is_visible()
        for other in ("system", "add", "author"):
            assert page.locator(f"#wfp-panel-{other}").is_hidden(), other
        # the heads are NOT in the Steps tab, and the step tabs are not in the System tab
        assert page.locator("#wfp-panel-steps #system-prompt").count() == 0
        assert page.locator("#wfp-panel-system #system-prompt").count() == 1
        page.locator("#wfp-tab-system").click()
        assert page.locator("#wfp-panel-system").is_visible()
        assert page.locator("#wfp-panel-steps").is_hidden()
        assert page.evaluate("location.hash") == "#system-prompt"
        assert page.locator("#wfp-tab-system").get_attribute("aria-selected") == "true"
        assert page.locator("#wfp-tab-system").get_attribute("tabindex") == "0"
        assert page.locator("#wfp-tab-steps").get_attribute("tabindex") == "-1"
        # a reload and a link with the hash keep the tab
        page.reload()
        assert page.locator("#wfp-panel-system").is_visible()
        assert page.locator("#wfp-tab-system").get_attribute("aria-selected") == "true"
        for tab in TABS:
            page.goto("http://ux.local" + WF + "#" + HASH[tab])
            expect(page.locator(f"#{tab}")).to_have_attribute("aria-selected", "true")
            panel = tab.replace("-tab-", "-panel-")
            assert page.locator(f"#{panel}").is_visible(), tab
            assert page.locator(".wf-pagetabs > [role=tabpanel]:visible").count() == 1, tab
        # a hash INSIDE a panel opens the panel that holds it
        page.goto("http://ux.local" + WF + "#wsp-mt_head")
        expect(page.locator("#wfp-tab-system")).to_have_attribute("aria-selected", "true")
        assert page.locator("#wsp-mt_head").is_visible()
        page.goto("http://ux.local" + WF + "#%E0%A4%A")  # malformed escape: ignored
        page.reload()
        expect(page.locator("#wfp-tab-steps")).to_have_attribute("aria-selected", "true")
        # arrow keys / Home / End
        page.locator("#wfp-tab-steps").focus()
        page.keyboard.press("ArrowRight")
        assert page.evaluate("document.activeElement.id") == "wfp-tab-system"
        assert page.locator("#wfp-panel-system").is_visible()
        page.keyboard.press("End")
        assert page.evaluate("document.activeElement.id") == "wfp-tab-author"
        page.keyboard.press("ArrowRight")
        assert page.evaluate("document.activeElement.id") == "wfp-tab-steps", "wraps"
        page.keyboard.press("ArrowLeft")
        assert page.evaluate("document.activeElement.id") == "wfp-tab-author"
        page.keyboard.press("Home")
        assert page.evaluate("document.activeElement.id") == "wfp-tab-steps"
        # step tabs inside Steps keep working beside the page tabs
        page.locator("#wftab-settings").click()
        assert page.locator("#wftabpanel-settings").is_visible()
        assert not errors, errors
        print("  [L2] switching: click, hash, reload, inner hash, arrow keys, roving tabindex OK")

        # ---- [L3] locked by default; Unlock per section ------------------------------------
        page.goto("http://ux.local" + WF + "#system-prompt")
        fields = page.locator("#wfp-panel-system [data-lock-field]")
        assert fields.count() == 2
        for i in range(2):
            assert fields.nth(i).get_attribute("readonly") is not None
            assert fields.nth(i).get_attribute("disabled") is None, "readonly, not disabled"
        assert page.locator("#wfp-panel-system [data-lock-apply]:visible").count() == 0
        status = page.locator("#plk-wsp-step_head")
        assert status.get_attribute("role") == "status"
        assert "Locked" in status.inner_text()
        page.locator("#wsp-lock-step_head [data-lock-unlock]").click()
        ta = page.locator("#wsp-body-step_head")
        assert ta.get_attribute("readonly") is None
        assert page.locator("#wsp-lock-step_head [data-lock-apply]").is_visible()
        assert page.locator("#wsp-lock-step_head [data-lock-unlock]").is_hidden()
        assert "Unlocked" in status.inner_text()
        assert page.evaluate("document.activeElement.id") == "wsp-body-step_head"
        # per section: the other head stays locked
        assert page.locator("#wsp-body-mt_head").get_attribute("readonly") is not None
        assert page.locator("#wsp-lock-mt_head [data-lock-apply]").is_hidden()
        # Apply with no change does not post: it locks again and says so
        page.locator("#wsp-lock-step_head [data-lock-apply]").click()
        assert ta.get_attribute("readonly") is not None
        assert "No changes" in status.inner_text()
        # every step's Inline prompt is locked too, with its own Unlock
        page.locator("#wfp-tab-steps").click()
        step_fields = page.locator("#wfp-panel-steps [data-lock-field]")
        n_steps = step_fields.count()
        assert n_steps >= 1
        for i in range(n_steps):
            assert step_fields.nth(i).get_attribute("readonly") is not None
        assert page.locator("#wfp-panel-steps [data-lock-unlock]").count() == n_steps
        print("  [L3] locked by default; Unlock/Apply per section; state announced OK")

        # ---- [L4] unapplied edits -----------------------------------------------------------
        page.goto("http://ux.local" + WF + "#system-prompt")
        page.locator("#wsp-lock-step_head [data-lock-unlock]").click()
        ta = page.locator("#wsp-body-step_head")
        original = ta.input_value()
        ta.fill(original + " Extra rule.")
        # an in-app link
        page.locator("a.back").click()
        dlg = page.locator("#locAskTitle")
        expect(dlg).to_have_text("You will lose your changes")
        assert page.locator("dialog[open] button.ghost").inner_text() == "Stay"
        page.locator("dialog[open] button.ghost").click()  # Stay
        assert page.url.endswith(WF + "#system-prompt") or WF in page.url
        assert ta.input_value() == original + " Extra rule.", "Stay keeps the edit"
        assert ta.get_attribute("readonly") is None
        # a page-tab switch
        page.locator("#wfp-tab-steps").click()
        expect(page.locator("#locAskTitle")).to_have_text("You will lose your changes")
        page.locator("dialog[open] button.ghost").click()
        page.wait_for_timeout(100)
        assert page.locator("#wfp-tab-system").get_attribute("aria-selected") == "true"
        # a HASH-driven switch (an in-page link or an edited address): the hash has already
        # changed when the dialog shows; Stay must put the previous hash back so the address
        # and the visible tab agree (a reload or a shared link then opens the same tab)
        assert page.evaluate("location.hash") == "#system-prompt"
        n_hist = page.evaluate("history.length")
        page.evaluate("location.hash = '#wf-add-step'")
        expect(page.locator("#locAskTitle")).to_have_text("You will lose your changes")
        page.locator("dialog[open] button.ghost").click()  # Stay
        for _ in range(30):  # the page CSP forbids wait_for_function's string eval: poll
            if page.evaluate("location.hash") == "#system-prompt":
                break
            page.wait_for_timeout(100)
        assert page.evaluate("location.hash") == "#system-prompt", "Stay restores the hash"
        assert page.locator("#wfp-tab-system").get_attribute("aria-selected") == "true"
        assert page.locator("#wfp-panel-add").is_hidden()
        assert ta.input_value() == original + " Extra rule.", "Stay keeps the edit"
        assert page.evaluate("history.length") == n_hist + 1, "restored in place, no new entry"
        # and Leave on a hash-driven switch follows the hash
        page.evaluate("location.hash = '#wf-add-step'")
        expect(page.locator("#locAskTitle")).to_have_text("You will lose your changes")
        page.locator("dialog[open] button:not(.ghost)").click()  # Leave and lose changes
        expect(page.locator("#wfp-tab-add")).to_have_attribute("aria-selected", "true")
        assert page.evaluate("location.hash") == "#wf-add-step"
        assert ta.input_value() == original, "Leave discards the edit"
        page.locator("#wfp-tab-system").click()
        page.locator("#wsp-lock-step_head [data-lock-unlock]").click()
        ta.fill(original + " Extra rule.")
        page.locator("#wfp-tab-steps").click()
        page.locator("dialog[open] button:not(.ghost)").click()  # Leave and lose changes
        expect(page.locator("#wfp-tab-steps")).to_have_attribute("aria-selected", "true")
        page.locator("#wfp-tab-system").click()
        expect(page.locator("#wfp-tab-system")).to_have_attribute("aria-selected", "true")
        assert ta.input_value() == original, "Leave discards the edit"
        assert ta.get_attribute("readonly") is not None, "and re-locks the section"
        # a step-tab switch with an edited step prompt
        page.locator("#wfp-tab-steps").click()
        step_tabs = page.locator("[data-wf-tabs] [role=tab]")
        agent = page.evaluate(AGENT_STEP)
        assert agent, "the fixture workflow has an AI step"
        page.locator("#wftab-step-" + agent).click()
        first_step_box = page.locator("#wftabpanel-step-" + agent)
        first_step_box.locator("details.step-edit > summary").click()
        first_step_box.locator("[data-lock-unlock]").click()
        sta = first_step_box.locator("[data-lock-field]")
        s_orig = sta.input_value()
        sta.fill(s_orig + " more")
        step_tabs.nth(0).click()
        expect(page.locator("#locAskTitle")).to_have_text("You will lose your changes")
        page.locator("dialog[open] button.ghost").click()
        assert sta.input_value() == s_orig + " more"
        page.locator("#wfp-tab-system").click()
        page.locator("dialog[open] button.ghost").click()
        assert page.locator("#wfp-tab-steps").get_attribute("aria-selected") == "true"
        # beforeunload: only while an edit is unapplied
        assert not dialogs, dialogs
        page.reload()
        assert dialogs == ["beforeunload"], ("an unapplied edit must warn on reload", dialogs)
        seen = dialogs
        seen.clear()
        page.goto("http://ux.local" + WF + "#system-prompt")
        page.locator("#wsp-body-step_head").click()
        page.reload()
        assert "beforeunload" not in seen, "nothing unapplied: no prompt"
        # refresh discards: the page comes back locked with the saved text
        assert page.locator("#wsp-body-step_head").input_value() == original
        assert page.locator("#wsp-body-step_head").get_attribute("readonly") is not None
        # Apply sends the edit and does not raise the unload prompt
        posted = []
        ctx.unroute("**/*")
        install_offline_route(
            ctx,
            client,
            stubs={f"{WF}/system-prompts/step_head/save": {"ok": True}},
        )
        page.on("request", lambda r: posted.append((r.method, r.url, r.post_data)))
        seen.clear()
        page.goto("http://ux.local" + WF + "#system-prompt")
        page.locator("#wsp-lock-step_head [data-lock-unlock]").click()
        page.locator("#wsp-body-step_head").fill("Be formal. Output only the result.")
        page.locator("#wsp-lock-step_head [data-lock-apply]").click()
        page.wait_for_timeout(300)
        saves = [p for p in posted if p[0] == "POST" and p[1].endswith("/step_head/save")]
        assert saves and "Be+formal" in (saves[0][2] or "").replace("%20", "+"), saves
        assert "beforeunload" not in seen, "Apply is not 'leaving with unapplied changes'"
        print("  [L4] lose-changes dialog (link, tab, step tab), beforeunload, Apply OK")
        ctx.close()

        # ---- [L5] 30 minutes re-lock --------------------------------------------------------
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        install_offline_route(ctx, client)
        page.clock.install()
        page.goto("http://ux.local" + WF + "#system-prompt")
        page.locator("#wsp-lock-mt_head [data-lock-unlock]").click()
        mt = page.locator("#wsp-body-mt_head")
        mt_orig = mt.input_value()
        mt.fill(mt_orig + " late edit")
        page.clock.fast_forward(29 * 60 * 1000)
        assert mt.get_attribute("readonly") is None, "still unlocked at 29 minutes"
        page.clock.fast_forward(2 * 60 * 1000)
        assert mt.get_attribute("readonly") is not None, "locked again after 30 minutes"
        assert mt.input_value() == mt_orig, "the unapplied edit was discarded"
        assert "30 minutes" in page.locator("#plk-wsp-mt_head").inner_text()
        print("  [L5] a section locks again and drops its edit after 30 minutes OK")
        ctx.close()

        # ---- [L6] Restore default prompt asks in the page -----------------------------------
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        natives = []
        page.on("dialog", lambda d: (natives.append(d.type), d.dismiss()))
        posted = []
        install_offline_route(ctx, client)
        page.on("request", lambda r: posted.append((r.method, r.url)))
        page.goto("http://ux.local" + WF + "#system-prompt")
        page.locator("#wsp-lock-step_head form.wfl-restore button").click()
        expect(page.locator("#locAskMsg")).to_contain_text("AI step: system message head")
        page.locator("dialog[open] button.ghost").click()  # Cancel
        page.wait_for_timeout(150)
        assert not [p for p in posted if p[0] == "POST"], "Cancel sends nothing"
        assert not natives, ("no native confirm", natives)
        # a step prompt: confirm clears the field and posts only the prompt restore
        page.goto("http://ux.local" + WF)
        sid = page.evaluate(AGENT_STEP)
        stub_path = f"{WF}/steps/{sid}/restore-prompt"
        ctx.unroute("**/*")
        install_offline_route(ctx, client, stubs={stub_path: {"ok": True}})
        page.goto("http://ux.local" + WF)
        box = page.locator(f"#wftabpanel-step-{sid}")
        page.locator(f"#wftab-step-{sid}").click()
        box.locator("details.step-edit > summary").click()
        posted.clear()
        # an unsaved edit elsewhere in the step form must not travel with a restore
        box.locator("details.step-edit input[name=name]").fill("UNSAVED-RENAME")
        post_bodies = []
        page.on(
            "request",
            lambda r: post_bodies.append(r.post_data or "") if r.method == "POST" else None,
        )
        box.locator("[data-lock-restore-default]").click()
        expect(page.locator("#locAskMsg")).to_contain_text("default prompt")
        page.locator("dialog[open] button:not(.ghost)").click()
        page.wait_for_timeout(300)
        edits = [
            p for p in posted if p[0] == "POST" and p[1].endswith(f"/steps/{sid}/restore-prompt")
        ]
        assert edits, posted
        assert not [p for p in posted if p[0] == "POST" and p[1].endswith(f"/steps/{sid}/edit")], (
            "restore must not post the step edit form"
        )
        assert not any("UNSAVED-RENAME" in b for b in post_bodies), post_bodies
        assert not natives, natives
        print("  [L6] Restore default prompt asks with the in-app dialog (head and step) OK")
        ctx.close()

        # ---- [L7] no JS, phone width, other languages ---------------------------------------
        ctx = browser.new_context(viewport={"width": 390, "height": 800}, java_script_enabled=False)
        page = ctx.new_page()
        install_offline_route(ctx, client)
        page.goto("http://ux.local" + WF)
        for panel in ("steps", "system", "add", "author"):
            assert page.locator(f"#wfp-panel-{panel}").is_visible(), panel
        assert page.locator(".wf-pagetabs > [role=tablist]").is_hidden()
        assert page.locator("#wsp-body-step_head").get_attribute("readonly") is not None
        ctx.close()
        ctx = browser.new_context(viewport={"width": 390, "height": 800})
        page = ctx.new_page()
        install_offline_route(ctx, client)
        page.goto("http://ux.local" + WF)
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        page.locator("#wfp-tab-system").click()
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        ctx.close()
    finally:
        browser.close()
    print("  [L7] no-JS stacked and locked, 390px no horizontal scroll OK")

    # translated labels (en/es/fr/de), server side
    want = {
        "es": ("Prompt del sistema", "Desbloquear"),
        "fr": ("Prompt système", "Déverrouiller"),
        "de": ("System-Prompt", "Entsperren"),
    }
    for loc, (tab, unlock) in want.items():
        client.post("/account/locale", data={"ui_locale": loc}, follow_redirects=False)
        page_html = client.get(WF).text
        assert tab in page_html and unlock in page_html, loc
        assert "prompt_lose_title" in page_html, loc  # the js catalog reaches the page
    client.post("/account/locale", data={"ui_locale": "en"}, follow_redirects=False)
    print("  [L7] es / fr / de render translated tab and button labels OK")
print("VERIFY WORKFLOW PROMPT LOCK OK")
