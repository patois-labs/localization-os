"""Workflow diagram text alternative (a11y train T8, WCAG 1.1.1 and 2.5.3). Hermetic: renders
app/templates/_macros.html with a plain Jinja environment (no app import, no data dir).

  [P1] the DEFAULT wf_graph output is unchanged by the new `text_id` parameter: the two
       fixtures verify_parallel_routing F3 / F3b and verify_workflow_canvas E1 pin render
       to the SAME sha256 + length here, and wf_graph(steps, text_id=none) == wf_graph(steps).
  [P2] the opted-in output differs from the default by EXACTLY one attribute,
       aria-describedby="<text_id>" on the svg, and is pinned by sha256 + length for the
       builder form (ungrouped, text_id='wfStepList') and the run form (grouped + overlay,
       text_id='wfgText').
  [T1] wf_graph_text for a BRANCHING workflow with a loop-back on a run: step lines,
       role / provider / package, pass / fail targets through key_index, the loop-back and
       its traversal count, the run status and visits; its loop-backs agree with the svg's.
  [T2] wf_graph_text for a PARALLEL workflow: parallel peers named per member, an unknown
       branch key falling through to the next step exactly as the svg draws it.
  [T3] escaping under autoescape, empty steps render nothing, and the list is not live.
  [C1] the call sites: the builder describes the svg by its step list (#wfStepList), the
       Run view card and the author-review page render wf_graph_text next to the svg with
       the same id, and the Run view refresh keeps the disclosure open and restores focus.

Pins are CRLF-safe: Jinja's lexer normalizes template newlines to "\\n" before rendering,
so a core.autocrlf=true checkout renders the same bytes as a clean LF clone.

Final line: "WORKFLOW DIAGRAM TEXT OK".
"""
# ruff: noqa: E501
# (Pinned render strings and expected text lines are verbatim data.)

import hashlib
import os
import re
import sys
from html.parser import HTMLParser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from jinja2 import Environment, FileSystemLoader

_TPL = os.path.join(_ROOT, "app", "templates")


class _S(dict):
    """A step the macros read with dotted access (they accept ORM rows AND draft dicts)."""

    def __getattr__(self, k):
        return self.get(k)


_BASE = {
    "key": None,
    "provider": "mock",
    "model": None,
    "qe_gate": False,
    "qe_threshold": 70,
    "glossary_id": None,
    "tm_examples": False,
    "redact_pii": False,
    "needs_review": False,
    "branch": None,
    "max_visits": None,
    "parallel_group": None,
}


def _step(**kw):
    return _S({**_BASE, **kw})


# Byte-for-byte the verify_parallel_routing F3 / verify_workflow_canvas E1 fixtures.
_UNGROUPED = [
    _step(id=1, name="Translate", step_type="llm_agent", model="m1", tm_examples=True),
    _step(
        id=2,
        name="LQA main",
        key="lqa_main",
        step_type="lqa",
        provider=None,
        branch={"on_fail": {"step": "fix"}},
        max_visits=3,
    ),
    _step(
        id=3,
        name="Fix",
        key="fix",
        step_type="llm_agent",
        qe_gate=True,
        qe_threshold=80,
        tm_examples=True,
        redact_pii=True,
    ),
]
_GROUPED = [
    _step(id=1, name="T", step_type="llm_agent", tm_examples=True),
    _step(id=2, name="L1", key="a", step_type="lqa", provider=None, parallel_group="checks"),
    _step(
        id=3,
        name="L2",
        key="b",
        step_type="lqa",
        provider=None,
        branch={"on_fail": {"step": "p"}},
        parallel_group="checks",
    ),
    _step(
        id=4,
        name="P",
        key="p",
        step_type="llm_agent",
        qe_gate=True,
        qe_threshold=80,
        tm_examples=True,
    ),
]
# The pins those two suites hold (unchanged by T8).
_DEFAULT_PIN = {
    "ungrouped": ("d710b0fceb0f316fa6fa31f7f416c267fbf84c9be934e33d62788eb5aff3fe05", 4476),
    "grouped": ("8e9618dafbcb6c2291288d2ce7760b778ec1b2446f1330333423962ab7be3d50", 5481),
}
# T8's new pins for the opted-in forms.
_OPTED_PIN = {
    "ungrouped": ("8b5e04c90aeb58e7c1c922f0dfa7c59cf3f9f6a9ca9cbf06e7c74ef1721ecb23", 4506),
    "grouped+overlay": ("34dad603873bda79c043615ac5d6a12f76009677406d3d60de9bf571d34a2dc7", 6015),
}

