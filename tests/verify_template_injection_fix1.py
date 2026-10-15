"""Round-one gate regressions, collected together to expose every red case."""

import os
import sys
from time import perf_counter
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_template_fix1_")

from app.filters import get_filter
from app.filters.html_filter import HtmlFilter
from app.services.connectors.document360 import (
    Document360Connector,
    _d360_build_payload,
    shield_codes,
)
from app.services.connectors.hubspot import _HUBL_SPAN_RE, HubSpotVariantStrategy
from app.services.connectors.template_integrity import template_spans_match
from app.services.connectors.wordpress import _SHORTCODE_RE, _wp_build_payload

failures = []


def check(label, ok):
    print(label, "PASS" if ok else "FAIL")
    if not ok:
        failures.append(label)


for attack in ("[foo 1]", '[foo "a"]', "[Foo]", "[3d-foo]", "[foo.bar]", "[see note]"):
    for field in ("body", "title", "excerpt"):
        prep = SimpleNamespace(
            extra={
                "source_title": "Hello",
                "source_excerpt": "Hello",
                "has_excerpt": True,
                "masked_body_bytes": b"<p>Hello</p>",
                "shortcodes": [],
                "post_type": "posts",
            }
        )
        result = _wp_build_payload(
            prep,
            "fr",
            attack if field == "title" else "Bonjour",
            {"0" if field == "body" else "excerpt": attack} if field != "title" else {},
        )
        check(f"G1 {field} {attack}", result["_shortcode_lost"])

hf = HtmlFilter(template_pattern=_HUBL_SPAN_RE)
source = b"<p>First</p><p>Second</p>"
for opening, closing in (("{#", "#}"), ("{{", "}}"), ("{%", "%}")):
    for current in (source.decode(), "<div>Drifted</div>"):
        payload = {
            "body": {
                "kind": "html",
                "filter": hf,
                "source_bytes": source,
                "n_units": 2,
                "complete": True,
                "mapping": {"0": "Premier " + opening, "1": closing + " second"},
            }
        }
        body, _, held, replaced = HubSpotVariantStrategy._build_patch(
            None, {"postBody": current}, payload
        )
        check(f"G2 {opening} {current}", "postBody" not in body and "postBody" in held)
        # A refused replacement must not be reported as written.
        if "postBody" not in body:
            assert "postBody" not in replaced

check(
    "G3 encoded literal to raw",
    not template_spans_match("&#123;&#123; x &#125;&#125;", "{{ x }}", _HUBL_SPAN_RE),
)
check(
    "G4 raw context",
    not template_spans_match(
        "{% raw %}{{ x }}{% endraw %}", "{{ x }}{% raw %}{% endraw %}", _HUBL_SPAN_RE
    ),
)
prep = SimpleNamespace(
    extra={
        "source_title": "[[foo]]",
        "source_excerpt": "",
        "has_excerpt": False,
        "masked_body_bytes": b"<p>Hello</p>",
        "shortcodes": [],
        "post_type": "posts",
    }
)
check("G4 escaped shortcode", _wp_build_payload(prep, "fr", "[foo]", {})["_shortcode_lost"])

for editor, raw in (("html", "<p>First</p><p>Second</p>"), ("markdown", "First\n\nSecond")):
    filt = get_filter("html" if editor == "html" else "md")
    for field in ("title", "body", "split", "duplicate", "missing"):
        original = raw + " {{ original }}" if field in ("duplicate", "missing") else raw
        shielded, codes = shield_codes(original)
        units = filt.parse(shielded.encode()).units
        mapping = {str(u.p_index): u.text for u in units}
        first = str(units[0].p_index)
        title = "Bonjour"
        if field == "title":
            title += " {{ added }}"
        elif field == "split":
            mapping[first] += " {{"
            mapping[str(units[1].p_index)] = "}} second"
        elif field == "missing":
            mapping = {k: v.replace("\ue0000\ue001", "") for k, v in mapping.items()}
        else:
            mapping[first] += " {{ original }}" if field == "duplicate" else " {{ added }}"
        prep = SimpleNamespace(
            extra={
                "shielded_title": "Hello",
                "title_codes": [],
                "body_codes": codes,
                "shielded_body_bytes": shielded.encode(),
                "body_filter": filt,
                "editor": editor,
            }
        )
        result = _d360_build_payload(prep, "fr", title, mapping)
        held = "template integrity" in result.get("_template_hold", "")
        check(f"D360 {editor} {field}", held)
        if held:
            strategy = Document360Connector({}, {}).strategy
            assert not strategy.write_target(None, "1", "fr", result).ok

started = perf_counter()
template_spans_match("", "{{" * 20000, _HUBL_SPAN_RE)
elapsed = perf_counter() - started
check(f"G5 unclosed 40 KB ({elapsed:.3f}s)", elapsed < 3)
started = perf_counter()
_SHORTCODE_RE.findall("[a " + "=" * 20000)
elapsed = perf_counter() - started
check(f"G5 shortcode 20 KB ({elapsed:.3f}s)", elapsed < 3)
assert not failures, failures
print("verify_template_injection_fix1 OK")
