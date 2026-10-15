"""Offline delivery regression for issue #307."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_template_injection_")

from app.filters import get_filter
from app.services.connectors.hubspot import (
    HubSpotVariantStrategy,
    _hubspot_build_payload,
    mask_hubl,
    unmask_hubl,
)
from app.services.connectors.wordpress import _mask_shortcodes, _wp_build_payload


def hub_payload(target, source="Hello", kind="layout"):
    masked, spans = mask_hubl(source)
    held = {}
    conn = SimpleNamespace(_record_incomplete=lambda aid, loc, reasons: held.update(reasons))
    key = "b0" if kind == "layout" else "0"
    prep = SimpleNamespace(
        aid="site-pages:1",
        title_text="Safe title",
        body_units=[SimpleNamespace(p_index=key, text=masked)],
        extra={
            "connector": conn,
            "masks": {key: spans},
            "body_kind": kind,
            "paths": [("text",)],
            "n_units": 1,
            "filter": get_filter("html"),
            "source_bytes": b"<p>Hello</p>",
        },
    )
    payload = _hubspot_build_payload(prep, "fr", "Titre", {key: target})
    return payload, held


def wp_payload(target, source="Hello", field="body"):
    body, codes = _mask_shortcodes("<p>" + source + "</p>")
    prep = SimpleNamespace(
        extra={
            "source_title": source,
            "source_excerpt": source,
            "has_excerpt": True,
            "masked_body_bytes": body.encode(),
            "shortcodes": codes,
            "post_type": "posts",
        }
    )
    key = str(get_filter("html").parse(body.encode()).units[0].p_index)
    mapping = {key: target} if field == "body" else {"excerpt": target}
    title = target if field == "title" else "Titre"
    if field == "title":
        mapping = {}
    return _wp_build_payload(prep, "fr", title, mapping)


for attack in (
    "{{ request.query_dict.q }}",
    "{{request.query_dict.q}}",
    "{% include x %}",
    "{# hidden #}",
    "&#123;&#123;request.query_dict.q&#125;&#125;",
    "&amp;#123;&amp;#123;q&amp;#125;&amp;#125;",
):
    assert not unmask_hubl("Bonjour " + attack, [])[1], attack
    for kind in ("layout", "html"):
        payload, held = hub_payload("Bonjour " + attack, kind=kind)
        assert payload["body"] is None and held, (attack, payload)
        assert "added, changed, or duplicated" in " ".join(held.values()), held

# Plain title and metadata have no mask entries. An all-held result must refuse
# before any client call, and retain the actionable reason in the WriteResult.
meta_prep = SimpleNamespace(
    aid="site-pages:1",
    title_text="Hello",
    body_units=[SimpleNamespace(p_index="m:htmlTitle", text="Hello")],
    extra={"connector": None, "meta_props": ["htmlTitle"], "body_kind": "layout"},
)
payload = _hubspot_build_payload(
    meta_prep, "fr", "Bonjour {{ q }}", {"m:htmlTitle": "Bonjour {% include x %}"}
)
result = HubSpotVariantStrategy().write_target(None, "site-pages:1", "fr", payload)
assert not result.ok and "added, changed, or duplicated" in result.message, result

source = "Hello {{ first }} {% if x %}world{% endif %} {# note #}"
masked, spans = mask_hubl(source)
assert len(spans) == 4
assert unmask_hubl(masked, spans) == (source, True)
from app.services.segment import ph_point

reordered = "Bonjour " + " ".join(ph_point("h", i) for i in (4, 3, 2, 1))
assert unmask_hubl(reordered, spans)[1]
assert not unmask_hubl(masked + spans[0], spans)[1], "duplicated source expression"
assert not unmask_hubl(masked + ph_point("h", 1), spans)[1]
assert not unmask_hubl(masked.replace(ph_point("h", 1), "{{ changed }}"), spans)[1]
encoded = "Hello &#123;&#123; first &#125;&#125;"
assert hub_payload(encoded, encoded)[0]["body"] is not None
for literal in ("{ { prose } }", "\uff5b\uff5b prose \uff5d\uff5d", "[ see note]"):
    assert unmask_hubl(literal, [])[1], literal
    assert not wp_payload(literal)["_shortcode_lost"], literal

for attack in ("[gallery]", "[/gallery]", '[gallery ids="1" /]', "&#91;gallery&#93;"):
    for field in ("body", "title", "excerpt"):
        assert wp_payload("Bonjour " + attack, field=field)["_shortcode_lost"], (attack, field)

# Body skeleton restores the original shortcode; an extra raw copy must fail.
assert wp_payload("Bonjour [gallery]", "Hello [gallery]")["_shortcode_lost"]
# Accessibility attributes are also a delivery door, despite remaining outside the mask.
from app.services.connectors.template_integrity import template_spans_match
from app.services.connectors.wordpress import _SHORTCODE_RE

for changed in ("[two]", "[one] [one]", ""):
    assert not template_spans_match("[one]", changed, _SHORTCODE_RE), changed
attr_body = b'<p>Hello</p><img alt="A cat" src="cat.png">'
attr_unit = next(u for u in get_filter("html").parse(attr_body).units if u.text == "A cat")
attr_prep = SimpleNamespace(
    extra={
        "source_title": "Hello",
        "source_excerpt": "",
        "masked_body_bytes": attr_body,
        "shortcodes": [],
        "post_type": "posts",
    }
)
assert _wp_build_payload(
    attr_prep, "fr", "Bonjour", {str(attr_unit.p_index): "Chat &#91;gallery&#93;"}
)["_shortcode_lost"]
# Title/excerpt spans are not masked: unchanged and reordered multisets are allowed.
for target in ("Bonjour [one] [two]", "Bonjour [two] [one]"):
    for field in ("title", "excerpt"):
        # Keep the other fields at their source, avoiding an unrelated missing span.
        source = "Hello [one] [two]"
        body, codes = _mask_shortcodes("<p>" + source + "</p>")
        prep = SimpleNamespace(
            extra={
                "source_title": source,
                "source_excerpt": source,
                "has_excerpt": True,
                "masked_body_bytes": body.encode(),
                "shortcodes": codes,
                "post_type": "posts",
            }
        )
        result = _wp_build_payload(
            prep,
            "fr",
            target if field == "title" else source,
            {"excerpt": target} if field == "excerpt" else {},
        )
        assert not result["_shortcode_lost"], result

print("verify_template_injection OK")