# A run overlay in the shape run_overlay.build_overlay returns (the fields the macros read).
_GROUPED_OVERLAY = {
    "by_id": {
        1: {
            "label": "done",
            "visits": 1,
            "gate": None,
            "token": "var(--success)",  # pregate: allow-secret CSS color token, not a credential
        },
        2: {
            "label": "done",
            "visits": 1,
            "gate": "passed",
            "token": "var(--success)",  # pregate: allow-secret CSS color token, not a credential
        },
        3: {
            "label": "failed",
            "visits": 1,
            "gate": "failed",
            "token": "var(--danger)",  # pregate: allow-secret CSS color token, not a credential
        },
    },
    "traversals": {},
}


def _macros(autoescape=False):
    env = Environment(loader=FileSystemLoader(_TPL), autoescape=autoescape)
    return env.get_template("_macros.html").module


def _digest(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _items(html):
    return [re.sub(r"\s+", " ", li).strip() for li in re.findall(r"<li>(.*?)</li>", html, re.S)]


def test_p1_default_output_unchanged():
    m = _macros()
    for label, steps in (("ungrouped", _UNGROUPED), ("grouped", _GROUPED)):
        svg = m.wf_graph(steps)
        want_sha, want_len = _DEFAULT_PIN[label]
        assert (len(svg), _digest(svg)) == (want_len, want_sha), (label, len(svg), _digest(svg))
        assert m.wf_graph(steps, text_id=None) == svg, f"text_id=None is not inert ({label})"
        assert "aria-describedby" not in svg, label
    print(
        "  [P1] default wf_graph still matches the F3 / E1 pins (4476 / 5481 bytes); text_id=None inert"
    )


def test_p2_opted_in_output_pinned():
    m = _macros()
    for label, steps, kw, tid in (
        ("ungrouped", _UNGROUPED, {}, "wfStepList"),
        ("grouped+overlay", _GROUPED, {"overlay": _GROUPED_OVERLAY}, "wfgText"),
    ):
        default = m.wf_graph(steps, **kw)
        opted = m.wf_graph(steps, text_id=tid, **kw)
        # Exactly one attribute added, on the svg's aria-label line, nothing else moved.
        want = default.replace(
            '" xmlns="http://www.w3.org/2000/svg"',
            f'" aria-describedby="{tid}" xmlns="http://www.w3.org/2000/svg"',
            1,
        )
        assert want != default and opted == want, label
        want_sha, want_len = _OPTED_PIN[label]
        assert (len(opted), _digest(opted)) == (want_len, want_sha), (
            label,
            len(opted),
            _digest(opted),
        )
    print("  [P2] opted-in wf_graph = default + one aria-describedby; both forms pinned")


def test_t1_branching_loop_text():
    m = _macros()
    steps = [
        _step(
            id=10,
            name="Translate",
            key="tr",
            step_type="llm_agent",
            provider="claude",
            model="sonnet",
            glossary_id=5,
        ),
        _step(
            id=11,
            name="Terminology check",
            key="term",
            step_type="lqa",
            provider="claude",
            qe_gate=True,
            qe_threshold=90,
            branch={"on_pass": "next", "on_fail": {"step": "tr"}},
            max_visits=3,
        ),
        _step(
            id=12, name="Review", step_type="human_review", provider=None, branch={"on_pass": "end"}
        ),
        _step(id=13, name="Polish", step_type="llm_agent"),
    ]
    overlay = {
        "by_id": {
            10: {"label": "done", "visits": 2, "gate": None},
            11: {"label": "done", "visits": 2, "gate": "passed"},
            12: {"label": "paused", "visits": 1, "gate": None},
        },
        "traversals": {"11:on_fail": 1},
    }
    html = str(m.wf_graph_text(steps, overlay, "wfgText"))
    assert html.startswith('<details class="wfg-text"><summary>Diagram as text</summary>'), html[
        :80
    ]
    assert '<ol id="wfgText" class="wfg-text-list">' in html
    assert _items(html) == [
        "Step 1: Translate. Translate, claude:sonnet. Package: glossary. Then: step 2. Status: done, 2 visits.",
        "Step 2: Terminology check. Check, claude. Package: QE gate 90%, at most 3 visits. On pass: next step."
        " On fail: back to step 1 (loop-back, taken 1 time). Status: done, 2 visits, gate passed.",
        "Step 3: Review. Review, default. On pass: end of workflow. On fail: next step. Status: paused.",
        "Step 4: Polish. Translate, mock. Then: end of workflow. Status: not run.",
    ], _items(html)
    # The text and the svg resolve targets the same way: one loop-back each.
    svg = m.wf_graph(steps, overlay=overlay)
    assert svg.count("&#8635;") - 1 == html.count("loop-back") == 1, (svg.count("&#8635;"), html)
    # Without an overlay: no status sentence, no traversal count.
    plain = str(m.wf_graph_text(steps))
    assert "Status:" not in plain and "taken" not in plain and 'id="wfgText"' in plain
    assert "(loop-back)." in plain
    print("  [T1] branching + loop-back run: targets, loop-back x1, status and visits in text")


def test_t2_parallel_text():
    m = _macros()
    steps = [
        _step(id=1, name="Draft", step_type="llm_agent"),
        _step(
            id=2,
            name="Fluency",
            key="a",
            step_type="lqa",
            parallel_group="checks",
            branch={"on_fail": {"step": "nope"}},
        ),
        _step(id=3, name="Terms", step_type="lqa", parallel_group="checks"),
        _step(id=4, name="Style", step_type="lqa", parallel_group="checks"),
        _step(id=5, name="Lone", step_type="lqa", parallel_group="solo"),
        _step(id=6, name="Finish", step_type="llm_agent", parallel_group="checks"),
    ]
    items = _items(str(m.wf_graph_text(steps, None, "t2")))
    assert items == [
        "Step 1: Draft. Translate, mock. Then: step 2.",
        "Step 2: Fluency. Check, mock. Package: LQA gate. On pass: next step. On fail: step 3."
        " Runs in parallel with steps 3, 4 (group checks).",
        "Step 3: Terms. Check, mock. Package: LQA gate. Then: step 4. Runs in parallel with steps 2, 4 (group checks).",
        "Step 4: Style. Check, mock. Package: LQA gate. Then: step 5. Runs in parallel with steps 2, 3 (group checks).",
        # A lone member does not fork (the engine's own rule), and a non-adjacent step
        # sharing a slug is not part of the earlier group.
        "Step 5: Lone. Check, mock. Package: LQA gate. Then: step 6.",
        "Step 6: Finish. Translate, mock. Then: end of workflow.",
    ], items
    # The svg draws the same single group outline.
    assert m.wf_graph(steps).count("(in parallel)") == 1
    print("  [T2] parallel group: peers per member, lone / non-adjacent members not grouped")


def test_t3_escaping_and_edges():
    m = _macros(autoescape=True)
    html = str(
        m.wf_graph_text(
            [_step(id=1, name="<b>x</b>", step_type="llm_agent", provider="p<q>")], None, "e"
        )
    )
    assert "<b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html and "p&lt;q&gt;" in html, html
    assert str(m.wf_graph_text([])).strip() == "" and str(m.wf_graph_text(None)).strip() == ""
    assert "aria-live" not in html and 'role="status"' not in html, "the text list must not be live"
    print("  [T3] names escaped, empty steps render nothing, no live region")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), "rb") as fh:
        return fh.read().decode("utf-8").replace("\r\n", "\n")


