"""Documents/delivery rendered geometry and unchanged execution surfaces, offline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright

from tests._ux_smoke import fresh_fixture, render_pages

# Small query adapter over Chromium's DOM, with no additional parser dependency.


class Node:
    def __init__(self, locator):
        self.locator = locator

    def select(self, selector):
        return [Node(n) for n in self.locator.locator(selector).all()]

    def select_one(self, selector):
        found = self.select(selector)
        return found[0] if found else None

    def get(self, key):
        return self.locator.get_attribute(key)

    __getitem__ = get

    def text(self):
        return self.locator.text_content() or ""

    def has_attr(self, key):
        return self.get(key) is not None


def parse_html(html):
    dom_page.set_content(html)
    return Node(dom_page.locator("html"))


OWNED = (
    "document",
    "project-keys",
    "images",
    "image-detail",
    "jobs",
    "exports",
    "export-run",
    "context-ab",
    "style-ab",
)
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome")
    try:
        dom_page = browser.new_page(java_script_enabled=False)
        dom_page.route("**/*", lambda route: route.abort())
        with fresh_fixture() as (client, pages, _):
            rendered = render_pages(client, {n: pages[n] for n in OWNED})
            for name, html in rendered.items():
                dom = parse_html(html)
                main = dom.select_one("main#main-content")
                assert len(main.select("h1")) == 1, name
                assert main.select_one(".page-header h1"), name
                assert not main.select(".hero"), name
                for region in main.select(".data-table-region"):
                    assert region.get("tabindex") == "0" and region.get("aria-label"), name
                    assert all(th.get("scope") == "col" for th in region.select("th")), name
                ids = [node["id"] for node in main.select("[id]")]
                assert len(ids) == len(set(ids)), (name, ids)
            doc = parse_html(rendered["document"])
            assert doc.select_one('#translateBtn[data-action="translate"]')
            for suffix in ("run", "translate", "apply-tm"):
                form = doc.select_one(f'form[action$="/{suffix}"]')
                assert form and form["method"] == "post"
                assert form.select_one('[name="locale"]')["value"] == "fr-FR"
            for suffix in ("editor", "lqa", "export"):
                assert doc.select_one(f'a[href*="/{suffix}?locale=fr-FR"]')
            assert doc.select_one('#tgt-0[lang="fr-FR"]')
            assert {n["data-card"] for n in doc.select("[data-card]")} == {
                "context",
                "workflow",
                "chunks",
                "segments",
            }
            exports = parse_html(rendered["exports"])
            assert exports.select_one('[name="scope"][checked]')["value"] == "self"
            assert exports.select_one('[name="history"][checked]')["value"] == "current"
            for field in ("include_eval", "include_embeddings", "include_audit"):
                assert not exports.select_one(f'[name="{field}"]').has_attr("checked")
            run = parse_html(rendered["export-run"])
            for hook in ("status", "rows", "bytes", "download-wrap", "download"):
                assert run.select_one("#" + hook)
            image_dom = parse_html(rendered["image-detail"])
            assert image_dom.select_one('button[aria-label="Save + re-render region 1"]')
            for name in ("context-ab", "style-ab"):
                dom = parse_html(rendered[name])
                region = dom.select_one(
                    '.comparison-results [role="region"][aria-label="Per-segment"]'
                )
                assert region and region.get("tabindex") == "0", name
                assert region.select("table.data-table th[scope=col]"), name
                # No English-only pseudo labels.
                assert not dom.select(".comparison-results [data-label]"), name
            # No raw enum text (underscores) in any rendered badge on these pages.
            for name, html in rendered.items():
                for badge in parse_html(html).select(".badge"):
                    assert "_" not in badge.text(), (name, badge.text())
            # Document page: translate actions in their own labelled action bar.
            doc_dom = parse_html(rendered["document"])
            bar = doc_dom.select_one('.action-bar[aria-label="Translation actions"]')
            assert bar, "translation action bar"
            assert bar.select_one(".action-bar-primary #translateBtn")
            # This fixture document carries two languages, so "Translate all 2 languages" (#563)
            # joins background and 100% TM in the secondary group.
            assert len(bar.select(".action-bar-secondary form button")) == 3
            # Same order as before the move: live, background, then 100% TM, then all languages.
            assert [f["action"].rsplit("/", 1)[-1] for f in bar.select("form")] == [
                "translate",
                "apply-tm",
                "translate",
            ]
            assert bar.select('form input[name="locale"][value="all"]')
            assert not doc_dom.select('[data-card="segments"] #translateBtn')
            assert not bar.select("details, p"), "no prose or details in the action region"
            assert not doc_dom.select('[aria-label="Run a multi-agent workflow"]')
            # Exports row action name starts with the visible label.
            assert parse_html(rendered["exports"]).select_one('a[aria-label^="Open export #"]')
            # Jobs: no leftover overflow wrapper.
            assert "overflow-x:auto" not in rendered["jobs"].replace(" ", "")
            # Actual browser geometry, focus, and no-JS access at phone/reflow/zoom widths.
            css = "\n".join(
                (Path("app/static") / file).read_text(encoding="utf-8")
                for file in ("style.css", "app_shell.css")
            )
            for width in (320, 390, 720):  # 720 CSS px is the 1440px view at 200% zoom.
                page = browser.new_page(
                    viewport={"width": width, "height": 844}, java_script_enabled=False
                )
                page.route("**/*", lambda route: route.abort())
                for name, html in rendered.items():
                    # No-JS documents load CSS as markup, not through an injected
                    # style onload promise (which cannot settle with scripts off).
                    page.set_content(html.replace("</head>", "<style>" + css + "</style></head>"))
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                        name,
                        width,
                    )
                    for region in page.locator(".data-table-region:visible").all():
                        region.focus()
                        assert region.evaluate("(e) => document.activeElement === e")
                        page.keyboard.press("Tab")
                        page.keyboard.press("Shift+Tab")
                    assert page.locator("main h1").count() == 1
                page.close()
            # jobs.js updates the real document watcher without replacing the focused button.
            watch_html = client.get(pages["document"] + "&job=1").text
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(watch_html)
            page.evaluate(
                """window.fetch = async () => ({ok: true, status: 200, """
                """json: async () => ({
                status:'completed', total:2, done:2, result:{}, message:'Fixture completed'
            })})"""
            )
            page.add_script_tag(content=Path("app/static/jobs.js").read_text(encoding="utf-8"))
            page.locator("#translateBtn").focus()
            page.evaluate("watchJob(1, document.querySelector('#jobWatch'), {reloadOnDone:false})")
            page.wait_for_function(
                "document.querySelector('#jobWatch .prog-bar').style.width === '100%'"
            )
            assert "Fixture completed" in page.locator("#jobWatch .job-label").inner_text()
            assert page.locator("#translateBtn").evaluate("(e) => document.activeElement === e")
            page.close()
            # The one-shot key-run cookie uses the same seeded internal document as production.
            key_pid = pages["project-keys"].split("/")[2]
            client.cookies.set("loc_keys_runs", key_pid + "|1")
            key_html = client.get(pages["project-keys"]).text
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(key_html)
            assert page.locator('[data-keysrun] [data-run-id="1"]').count() == 1
            page.evaluate("""() => { window.connectSSE = (url, options) => {
        window.runMessage = options.onMessage; return {close() {}};
    }; }""")
            page.add_script_tag(content=Path("app/static/keysrun.js").read_text(encoding="utf-8"))
            run_link = page.locator('[data-run-id="1"] a')
            run_link.focus()
            page.evaluate("runMessage({data:JSON.stringify({type:'run_started',steps:3})})")
            page.evaluate("runMessage({data:JSON.stringify({type:'step',step_index:0})})")
            assert page.locator("[data-run-steps]").inner_text() == "1 of 3 steps"
            page.evaluate(
                "runMessage({data:JSON.stringify({type:'closed',status:'failed',"
                "error:'Fixture failure <b>text</b>'})})"
            )
            assert page.locator("[data-run-error]").inner_text() == "Fixture failure <b>text</b>"
            assert page.locator("[data-run-error] b").count() == 0
            assert run_link.evaluate("(e) => document.activeElement === e")
            assert page.locator("[data-run-foot] a").inner_text() == "Reload the grid"
            page.close()
            for path in (
                pages["jobs"] + "?kind=missing-fixture",
                pages["project-keys"] + "?q=missing-fixture",
            ):
                response = client.get(path)
                assert response.status_code == 200
                assert parse_html(response.text).select_one("main .empty-state")
            response = client.get("/images?customer=1&err=badimage")
            assert response.status_code == 200 and "Not a usable image" in response.text
            # Long user-controlled titles must leave room for the existing edit button.
            from sqlmodel import Session

            from app.db import engine
            from app.models import Document

            with Session(engine) as session:
                document = session.get(
                    Document, int(pages["document"].split("?")[0].split("/")[-1])
                )
                document.filename = "Atlas product launch documentation " * 8 + ".txt"
                session.add(document)
                session.commit()
            long_html = client.get(pages["document"]).text
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.route("**/*", lambda route: route.abort())
            page.set_content(long_html.replace("</head>", "<style>" + css + "</style></head>"))
            page.locator("[data-layout-edit]").evaluate("(e) => e.hidden = false")
            title_box = page.locator("main h1").bounding_box()
            edit_box = page.locator("[data-layout-edit]").bounding_box()
            assert title_box["x"] + title_box["width"] <= edit_box["x"]
            page.set_viewport_size({"width": 320, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.close()
    finally:
        browser.close()
print("UX DOCUMENTS OK")
