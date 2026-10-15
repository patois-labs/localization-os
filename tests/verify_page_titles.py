"""Issue #566: one page-title convention, applied centrally.

Convention: "<Page> - <Brand>", and for a detail page "<Item> - <Section> - <Brand>".
Templates set only the page part in `{% block title %}`; base.html (and the standalone
pages) append the brand through `compose_title`. An "Error: " prefix may lead the title.

Checks:
  1. compose_title / page_title_text on a table of inputs (separators, brand suffix, blank).
  2. Source: every template extending base.html has a non-empty title block with no brand,
     no em/en dash, no middle dot, no pipe; every standalone `<title>` goes through
     compose_title.
  3. Rendered: every seeded page (the UX smoke fixture, 100+ pages incl. auth pages)
     has a <title> that matches the convention.
"""

import html as _html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEMPLATES = ROOT / "app" / "templates"
BAD_CHARS = ("\u2014", "\u2013", "\u00b7", "|", "&mdash;", "&ndash;", "&middot;")


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        _fail(msg)


# --- 2. source checks (no app import needed) ---------------------------------------------
BLOCK = re.compile(r"\{% block title %\}(.*?)\{% endblock %\}", re.S)
TITLE_TAG = re.compile(r"<title>(.*?)</title>", re.S)
bad = []
n_block = n_standalone = 0
for path in sorted(TEMPLATES.glob("*.html")):
    src = path.read_text(encoding="utf-8")
    if path.name == "base.html":
        check("compose_title(_raw_title)" in src, "base.html must build <title> via compose_title")
        continue
    if re.search(r"\{%-?\s*extends\s+[\"']base\.html[\"']", src):
        m = BLOCK.search(src)
        if not m or not m.group(1).strip():
            bad.append(f"{path.name}: extends base.html but sets no title block")
            continue
        n_block += 1
        text = m.group(1)
        if any(c in text for c in BAD_CHARS):
            bad.append(f"{path.name}: title block uses a non-hyphen separator: {text!r}")
        if "brand" in text or "Localization OS" in text:
            bad.append(f"{path.name}: title block repeats the brand (base adds it): {text!r}")
        if text.strip().startswith("-") or text.strip().endswith("-"):
            bad.append(f"{path.name}: title block has a dangling separator: {text!r}")
    else:
        m = TITLE_TAG.search(src)
        if m and not path.name.startswith("_"):
            n_standalone += 1
            if "compose_title(" not in m.group(1):
                bad.append(f"{path.name}: standalone <title> must use compose_title")
check(not bad, "title convention violations:\n  " + "\n  ".join(bad))
check(n_block >= 70, f"expected 70+ pages with a title block, found {n_block}")
check(n_standalone >= 8, f"expected 8+ standalone pages, found {n_standalone}")

# --- 3. rendered pages -------------------------------------------------------------------
from tests._ux_smoke import fresh_fixture, render_pages

with fresh_fixture(review_runs=True, connections=True, auth=True) as (client, pages, _data):
    from app.web import deps

    # --- 1. helper table ---------------------------------------------------------------------
    BRAND = str(deps.branding.BRAND)
    SHAPE = re.compile(r"^(Error: )?(?P<page>\S.*?) - " + re.escape(BRAND) + r"$")
    TABLE = [
        ("Dashboard", f"Dashboard - {BRAND}"),
        ("  Workflows  ", f"Workflows - {BRAND}"),
        (f"Workflows \u2014 {BRAND}", f"Workflows - {BRAND}"),
        (f"Workflows - {BRAND}", f"Workflows - {BRAND}"),
        (f"Step differences: run 4 | {BRAND}", f"Step differences: run 4 - {BRAND}"),
        ("Game terms \u2014 Glossary", f"Game terms - Glossary - {BRAND}"),
        ("Meta-evaluation &mdash; Set A", f"Meta-evaluation - Set A - {BRAND}"),
        ("Vendor \u00b7 Acme", f"Vendor - Acme - {BRAND}"),
        ("Help &amp; API", f"Help & API - {BRAND}"),
        ("", BRAND),
        ("   ", BRAND),
        (BRAND, BRAND),
    ]
    for raw, want in TABLE:
        got = deps.compose_title(raw)
        check(got == want, f"compose_title({raw!r}) = {got!r}, wanted {want!r}")
    long_item = "A" * 90
    got = deps.compose_title(f"{long_item} - Glossary")
    check(got == "A" * 59 + f"… - Glossary - {BRAND}", f"long item name is shortened: {got!r}")
    check(
        deps.compose_title("<b>x</b>") == "<b>x</b> - " + BRAND,
        "unescaped text passes through unchanged",
    )
    check(
        deps.page_title_text(f"Editor \u2014 a.docx \u2014 {BRAND}") == "Editor - a.docx",
        "page part",
    )

    rendered = render_pages(client, pages)
    check(len(rendered) >= 100, f"expected 100+ rendered pages, got {len(rendered)}")
    problems = []
    for name, page_html in rendered.items():
        if name == "components":
            continue  # tests/fixtures/ux_components.html: a test fixture, not an app page
        m = TITLE_TAG.search(page_html)
        if not m:
            problems.append(f"{name}: no <title>")
            continue
        title = _html.unescape(" ".join(m.group(1).split()))
        if not SHAPE.match(title):
            problems.append(f"{name}: {title!r} is not '<Page> - {BRAND}'")
        elif any(c in title for c in BAD_CHARS) or f" - {BRAND} - " in title:
            problems.append(f"{name}: {title!r} has a stray separator or doubled brand")
    check(not problems, "rendered title problems:\n  " + "\n  ".join(problems))
    glossary_title = _html.unescape(TITLE_TAG.search(rendered["glossary"]).group(1))
    check(
        re.fullmatch(r"\S.* - Glossary - " + re.escape(BRAND), glossary_title.strip()),
        f"glossary detail title: {glossary_title!r}",
    )
    print(f"{len(rendered)} rendered pages, {n_block} title blocks, {n_standalone} standalone")

print("page titles OK")