def test_c1_call_sites():
    builder = _read("app", "templates", "workflow_builder.html")
    assert '<ol class="steplist" id="wfStepList">' in builder
    assert builder.count("text_id='wfStepList'") == 2, "both builder graphs must name the step list"
    for name, call in (
        (
            "_run_graph_card.html",
            "{{ m.wf_graph(steps, overlay=overlay, text_id='wfgText') }}{{ m.wf_graph_text(steps, overlay, 'wfgText') }}",
        ),
        (
            "workflow_author_review.html",
            "{{ m.wf_graph(fields.steps, text_id='wfgText') }}{{ m.wf_graph_text(fields.steps, none, 'wfgText') }}",
        ),
    ):
        # ux/workflows: a named scroll region now surrounds the SVG. Preserve the
        # macro arguments and prove the rendered SVG still resolves its text list;
        # adjacency of the two source expressions is no longer the contract.
        source = _read("app", "templates", name)
        for expression in re.findall(r"\{\{.*?\}\}", call):
            assert source.count(expression) == 1, (name, expression)
        graph_line = next(line for line in source.splitlines() if "m.wf_graph_text(" in line)
        env = Environment(loader=FileSystemLoader(_TPL), autoescape=True)
        rendered = env.from_string('{% import "_macros.html" as m %}' + graph_line).render(
            steps=_GROUPED, fields={"steps": _GROUPED}, overlay=_GROUPED_OVERLAY
        )

        class GraphDOM(HTMLParser):
            def __init__(self):
                super().__init__()
                self.elements = []

            def handle_starttag(self, tag, attrs):
                self.elements.append((tag, dict(attrs)))

        dom = GraphDOM()
        dom.feed(rendered)
        assert (
            sum(tag == "svg" and a.get("aria-describedby") == "wfgText" for tag, a in dom.elements)
            == 1
        ), name
        assert sum(tag == "ol" and a.get("id") == "wfgText" for tag, a in dom.elements) == 1, name
        assert (
            sum(
                a.get("class") == "wf-graph-region"
                and a.get("tabindex") == "0"
                and bool(a.get("aria-label"))
                for tag, a in dom.elements
            )
            == 1
        ), name
    # Rendered, the Run view card really pairs the svg with its list.
    env = Environment(loader=FileSystemLoader(_TPL), autoescape=True)
    card = env.get_template("_run_graph_card.html").render(steps=_GROUPED, overlay=_GROUPED_OVERLAY)
    assert 'aria-describedby="wfgText"' in card and '<ol id="wfgText"' in card
    assert 'aria-label="Workflow run diagram"' in card and "aria-live" not in card
    rv = _read("app", "templates", "run_view.html")
    refresh = rv.split("function refreshGraph()", 1)[1].split(".finally", 1)[0]
    for marker in (
        "details.wfg-text[open]",
        "cur.contains(document.activeElement)",
        "det.open = true",
        "querySelector('summary').focus()",
        "window.locA11yEnhance",
    ):
        assert marker in refresh, f"refreshGraph lost {marker!r}"
    print(
        "  [C1] builder -> #wfStepList; run card + author review pair svg and list; refresh keeps state"
    )


def main():
    test_p1_default_output_unchanged()
    test_p2_opted_in_output_pinned()
    test_t1_branching_loop_text()
    test_t2_parallel_text()
    test_t3_escaping_and_edges()
    test_c1_call_sites()
    print("WORKFLOW DIAGRAM TEXT OK")


if __name__ == "__main__":
    main()
