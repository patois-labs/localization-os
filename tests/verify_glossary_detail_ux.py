"""Issue #565: glossary detail page structure, the one visible next step for a draft glossary,
and additive pair editing (add / remove) over the unchanged replace-the-list /pairs route.

Offline, no browser. Anchors on element ids, never on visible text.
"""

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._ux_smoke import fresh_fixture


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        _fail(msg)


class Dom(HTMLParser):
    """Collects element ids, per-form hidden/select fields, and data-card order."""

    def __init__(self):
        super().__init__()
        self.ids = {}
        self.cards = []
        self.forms = []  # [{id, action, fields: [(name, value)], chip_text}]
        self._form = None
        self.chips = []  # pair chips: (text-less) forms inside li.glossary-pair-chip
        self._in_chip = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids[a["id"]] = tag
        if tag == "section" and a.get("data-card"):
            self.cards.append(a["data-card"])
        if tag == "li" and "glossary-pair-chip" in (a.get("class") or ""):
            self._in_chip = True
        if tag == "form":
            self._form = {
                "id": a.get("id"),
                "action": a.get("action"),
                "fields": [],
                "chip": self._in_chip,
            }
            self.forms.append(self._form)
        if self._form is not None and tag == "input" and a.get("type") == "hidden":
            self._form["fields"].append((a.get("name"), a.get("value", "")))

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        if tag == "li":
            self._in_chip = False


def parse(html):
    d = Dom()
    d.feed(html)
    return d


