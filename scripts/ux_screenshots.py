"""Offline Playwright contact sheet using shared disposable UX fixtures.

Documented in: DESIGN.md. No live URL or database is accepted.
"""

import argparse
import html
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._ux_smoke import fresh_fixture, render_pages


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def capture(output, selected=None, channel=None, palette=False):
    from playwright.sync_api import sync_playwright

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    records = []
    # ux/review-runs pages need the opt-in seed (paused run, batch, query thread).
    review_run_pages = {"run", "run-step-diff", "batch", "my-work", "queries", "query-thread"}
    wants_runs = bool(set(selected or ()) & review_run_pages)
    with fresh_fixture(
        databricks=bool(selected and "databricks" in selected),
        review_runs=wants_runs,
        connections=True,
        auth=True,
    ) as (client, pages, _):
        if selected:
            unknown = set(selected) - pages.keys()
            if unknown:
                raise ValueError(f"Unknown pages: {sorted(unknown)}; available: {list(pages)}")
            pages = {name: pages[name] for name in selected}
        rendered = render_pages(client, pages)
        # serve already POST-rendered offline previews.
        post_rendered = {
            spec[0]: rendered[name] for name, spec in pages.items() if isinstance(spec, tuple)
        }
        pages = {name: spec[0] if isinstance(spec, tuple) else spec for name, spec in pages.items()}
        if "components" in rendered:
            assets = Path(os.path.relpath(ROOT / "app/static", output)).as_posix() + "/"
            (output / "components.html").write_bytes(
                rendered["components"].replace("/static/", assets).encode()
            )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=channel)
            try:
                for theme in ("light", "dark"):
                    for width, height in ((1440, 900), (390, 844)):
                        context = browser.new_context(
                            viewport={"width": width, "height": height},
                            reduced_motion="reduce",
                            service_workers="block",
                        )
                        context.add_init_script(
                            "localStorage.setItem('loc_theme', " + json.dumps(theme) + ");"
                        )

                        def route(request_route):
                            request = request_route.request
                            url = urlsplit(request.url)
                            if url.netloc != "ux.local" or request.method != "GET":
                                request_route.abort()
                                return
                            # no GET application route is introduced.
                            if url.path in post_rendered:
                                request_route.fulfill(
                                    status=200,
                                    content_type="text/html",
                                    body=post_rendered[url.path],
                                )
                                return
                            # replay captured real POST responses.
                            if (
                                url.path == "/auth/session" and "/__ux-fixtures/auth/" in page.url  # noqa: B023
                            ):
                                # Recorded sessions no longer exist after seeding. Treat this as
                                # offline, as the focused browser fixture does; never sign out a
                                # captured page against the restored auth-off backend.
                                request_route.abort()
                                return
                            # Fixture pages and the POST comparison pages are rendered
                            # offline once above; they are not application GET routes.
                            # offline POST replay
                            fixture_name = next(
                                (
                                    key
                                    for key, value in pages.items()
                                    if url.path == value
                                    and (
                                        value.startswith("/__ux-fixtures/")
                                        or key in ("context-ab", "style-ab", "glossary-suggest")
                                    )
                                ),
                                None,
                            )
                            if fixture_name:
                                request_route.fulfill(
                                    status=200,
                                    content_type="text/html",
                                    body=rendered[fixture_name],
                                )
                                return
                            response = client.get(
                                url.path + ("?" + url.query if url.query else ""),
                                follow_redirects=False,
                            )
                            headers = {
                                k: v
                                for k, v in response.headers.items()
                                if k.lower()
                                not in (
                                    "content-encoding",
                                    "content-length",
                                    "transfer-encoding",
                                )
                            }
                            request_route.fulfill(
                                status=response.status_code,
                                headers=headers,
                                body=response.content,
                            )

                        context.route("**/*", route)
                        page = context.new_page()
                        for name, path in pages.items():
                            # Branch 05: fragment fixtures need a fresh navigation,
                            # otherwise goto can return None for the same document.
                            page.goto("about:blank")
                            response = page.goto("http://ux.local" + path, wait_until="networkidle")
                            require(
                                response.status == (404 if name == "error" else 200),
                                (name, response.status),
                            )
                            page.evaluate("document.fonts.ready")
                            require(
                                page.locator(
                                    # both existing shell variants.
                                    "main.authcard, main#main-content"
                                ).count()
                                == 1,
                                name,
                            )
                            require(
                                page.locator("html").get_attribute("data-theme") == theme, theme
                            )
                            # Exercise actual keyboard focus before capturing the resting page.
                            # A fragment can focus its panel, including a read-only
                            # panel with no next control. Start from the document
                            # so Tab consistently exercises the first focus target.
                            page.evaluate("""() => {
                              const body = document.body, old = body.getAttribute('tabindex');
                              body.tabIndex = -1; body.focus();
                              if (old === null) body.removeAttribute('tabindex');
                              else body.setAttribute('tabindex', old);
                            }""")
                            page.keyboard.press("Tab")
                            focus = page.evaluate("""() => {
                              const e = document.activeElement, s = getComputedStyle(e);
                              return {tag: e.tagName, width: s.outlineWidth,
                                      style: s.outlineStyle};
                            }""")
                            require(
                                focus["tag"] != "BODY" and focus["style"] != "none",
                                (name, focus),
                            )
                            page.evaluate("document.activeElement.blur()")
                            page.evaluate("window.scrollTo(0, 0)")
                            filename = f"{name}-{theme}-{width}.png"
                            # Long API reference pages exceed Chrome's full-page
                            # bitmap height at phone widths. Capture their viewport.
                            full_page = page.evaluate(
                                "document.documentElement.scrollHeight < 30000"
                            )
                            if palette:
                                page.locator("[data-command-palette-open]").click()
                                require(
                                    page.locator("#command-palette-input").evaluate(
                                        "(e)=>e===document.activeElement"
                                    ),
                                    name,
                                )
                                filename = "palette-" + filename
                            page.screenshot(
                                path=str(output / filename), full_page=full_page and not palette
                            )
                            overflow = page.evaluate(
                                "document.documentElement.scrollWidth - "
                                "document.documentElement.clientWidth"
                            )
                            records.append(
                                {
                                    "page": name,
                                    "path": path,
                                    "theme": theme,
                                    "width": width,
                                    "height": height,
                                    "image": filename,
                                    "full_page": full_page,
                                    "overflow_px": overflow,
                                    "focus": focus,
                                    "browser": browser.version,
                                }
                            )
                        context.close()
            finally:
                browser.close()
    (output / "manifest.json").write_bytes(json.dumps(records, indent=2).encode())
    cards = []
    for row in records:
        label = html.escape(f"{row['page']} / {row['theme']} / {row['width']}px")
        image = html.escape(row["image"], quote=True)
        cards.append(
            f'<figure><a href="{image}"><img loading="lazy" src="{image}" alt="{label}"></a>'
            f"<figcaption>{label}</figcaption></figure>"
        )
    (output / "index.html").write_bytes(
        (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width"><title>UX contact sheet</title>'
            "<style>body{font:14px system-ui;background:#eee;color:#17191e}"
            "main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}"
            "figure{margin:0;background:white;padding:12px}"
            "img{width:100%;height:260px;object-fit:contain;object-position:top}"
            "figcaption{padding:8px}</style>"
            "<h1>UX contact sheet</h1>"
            + (
                '<p><a href="components.html">Interactive component fixture</a></p>'
                if "components" in rendered
                else ""
            )
            + "<main>"
            + "".join(cards)
            + "</main></html>"
        ).encode()
    )
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pages", nargs="+", help="Fixture names; defaults to every shared page")
    parser.add_argument(
        "--channel",
        choices=("chrome", "msedge"),
        help="Use an installed browser instead of bundled Chromium",
    )
    parser.add_argument("--palette", action="store_true", help="Capture the open command palette")
    args = parser.parse_args()
    records = capture(args.output, args.pages, args.channel, args.palette)
    print(f"Captured {len(records)} screenshots. Contact sheet: {args.output / 'index.html'}")


if __name__ == "__main__":
    main()
