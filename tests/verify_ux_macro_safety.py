"""Issue #549: the shared UX macros write labels into attributes safely (tl() Markup in
non-English UIs, hostile quote-bearing set blocks). Offline, no browser."""

import re
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape())
env.globals["static_v"] = lambda name: "fixture"


class _Attrs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs), [name for name, _ in attrs]))


def parse(html):
    p = _Attrs()
    p.feed(html)
    return p.tags


def render(source, **ctx):
    return env.from_string("{% import '_macros.html' as m %}" + source).render(**ctx)


# What tl() returns for a non-English locale (app/web/deps.py).
TL_DE = Markup('<span lang="{}">{}</span>').format("de", "Konnektortypen")
HOSTILE = 'x" style=position:fixed;inset:0 y onfocus=alert(1) "'

# 1. each attribute-writing macro, with a tl() Markup label.
html = render(
    "{% call m.data_table(label, [{'label': 'A'}]) %}<tr><td>1</td></tr>{% endcall %}"
    "{{ m.action_bar(label, primary='Go') }}"
    "{% call(s) m.tabs('t', label, [{'id': 'one', 'label': 'One'}]) %}x{% endcall %}"
    "{{ m.step_indicator(label, [{'label': 'S', 'status': 'completed', 'status_label': label}]) }}",
    label=TL_DE,
)
by_class = {a.get("class", ""): a for _, a, _ in parse(html) if a.get("aria-label")}
labelled = [a for _, a, _ in parse(html) if "aria-label" in a]
assert len(labelled) == 4, labelled
for attrs in labelled:
    assert attrs["aria-label"] == "Konnektortypen", attrs
region = next(a for t, a, _ in parse(html) if a.get("role") == "region")
assert region.get("tabindex") == "0", "the table region must stay keyboard focusable"
assert "<span" not in html.split("aria-label=")[1].split(">")[0], "markup leaked into an attribute"
badge = next(a for t, a, _ in parse(html) if "badge" in a.get("class", ""))
assert badge["title"] == "Konnektortypen", badge

# 2. a quote-bearing set block (the eval promote-confirm title) and a plain hostile string.
for src in (
    '{% set t %}Promote "{{ name }}" into production?{% endset %}',
    "{% set t = name %}",
):
    html = render(
        src + "{% call m.data_table(t, [{'label': 'A'}]) %}<tr><td>1</td></tr>{% endcall %}"
        "{{ m.action_bar(t) }}{{ m.step_indicator(t, []) }}",
        name=HOSTILE,
    )
    for tag, _attrs, names in parse(html):
        assert "style" not in names and "onfocus" not in names, (tag, names)
        assert "y" not in names, (tag, names)
    region = next(a for t, a, _ in parse(html) if a.get("role") == "region")
    assert region.get("tabindex") == "0"
    assert "position:fixed" in region["aria-label"], "the label text must survive as text"

# A tl() Markup status label cannot break out of the title attribute.
tl_badge = parse(render("{{ m.status_badge('failed', label) }}", label=TL_DE))[0][1]
assert tl_badge["title"] == "Konnektortypen", tl_badge

# Plain (non-Markup) text is not HTML: angle brackets, entities and non-English text keep
# their characters in the attribute (no tag stripping, no double unescaping of "&amp;").
for plain in ("5 < 6 and 7 > 3", "R&amp;D", "Tom & Jerry", "Prüfen 日本語"):
    attrs = next(a for t, a, _ in parse(render("{{ m.action_bar(label) }}", label=plain)))
    assert attrs["aria-label"] == plain, (plain, attrs)
    attrs = parse(render("{{ m.status_badge('x', label) }}", label=plain))[0][1]
    assert attrs["title"] == plain, (plain, attrs)

# Markup keeps its entities as text and loses only the tags.
mk = Markup('<span lang="de">Prüfen &amp; "ok" &lt;3</span>')
attrs = next(a for t, a, _ in parse(render("{{ m.action_bar(label) }}", label=mk)))
assert attrs["aria-label"] == 'Prüfen & "ok" <3', attrs

# 2b. Every label KIND through every label-bearing macro (main-red 2026-10-06: an undefined
# label, e.g. a dict row with no such key, made `label.__html__` raise and 500ed Prompt Tap).
# Each kind renders, cannot break out of the attribute, is escaped exactly once, and the
# attribute text equals what `{{ label }}` shows as visible text (Markup: tags stripped).
KINDS = {
    "markup": (TL_DE, "Konnektortypen"),
    "markup_hostile": (Markup("<b>{}</b>").format(HOSTILE), HOSTILE),
    "plain": (HOSTILE, HOSTILE),
    "entity_text": ("R&amp;D <x>", "R&amp;D <x>"),
    "none": (None, "None"),
    "int": (42, "42"),
    "float": (1.5, "1.5"),
    "zero": (0, "0"),
}
CALLS = {
    "action_bar": "{{ m.action_bar(L) }}",
    "data_table": "{% call m.data_table(L, [{'label': 'A'}]) %}<tr><td>1</td></tr>{% endcall %}",
    "tabs": "{% call(s) m.tabs('t', L, [{'id': 'one', 'label': 'One'}]) %}x{% endcall %}",
    "step_indicator": "{{ m.step_indicator(L, []) }}",
}


def _check_no_breakout(html, where):
    for tag, _attrs, names in parse(html):
        assert "style" not in names and "onfocus" not in names and "y" not in names, (
            where,
            tag,
            names,
        )


for kind, (value, expected) in KINDS.items():
    for name, call in CALLS.items():
        html = render(call.replace("L", "label"), label=value)
        _check_no_breakout(html, (kind, name))
        labelled = [a for _, a, _ in parse(html) if "aria-label" in a]
        assert len(labelled) == 1 and labelled[0]["aria-label"] == expected, (kind, name, html)
    html = render("{{ m.status_badge('failed', label) }}", label=value)
    _check_no_breakout(html, (kind, "status_badge"))
    # The parser unescapes once, so equality with the raw value proves single escaping.
    title = parse(html)[0][1]["title"]
    want = "failed" if value is None else expected
    assert title == want, (kind, title)
    # The badge's visible text (Markup: its text content) equals its title.
    inner = html.split(">", 1)[1].rsplit("</span>", 1)[0]
    visible = " ".join(unescape(re.sub(r"<[^>]*>", "", inner)).split())
    assert visible == title, (kind, visible, title)

# An UNDEFINED label (dict row with no such key, the Prompt Tap outcome-less call) renders
# exactly like `{{ undefined }}`: an empty attribute, never a 500.
row = {"status": "error"}
for name, call in CALLS.items():
    html = render(call.replace("L", "row.label"), row=row)
    labelled = [a for _, a, _ in parse(html) if "aria-label" in a]
    assert len(labelled) == 1 and labelled[0]["aria-label"] == "", (name, html)
assert (
    render("{{ m.status_badge(row.status, row.label) }}", row=row)
    == '<span class="badge danger" title=""></span>'
)
# A step with an undefined status_label falls back to the status value (default(none)).
html = render("{{ m.step_indicator('S', [{'label': 'a', 'status': 'completed'}]) }}")
assert 'title="completed">completed</span>' in html, html

# 3. the real call sites use the safe forms.
manage = (ROOT / "app/templates/connectors_manage.html").read_text(encoding="utf-8")
assert "|striptags }} {{ r.name }}" in manage, "enable/disable button label must be attribute-safe"
print("verify_ux_macro_safety OK")