with fresh_fixture() as (client, pages, _data):
    from app.services import page_layouts

    def post(path, data):
        r = client.post(path, data=data, follow_redirects=False)
        check(r.status_code == 303, f"{path}: {r.status_code}")
        return r

    r = post("/glossaries", {"name": "Draft path glossary", "customer_id": "1"})
    gid = r.headers["location"].split("?")[0].rstrip("/").split("/")[-1]
    base = f"/glossaries/{gid}"

    def page():
        r = client.get(base)
        check(r.status_code == 200, f"GET {base}: {r.status_code}")
        return r.text, parse(r.text)

    # --- a brand-new glossary: draft, no pair, no terms ---------------------------------
    html, dom = page()
    check("glossary-next-step" in dom.ids, "a draft glossary shows the next-step panel")
    check('href="#glossary-pair-source"' in html, "step 1 links to the pair form")
    check("glossary-next-approve" not in dom.ids, "approve is not offered before pair and terms")
    check("glossary-add-pair" in dom.ids and "glossary-pair-source" in dom.ids, "add-pair form")
    check(
        "Save pairs" not in html and "replaces the current list" not in html, "old pairs form gone"
    )
    # one add-pair form: exactly two locale selects for pairs (not 2 * (n + 1))
    check(len(re.findall(r'id="glossary-pair-(?:source|target)"', html)) == 2, "two pair selects")

    # --- structure: terms first, DOM order == registered default rows --------------------
    spec = page_layouts.REGISTRY["glossary"]
    registered = [k for row in spec.default_rows for k in row]
    check(dom.cards == registered, f"DOM card order {dom.cards} != registry {registered}")
    check(dom.cards[0] == "terms", "terms come first")
    # A layout saved before #564 (old keys, no "danger") still wins and loses no card.
    old = [
        ["terms"],
        ["pairs", "status"],
        ["concepts"],
        ["extract"],
        ["import", "ai_builder"],
        ["rename"],
    ]
    resolved = page_layouts.resolve_rows("glossary", old)
    check(resolved[: len(old)] == old, f"saved order kept: {resolved}")
    check(
        sorted(k for row in resolved for k in row) == sorted(registered),
        f"a saved layout without 'danger' dropped or duplicated a card: {resolved}",
    )
    check(resolved[-1] == ["danger"], "the new danger card is appended after a saved layout")
    check(dom.cards[-1] == "danger" or "danger" not in dom.cards, "danger zone is last")
    check(dom.cards.index("terms") < dom.cards.index("pairs"), "terms come before pairs")
    check(dom.cards.index("terms") < dom.cards.index("status"), "terms come before status")
    for anchor in (
        "glossary-terms",
        "glossary-pairs",
        "glossary-status",
        "concepts",
        "candidates",
        "glossary-import",
        "glossary-ai-builder",
        "glossary-rename",
        "glossary-danger",
        "glossary-new-term",
    ):
        check(anchor in dom.ids, f"anchor id {anchor} missing")
    check(html.count('class="asset-sections"') == 1, "one section nav")

    # --- add pairs additively ------------------------------------------------------------
    def add_pair(src, tgt):
        _, d = page()
        form = next(f for f in d.forms if f["id"] == "glossary-add-pair")
        check(form["action"] == f"{base}/pairs", "add form posts to the existing /pairs route")
        fields = [(n, v) for n, v in form["fields"] if n != "csrf_token"]
        data = {
            "source_locale": [v for n, v in fields if n == "source_locale"] + [src],
            "target_locale": [v for n, v in fields if n == "target_locale"] + [tgt],
        }
        post(f"{base}/pairs", data)

    add_pair("en-US", "fr-FR")
    add_pair("en-US", "de-DE")
    html, dom = page()
    chips = [f for f in dom.forms if f["chip"]]
    check(len(chips) == 2, f"two pair chips with a remove form, got {len(chips)}")
    from sqlmodel import Session

    from app.db import engine
    from app.services import glossary_module as gm

    with Session(engine) as s:
        got = {(p["source_locale"], p["target_locale"]) for p in gm.pairs_for(s, int(gid))}
    check(got == {("en-US", "fr-FR"), ("en-US", "de-DE")}, f"pairs after two adds: {got}")
    check(
        'id="glossary-next-step"' in html and "glossary-next-approve" not in html,
        "still terms step",
    )

    # --- remove one pair: the chip form resubmits every OTHER pair ------------------------
    first = chips[0]
    check(first["action"] == f"{base}/pairs", "remove posts to /pairs")
    names = [n for n, _ in first["fields"] if n != "csrf_token"]
    check(
        names.count("source_locale") == 1 and names.count("target_locale") == 1,
        f"remove fields {names}",
    )
    fl = [(n, v) for n, v in first["fields"] if n != "csrf_token"]
    post(
        f"{base}/pairs",
        {
            "source_locale": [v for n, v in fl if n == "source_locale"],
            "target_locale": [v for n, v in fl if n == "target_locale"],
        },
    )
    with Session(engine) as s:
        left = {(p["source_locale"], p["target_locale"]) for p in gm.pairs_for(s, int(gid))}
    check(len(left) == 1 and left < got, f"one pair left after remove: {left}")

    # --- a stale add/remove form is refused, not silently applied (review) -----------------
    _, d_now = page()
    seen_now = next(
        v
        for n, v in next(f for f in d_now.forms if f["id"] == "glossary-add-pair")["fields"]
        if n == "pairs_seen"
    )
    check(
        all(
            any(n == "pairs_seen" and v == seen_now for n, v in f["fields"])
            for f in d_now.forms
            if f["chip"]
        ),
        "remove forms carry the same pairs_seen as the add form",
    )
    post(
        f"{base}/pairs", {"source_locale": ["en-US", "en-US"], "target_locale": ["es-ES", "it-IT"]}
    )  # another tab
    with Session(engine) as s:
        other = {(p["source_locale"], p["target_locale"]) for p in gm.pairs_for(s, int(gid))}
    post(
        f"{base}/pairs",
        {
            "source_locale": [v for n, v in fl if n == "source_locale"],
            "target_locale": [v for n, v in fl if n == "target_locale"],
            "pairs_seen": seen_now,
        },
    )  # the stale tab submits
    with Session(engine) as s:
        after_stale = {(p["source_locale"], p["target_locale"]) for p in gm.pairs_for(s, int(gid))}
    check(after_stale == other, f"stale submit changed the pairs: {other} -> {after_stale}")
    _, d_new = page()
    seen_new = next(
        v
        for n, v in next(f for f in d_new.forms if f["id"] == "glossary-add-pair")["fields"]
        if n == "pairs_seen"
    )
    check(seen_new != seen_now, "pairs_seen changes when the pairs change")

    # --- the replace-the-list API semantics are unchanged --------------------------------
    post(
        f"{base}/pairs", {"source_locale": ["en-US", "en-US"], "target_locale": ["es-ES", "it-IT"]}
    )
    with Session(engine) as s:
        got2 = {(p["source_locale"], p["target_locale"]) for p in gm.pairs_for(s, int(gid))}
    check(got2 == {("en-US", "es-ES"), ("en-US", "it-IT")}, f"replace semantics: {got2}")

    # --- removing the last pairs is possible (empty list) --------------------------------
    post(f"{base}/pairs", {})
    with Session(engine) as s:
        check(gm.pairs_for(s, int(gid)) == [], "empty list clears pairs")
    post(f"{base}/pairs", {"source_locale": "en-US", "target_locale": "fr-FR"})

    # --- terms step, then approve step ---------------------------------------------------
    post(f"{base}/terms/add", {"source_term": "health bar", "target_term": "barre de vie"})
    html, dom = page()
    check("glossary-next-approve" in dom.ids, "approve is the next step once pair and terms exist")
    approve = next(f for f in dom.forms if f["id"] == "glossary-next-approve")
    check(approve["action"] == f"{base}/approve", "approve form posts to the existing route")
    post(f"{base}/approve", {})
    html, dom = page()
    check("glossary-next-step" not in dom.ids, "an approved glossary shows no next-step panel")

    # --- title convention on the detail page ---------------------------------------------
    t = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
    check(t.startswith("Draft path glossary - Glossary - "), f"detail title: {t!r}")

    # --- a hostile item name is escaped in <title> (review) -------------------------------
    r2 = post(
        "/glossaries", {"name": "</title><script>alert(1)</script> " + "x" * 90, "customer_id": "1"}
    )
    gid2 = r2.headers["location"].split("?")[0].rstrip("/").split("/")[-1]
    raw = client.get(f"/glossaries/{gid2}").text
    title_html = re.search(r"<title>(.*?)</title>", raw, re.S).group(1)
    check(
        "<script" not in title_html and "&lt;/title&gt;" in title_html,
        f"title escaped: {title_html[:120]!r}",
    )
    check(len(title_html) < 200, f"title shortened: {len(title_html)} chars")
    check("alert(1)</script>" not in raw.split("</head>")[0], "no raw script in <head>")

print("glossary detail ux OK")
