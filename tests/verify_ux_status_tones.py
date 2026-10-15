"""Issue #551: status_badge keeps a distinct tone for every status the app emits, and
data_table passes a class through. Offline, no browser."""

import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape

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


# status_badge keeps a distinct tone for every status the app emits.
EXPECT = {
    "completed": "exact",
    "failed": "danger",
    "error": "danger",
    "failed_budget": "danger",
    "held": "danger",
    "reauth": "reauth",
    "beta": "beta",
    "indicative": "beta",
    # Pre-overhaul macro tone: neutral everywhere (customer, jobs, runs, documents,
    # memories, keys). Pages that showed amber before pass tone='warn' themselves.
    "archived": "status",
    "cancelled": "status",
    "skipped": "status",
    "in_review": "warn",
    "paused": "paused",
    "running": "run",
    "pending": "status",
    "draft": "status",
}
for value, tone in EXPECT.items():
    cls = next(a["class"] for t, a, _ in parse(render("{{ m.status_badge(v) }}", v=value)))
    assert cls == f"badge {tone}", (value, cls)
override = parse(render("{{ m.status_badge('in_review', 'In review', 'run') }}"))[0][1]["class"]
assert override == "badge run", override

# data_table passes a class through (the workflow coverage muted cells).
html = render(
    "{% call m.data_table('Coverage', [{'label': 'A'}], css_class='wf-cov') %}{% endcall %}"
)
assert parse(html)[0][1]["class"] == "data-table-region wf-cov", parse(html)[0]

# Vendor archived badges were amber before the overhaul (explicit tone at the call site).
for name in ("vendors.html", "vendor_detail.html"):
    src = (ROOT / "app/templates" / name).read_text(encoding="utf-8")
    assert "m.status_badge('archived', 'Archived', 'warn')" in src, name

group = (ROOT / "app/templates/workflow_group.html").read_text(encoding="utf-8")
assert "css_class='wf-cov'" in group
print("verify_ux_status_tones OK")
